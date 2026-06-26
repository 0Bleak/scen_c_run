#!/usr/bin/env python3
# CQI injector -- 3 UEs / 1 ZMQ DU. Reads the DU metrics on :8003, overwrites
# cqi with the SNCF trace value, stamps slice_sd / f1ap / e2_node by attach
# order, and re-serves on :8002 for the xApp and logger. RNTI passed through
# unchanged (no remap). Launch UEs in slice order:
#   slot0 -> sd1 CRITICAL, slot1 -> sd2 PERFORMANCE, slot2 -> sd3 BUSINESS.
import argparse, json, os, glob, random, time, threading, csv
import websocket
from websocket_server import WebsocketServer

# Verify e2_node against: docker exec ric_dbaas redis-cli KEYS '*RAN*'
DU_URL    = "127.0.0.1:8003"
E2_NODE   = "gnbd_001_001_00000213_1"
SLICE_ORD = [2, 1, 3]

class SNCFTrace:
    def __init__(self, path):
        self.v = []; self.i = 0
        with open(path) as f:
            last = None
            for row in csv.DictReader(f):
                ts = row.get("Timestamp", ""); c = int(row.get("CQI_wb", 15))
                if last is None or ts[:19] != last[:19]:
                    self.v.append(c); last = ts
        if not self.v: self.v = [15]
    def next(self):
        x = self.v[self.i]; self.i = (self.i + 1) % len(self.v); return x

class TraceMgr:
    def __init__(self, d):
        self.files = sorted(glob.glob(os.path.join(d, "train*_*.csv")))
        if not self.files: raise FileNotFoundError(d)
        random.shuffle(self.files); self.t = {}; self.idx = 0
    def cqi(self, key):
        if key not in self.t:
            f = self.files[self.idx % len(self.files)]; self.idx += 1
            self.t[key] = SNCFTrace(f)
        return self.t[key].next()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--proxy_port", type=int, default=8002)
    p.add_argument("--dataset_dir", required=True)
    a = p.parse_args()
    tm = TraceMgr(a.dataset_dir)

    latest = {"msg": None, "lock": threading.Lock()}
    slot_of = {}                       # raw_rnti -> slot (0,1,2), by attach order

    srv = WebsocketServer(host="0.0.0.0", port=a.proxy_port)
    srv.set_fn_message_received(
        lambda c, s, m: s.send_message(c, json.dumps({"cmd": "metrics_subscribe"}))
        if '"metrics_subscribe"' in m else None)
    threading.Thread(target=srv.run_forever, daemon=True).start()

    def du_loop():
        while True:
            try:
                ws = websocket.create_connection(f"ws://{DU_URL}", timeout=30)
                ws.send(json.dumps({"cmd": "metrics_subscribe"}))
                print(f"[INJ] connected {DU_URL} -> {E2_NODE}")
                while True:
                    d = json.loads(ws.recv())
                    if "cells" not in d: continue
                    for cell in d["cells"]:
                        for ue in cell.get("ue_list", []):
                            r = ue.get("rnti", 0)
                            if not r: continue
                            if r not in slot_of:
                                slot_of[r] = min(len(slot_of), 2)
                            ue["f1ap"]     = slot_of[r]
                            ue["e2_node"]  = E2_NODE
                            ue["slice_sd"] = SLICE_ORD[slot_of[r]]
                            ue["cqi"]      = tm.cqi(r)
                    with latest["lock"]: latest["msg"] = d
            except Exception as e:
                print(f"[INJ] {DU_URL} err {e}, retry"); time.sleep(2)

    def push():
        while True:
            time.sleep(1)
            with latest["lock"]: m = latest["msg"]
            if not m: continue
            ues = []
            for c in m["cells"]: ues += c.get("ue_list", [])
            if ues: srv.send_message_to_all(json.dumps({"cells": [{"ue_list": ues}]}))

    threading.Thread(target=du_loop, daemon=True).start()
    threading.Thread(target=push, daemon=True).start()
    print("[INJ] 1 DU (8003) -> 8002, raw RNTI, SNCF CQI injection")
    while True: time.sleep(60)

if __name__ == "__main__":
    main()