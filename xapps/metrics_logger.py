#!/usr/bin/env python3
# Logger -- raw srsRAN fields only, plus two pure-arithmetic ratios:
#   bler_dl    = dl_nof_nok / (dl_nof_ok + dl_nof_nok)   (real HARQ counters)
#   sla_dl_sat = dl_brate_bps / sla_dl_target_bps        (real brate / config target)
# No models, no estimates. cqi is injected (see METRICS_PROVENANCE.md).
import json, time, csv, os, threading
import websocket

CSV_FILE = "/tmp/ue_metrics_log.csv"
PRB_FILE = "/tmp/prb_decisions.json"
prb = {}

SLICE_NAME    = {1: "CRITICAL", 2: "PERFORMANCE", 3: "BUSINESS"}
SLA_DL_TARGET = {1: 350_000,   2: 300_000,        3: 20_000_000}

def load_prb():
    global prb
    while True:
        try:
            if os.path.exists(PRB_FILE):
                prb = json.load(open(PRB_FILE))
        except: pass
        time.sleep(2)

HEADER = [
    "timestamp", "rnti", "e2_node", "f1ap", "slice_sd", "slice_name",
    "cqi", "ri", "dl_mcs", "ul_mcs", "pusch_snr_db", "pusch_rsrp_db", "phr",
    "dl_brate_bps", "ul_brate_bps", "dl_nof_ok", "dl_nof_nok",
    "dl_latency_us", "ul_latency_us", "prb_min", "prb_max",
    "alloc_req_bps", "sla_dl_target_bps",
    # ---- pure-arithmetic on real fields only ----
    "bler_dl", "sla_dl_sat",
]

with open(CSV_FILE, "w", newline="") as f:
    csv.writer(f).writerow(HEADER)

def on_open(ws):
    ws.send(json.dumps({"cmd": "metrics_subscribe"}))
    print("[LOG] subscribed to 8002 (3 UEs)")

def on_msg(ws, msg):
    try:
        d = json.loads(msg)
        if "cells" not in d: return
        ts = time.time()
        for cell in d["cells"]:
            for ue in cell.get("ue_list", []):
                r = ue.get("rnti", 0); cqi = ue.get("cqi", 0)
                if not cqi: continue
                sd = ue.get("slice_sd", 3)
                p = prb.get(str(r), {})

                dl_brate = ue.get("dl_brate", 0) or 0
                ok       = ue.get("dl_nof_ok", 0) or 0
                nok      = ue.get("dl_nof_nok", 0) or 0
                sla      = SLA_DL_TARGET.get(sd, 0)

                bler_dl = nok / (ok + nok) if (ok + nok) > 0 else 0.0
                sla_sat = dl_brate / sla if sla > 0 else 0.0

                csv.writer(open(CSV_FILE, "a", newline="")).writerow([
                    round(ts, 3), r, ue.get("e2_node", ""), ue.get("f1ap", ""),
                    sd, SLICE_NAME.get(sd, "?"),
                    cqi, ue.get("ri", 1), ue.get("dl_mcs", 0), ue.get("ul_mcs", 0),
                    round(ue.get("pusch_snr_db", 0), 1), round(ue.get("pusch_rsrp_db", 0), 1),
                    ue.get("phr", 0), dl_brate, ue.get("ul_brate", 0),
                    ok, nok,
                    ue.get("dl_latency", 0), ue.get("ul_latency", 0),
                    p.get("prb_min", ""), p.get("prb_max", ""),
                    p.get("alloc_req_bps", ""), sla,
                    round(bler_dl, 4), round(sla_sat, 4),
                ])
    except: pass

threading.Thread(target=load_prb, daemon=True).start()
ws = websocket.WebSocketApp("ws://10.0.2.1:8002", on_open=on_open, on_message=on_msg)
while ws.run_forever(): time.sleep(1)