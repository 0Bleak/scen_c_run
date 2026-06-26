#!/usr/bin/env python3
"""
DQN online slice-aware DL-PRB allocator xApp -- 1 DU / 3 slices.

Identical MDP to PPO xApp for fair comparison:
  state  (6): [sat_C, sat_P, sat_B, cqi_C/15, cqi_P/15, cqi_B/15]
  action (6 discrete): same PRB profiles as PPO
  reward: same weighted SLA satisfaction + CRITICAL penalty

DQN specifics:
  - Experience replay buffer (capacity 10000)
  - Target network updated every TARGET_UPDATE steps
  - Epsilon-greedy exploration: eps decays 1.0 -> 0.05 over EPS_DECAY steps
  - Huber loss for stability
  - Same state/action/reward as PPO -> direct comparison valid
"""
import argparse, signal, json, threading, time, datetime, os, random
from collections import deque
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from lib.xAppBase import xAppBase

# ----------------------------- configuration --------------------------------
WS_URL  = "10.0.2.1:8002"
E2_NODE = "gnbd_001_001_00000213_1"
SLICES  = ["CRITICAL", "PERFORMANCE", "BUSINESS"]

SLA_DL = {"CRITICAL": 356_000, "PERFORMANCE": 311_000, "BUSINESS": 20_000_000}

PROFILES = [
    [34, 33, 33],
    [60, 25, 15],
    [25, 60, 15],
    [15, 25, 60],
    [45, 45, 10],
    [10, 10, 80],
]
N_ACT, N_OBS = len(PROFILES), 6
W = np.array([0.6, 0.3, 0.1])
CRIT_PENALTY  = 0.3
CRIT_MIN_FLOOR = 30

# DQN hyperparameters
BUFFER_CAP   = 10_000   # replay buffer capacity
BATCH_SIZE   = 64       # training batch size
GAMMA        = 0.99     # discount factor
LR           = 1e-3     # Adam learning rate
TARGET_UPDATE = 50      # steps between target network sync
EPS_START    = 1.0      # initial epsilon
EPS_END      = 0.05     # final epsilon
EPS_DECAY    = 500      # steps to decay epsilon over
TRAIN_START  = 128      # minimum buffer size before training starts
CKPT_DEFAULT = "/tmp/dqn_slice.pt"
TRAIN_LOG    = "/tmp/dqn_train_log.csv"

# ------------------------------ network -------------------------------------
class QNetwork(nn.Module):
    """
    Deep Q-Network: maps state -> Q-value per action.
    Same architecture as PPO actor for fair comparison.
    """
    def __init__(self, obs=N_OBS, act=N_ACT, h=64):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs, h), nn.Tanh(),
            nn.Linear(h, h),   nn.Tanh(),
            nn.Linear(h, act),
        )

    def forward(self, x):
        return self.net(x)


class ReplayBuffer:
    def __init__(self, capacity):
        self.buf = deque(maxlen=capacity)

    def push(self, s, a, r, s2, done):
        self.buf.append((s, a, r, s2, done))

    def sample(self, n):
        batch = random.sample(self.buf, n)
        s, a, r, s2, d = zip(*batch)
        return (torch.as_tensor(np.array(s),  dtype=torch.float32),
                torch.as_tensor(a,             dtype=torch.long),
                torch.as_tensor(r,             dtype=torch.float32),
                torch.as_tensor(np.array(s2), dtype=torch.float32),
                torch.as_tensor(d,             dtype=torch.float32))

    def __len__(self):
        return len(self.buf)


