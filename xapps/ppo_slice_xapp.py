#!/usr/bin/env python3
"""
PPO online slice-aware DL-PRB allocator xApp -- 1 DU / 3 slices.

Differentiator vs offline ADSS (Mukuhi et al.): on-policy PPO trained ONLINE in
closed loop on the live srsRAN/OSC-RIC testbed via E2SM-RC.

Control surface (matches srsRAN reality):
  * E2SM-RC slice-level PRB quota, DOWNLINK only (min/max ratio; dedicated ignored).
  * RRU.PrbUsedDl == 0 in ZMQ -> reward built from dl_brate vs SLA + injected CQI.

MDP:
  state  (6 floats): [sat_C, sat_P, sat_B, cqi_C, cqi_P, cqi_B]
                     sat_i = dl_brate_i / SLA_DL_i  (clipped);  cqi_i = CQI_i/15
  action (6 discrete): per-slice DL PRB min-ratio profile (sum=100), max=100 (work-conserving)
  reward: 0.6*min(sat_C,1)+0.3*min(sat_P,1)+0.1*min(sat_B,1) - penalty*[sat_C<1]
  masking: forbid profiles starving CRITICAL while CRITICAL unsatisfied.

Run (inside python_xapp_runner): python3 ppo_slice_xapp.py --interval 1 --train
Eval (frozen, for comparison vs baseline/DQN/xSlice): python3 ppo_slice_xapp.py --eval --ckpt ppo.pt

LOGGING FIX (no MDP/reward/policy change):
  * _snapshot now carries the UE rnti into the per-slice dict.
  * _apply writes /tmp/prb_decisions.json keyed by str(rnti) so metrics_logger.py
    (which does prb.get(str(r))) can join the applied PRB action onto each row.
"""
import argparse, signal, json, threading, time, datetime, os
import numpy as np
import torch
import torch.nn as nn
from lib.xAppBase import xAppBase

# ----------------------------- configuration --------------------------------
WS_URL  = "10.0.2.1:8002"
E2_NODE = "gnbd_001_001_00000213_1"          # verify: redis-cli KEYS '*RAN*'
SLICES  = ["CRITICAL", "PERFORMANCE", "BUSINESS"]
SD_TO_SLICE = {}                              # filled from stream; fallback by order

# DL SLA targets (bits/s).  From SLA Table 1, DL column.
SLA_DL = {"CRITICAL": 9_000_000, "PERFORMANCE": 8_000_000, "BUSINESS": 25_000_000}

# 6 discrete DL PRB min-ratio profiles, [CRITICAL, PERFORMANCE, BUSINESS], sum=100
PROFILES = [
    [34, 33, 33],   # 0 balanced
    [60, 25, 15],   # 1 favor CRITICAL
    [25, 60, 15],   # 2 favor PERFORMANCE
    [15, 25, 60],   # 3 favor BUSINESS
    [45, 45, 10],   # 4 protect CRITICAL+PERFORMANCE
    [10, 10, 80],   # 5 max BUSINESS throughput
]
N_ACT, N_OBS = len(PROFILES), 6
W = np.array([0.6, 0.3, 0.1])                 # CRITICAL / PERFORMANCE / BUSINESS
CRIT_PENALTY = 0.3                            # subtracted when CRITICAL unsatisfied
CRIT_MIN_FLOOR = 30                           # mask any profile giving CRITICAL < this when unsatisfied

# PPO hyper-parameters
ROLLOUT, EPOCHS, GAMMA, LAM = 32, 6, 0.99, 0.95
CLIP, LR, ENT_COEF, VF_COEF = 0.2, 3e-4, 0.03, 0.5
CKPT_DEFAULT = "/tmp/ppo_slice.pt"
TRAIN_LOG    = "/tmp/ppo_train_log.csv"
PRB_FILE     = "/tmp/prb_decisions.json"      # consumed by metrics_logger.py (keyed by str(rnti))

# ------------------------------ networks ------------------------------------
class ActorCritic(nn.Module):
    def __init__(self, obs=N_OBS, act=N_ACT, h=64):
        super().__init__()
        self.body   = nn.Sequential(nn.Linear(obs, h), nn.Tanh(), nn.Linear(h, h), nn.Tanh())
        self.pi     = nn.Linear(h, act)
        self.v      = nn.Linear(h, 1)

    def forward(self, x):
        z = self.body(x)
        return self.pi(z), self.v(z).squeeze(-1)

    def act(self, obs, mask):
        logits, value = self.forward(obs)
        logits = logits.masked_fill(~mask, -1e9)
        dist = torch.distributions.Categorical(logits=logits)
        a = dist.sample()
        return a, dist.log_prob(a), value, dist.entropy()

    def evaluate(self, obs, mask, act):
        logits, value = self.forward(obs)
        logits = logits.masked_fill(~mask, -1e9)
        dist = torch.distributions.Categorical(logits=logits)
        return dist.log_prob(act), value, dist.entropy()


