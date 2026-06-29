# System Architecture — How the Components Play Together

### CQI Injector, ZMQ Broker, Metrics Logger, and the end-to-end data flow

This document explains the three custom support components of the testbed and, most importantly, how the whole system fits together as one closed loop. It assumes the reader knows the testbed is a single-machine ZMQ setup: srsRAN split CU/DU + Open5GS core + OSC near-RT RIC + three virtual srsUEs + two RL xApps.

---

## PART 1 — THE BIG PICTURE: ONE CLOSED LOOP

The system is a control loop. Radio metrics flow up from the radio layer to the RL agent; control decisions flow back down from the agent to the scheduler. Three custom components sit between the standard pieces to make this loop work.

```
                    ┌─────────────────────────────────────────────┐
                    │                                             │
   ┌────────┐   ZMQ samples   ┌────────┐   ZMQ samples   ┌──────────┐
   │  srsUE │◄───────────────►│ BROKER │◄───────────────►│   srsDU  │
   │  x3    │  (DL fan-out,   │        │  (DL 3000,      │ (sched.) │
   │(vue1-3)│   UL sum)       └────────┘   UL 3009)      └────┬─────┘
   └────────┘                                                 │ raw metrics (ws 8003)
                                                              ▼
                                                       ┌─────────────┐
                                                       │  INJECTOR   │  reads c-rnti from
                                                       │             │  /tmp/ue_0X.log,
                                                       │ enriches    │  adds slice_sd + CQI
                                                       └──────┬──────┘
                                                              │ enriched metrics (ws 8002)
                                            ┌─────────────────┼─────────────────┐
                                            ▼                                   ▼
                                     ┌────────────┐                     ┌─────────────┐
                                     │   xApp     │  E2SM-RC PRB        │   LOGGER    │
                                     │ (PPO/DQN)  │  control ──────────►│  writes CSV │
                                     └─────┬──────┘  via RIC → DU       └─────────────┘
                                           │
                                           └──► /tmp/prb_decisions.json (applied action)
```

In one sentence: the **DU** produces radio metrics, the **injector** enriches them with slice identity and channel quality and republishes them, the **xApp** reads the enriched stream and decides a PRB allocation, that decision is sent back to the **DU** scheduler via the RIC, and the **logger** records everything for analysis. The **broker** is what makes the radio link itself work over ZMQ.

---

## PART 2 — THE ZMQ BROKER

### 2.1 What problem it solves

In a real deployment, the DU and UEs exchange radio samples over the air through SDR hardware. In this single-machine testbed there is no radio — the samples are passed as ZMQ messages over TCP loopback. But srsRAN's ZMQ device expects a single point-to-point sample stream, while the setup needs **one DU talking to three UEs**. The broker bridges this mismatch: it is a GNU Radio flowgraph that fans the DU's downlink out to three UEs and sums the three uplinks back into one stream for the DU.

### 2.2 The two paths

**Downlink (DU → UEs): fan-out.** The DU transmits one downlink sample stream on TCP port 3000. The broker reads it and copies it to three separate ports, one per UE:

```
DU(3000) ──► broker ──► UE1(3010)
                   ├──► UE2(3011)
                   └──► UE3(3012)
```

Each UE receives the same downlink stream. (This is why, without a shared throttle, each UE sees a full-rate pipe — a property that matters for the contention analysis discussed elsewhere.)

**Uplink (UEs → DU): sum.** Each UE transmits its uplink on its own port. The broker reads all three, **adds them together** (the `add_cc` block — complex sample addition, mimicking how signals superimpose on a shared medium), and sends the combined stream to the DU:

```
UE1(3001) ──┐
UE2(3002) ──┼──► add ──► DU(3009)
UE3(3003) ──┘
```

The summing is what makes the uplink behave like a shared channel: all three UEs' signals arrive at the DU combined, exactly as they would over the air.

### 2.3 Why startup order matters

