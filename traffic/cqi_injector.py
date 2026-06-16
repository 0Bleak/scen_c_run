#!/usr/bin/env python3
import argparse, json, os, glob, random, time, threading, csv
import websocket
from websocket_server import WebsocketServer

DU_STREAMS = [
    {"url": "127.0.0.1:8003", "e2_node": "gnbd_001_001_00000213_1", "slice_order": [1, 2, 3]},
    {"url": "127.0.0.1:8004", "e2_node": "gnbd_001_001_00000213_2", "slice_order": [1, 2, 3]},
]

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
    latest = {s["url"]: {"msg": None, "lock": threading.Lock()} for s in DU_STREAMS}
    rnti_map = {}; rnti_ctr = [0xF001]; order = {}

    srv = WebsocketServer(host="0.0.0.0", port=a.proxy_port)
    srv.set_fn_message_received(lambda c, s, m: s.send_message(c, json.dumps({"cmd": "metrics_subscribe"}))
                                if '"metrics_subscribe"' in m else None)
    threading.Thread(target=srv.run_forever, daemon=True).start()

    def du_loop(stream):
        url = stream["url"]; node = stream["e2_node"]; so = stream["slice_order"]
        order.setdefault(url, {})
        while True:
            try:
                ws = websocket.create_connection(f"ws://{url}", timeout=30)
                ws.send(json.dumps({"cmd": "metrics_subscribe"}))
                print(f"[INJ] connected {url} -> {node}")
                while True:
                    d = json.loads(ws.recv())
                    if "cells" not in d: continue
                    for cell in d["cells"]:
                        for ue in cell.get("ue_list", []):
                            o = ue.get("rnti", 0)
                            if not o: continue
                            gk = (url, o)
                            if gk not in rnti_map:
                                rnti_map[gk] = rnti_ctr[0]; rnti_ctr[0] += 1
                                fid = len(order[url]); order[url][o] = min(fid, 2)
                            ue["rnti"] = rnti_map[gk]
                            ue["f1ap"] = order[url][o]
                            ue["e2_node"] = node
                            ue["slice_sd"] = so[order[url][o]]
                            ue["cqi"] = tm.cqi(rnti_map[gk])
                    with latest[url]["lock"]: latest[url]["msg"] = d
            except Exception as e:
                print(f"[INJ] {url} err {e}, retry"); time.sleep(2)

    def merge():
        while True:
            time.sleep(1); ues = []
            for s in DU_STREAMS:
                with latest[s["url"]]["lock"]:
                    m = latest[s["url"]]["msg"]
                    if m:
                        for c in m["cells"]: ues += c.get("ue_list", [])
            if ues: srv.send_message_to_all(json.dumps({"cells": [{"ue_list": ues}]}))

    for s in DU_STREAMS:
        threading.Thread(target=du_loop, args=(s,), daemon=True).start()
    threading.Thread(target=merge, daemon=True).start()
    print("[INJ] 2 DUs (8003-8004) -> 8002")
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
