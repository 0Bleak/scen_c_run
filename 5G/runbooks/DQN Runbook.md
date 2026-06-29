
### Core bring-up → training → save → evaluation → save

Two paths are given. **Path 1** assumes the stack is already up from a PPO run (core, RIC, CU, UEs, broker, DU, injector, traffic still running) — only the xApp is swapped. **Path 2** is the full cold start if nothing is running. Use Path 1 if the radio stack is live; otherwise run the cold start first (identical to the PPO runbook stages A–M), then start at stage N here.

---

# PATH 1 — STACK ALREADY UP (swap PPO → DQN)

## 1 — Stop PPO (Terminal 8)

Ctrl-C in the PPO terminal.

## 2 — Clear DQN state (Terminal 1)

DQN uses its own checkpoint and trainlog, separate from PPO. Clear the shared dataset and decision log so the run logs cleanly:

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/dqn_slice.pt /tmp/dqn_train_log.csv /tmp/ue_metrics_log.csv /tmp/prb_decisions.json
```

Re-copy the xApp if it was edited since the last deploy:

```bash
docker cp ~/scen_c_run/xapps/dqn_slice_xapp.py python_xapp_runner:/opt/xApps/
```

## 3 — Restart the logger (Terminal 7)

The logger holds the deleted CSV's file handle; Ctrl-C and relaunch so it writes a fresh file:

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

## 4 — Confirm traffic and injector still live (Terminal 1)

```bash
pgrep -a iperf3
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    sh -c "tail -3 /tmp/ue_metrics_log.csv"
```

Need 6 iperf processes and nonzero `dl_brate_bps`. If iperf died, relaunch the traffic block (stage J of the cold start). If the injector died (`pgrep -f cqi_injector`), relaunch it and wait for `Map complete`.

Proceed to stage N below.

---

# PATH 2 — FULL COLD START

Run stages A through M from the PPO runbook (identical: sleep prevention, core, network, RIC, CU, UEs, broker, DU, injector, traffic, deploy, logger, verify traffic). In the deploy stage, copy the DQN xApp:

```bash
docker cp ~/scen_c_run/xapps/dqn_slice_xapp.py python_xapp_runner:/opt/xApps/
docker cp ~/scen_c_run/xapps/metrics_logger.py python_xapp_runner:/opt/xApps/
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/dqn_slice.pt /tmp/dqn_train_log.csv /tmp/ue_metrics_log.csv /tmp/prb_decisions.json
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    pip install numpy torch websocket-client --break-system-packages
```

Then continue to stage N.

---

## N — Train DQN (Terminal 8)

DQN defaults to http 8096 / rmr 4566 (distinct from PPO's 8095/4565), so ports do not collide if a PPO socket lingers.

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/dqn_slice_xapp.py --interval 1 --train
```

Confirm startup prints `[DQN-CTRL] TRAIN` and `[WS] subscribed 8002`.

The printed line shows epsilon and Q-stats, distinct from PPO:

```
HH:MM:SS sat=[1.00,1.00,0.55] cqi=[15,15,15] act=1[60, 25, 15] r=0.955 eps=0.873
  [STEP 0130] eps=0.870 loss=0.41 mean_q=2.3
```

**Check N:** confirm the decision log populates:

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner cat /tmp/prb_decisions.json
```

Three rnti keys with `prb_min` = logging correct.

---

## O — Let it train

Approximately 45 minutes. Epsilon starts at 1.0 (fully random) and decays over ~500 steps; the first ~8 minutes are heavy exploration and the policy is deliberately random during that window. Watch `mean_q` rise and `loss` fall. The checkpoint auto-saves to `/tmp/dqn_slice.pt` every update.

**When to stop training:** stop once `mean_q` has risen and plateaued and `loss` has fallen and stabilized. Do not assess or stop before epsilon has decayed below ~0.2 (around step 350), since behaviour before that is exploration noise, not the learned policy. A typical run is 30–45 min.

---

## P — Stop + save training (Terminal 8: Ctrl-C, then Terminal 1)

Ctrl-C in Terminal 8 to stop cleanly.

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/dqn_slice.pt        ~/scen_c_run/datasets/dqn_train_checkpoint_${TS}.pt
docker cp python_xapp_runner:/tmp/dqn_train_log.csv   ~/scen_c_run/datasets/dqn_train_log_${TS}.csv
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/dqn_train_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/dqn_train_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/dqn_train_*${TS}*
```

**Check P:** four files with nonzero sizes.

---

## Q — Evaluate DQN (Terminal 8)

Clear the dataset (the checkpoint is preserved):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/ue_metrics_log.csv /tmp/prb_decisions.json
```

Restart the logger so it writes a fresh CSV (Terminal 7: Ctrl-C, then relaunch):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

Run eval (frozen greedy policy, epsilon effectively zero):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/dqn_slice_xapp.py --eval --ckpt /tmp/dqn_slice.pt --interval 1
```

Confirm startup prints `[DQN] loaded checkpoint` and `[DQN-CTRL] EVAL`. If the checkpoint did not load, the eval is invalid — stop and recheck the path.

**When to stop evaluation:** run a minimum of 5 minutes (~300 steps); 10 minutes is preferable for a thesis-grade number, to span the CQI trace range. The policy is frozen, so duration is purely about statistical coverage, not convergence.

---

## R — Save evaluation (Terminal 8: Ctrl-C, then Terminal 1)

Ctrl-C to stop, then:

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/dqn_eval_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/dqn_eval_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/dqn_eval_*${TS}*
```

---

## S — Compute satisfaction from the eval dataset (Terminal 1)

Recompute from raw `dl_brate` (field 14) against the true SLAs, independent of the logger's SLA column:

```bash
f=$(ls -t ~/scen_c_run/datasets/dqn_eval_dataset_*.csv | head -1)
for sl in CRITICAL:9000000 PERFORMANCE:8000000 BUSINESS:25000000; do
  name=${sl%:*}; sla=${sl#*:}
  echo -n "$name: "
  awk -F, -v S=$sla -v N=$name 'NR>1 && $6==N{s=$14/S; if(s>1)s=1; sum+=s; n++} END{printf "%.3f (n=%d)\n", sum/n, n}' "$f"
done
```

Expect CRITICAL ≥ PERFORMANCE ≫ BUSINESS. Adjust the SLA constants in the loop to the values used during the run.

---

## T — Behavioural check (Terminal 1)

Confirm the policy adapts to channel rather than picking a constant action — the key evidence of a genuine policy:

```bash
f=$(ls -t ~/scen_c_run/datasets/dqn_eval_dataset_*.csv | head -1)
awk -F, 'NR>1 && $6=="CRITICAL"{print "cqi="$7, "prb_min="$20}' "$f" | sort | uniq -c
```

A channel-aware policy allocates more PRBs to CRITICAL at low CQI and fewer at high CQI. If `prb_min` is identical across all CQI values, the policy is state-independent.

---

## Quick reference — start/stop summary

|Phase|Start|Stop condition|
|---|---|---|
|Training|stage N|`mean_q` risen and plateaued, `loss` fallen and stable; never before epsilon < ~0.2 (~30–45 min)|
|Evaluation|stage Q|≥5 min for sanity, ≥10 min for thesis-grade (statistical coverage only)|

The whole difference from the PPO lifecycle: stop PPO → clear DQN files → restart logger → run `dqn_slice_xapp.py` (ports auto-distinct) → save with `dqn_` filenames. The radio stack, traffic, and injector remain up throughout.