The broker's ZMQ source blocks connect to the UE and DU sockets. If the broker starts before the UEs are listening, or the DU starts before the broker is bridging, the sample routing is wrong and UEs fail to decode (PBCH-MIB CRC failures). The required order is: **UEs first (they wait), then broker, then DU last.** The broker prints `[BROKER DU1] start` once its flowgraph is running.

---

## PART 3 — THE CQI INJECTOR

### 3.1 What problem it solves

The xApp's decision needs two things the raw DU metrics stream does not cleanly provide:

1. **Which slice each UE belongs to.** The DU reports per-UE metrics keyed by RNTI (a radio identifier assigned at attach), but RNTI carries no slice information and changes every session. The agent needs to know "this UE is CRITICAL, that one is BUSINESS."
2. **A meaningful, varying channel-quality signal.** In the ZMQ loopback the channel is artificially clean, so CQI does not vary realistically. To study channel-aware allocation, a realistic CQI time series is injected from recorded railway measurements.

The injector solves both: it sits between the DU's raw metrics and the xApp, **enriches** each metrics record, and republishes.

### 3.2 The RNTI → slice map (solving problem 1)

At attach, each srsUE writes its assigned `c-rnti` to its log file (`/tmp/ue_0X.log`). Because each UE config is tied to a fixed IMSI and therefore a fixed slice, the log file identity _is_ the slice identity:

```
/tmp/ue_01.log → c-rnti → CRITICAL    (sd=1)
/tmp/ue_02.log → c-rnti → PERFORMANCE (sd=2)
/tmp/ue_03.log → c-rnti → BUSINESS    (sd=3)
```

The injector reads the `c-rnti` line from each log and builds a map `{rnti → slice_sd}`. This is robust: it does not rely on attach order or any RNTI guessing — it reads the ground truth each UE recorded. It waits until all three RNTIs are found, then prints `[INJ] Map complete`.

This design fixed an earlier class of bug where slice labels were assigned by stream order rather than identity, causing systematic mislabeling every session.

### 3.3 CQI injection from SNCF traces (solving problem 2)

The injector loads CQI time series from recorded SNCF (French railway) measurement CSVs. Each UE is assigned a trace, and on every metrics tick the next CQI value from that trace is substituted into the UE's record. This gives each slice a realistic, varying channel-quality signal (CQI sweeping roughly 5–15) that the agent observes in its state vector.

The injected CQI is what makes channel-aware behaviour studyable: the agent can learn, for example, to allocate more PRBs to a slice when its CQI drops.

### 3.4 The enrich-and-republish flow

```
DU raw metrics (ws://127.0.0.1:8003)
        │  per-UE: rnti, dl_brate, snr, ...
        ▼
   INJECTOR  for each UE record:
        │      • look up slice_sd from rnti map
        │      • overwrite cqi with next SNCF trace value
        │      • tag e2_node and f1ap id
        ▼
   enriched metrics (ws://0.0.0.0:8002)
        │
        ├──► consumed by xApp (state input)
        └──► consumed by logger (CSV input)
```

The injector subscribes to the DU's raw metrics on WebSocket port 8003, enriches each UE record, and serves the enriched stream on port 8002. Both the xApp and the logger consume from 8002 — they never touch the raw DU stream directly.

---

## PART 4 — THE METRICS LOGGER

### 4.1 What it does

The logger is the system's data recorder. It subscribes to the enriched metrics stream (8002) and writes one CSV row per UE per tick to `/tmp/ue_metrics_log.csv`. This CSV is the raw material for every result, plot, and comparison table.

### 4.2 What it records

Each row contains the real radio fields (CQI, MCS, downlink/uplink bitrate, HARQ ok/nok counts, SNR, latency) plus two joined pieces and two derived quantities:

- **Joined from the decision log:** the `prb_min`/`prb_max` the xApp applied — read from `/tmp/prb_decisions.json`, keyed by RNTI. This is how each metrics row knows _which allocation was in effect_ when it was measured.
- **Derived (pure arithmetic on real fields):**
    - `bler_dl = dl_nof_nok / (dl_nof_ok + dl_nof_nok)` — block error rate from real HARQ counters.
    - `sla_dl_sat = dl_brate / sla_dl_target` — satisfaction, delivered rate over SLA target.