class DQN:
    def __init__(self, train=True, ckpt=CKPT_DEFAULT):
        self.q       = QNetwork()
        self.q_tgt   = QNetwork()
        self.q_tgt.load_state_dict(self.q.state_dict())
        self.q_tgt.eval()
        self.opt     = torch.optim.Adam(self.q.parameters(), lr=LR)
        self.buf     = ReplayBuffer(BUFFER_CAP)
        self.train_mode = train
        self.ckpt    = ckpt
        self.steps   = 0
        self.eps     = EPS_START

        if os.path.exists(ckpt):
            self.q.load_state_dict(torch.load(ckpt, map_location="cpu"))
            self.q_tgt.load_state_dict(self.q.state_dict())
            print(f"[DQN] loaded checkpoint {ckpt}")

        if train and not os.path.exists(TRAIN_LOG):
            with open(TRAIN_LOG, "w") as f:
                f.write("ts,step,epsilon,loss,mean_q\n")

    def select(self, obs_np, mask_np):
        """Epsilon-greedy with action masking."""
        self.steps += 1
        self.eps = EPS_END + (EPS_START - EPS_END) * \
                   np.exp(-self.steps / EPS_DECAY)

        if self.train_mode and random.random() < self.eps:
            # random valid action
            valid = [i for i, m in enumerate(mask_np) if m]
            return random.choice(valid)

        obs = torch.as_tensor(obs_np, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            q_vals = self.q(obs).squeeze(0).numpy()
        q_vals[~mask_np] = -1e9
        return int(np.argmax(q_vals))

    def store(self, s, a, r, s2, done=False):
        self.buf.push(s, a, r, s2, done)

    def maybe_update(self):
        if not self.train_mode or len(self.buf) < TRAIN_START:
            return None

        s, a, r, s2, d = self.buf.sample(BATCH_SIZE)

        # current Q values
        q_curr = self.q(s).gather(1, a.unsqueeze(1)).squeeze(1)

        # target Q values (Double DQN style)
        with torch.no_grad():
            a_next = self.q(s2).argmax(1)
            q_next = self.q_tgt(s2).gather(1, a_next.unsqueeze(1)).squeeze(1)
            q_tgt  = r + GAMMA * q_next * (1 - d)

        loss = F.huber_loss(q_curr, q_tgt)
        self.opt.zero_grad(); loss.backward()
        nn.utils.clip_grad_norm_(self.q.parameters(), 1.0)
        self.opt.step()

        # sync target network
        if self.steps % TARGET_UPDATE == 0:
            self.q_tgt.load_state_dict(self.q.state_dict())

        torch.save(self.q.state_dict(), self.ckpt)

        loss_val = float(loss)
        mean_q   = float(q_curr.mean())
        with open(TRAIN_LOG, "a") as f:
            f.write(f"{datetime.datetime.now():%H:%M:%S},"
                    f"{self.steps},{self.eps:.4f},"
                    f"{loss_val:.4f},{mean_q:.4f}\n")
        return loss_val, mean_q


# ------------------------------ xApp ----------------------------------------
class DqnXApp(xAppBase):
    def __init__(self, c, h, r, interval, dqn):
        super().__init__(c, h, r)
        self.interval = interval
        self.dqn      = dqn
        self.m        = {}
        self.lock     = threading.Lock()
        self.prev     = None   # (obs, action) awaiting next state for buffer
        self._ws()

    def _ws(self):
        import websocket
        def on_open(ws):
            ws.send(json.dumps({"cmd": "metrics_subscribe"}))
            print("[WS] subscribed 8002")
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if "cells" not in d: return
                for cell in d["cells"]:
                    for ue in cell.get("ue_list", []):
                        r = ue.get("rnti")
                        if not r: continue
                        with self.lock:
                            self.m[r] = {
                                "cqi":  ue.get("cqi", 1),
                                "sd":   ue.get("slice_sd", 3),
                                "node": ue.get("e2_node", E2_NODE),
                                "f1ap": ue.get("f1ap", 0),
                                "dl":   ue.get("dl_brate", 0),
                                "ts":   time.time(),
                            }
            except: pass
        def th():
            ws = websocket.WebSocketApp("ws://"+WS_URL,
                                        on_open=on_open, on_message=on_msg)
            while ws.run_forever(): time.sleep(1)
        threading.Thread(target=th, daemon=True).start()

    def _slice_of(self, sd):
        return {1: "CRITICAL", 2: "PERFORMANCE", 3: "BUSINESS"}.get(sd, "BUSINESS")

    def _snapshot(self):
        with self.lock:
            act = {r: dict(v) for r, v in self.m.items()
                   if time.time()-v["ts"] < 10}
        slc = {}
        for r, x in act.items():
            name = self._slice_of(x["sd"])
            slc[name] = {"cqi": x["cqi"], "dl": x["dl"],
                         "f1ap": x["f1ap"], "node": x["node"]}
        return slc

    def _state_reward(self, slc):
        sat, cqi = [], []
        for s in SLICES:
            d = slc.get(s, {"cqi": 1, "dl": 0})
            sat.append(min(d["dl"] / SLA_DL[s], 1.5))
            cqi.append(d["cqi"] / 15.0)
        state    = np.array(sat + cqi, dtype=np.float32)
        sat_clip = np.clip(np.array(sat), 0, 1.0)
        reward   = float(np.dot(W, sat_clip))
        if sat_clip[0] < 1.0:
            reward -= CRIT_PENALTY
        return state, reward, sat_clip

    def _mask(self, sat_clip):
        m = np.ones(N_ACT, dtype=bool)
        if sat_clip[0] < 1.0:
            for i, p in enumerate(PROFILES):
                if p[0] < CRIT_MIN_FLOOR:
                    m[i] = False
        if not m.any(): m[1] = True
        return m

    def _apply(self, action, slc):
        prof = PROFILES[action]
        prb_out = {}
        for ratio, s in zip(prof, SLICES):
            if s not in slc: continue
            d = slc[s]
            try:
                self.e2sm_rc.control_slice_level_prb_quota(
                    d["node"], d["f1ap"], int(ratio), 100,
                    dedicated_prb_ratio=100, ack_request=1)
            except Exception as e:
                print(f"  [E2] {s} f1ap={d['f1ap']} FAIL: {e}")
            prb_out[str(d["f1ap"])] = {
                "prb_min": ratio, "prb_max": 100,
                "slice_name": s, "f1ap_id": d["f1ap"],
                "alloc_req_bps": SLA_DL[s],
            }
        try:
            import json as _j
            _j.dump(prb_out, open("/tmp/prb_decisions.json", "w"))
        except: pass

    def _loop(self):
        mode = "TRAIN" if self.dqn.train_mode else "EVAL"
        print(f"[DQN-CTRL] {mode} | interval={self.interval}s | "
              f"buffer={BUFFER_CAP} | batch={BATCH_SIZE}")
        print(f"[DQN-CTRL] eps: {EPS_START}→{EPS_END} over {EPS_DECAY} steps")
        while self.running:
            time.sleep(self.interval)
            slc = self._snapshot()
            if not slc:
                print(f"{datetime.datetime.now():%H:%M:%S} (0 UEs)"); continue

            state, reward, sat = self._state_reward(slc)
            mask = self._mask(sat)

            # store transition from previous step
            if self.prev is not None and self.dqn.train_mode:
                prev_s, prev_a = self.prev
                self.dqn.store(prev_s, prev_a, reward, state, done=False)
                out = self.dqn.maybe_update()
                if out and self.dqn.steps % 10 == 0:
                    loss, mean_q = out
                    print(f"  [STEP {self.dqn.steps:04d}] "
                          f"eps={self.dqn.eps:.3f} "
                          f"loss={loss:.4f} mean_q={mean_q:.4f}")

            action = self.dqn.select(state, mask)
            self._apply(action, slc)
            self.prev = (state, action)

            t = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"{t} "
                  f"sat=[{sat[0]:.2f},{sat[1]:.2f},{sat[2]:.2f}] "
                  f"cqi=[{int(state[3]*15)},{int(state[4]*15)},{int(state[5]*15)}] "
                  f"act={action}{PROFILES[action]} "
                  f"r={reward:.3f} eps={self.dqn.eps:.3f}")

    @xAppBase.start_function
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config",           default="")
    p.add_argument("--http_server_port", type=int,   default=8096)
    p.add_argument("--rmr_port",         type=int,   default=4566)
    p.add_argument("--interval",         type=float, default=1.0)
    p.add_argument("--train", action="store_true")
    p.add_argument("--eval",  action="store_true")
    p.add_argument("--ckpt",  default=CKPT_DEFAULT)
    a = p.parse_args()
    train = not a.eval

    dqn = DQN(train=train, ckpt=a.ckpt)
    x   = DqnXApp(a.config, a.http_server_port, a.rmr_port, a.interval, dqn)
    x.e2sm_rc.set_ran_func_id(3)
    for s in (signal.SIGQUIT, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, x.signal_handler)
    x.start()
