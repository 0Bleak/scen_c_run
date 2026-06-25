#!/usr/bin/env python3
# Per-UE gain + noise broker.
# SNR_dB = 20*log10(gain/noise_v)
# Target: slot0 CQI~13-15, slot1 CQI~10-12, slot2 CQI~8-10
# Gains and noise tuned so SNR spreads across CQI 8-15 range.
# noise_v stepped up slowly after attach to avoid sync loss.
import threading, time
from gnuradio import gr, blocks, zeromq, channels

# Per-UE gain: controls signal level independently
# slot0=BUSINESS slot1=CRITICAL slot2=PERFORMANCE (your SLICE_ORD=[3,1,2])
GAINS  = [0.80, 0.25, 0.08]

# Noise steps: ramp up slowly after attach, watch DU after each step
# Step 0: noise=0    (clean attach)
# Step 1: noise=0.01 (mild, SNR: 38dB / 28dB / 18dB -> CQI ~15/14/12)
# Step 2: noise=0.03 (moderate, SNR: 28dB / 18dB / 8dB -> CQI ~14/12/9)
# Step 3: noise=0.06 (strong, SNR: 22dB / 12dB / 2dB -> CQI ~13/10/8)
# If UEs drop at a step we stop at the previous step.
NOISE_STEPS  = [0.0, 0.01, 0.03, 0.06]
STEP_DELAY   = 20   # seconds between steps
ATTACH_DELAY = 30   # seconds clean before any noise

UE_RX  = [3010, 3011, 3012]
UE_TX  = [3001, 3002, 3003]
SEEDS  = [13, 47, 91]

class Broker(gr.top_block):
    def __init__(self):
        gr.top_block.__init__(self, "3-UE SNR Broker")

        self.gnb_dl    = zeromq.req_source(
            gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3000", 100, False, -1)
        self.chan_list = []

        for i, (p, gain, seed) in enumerate(zip(UE_RX, GAINS, SEEDS)):
            scale = blocks.multiply_const_cc(gain)
            chan  = channels.channel_model(
                noise_voltage=0.0,
                frequency_offset=0.0,
                epsilon=1.0,
                taps=[1.0 + 0j],
                noise_seed=seed,
                block_tags=False,
            )
            sink  = zeromq.rep_sink(
                gr.sizeof_gr_complex, 1, f"tcp://127.0.0.1:{p}", 100, False, -1)
            self.connect(self.gnb_dl, scale, chan, sink)
            self.chan_list.append(chan)
            snr_db = 20 * __import__('math').log10(gain / 0.001 + 1e-9)
            print(f"[BROKER] slot{i} port={p} gain={gain} seed={seed}")

        # UL: plain summer
        self.adder  = blocks.add_cc(1)
        self.gnb_ul = zeromq.rep_sink(
            gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:3009", 100, False, -1)
        for i, p in enumerate(UE_TX):
            self.connect(
                zeromq.req_source(gr.sizeof_gr_complex, 1,
                    f"tcp://127.0.0.1:{p}", 100, False, -1),
                (self.adder, i))
        self.connect(self.adder, self.gnb_ul)

    def ramp_noise(self):
        import math
        def worker():
            time.sleep(ATTACH_DELAY)
            for step, nv in enumerate(NOISE_STEPS):
                if nv == 0.0:
                    continue
                for ch in self.chan_list:
                    ch.set_noise_voltage(nv)
                print(f"\n[BROKER] noise step {step}/{len(NOISE_STEPS)-1} "
                      f"noise_v={nv}")
                for i, (gain, ch) in enumerate(zip(GAINS, self.chan_list)):
                    snr = 20 * math.log10(gain / nv) if nv > 0 else 99
                    print(f"  slot{i} gain={gain} SNR={snr:.1f}dB")
                print("  -> watch DU console for CQI/MCS changes")
                time.sleep(STEP_DELAY)
            print("[BROKER] max noise reached, holding")
        threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    tb = Broker()
    print(f"[BROKER SNR] gains={GAINS}")
    print(f"[BROKER SNR] clean for {ATTACH_DELAY}s then stepping noise: {NOISE_STEPS}")
    tb.start()
    tb.ramp_noise()
    try:
        tb.wait()
    except KeyboardInterrupt:
        tb.stop()
        tb.wait()
