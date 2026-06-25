#!/usr/bin/env python3
# Per-UE gain only — no channel_model block, no timing disruption.
# Different gains -> different SINR at DU -> different MCS -> different CQI.
# slot0=BUSINESS gain=0.80, slot1=CRITICAL gain=0.25, slot2=PERF gain=0.08
from gnuradio import gr, blocks, zeromq

GAINS  = [0.80, 0.25, 0.08]
UE_RX  = [3010, 3011, 3012]
UE_TX  = [3001, 3002, 3003]

class Broker(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "3-UE Gain Broker DU1")
        self.gnb_dl = zeromq.req_source(
            gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3000", 100, False, -1)
        for p, gain in zip(UE_RX, GAINS):
            scale = blocks.multiply_const_cc(gain)
            sink  = zeromq.rep_sink(
                gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1)
            self.connect(self.gnb_dl, scale, sink)
            print(f"[BROKER] port={p} gain={gain}")
        self.adder  = blocks.add_cc(1)
        self.gnb_ul = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3009", 100, False, -1)
        for i, p in enumerate(UE_TX):
            self.connect(
                zeromq.req_source(gr.sizeof_gr_complex, 1,
                    f"tcp://127.0.0.1:{p}", 100, False, -1),
                (self.adder, i))
        self.connect(self.adder, self.gnb_ul)

if __name__ == "__main__":
    tb = Broker()
    print(f"[BROKER GAIN] gains={GAINS}")
    try:
        tb.start()
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()
