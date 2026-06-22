#!/usr/bin/env python3
# 3-UE ZMQ broker: identical to the classic working broker for the first DELAY
# seconds (clean DL -> all UEs attach), THEN per-UE Rayleigh-fading noise turns
# on, producing real time-varying CQI without a restart or re-attach.
#
# How it stays clean for attach: each DL path has fading_model -> channel_model,
# but channel_model.noise_voltage starts at 0. With no noise floor the Rayleigh
# fade does not move SINR, so CQI stays ~15 and SSB/RACH decode normally. After
# DELAY s a timer calls set_noise_voltage(NOISE_V): now fade x signal vs noise
# floor -> SINR swings -> CQI dips, time-varying, independent per UE.
#
# Tune: NOISE_V (dip depth / drop risk), FDTS (fade speed), DELAY (attach window).
import threading, time
from gnuradio import gr, blocks, zeromq, channels

# ---- tunables -------------------------------------------------------------
NOISE_V = 0.10            # noise floor switched on after DELAY. raise for deeper CQI dips.
FDTS    = 5e-7            # normalized Doppler (fade speed). raise for choppier CQI.
DELAY   = 20             # seconds of CLEAN channel for attach before noise turns on
SEEDS   = [13, 47, 91]    # per-UE independent fading
UE_RX   = [3010, 3011, 3012]
UE_TX   = [3001, 3002, 3003]
# ---------------------------------------------------------------------------

class Broker(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "3-UE delayed-fading Broker DU1")
        # DL: DU(3000) -> per-UE [Rayleigh fade -> channel_model(noise=0 for now)] -> UE rx
        self.gnb_dl = zeromq.req_source(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3000", 100, False, -1)
        self.noise_blocks = []
        for p, sd in zip(UE_RX, SEEDS):
            fade = channels.fading_model(8, FDTS, False, 4.0, sd)            # Rayleigh, always on
            chan = channels.channel_model(noise_voltage=0.0, frequency_offset=0.0,
                                          epsilon=1.0, taps=[1.0 + 0j], noise_seed=sd, block_tags=False)
            sink = zeromq.rep_sink(gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1)
            self.connect(self.gnb_dl, fade, chan, sink)
            self.noise_blocks.append(chan)
        # UL: plain sum (unchanged, exactly classic)
        self.adder = blocks.add_cc(1)
        self.gnb_ul = zeromq.rep_sink(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3009", 100, False, -1)
        for i, p in enumerate(UE_TX):
            self.connect(zeromq.req_source(gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1),
                         (self.adder, i))
        self.connect(self.adder, self.gnb_ul)

    def arm_noise(self, delay, noise_v):
        def worker():
            time.sleep(delay)
            for ch in self.noise_blocks:
                ch.set_noise_voltage(noise_v)
            print(f"[FADING] noise ON after {delay}s  ->  NOISE_V={noise_v}  (CQI now varying)")
        threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    tb = Broker()
    print(f"[BROKER FADING DU1] start CLEAN (noise=0). Noise turns on in {DELAY}s. "
          f"target NOISE_V={NOISE_V} FDTS={FDTS} seeds={SEEDS}")
    tb.start()
    tb.arm_noise(DELAY, NOISE_V)
    try:
        tb.wait()
    except KeyboardInterrupt:
        tb.stop(); tb.wait()