No values are modelled or estimated; every field is either a real measurement, the injected CQI, the applied PRB decision, or simple arithmetic on those.

### 4.3 The decision-log join

The xApp writes its applied action to `/tmp/prb_decisions.json` each interval, keyed by RNTI:

```
{ "17923": {"prb_min": 60, "prb_max": 100, "slice_name": "CRITICAL", ...}, ... }
```

The logger reads this file and, for each metrics row, looks up the decision by the row's RNTI. This is what links _measured outcome_ to _applied action_ in the dataset — without it, the dataset records what happened but not what the agent did, making it impossible to analyse whether the action drove the outcome. (The key must be RNTI on both sides for the join to land.)

### 4.4 The SLA-consistency caveat

The logger has its own `SLA_DL_TARGET` dictionary used to compute `sla_dl_sat`. If the SLA values used by the xApp differ from the logger's, the logged satisfaction column is computed against the wrong target and is incorrect. The robust practice is to **recompute satisfaction in post-processing** from the raw `dl_brate` field against the true SLAs, rather than trusting the logged `sla_dl_sat` column.

---

## PART 5 — THE FULL CLOSED LOOP, STEP BY STEP

What happens in one decision interval (one second), tracing the data through every component:

1. **Radio exchange.** The DU scheduler allocates PRBs to the three UEs; samples flow through the **broker** (DL fan-out, UL sum). Each UE achieves some downlink throughput.
    
2. **Raw metrics emitted.** The DU publishes per-UE metrics (RNTI, dl_brate, SNR, HARQ counts) on WebSocket 8003.
    
3. **Enrichment.** The **injector** reads each record, attaches the slice identity (via the RNTI map) and the injected SNCF CQI, and republishes on 8002.
    
4. **Agent observes.** The **xApp** reads the enriched stream, builds its 6-component state (per-slice satisfaction and CQI), and computes the reward from the previous action's outcome.
    
5. **Agent decides.** The xApp selects a PRB-allocation profile (one of the discrete actions).
    
6. **Control applied.** The xApp issues an E2SM-RC PRB-quota control message through the RIC to the DU scheduler, changing how PRBs are split next interval. It also writes the applied profile to `/tmp/prb_decisions.json`.
    
7. **Recording.** The **logger** reads the same enriched stream, joins the applied PRB decision by RNTI, and writes a CSV row capturing the measured outcome alongside the action that produced it.
    
8. **Loop closes.** The new allocation takes effect, the DU delivers different throughput next interval, new metrics flow up, and the cycle repeats — the agent continuously adapting allocation to observed conditions.
    

This is the closed-loop, online control that distinguishes the approach: the agent is trained and acting on live radio metrics in real time, not on a pre-collected offline dataset.

---

## PART 6 — COMPONENT QUICK REFERENCE

|Component|Role|Input|Output|Key port/file|
|---|---|---|---|---|
|**Broker**|bridge DU↔3 UEs over ZMQ|DU/UE sample streams|fanned-out DL, summed UL|TCP 3000/3009, 3010-12/3001-03|
|**Injector**|add slice id + realistic CQI|DU raw metrics|enriched metrics|reads ws 8003, serves ws 8002, reads `/tmp/ue_0X.log`|
|**xApp**|RL decide PRB allocation|enriched metrics|E2SM-RC control + decision log|reads ws 8002, writes `/tmp/prb_decisions.json`|
|**Logger**|record dataset|enriched metrics + decision log|CSV|reads ws 8002 + `/tmp/prb_decisions.json`, writes `/tmp/ue_metrics_log.csv`|

### Startup dependency order

1. UEs (write their c-rnti to logs)
2. Broker (after UEs)
3. DU (last in the radio chain; registers to RIC)
4. Injector (needs UE logs for the RNTI map, needs DU metrics on 8003)
5. Logger and xApp (both consume the injector's 8002 stream)

Each downstream component depends on the one above being live: the injector cannot build its map until the UEs have attached and written their RNTIs, and neither the xApp nor the logger has anything to read until the injector is republishing on 8002.