#!/usr/bin/env python3
import subprocess, threading, time

SERVER = "10.45.0.1"
MAP = {
    "vue1": "critical",
    "vue2": "critical",
    "vue3": "performance",
    "vue4": "performance",
    "vue5": "business",
    "vue6": "business",
}
SCRIPT = {
    "critical":    "/home/bleak/scen_c_run/traffic/critical_traffic.py",
    "performance": "/home/bleak/scen_c_run/traffic/performance_traffic.py",
    "business":    "/home/bleak/scen_c_run/traffic/business_traffic.py",
}

def run(ns, kind):
    cmd = ["sudo", "ip", "netns", "exec", ns, "python3", SCRIPT[kind],
           "--server", SERVER, "--duration", "999999", "--ue", ns,
           "--latency_log", f"/tmp/latency_{ns}.csv"]
    while True:
        try:
            subprocess.run(cmd)
        except Exception as e:
            print(f"[{ns}] {e}, restart 3s")
            time.sleep(3)

for ns, kind in MAP.items():
    threading.Thread(target=run, args=(ns, kind), daemon=True).start()

print("[VT6] 6 UEs launched (2 critical / 2 performance / 2 business)")
while True:
    time.sleep(60)
    print("[VT6] running")
