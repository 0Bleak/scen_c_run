#!/usr/bin/env python3
# Rule-based allocator xApp -- 3 UEs / 1 DU / one slice per UE.
#
# Allocates min/max PRB ratio per slice from reported CQI + slice priority, with
# a GUARANTEED minimum: every slice's min floor is granted first, in priority
# order (CRITICAL > PERFORMANCE > BUSINESS), so the protected slices are never
# starved. The min is also bumped CQI-adaptively (bad channel -> more PRBs).
#
# Why the old version failed: it sized min = ceil(SLA / theoretical_per_PRB),
# which at high CQI is ~1 PRB -> min=2% (and req=0 -> min=0). No floor, and
# best-effort BUSINESS ate the spare, so CRITICAL fell below SLA under load.
import argparse, signal, json, threading, time, math, datetime
from lib.xAppBase import xAppBase

TOTAL_PRBS = 52
PRB_BPS_FACTOR = 156 * 1000            # ~bits/s per PRB at CQI spectral-eff 1.0
CQI_EFF = {
    1: .1523, 2: .2344, 3: .3770, 4: .6016, 5: .8770,
    6: 1.1758, 7: 1.4766, 8: 1.9141, 9: 2.4063, 10: 2.7305,
    11: 3.3223, 12: 3.9023, 13: 4.5234, 14: 5.1152, 15: 5.5547
}
WS_URL = "10.0.2.1:8002"
E2_NODE = "gnbd_001_001_00000213_1"     # verify: redis-cli KEYS '*RAN*'

SAFETY = 1.5                            # margin on theoretical demand (real < theory)

# floor = guaranteed min PRB % always granted; ceil = cap; prio low = first.
# sla used only to bump the floor up when CQI is low.
SLICE = {
    1: {"label": "CRITICAL",    "sla": 350_000,    "floor": 20, "ceil": 80, "prio": 0, "be": False},
    2: {"label": "PERFORMANCE", "sla": 300_000,    "floor": 15, "ceil": 60, "prio": 1, "be": False},
    3: {"label": "BUSINESS",    "sla": 20_000_000, "floor": 5,  "ceil": 100,"prio": 2, "be": True},
}


class XApp3(xAppBase):
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
                                "node": ue.get("e2_node", E2_NODE),
                                "f1ap": ue.get("f1ap", 0),
                                "dl": ue.get("dl_brate", 0),
                                "ts": time.time(),
                            }
            except:
                pass
        def th():
            ws = websocket.WebSocketApp("ws://" + WS_URL, on_open=on_open, on_message=on_msg)
            while ws.run_forever(): time.sleep(1)
        threading.Thread(target=th, daemon=True).start()

    def _desired_min(self, sd, cqi):
        s = SLICE[sd]
        if s["be"]:
            return s["floor"]                       # best-effort: floor only
        per_prb = CQI_EFF.get(cqi, 1.0) * PRB_BPS_FACTOR
        need_prb = math.ceil(s["sla"] * SAFETY / per_prb) if per_prb > 0 else TOTAL_PRBS
        need_pct = math.ceil(need_prb / TOTAL_PRBS * 100)
        return min(max(s["floor"], need_pct), s["ceil"])   # floor-guaranteed, CQI-bumped

    def alloc(self, ues):
        if not ues: return {}
        desired = {r: self._desired_min(x["sd"], x["cqi"]) for r, x in ues.items()}
        order = sorted(ues, key=lambda r: SLICE[ues[r]["sd"]]["prio"])
        # grant mins in priority order; protected slices get their full floor first
        gmin = {}; budget = 100
        for r in order:
            g = min(desired[r], budget); gmin[r] = g; budget -= g
        leftover = max(0, 100 - sum(gmin.values()))
        out = {}
        for r in order:
            x = ues[r]; s = SLICE[x["sd"]]
            mx = min(s["ceil"], gmin[r] + leftover)     # may burst into spare, scheduler arbitrates
            out[r] = {"min": gmin[r], "max": mx, "label": s["label"], "sla": s["sla"],
                      "node": x["node"], "f1ap": x["f1ap"], "cqi": x["cqi"], "dl": x["dl"]}
        return out

    def _loop(self):
        print("[CTRL] 3 UEs / 1 DU / guaranteed-min allocator")
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
                    "alloc_req_bps": al["sla"]
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
    x = XApp3(a.config, a.http_server_port, a.rmr_port)
    x.e2sm_rc.set_ran_func_id(3)
    for s in (signal.SIGQUIT, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, x.signal_handler)
    x.start()