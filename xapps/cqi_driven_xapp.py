#!/usr/bin/env python3
import argparse, signal, json, threading, time, math, datetime
from lib.xAppBase import xAppBase

TOTAL_PRBS = 52
N_RE_PRIME = 156
PRB_BPS_FACTOR = N_RE_PRIME * 1000
CQI_EFF = {
    1: .1523, 2: .2344, 3: .3770, 4: .6016, 5: .8770,
    6: 1.1758, 7: 1.4766, 8: 1.9141, 9: 2.4063, 10: 2.7305,
    11: 3.3223, 12: 3.9023, 13: 4.5234, 14: 5.1152, 15: 5.5547
}
E2_NODES = [
    "gnbd_001_001_00000213_1",
    "gnbd_001_001_00000213_2",
]
WS_URL = "10.0.2.1:8002"

# sd:1=CRITICAL, sd:2=PERFORMANCE, sd:3=BUSINESS
SLICE = {
    1: {"label": "CRITICAL",    "ceil": 80, "req": 300_000},
    2: {"label": "PERFORMANCE", "ceil": 50, "req": 300_000},
    3: {"label": "BUSINESS",    "ceil": 30, "req": 0},
}

class XApp6(xAppBase):
    def __init__(self, c, h, r):
        super().__init__(c, h, r)
        self.m = {}
        self.lock = threading.Lock()
        self.interval = 5
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
                        if not cqi: continue
                        with self.lock:
                            self.m[r] = {
                                "cqi": cqi,
                                "sd": ue.get("slice_sd", 3),
                                "node": ue.get("e2_node"),
                                "f1ap": ue.get("f1ap", 0),
                                "dl": ue.get("dl_brate", 0),
                                "ts": time.time()
                            }
            except:
                pass
        def th():
            ws = websocket.WebSocketApp("ws://" + WS_URL, on_open=on_open, on_message=on_msg)
            while ws.run_forever(): time.sleep(1)
        threading.Thread(target=th, daemon=True).start()

    def _req_pct(self, req, cqi):
        if req <= 0: return 0
        prb = CQI_EFF.get(cqi, 1.0) * PRB_BPS_FACTOR
        return math.ceil(min(math.ceil(req / prb), TOTAL_PRBS) / TOTAL_PRBS * 100)

    def alloc(self, ues):
        if not ues: return {}
        mn = {}
        for r, x in ues.items():
            pr = SLICE[x["sd"]]
            mn[r] = min(self._req_pct(pr["req"], x["cqi"]), pr["ceil"])
        tot = sum(mn.values())
        if tot > 100:
            sc = 100 / tot
            for r in mn: mn[r] = max(0, math.floor(mn[r] * sc))
        left = max(0, 100 - sum(mn.values()))
        csum = sum(x["cqi"] for x in ues.values() if x["cqi"] > 0)
        out = {}
        for r, x in ues.items():
            pr = SLICE[x["sd"]]
            b = 0
            if csum > 0 and left > 0:
                b = max(0, min(int(left * x["cqi"] / csum), pr["ceil"] - mn[r]))
            fmin = mn[r]; fmax = min(fmin + b, 100)
            out[r] = {
                "min": fmin, "max": fmax,
                "label": pr["label"], "req": pr["req"],
                "node": x["node"], "f1ap": x["f1ap"],
                "cqi": x["cqi"], "dl": x["dl"]
            }
        return out

    def _loop(self):
        print("[CTRL] 6 UEs / 2 DUs / shared 52-PRB pool")
        while self.running:
            time.sleep(self.interval)
            with self.lock:
                act = {r: v for r, v in self.m.items() if time.time() - v["ts"] < 10}
            a = self.alloc(act)
            t = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"\n{t} === alloc ({len(a)} UEs) ===")
            prb_out = {}
            for r, al in a.items():
                print(f"  {al['node']:>26} f1ap={al['f1ap']} {al['label']:>11} "
                      f"RNTI={r} CQI={al['cqi']} DL={al['dl']/1e6:.2f}Mb "
                      f"min={al['min']} max={al['max']}")
                try:
                    self.e2sm_rc.control_slice_level_prb_quota(
                        al["node"], al["f1ap"], al["min"], al["max"],
                        dedicated_prb_ratio=100, ack_request=1)
                except Exception as e:
                    print(f"  [E2] {al['node']} f1ap={al['f1ap']} FAIL: {e}")
                prb_out[str(r)] = {
                    "prb_min": al["min"], "prb_max": al["max"],
                    "slice_name": al["label"], "f1ap_id": al["f1ap"],
                    "alloc_req_bps": al["req"]
                }
            try: json.dump(prb_out, open("/tmp/prb_decisions.json", "w"))
            except: pass

    @xAppBase.start_function
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="")
    p.add_argument("--http_server_port", type=int, default=8094)
    p.add_argument("--rmr_port", type=int, default=4564)
    a = p.parse_args()
    x = XApp6(a.config, a.http_server_port, a.rmr_port)
    x.e2sm_rc.set_ran_func_id(3)
    for s in (signal.SIGQUIT, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, x.signal_handler)
    x.start()
