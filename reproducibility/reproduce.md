# Reproducibility Notes — FRMCS O-RAN Slice-Aware RL Testbed

This directory contains every modified configuration, custom component, and captured system-state artifact required to reproduce the testbed from a clean machine. This document explains what each piece is, why it was changed from the default, and the exact order to bring the system up.

The testbed is a single-machine ZMQ loopback: srsRAN Project (split CU/DU) + Open5GS core + OSC near-RT RIC + three virtual srsUEs, with two reinforcement-learning xApps (PPO and DQN) controlling downlink PRB allocation per slice via E2SM-RC.

---

## 0. SOFTWARE PREREQUISITES

Reproduce on Fedora 42 (or equivalent), with:
- **Open5GS** built from source, installed under `~/open5gs/install/`
- **srsRAN Project** built from source under `~/srsRAN_Project/build/` (record the exact commit — see `system_state/srsran_version.txt`)
- **srsRAN 4G srsue** on the system path (ZMQ-enabled build)
- **OSC near-RT RIC** (`oran-sc-ric`) via Docker Compose under `~/oran-sc-ric/`
- **GNU Radio** with ZMQ blocks (for the broker)
- **Docker + Docker Compose**, **MongoDB** (Open5GS subscriber DB), **iperf3**

Inside the RIC's `python_xapp_runner` container, the xApps require `numpy`, `torch`, `websocket-client`. These do **not** persist across container recreation and must be reinstalled each time the container is recreated (see `system_state/xapp_container_pip_freeze.txt` for exact versions):

```
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    pip install numpy torch websocket-client --break-system-packages
```

---

## 1. SLICE MAPPING (the fixed identity of the system)

The slice assignment is anchored in MongoDB and never changes. All other components inherit it. See `system_state/mongodb_subscribers.txt` for the captured records.

| IMSI | UE config | sd | Slice | netns |
|---|---|---|---|---|
| 001010000000006 | ue_01.conf | 1 | CRITICAL | vue1 |
| 001010000000007 | ue_02.conf | 2 | PERFORMANCE | vue2 |
| 001010000000008 | ue_03.conf | 3 | BUSINESS | vue3 |

Critical points:
- **Slice assignment is server-side**, defined in the MongoDB subscriber records (`slice.sd`). The UE configs select which subscriber they are via IMSI; the slice follows from the database, not the UE.
- The xApp maps `slice_sd → name` as `{1: CRITICAL, 2: PERFORMANCE, 3: BUSINESS}`.
- The netns↔slice binding (vue1=CRITICAL, etc.) is fixed by which UE config runs in which namespace; it does **not** depend on the per-session RNTI, which changes every attach.

---

## 2. OPEN5GS CORE — modifications (`open5gs/`)

Default Open5GS does not run this configuration out of the box. Key changes:

- **NSSF disabled.** The NSSF crash-loops in this single-slice-injector setup and must not be started. Only the NFs listed in the bring-up order below are launched. (If the deployment includes an `nssf.yaml`, do not start `open5gs-nssfd`.)
- **AMF / SMF / UPF** configured for the three-slice S-NSSAI set (sst/sd matching the table above).
- **ogstun** is the core's user-plane TUN interface; the gateway address `10.45.0.1/16` is added to it at bring-up (it is not persistent).
- Subscriber records for IMSIs ...006/007/008 must exist in MongoDB with the correct `slice.sd`. Missing subscriber records cause attach failures with no obvious error.

Logs: `/var/log/open5gs/amf.log` at debug level is the primary attach-diagnostic.

---

## 3. NETWORK PLUMBING — the per-boot setup that is NOT persistent

None of the following survives a reboot. It must be re-applied every session, before the radio layer starts. This is the single most common source of "it worked yesterday" failures.

### 3.1 IP forwarding
```
sudo sysctl -w net.ipv4.ip_forward=1
```

### 3.2 iptables FORWARD rules
The Docker daemon sets the FORWARD chain default policy to DROP. Without explicit ACCEPT rules, user-plane traffic between `ogstun` and the uplink interface (`wlp2s0`) is silently dropped. Re-add every boot:
```
sudo iptables -I FORWARD -i ogstun -o wlp2s0 -j ACCEPT
sudo iptables -I FORWARD -i wlp2s0 -o ogstun -m state --state RELATED,ESTABLISHED -j ACCEPT
```
Captured current state: `system_state/iptables.txt`.

