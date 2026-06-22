#!/usr/bin/env python3
from gnuradio import gr, blocks, zeromq

class Broker(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "3-UE ZMQ Broker DU1")
        # DL: DU -> fan-out to 3 UEs
        self.gnb_dl = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3000", 100, False, -1)
        for p in [3010, 3011, 3012]:
            self.connect(self.gnb_dl, zeromq.rep_sink(gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1))
        # UL: 3 UEs summed -> DU
        self.adder = blocks.add_cc(1)
        self.gnb_ul = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3009", 100, False, -1)
        for i, p in enumerate([3001, 3002, 3003]):
            self.connect(zeromq.req_source(gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1), (self.adder, i))
        self.connect(self.adder, self.gnb_ul)

if __name__ == "__main__":
    tb = Broker()
    print("[BROKER DU1] start")
    try:
        tb.start()
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()