class PPO:
    def __init__(self, train=True, ckpt=CKPT_DEFAULT):
        self.net = ActorCritic()
        self.opt = torch.optim.Adam(self.net.parameters(), lr=LR)
        self.train_mode = train
        self.ckpt = ckpt
        self.buf = []   # (obs, act, logp, val, mask, rew)
        if os.path.exists(ckpt):
            self.net.load_state_dict(torch.load(ckpt, map_location="cpu"))
            print(f"[PPO] loaded checkpoint {ckpt}")
        if train and not os.path.exists(TRAIN_LOG):
            with open(TRAIN_LOG, "w") as f:
                f.write("ts,update,mean_reward,policy_loss,value_loss,entropy\n")

    def select(self, obs_np, mask_np):
        obs = torch.as_tensor(obs_np, dtype=torch.float32).unsqueeze(0)
        mask = torch.as_tensor(mask_np, dtype=torch.bool).unsqueeze(0)
        with torch.no_grad():
            if self.train_mode:
                a, logp, v, _ = self.net.act(obs, mask)
                return int(a.item()), float(logp.item()), float(v.item())
            logits, v = self.net.forward(obs)
            logits = logits.masked_fill(~mask, -1e9)
            return int(torch.argmax(logits, -1).item()), 0.0, float(v.item())  # greedy in eval

    def store(self, obs, act, logp, val, mask, rew):
        self.buf.append((obs, act, logp, val, mask, rew))

    def maybe_update(self, last_val, update_idx):
        if not self.train_mode or len(self.buf) < ROLLOUT:
            return None
        obs  = torch.as_tensor(np.array([b[0] for b in self.buf]), dtype=torch.float32)
        act  = torch.as_tensor([b[1] for b in self.buf], dtype=torch.long)
        logp = torch.as_tensor([b[2] for b in self.buf], dtype=torch.float32)
        val  = np.array([b[3] for b in self.buf] + [last_val], dtype=np.float32)
        mask = torch.as_tensor(np.array([b[4] for b in self.buf]), dtype=torch.bool)
        rew  = np.array([b[5] for b in self.buf], dtype=np.float32)

        # GAE-lambda
        adv = np.zeros(len(rew), dtype=np.float32); gae = 0.0
        for t in reversed(range(len(rew))):
            delta = rew[t] + GAMMA * val[t + 1] - val[t]
            gae = delta + GAMMA * LAM * gae
            adv[t] = gae
        ret = adv + val[:-1]
        adv_t = torch.as_tensor((adv - adv.mean()) / (adv.std() + 1e-8))
        ret_t = torch.as_tensor(ret)

        pl = vl = ent = 0.0
        for _ in range(EPOCHS):
            new_logp, v, entropy = self.net.evaluate(obs, mask, act)
            ratio = torch.exp(new_logp - logp)
            s1 = ratio * adv_t
            s2 = torch.clamp(ratio, 1 - CLIP, 1 + CLIP) * adv_t
            policy_loss = -torch.min(s1, s2).mean()
            value_loss  = ((v - ret_t) ** 2).mean()
            entropy_l   = entropy.mean()
            loss = policy_loss + VF_COEF * value_loss - ENT_COEF * entropy_l
            self.opt.zero_grad(); loss.backward()
            nn.utils.clip_grad_norm_(self.net.parameters(), 0.5)
            self.opt.step()
            pl, vl, ent = float(policy_loss), float(value_loss), float(entropy_l)

        mean_r = float(rew.mean())
        torch.save(self.net.state_dict(), self.ckpt)
        with open(TRAIN_LOG, "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S},{update_idx},{mean_r:.4f},{pl:.4f},{vl:.4f},{ent:.4f}\n")
        self.buf.clear()
        return mean_r, pl, vl, ent


