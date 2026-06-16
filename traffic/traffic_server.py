#!/usr/bin/env python3
"""
Bidirectional Traffic Server
Runs on Tower-2 (or any reachable host for testing).
- Receives UL packets and logs them
- Responds to DL_REQ packets with real payload of requested size
- Responds to PING packets with PONG for latency measurement

Protocol:
  UL data:  raw bytes (just UL traffic, server logs it)
  DL_REQ:   [4 bytes size][6 bytes "DL_REQ"] -> server responds with [size] bytes
  PING:     [8 bytes timestamp][4 bytes "PING"] -> server responds with [8 bytes timestamp][4 bytes "PONG"]

Usage: python3 traffic_server.py
Test:  python3 traffic_server.py --test
"""

import socket
import struct
import threading
import time
import argparse


class PortHandler:
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", port))
        self.ul_bytes = 0
        self.dl_bytes = 0
        self.ul_packets = 0
        self.dl_packets = 0
        self.ping_count = 0

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
                self.ul_bytes += len(data)
                self.ul_packets += 1

                # DL request: respond with requested bytes
                if len(data) >= 10 and data[4:10] == b"DL_REQ":
                    size = struct.unpack("!I", data[:4])[0]
                    size = min(size, 60000)
                    # Include a sequence header for tracking
                    response = struct.pack("!I", size) + bytes(size - 4 if size > 4 else 0)
                    self.sock.sendto(response, addr)
                    self.dl_bytes += len(response)
                    self.dl_packets += 1

                # Latency ping: echo back timestamp
                elif len(data) >= 12 and data[8:12] == b"PING":
                    ts_bytes = data[:8]
                    pong = ts_bytes + b"PONG"
                    self.sock.sendto(pong, addr)
                    self.ping_count += 1

            except Exception as e:
                print(f"[PORT {self.port}] Error: {e}")


def stats_loop(handlers, interval=30):
    start = time.time()
    while True:
        time.sleep(interval)
        elapsed = time.time() - start
        print(f"\n{'='*70}")
        print(f"[SERVER STATS] Elapsed: {elapsed:.0f}s")
        print(f"{'  Port':>8} {'UL kbps':>10} {'DL kbps':>10} {'UL pkts':>10} {'DL pkts':>10} {'Pings':>8}")
        print(f"{'-'*70}")
        for h in handlers:
            ul_kbps = (h.ul_bytes * 8 / 1000) / elapsed if elapsed > 0 else 0
            dl_kbps = (h.dl_bytes * 8 / 1000) / elapsed if elapsed > 0 else 0
            print(f"  {h.port:>6} {ul_kbps:>10.2f} {dl_kbps:>10.2f} {h.ul_packets:>10} {h.dl_packets:>10} {h.ping_count:>8}")
        print(f"{'='*70}\n")


def main():
    parser = argparse.ArgumentParser(description="Bidirectional Traffic Server")
    parser.add_argument("--test", action="store_true", help="Run self-test")
    args = parser.parse_args()

    # Critical slice ports: 6001-6005
    # Performance slice ports: 7001-7005
    ports = list(range(6001, 6006)) + list(range(7001, 7006)) + [9001]

    handlers = []
    for port in ports:
        h = PortHandler(port)
        t = threading.Thread(target=h.run, daemon=True)
        t.start()
        handlers.append(h)

    print(f"[SERVER] Bidirectional traffic server running")
    print(f"[SERVER] Critical ports:    6001-6005")
    print(f"[SERVER] Performance ports: 7001-7005")
    print(f"[SERVER] Protocol: UL raw | DL_REQ->response | PING->PONG")

    if args.test:
        print("\n[TEST] Running self-test...")
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Test UL
        test_sock.sendto(b"hello", ("127.0.0.1", 6001))
        time.sleep(0.1)
        print(f"  UL test: sent 5 bytes, server received {handlers[0].ul_bytes} bytes -- {'OK' if handlers[0].ul_bytes == 5 else 'FAIL'}")

        # Test DL
        req = struct.pack("!I", 1000) + b"DL_REQ"
        test_sock.sendto(req, ("127.0.0.1", 6001))
        test_sock.settimeout(1)
        try:
            resp, _ = test_sock.recvfrom(65535)
            print(f"  DL test: requested 1000 bytes, received {len(resp)} bytes -- {'OK' if len(resp) == 1000 else 'FAIL'}")
        except socket.timeout:
            print(f"  DL test: TIMEOUT -- FAIL")

        # Test PING
        ts = struct.pack("!d", time.perf_counter())
        test_sock.sendto(ts + b"PING", ("127.0.0.1", 6001))
        try:
            resp, _ = test_sock.recvfrom(65535)
            if resp[8:12] == b"PONG":
                sent_ts = struct.unpack("!d", resp[:8])[0]
                rtt = (time.perf_counter() - sent_ts) * 1000
                print(f"  PING test: RTT={rtt:.2f}ms -- OK")
            else:
                print(f"  PING test: bad response -- FAIL")
        except socket.timeout:
            print(f"  PING test: TIMEOUT -- FAIL")

        test_sock.close()
        print("[TEST] Done\n")

    stats_thread = threading.Thread(target=stats_loop, args=(handlers,), daemon=True)
    stats_thread.start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[SERVER] Stopped")


if __name__ == "__main__":
    main()