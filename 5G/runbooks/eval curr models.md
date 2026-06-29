## PPO eval

```bash
# 1. put the trained checkpoint where the xApp loads it
docker cp ~/scen_c_run/datasets/ppo_5act_checkpoint_20260628_1305.pt \
    python_xapp_runner:/tmp/ppo_slice.pt

# 2. clear old logged data
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/ue_metrics_log.csv /tmp/prb_decisions.json

# 3. restart logger so it writes a fresh CSV (Terminal: logger — Ctrl-C then relaunch)
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

```bash
# 4. run PPO eval (own terminal) — confirm it prints "[PPO] loaded checkpoint"
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/ppo_slice_xapp.py --eval --ckpt /tmp/ppo_slice.pt --interval 1
```

Let it run **at least 5 min** (10 better). Ctrl-C, then save:

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/ppo_eval_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/ppo_eval_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/ppo_eval_*${TS}*
```

## DQN eval

```bash
# 1. load DQN checkpoint
docker cp ~/scen_c_run/datasets/dqn_5act_checkpoint_20260628_1318.pt \
    python_xapp_runner:/tmp/dqn_slice.pt

# 2. clear old data
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/ue_metrics_log.csv /tmp/prb_decisions.json

# 3. restart logger (Ctrl-C then relaunch)
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

```bash
# 4. run DQN eval — confirm "[DQN] loaded checkpoint"
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/dqn_slice_xapp.py --eval --ckpt /tmp/dqn_slice.pt --interval 1
```

≥5 min, Ctrl-C, save:

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/dqn_eval_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/dqn_eval_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/dqn_eval_*${TS}*
```

Two things to verify each run, or the eval is wasted:

- **`[X] loaded checkpoint` must print at startup.** If it doesn't, it's running a random net — stop and check the `docker cp` landed.
- **`dl_brate` nonzero** before you trust it: `docker compose ... exec python_xapp_runner sh -c "tail -3 /tmp/ue_metrics_log.csv"` — confirms traffic is flowing into the eval.

Then the table from the new eval datasets:

```bash
for m in ppo dqn; do
  f=$(ls -t ~/scen_c_run/datasets/${m}_eval_dataset_*.csv | head -1)
  echo "=== $m ==="
  for sl in CRITICAL:9000000 PERFORMANCE:8000000 BUSINESS:25000000; do
    name=${sl%:*}; sla=${sl#*:}
    echo -n "  $name: "
    awk -F, -v S=$sla -v N=$name 'NR>1 && $6==N{s=$14/S; if(s>1)s=1; sum+=s; n++} END{printf "%.3f (n=%d)\n", sum/n, n}' "$f"
  done
done
```

Adjust the `9000000/8000000/25000000` if you ran with different SLAs. That's your eval table from clean ≥5-min runs.