# ------------------------------ the xApp ------------------------------------
class PpoXApp(xAppBase):
    def __init__(self, c, h, r, interval, ppo):
        super().__init__(c, h, r)
        self.interval = interval
        self.ppo = ppo
        self.m = {}
        self.lock = threading.Lock()
        self.update_idx = 0
        self.prev = None     # (obs, act, logp, val, mask) awaiting reward
        self._ws()

    def _ws(self):
        import websocket
        def on_open(ws):
            ws.send(json.dumps({"cmd": "metrics_subscribe"})); print("[WS] subscribed 8002")
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if "cells" not in d: return
                for cell in d["cells"]:
                    for ue in cell.get("ue_list", []):
                        r = ue.get("rnti")
                        if not r: continue
                        with self.lock:
                            self.m[r] = {"cqi": ue.get("cqi", 1), "sd": ue.get("slice_sd", 3),
                                         "node": ue.get("e2_node", E2_NODE), "f1ap": ue.get("f1ap", 0),
                                         "dl": ue.get("dl_brate", 0), "ts": time.time()}
            except: pass
        def th():
            ws = websocket.WebSocketApp("ws://" + WS_URL, on_open=on_open, on_message=on_msg)
            while ws.run_forever(): time.sleep(1)
        threading.Thread(target=th, daemon=True).start()

    def _slice_of(self, sd):
        # map slice_sd -> name; fallback handled by attach order in injector
        return {1: "CRITICAL", 2: "PERFORMANCE", 3: "BUSINESS"}.get(sd, "BUSINESS")

    def _snapshot(self):
        """Return per-slice dict {name: {cqi, dl, f1ap, node, rnti}} for active UEs."""
        with self.lock:
            act = {r: dict(v) for r, v in self.m.items() if time.time() - v["ts"] < 10}
        slc = {}
        for r, x in act.items():
            name = self._slice_of(x["sd"])
            # carry rnti so _apply can key the PRB-decision log the way metrics_logger expects
            slc[name] = {"cqi": x["cqi"], "dl": x["dl"], "f1ap": x["f1ap"],
                         "node": x["node"], "rnti": r}
        return slc

    def _state_reward(self, slc):
        sat, cqi = [], []
        for s in SLICES:
            d = slc.get(s, {"cqi": 1, "dl": 0})
            sat.append(min(d["dl"] / SLA_DL[s], 1.5))
            cqi.append(d["cqi"] / 15.0)
        state = np.array(sat + cqi, dtype=np.float32)
        sat_clip = np.clip(np.array(sat), 0, 1.0)
        reward = float(np.dot(W, sat_clip))
        if sat_clip[0] < 1.0:                      # CRITICAL unsatisfied
            reward -= CRIT_PENALTY
        return state, reward, sat_clip

    def _mask(self, sat_clip):
        m = np.ones(N_ACT, dtype=bool)
        if sat_clip[0] < 1.0:                       # CRITICAL hungry -> forbid starving it
            for i, p in enumerate(PROFILES):
                if p[0] < CRIT_MIN_FLOOR:
                    m[i] = False
        if not m.any(): m[1] = True                 # safety: always allow favor-CRITICAL
        return m

    def _apply(self, action, slc):
        prof = PROFILES[action]
        decisions = {}                              # logging only -- keyed by str(rnti)
        for ratio, s in zip(prof, SLICES):
            if s not in slc: continue
            d = slc[s]
            try:
                self.e2sm_rc.control_slice_level_prb_quota(
                    d["node"], d["f1ap"], int(ratio), 100, dedicated_prb_ratio=100, ack_request=1)
            except Exception as e:
                print(f"  [E2] {s} f1ap={d['f1ap']} FAIL: {e}")
            decisions[str(d["rnti"])] = {
                "prb_min": int(ratio), "prb_max": 100,
                "slice_name": s, "f1ap_id": d["f1ap"],
                "alloc_req_bps": int(SLA_DL[s]),
            }
        try:
            with open(PRB_FILE, "w") as f:
                json.dump(decisions, f)
        except Exception as e:
            print(f"  [PRB-LOG] write failed: {e}")

    def _loop(self):
        mode = "TRAIN" if self.ppo.train_mode else "EVAL"
        print(f"[PPO-CTRL] {mode} | interval={self.interval}s | rollout={ROLLOUT}")
        while self.running:
            time.sleep(self.interval)
            slc = self._snapshot()
            if len(slc) < 1:
                print(f"{datetime.datetime.now():%H:%M:%S} (0 UEs)"); continue
            state, reward, sat = self._state_reward(slc)

            # close previous transition with the reward observed AFTER acting
            if self.prev is not None and self.ppo.train_mode:
                o, a, lp, v, msk = self.prev
                self.ppo.store(o, a, lp, v, msk, reward)
                out = self.ppo.maybe_update(last_val=0.0, update_idx=self.update_idx)
                if out:
                    self.update_idx += 1
                    mr, pl, vl, ent = out
                    print(f"  [UPDATE {self.update_idx}] mean_r={mr:.3f} pl={pl:.3f} vl={vl:.3f} ent={ent:.3f}")

            # choose and apply the next action
            mask = self._mask(sat)
            action, logp, val = self.ppo.select(state, mask)
            self._apply(action, slc)
            self.prev = (state, action, logp, val, mask)

            t = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"{t} sat=[{sat[0]:.2f},{sat[1]:.2f},{sat[2]:.2f}] "
                  f"cqi=[{state[3]*15:.0f},{state[4]*15:.0f},{state[5]*15:.0f}] "
                  f"act={action}{PROFILES[action]} r={reward:.3f}")

    @xAppBase.start_function
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="")
    p.add_argument("--http_server_port", type=int, default=8095)
    p.add_argument("--rmr_port", type=int, default=4565)
    p.add_argument("--interval", type=float, default=1.0, help="decision interval (s)")
    p.add_argument("--train", action="store_true", help="online training (default if neither flag)")
    p.add_argument("--eval", action="store_true", help="frozen greedy policy for comparison runs")
    p.add_argument("--ckpt", default=CKPT_DEFAULT)
    a = p.parse_args()
    train = not a.eval
    ppo = PPO(train=train, ckpt=a.ckpt)
    x = PpoXApp(a.config, a.http_server_port, a.rmr_port, a.interval, ppo)
    x.e2sm_rc.set_ran_func_id(3)
    for s in (signal.SIGQUIT, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, x.signal_handler)
    x.start()