### 3.3 Loopback addresses for CU/DU F1
The split CU/DU uses loopback addresses `127.0.10.1` and `127.0.10.2` for the F1 interface. Add them to `lo`:
```
sudo ip addr add 127.0.10.1/32 dev lo 2>/dev/null; true
sudo ip addr add 127.0.10.2/32 dev lo 2>/dev/null; true
```
The DU config's `cu_cp_addr` must be `127.0.10.1`. Captured: `system_state/loopback.txt`.

### 3.4 Network namespaces (one per UE)
Network namespaces do **not** survive reboot. Create three fresh ones each session:
```
for n in vue1 vue2 vue3; do sudo ip netns del $n 2>/dev/null; done
sudo ip netns add vue1 && sudo ip netns add vue2 && sudo ip netns add vue3
```
Each srsUE runs inside its namespace and creates a `tun_srsue` interface holding a `10.45.0.x` address **only after successful attach**. If `tun_srsue` does not exist, the UE has not attached. Captured: `system_state/netns_list.txt`, `interfaces.txt`, `routes.txt`.

### 3.5 ogstun and traffic shaping (tc)
`ogstun` carries the downlink user plane. The final working regime uses **no tc shaping** — traffic contention is created by SLA sizing and offered-load shaping, not by capping. Confirm no stale qdisc:
```
sudo tc qdisc del dev ogstun root 2>/dev/null; true
```
Captured: `system_state/tc_ogstun.txt` (should show only the default qdisc).

Note on tc (documented so it is not re-attempted): tc HTB/TBF capping on `ogstun` was found unreliable — UDP bursts through the cap even with offload disabled, because the cap sits upstream of the DU scheduler and does not gate the PRB-controlled delivery. It is not used in the final setup.

---

## 4. srsRAN DU/CU — modifications (`srsran_du_cu/`)

### 4.1 DU ZMQ config (`du_zmq_01.yml`)
- **ZMQ device** at `base_srate=11.52e6`, `srate: 11.52`, `channel_bandwidth_MHz: 10`. These three must be mutually consistent and must match the UE srate. 10 MHz requires srate 11.52; lower srates (5.76/7.68) cause PBCH-MIB CRC failures and prevent attach. 5 MHz additionally requires a different `coreset0_index` and is not used here.
- **PRACH fix** (mandatory): `prach_config_index: 1`, `total_nof_ra_preambles: 64`, `nof_cb_preambles_per_ssb: 64`. Without this, the three UEs collide on PRACH and never attach.
- `cu_cp_addr` must be `127.0.10.1` (see 3.3).
- E2 agent points at the RIC; the E2 node id is `gnbd_001_001_00000213_1` (verify via `redis-cli KEYS '*RAN*'`).

### 4.2 ZMQ port layout
DU DL on tcp 3000; UL on 3009. The broker fans DL out to UE ports 3010/3011/3012 and sums UL from 3001/3002/3003. See the broker in `traffic/`.

Build commit recorded in `system_state/srsran_version.txt` — reproduce against the same commit to avoid scheduler-behaviour drift.

---

## 5. UE CONFIGS — modifications (`ue/`)

Three configs `ue_01/02/03.conf`:
- `srate = 11.52e6` and `device_args` `base_srate=11.52e6` — must match the DU exactly.
- Each binds a distinct ZMQ tx/rx port pair (3001/3010, 3002/3011, 3003/3012).
- Each sets `filename = /tmp/ue_0X.log` so the CQI injector can read the per-UE `c-rnti` for the RNTI→slice map.
- IMSI selects the subscriber (006/007/008), which determines the slice via MongoDB.

---

## 6. CUSTOM COMPONENTS (`xapps/`, `traffic/`)

### 6.1 ZMQ broker (`traffic/zmq_broker_01.py`)
GNU Radio flowgraph bridging the DU and the three UEs over ZMQ: DL fan-out (3000 → 3010/3011/3012), UL sum (3001/3002/3003 → 3009). Must start **after** the UEs and **before** the DU. Prints `[BROKER DU1] start`.

### 6.2 CQI injector (`traffic/cqi_injector.py`)
Reads each UE's `c-rnti` from its log file to build a robust RNTI→slice map (eliminating ordering bugs), subscribes to the DU metrics WebSocket (8003), enriches each UE record with slice_sd and an injected CQI value drawn from SNCF railway traces, and re-serves the enriched stream on 8002 for the xApps and logger. Wait for `[INJ] Map complete`.

