#!/usr/bin/env python3
# Minimal passthrough: reads DU metrics on :8003, stamps slice_sd/f1ap/e2_node
# by attach order, passes CQI UNTOUCHED from DU (real CQI from broker SNR).
# Re-serves on :8002 for xApp and logger.
import argparse, json, time, threading
import websocket
from websocket_server import WebsocketServer

DU_URL    = "127.0.0.1:8003"
E2_NODE   = "gnbd_001_001_00000213_1"
# attach order on this PC: 008->006->007 -> SLICE_ORD=[3,1,2]
# slot0=sd3=BUSINESS, slot1=sd1=CRITICAL, slot2=sd2=PERFORMANCE
SLICE_ORD = [3, 1, 2]

def main():
    latest = {"msg": None, "lock": threading.Lock()}
    slot_of = {}

    srv = WebsocketServer(host="0.0.0.0", port=8002)
    srv.set_fn_message_received(
        lambda c, s, m: s.send_message(c, json.dumps({"cmd": "metrics_subscribe"}))
        if '"metrics_subscribe"' in m else None)
    threading.Thread(target=srv.run_forever, daemon=True).start()

    def du_loop():
        while True:
            try:
                ws = websocket.create_connection(f"ws://{DU_URL}", timeout=30)
                ws.send(json.dumps({"cmd": "metrics_subscribe"}))
                print(f"[STAMP] connected {DU_URL} -> {E2_NODE}")
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
                            # CQI left untouched — real value from DU
                    with latest["lock"]: latest["msg"] = d
            except Exception as e:
                print(f"[STAMP] err {e}, retry"); time.sleep(2)

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
    print("[STAMP] DU:8003 -> :8002 passthrough, real CQI, slice stamping only")
    while True: time.sleep(60)

if __name__ == "__main__":
    main()
