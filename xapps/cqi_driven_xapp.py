#!/usr/bin/env python3
# Classic CQI-proportional allocator xApp -- 3 UEs / 1 DU.  BASELINE for RL comparison.
#
# Channel-aware proportional (academic baseline): each slice's PRB share is
# proportional to its reported CQI, normalised across active UEs:
#     ratio_i = round(100 * CQI_i / sum(CQI))
# min = ratio (guaranteed proportional share), max = 100 (may use spare PRBs).
# No SLA targets, no bitrate math, no priorities, no efficiency tables, no learning.
# This is the non-learning reference to compare PPO / DQN / xSlice against.
import argparse, signal, json, threading, time, datetime, os
from lib.xAppBase import xAppBase

WS_URL     = "10.0.2.1:8002"
E2_NODE    = "gnbd_001_001_00000213_1"   # verify: redis-cli KEYS '*RAN*'
SLICE_NAME = {1: "CRITICAL", 2: "PERFORMANCE", 3: "BUSINESS"}
DECISION_CSV = "/tmp/baseline_cqi_decisions.csv"


class CqiXApp(xAppBase):
    def __init__(self, c, h, r, interval):
        super().__init__(c, h, r)
        self.m = {}
        self.lock = threading.Lock()
        self.interval = interval
        if not os.path.exists(DECISION_CSV):
            with open(DECISION_CSV, "w") as f:
                f.write("ts,rnti,f1ap,slice,cqi,prb_min,prb_max\n")
        self._ws()

    def _ws(self):
        import websocket
        def on_open(ws):
            ws.send(json.dumps({"cmd": "metrics_subscribe"}))
            print("[WS] subscribed to 8002")
        def on_msg(ws, msg):
            try:
                d = json.loads(msg)
                if "cells" not in d: return
                for cell in d["cells"]:
                    for ue in cell.get("ue_list", []):
                        r = ue.get("rnti"); cqi = ue.get("cqi")
                        if not r or not cqi: continue
                        with self.lock:
                            self.m[r] = {"cqi": cqi, "sd": ue.get("slice_sd", 3),
                                         "node": ue.get("e2_node", E2_NODE),
                                         "f1ap": ue.get("f1ap", 0),
                                         "dl": ue.get("dl_brate", 0), "ts": time.time()}
            except:
                pass
        def th():
            ws = websocket.WebSocketApp("ws://" + WS_URL, on_open=on_open, on_message=on_msg)
            while ws.run_forever(): time.sleep(1)
        threading.Thread(target=th, daemon=True).start()

    def _loop(self):
        print(f"[CTRL] BASELINE CQI-proportional | 3 UEs / 1 DU | interval={self.interval}s")
        while self.running:
            time.sleep(self.interval)
            with self.lock:
                act = {r: dict(v) for r, v in self.m.items() if time.time() - v["ts"] < 10}
            t = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"\n{t} === alloc ({len(act)} UEs) ===")
            if not act:
                continue
            csum = sum(v["cqi"] for v in act.values())
            prb_out = {}
            rows = []
            for r, x in act.items():
                ratio = int(round(100 * x["cqi"] / csum)) if csum > 0 else 0
                mn = ratio; mx = 100
                sn = SLICE_NAME.get(x["sd"], "?")
                print(f"  f1ap={x['f1ap']} {sn:>11} RNTI={r} CQI={x['cqi']} "
                      f"DL={x['dl']/1e6:.2f}Mb min={mn} max={mx}")
                try:
                    self.e2sm_rc.control_slice_level_prb_quota(
                        x["node"], x["f1ap"], mn, mx, dedicated_prb_ratio=100, ack_request=1)
                except Exception as e:
                    print(f"  [E2] f1ap={x['f1ap']} FAIL: {e}")
                prb_out[str(r)] = {"prb_min": mn, "prb_max": mx, "slice_name": sn,
                                   "f1ap_id": x["f1ap"], "alloc_req_bps": ""}
                rows.append(f"{t},{r},{x['f1ap']},{sn},{x['cqi']},{mn},{mx}")
            try: json.dump(prb_out, open("/tmp/prb_decisions.json", "w"))
            except: pass
            try:
                with open(DECISION_CSV, "a") as f: f.write("\n".join(rows) + "\n")
            except: pass

    @xAppBase.start_function
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="")
    p.add_argument("--http_server_port", type=int, default=8094)
    p.add_argument("--rmr_port", type=int, default=4564)
    p.add_argument("--interval", type=int, default=5, help="decision cadence (s), match your RL agents")
    a = p.parse_args()
    x = CqiXApp(a.config, a.http_server_port, a.rmr_port, a.interval)
    x.e2sm_rc.set_ran_func_id(3)
    for s in (signal.SIGQUIT, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, x.signal_handler)
    x.start()