### 6.3 xApps (`xapps/ppo_slice_xapp.py`, `dqn_slice_xapp.py`)
Identical MDP (6-state, discrete PRB-profile actions, weighted-satisfaction reward) so PPO and DQN are directly comparable. Both issue E2SM-RC downlink PRB-quota control each interval and log applied decisions to `/tmp/prb_decisions.json` keyed by RNTI. Full algorithm and lifecycle in `docs/`.

### 6.4 Metrics logger (`xapps/metrics_logger.py`)
Subscribes to the enriched 8002 stream and writes per-UE rows to `/tmp/ue_metrics_log.csv`, joining the applied PRB decision by RNTI. Its `SLA_DL_TARGET` dict must match the SLA values used by the xApps for the logged `sla_dl_sat` column to be correct; if they diverge, recompute satisfaction from raw `dl_brate` (CSV field 14) against the true SLAs in post-processing.

### 6.5 SNCF CQI traces
The injector consumes CSV traces from `~/scen_c_run/traffic/traces/sncf_traces/` (not included in this bundle due to size). Place the trace CSVs there before running the injector.

---

## 7. BRING-UP ORDER (must be followed exactly)

Each numbered group in its own terminal where noted; several stay running.

1. **Per-boot plumbing** (section 3): ip_forward, iptables, loopback addresses, netns.
2. **Core**: launch the Open5GS NFs (nrf, scp, ausf, udm, udr, pcf, bsf, amf, smf, upf — **not** nssf), then add `10.45.0.1/16` to ogstun and bring it up.
3. **RIC**: `docker compose up -d` in `~/oran-sc-ric`, wait ~25 s.
4. **CU** (own terminal): wait for NG setup with the AMF.
5. **UEs** (own terminal): start ue_01/02/03 staggered ~30 s apart, logging to `/tmp/ue_0X.log`. They wait for the DU.
6. **Broker** (own terminal): start after all three UEs.
7. **DU** (own terminal): start last.
8. **Verify attach**: three distinct c-rntis in the UE logs, a `10.45.0.x` on each `tun_srsue`, and a RAN key in redis.
9. **Injector** (own terminal): wait for `Map complete`.
10. **Traffic**: iperf3 servers on the host, then per-slice flows (CRITICAL/PERFORMANCE near SLA, BUSINESS backlogged), no tc.
11. **xApp deploy**: copy xApps + logger into the container, reinstall pip deps, clear stale `/tmp` artifacts.
12. **Logger** (own terminal), then **xApp** train/eval (own terminal).

Full command-level detail is in `docs/PPO_runbook.md` and `docs/DQN_runbook.md`.

---

## 8. CONTENTS OF THIS BUNDLE

```
reproducibility/
├── NOTES.md                      ← this file
├── open5gs/                      all core NF yaml configs
├── srsran_du_cu/                 DU/CU configs (config/)
├── ue/                           ue_01/02/03.conf
├── xapps/                        PPO, DQN, logger, injector
├── traffic/                      broker, iperf scripts (traces excluded)
├── docs/                         algorithm master docs + lifecycle runbooks
└── system_state/                 captured live state:
    ├── host.txt                  OS, kernel, hostname
    ├── netns_list.txt            network namespaces
    ├── interfaces.txt            host + per-netns IP addresses
    ├── routes.txt                routing tables
    ├── iptables.txt              FORWARD/NAT rules
    ├── sysctl.txt                ip_forward state
    ├── tc_ogstun.txt             ogstun qdisc (no-tc regime)
    ├── loopback.txt              127.0.10.x F1 addresses
    ├── processes.txt             running components
    ├── ric_state.txt             docker compose ps + RAN keys
    ├── srsran_version.txt        srsRAN build commit
    ├── xapp_container_pip_freeze.txt   exact python deps
    └── mongodb_subscribers.txt   IMSI → slice mapping
```

---

## 9. REGENERATING THIS BUNDLE

From the host:
```
bash build_reproducibility_bundle.sh
cd ~/scen_c_run
git add reproducibility && git commit -m "reproducibility bundle" && git push
```

Re-run the collector whenever configs change so the captured state stays in sync with the code.
