
### Core bring-up → training → save → evaluation → save

Original broker, 10 MHz / 11.52e6 across all configs. Each stage has a checkpoint; confirm it before proceeding. Several stages each require their own terminal and stay running — terminal assignments are noted per stage.

---

## A — Sleep prevention + config sanity (Terminal 1)

```bash
gsettings set org.gnome.desktop.session idle-delay 0
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-timeout 0
gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-timeout 0
systemd-inhibit --what=idle:sleep --why="PPO run" sleep infinity & echo "inhibitor PID $!"
```

```bash
grep -nE 'srate|channel_bandwidth' ~/scen_c_run/config/du_zmq_01.yml ~/scen_c_run/ue/ue_0*.conf
```

**Check A:** all srate = 11.52 / 11.52e6, channel_bandwidth_MHz: 10. If any 5.76/7.68/5 remains, fix before continuing.

---

## B — Core (Terminal 1)

```bash
cd ~/open5gs/install/bin
for nf in nrf scp ausf udm udr pcf bsf amf smf upf; do
  ./open5gs-${nf}d -c ~/open5gs/install/etc/open5gs/${nf}.yaml -D
done
sleep 3
sudo ip addr add 10.45.0.1/16 dev ogstun 2>/dev/null; true
sudo ip link set ogstun up
```

**Check B:** `pgrep -a open5gs | wc -l` → 10; `ip -4 addr show ogstun | grep 10.45.0.1` shows the IP.

---

## C — Network plumbing (Terminal 1)

```bash
sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -I FORWARD -i ogstun -o wlp2s0 -j ACCEPT
sudo iptables -I FORWARD -i wlp2s0 -o ogstun -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo ip addr add 127.0.10.1/32 dev lo 2>/dev/null; true
sudo ip addr add 127.0.10.2/32 dev lo 2>/dev/null; true
for n in vue1 vue2 vue3; do sudo ip netns del $n 2>/dev/null; done
sudo ip netns add vue1 && sudo ip netns add vue2 && sudo ip netns add vue3
```

**Check C:** `ip netns list` → vue1 vue2 vue3; `sysctl net.ipv4.ip_forward` → 1.

---

## D — RIC (Terminal 1)

```bash
cd ~/oran-sc-ric && docker compose up -d && sleep 25
```

**Check D:** `docker exec ric_dbaas redis-cli PING` → PONG.

---

## E — CU (Terminal 2, foreground, leave running)

```bash
cd ~/scen_c_run && sudo ~/srsRAN_Project/build/apps/cu/srscu -c config/cu.yml
```

**Check E:** `grep -a -iE "NG Setup|connected to AMF" /var/log/open5gs/amf.log | tail` shows NG established. Wait for this before F.

---

## F — UEs (Terminal 3, staggered, leave running)

```bash
sudo rm -f /tmp/ue_01.log /tmp/ue_02.log /tmp/ue_03.log
sudo -v; cd ~/scen_c_run
sudo srsue ue/ue_01.conf 2>&1 | sudo tee /tmp/ue_01.log &
sleep 30
sudo srsue ue/ue_02.conf 2>&1 | sudo tee /tmp/ue_02.log &
sleep 30
sudo srsue ue/ue_03.conf 2>&1 | sudo tee /tmp/ue_03.log &
sleep 30
```

---

## G — Broker (Terminal 4, leave running)

```bash
cd ~/scen_c_run && python3 traffic/zmq_broker_01.py
```

Must print `[BROKER DU1] start`, no traceback.

---

## H — DU (Terminal 5, last, leave running)

```bash
cd ~/scen_c_run && sudo ~/srsRAN_Project/build/apps/du/srsdu -c config/du_zmq_01.yml
```

**Check H (gate) — Terminal 1:**

```bash
grep -a "c-rnti" /tmp/ue_01.log /tmp/ue_02.log /tmp/ue_03.log
for n in 1 2 3; do echo -n "vue$n: "; sudo ip netns exec vue$n ip -4 addr show tun_srsue | grep -o '10.45.0.[0-9]*'; done
docker exec ric_dbaas redis-cli KEYS '*RAN*'
```

Need: 3 distinct c-rntis, 3 IPs, RAN key. Record which IP is vue1/2/3 (vue1=CRITICAL, vue2=PERFORMANCE, vue3=BUSINESS).

---

## I — Injector (Terminal 6, leave running)

```bash
cd ~/scen_c_run && python3 traffic/cqi_injector.py --dataset_dir ~/scen_c_run/traces/sncf_traces &
```

**Check I:** wait for `[INJ] Map complete: {...}`.

---

## J — Traffic, no tc (Terminal 1)

```bash
sudo tc qdisc del dev ogstun root 2>/dev/null; true
for p in 5211 5212 5213; do iperf3 -s -p $p -D; done
S=10.45.0.1
sudo ip netns exec vue1 iperf3 -c $S -p 5211 -u -b 5M  -t 0 -l 800 -R &
sudo ip netns exec vue2 iperf3 -c $S -p 5212 -u -b 5M  -t 0 -l 800 -R &
sudo ip netns exec vue3 iperf3 -c $S -p 5213 -u -b 30M -t 0 -l 800 -R &
```

CRITICAL and PERFORMANCE near SLA, BUSINESS backlogged.

**Check J:** `pgrep -a iperf3` → 6 processes.

---

## K — Deploy xApps + logger (Terminal 1)

```bash
docker cp ~/scen_c_run/xapps/ppo_slice_xapp.py python_xapp_runner:/opt/xApps/
docker cp ~/scen_c_run/xapps/metrics_logger.py python_xapp_runner:/opt/xApps/
```

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/ppo_slice.pt /tmp/ppo_train_log.csv /tmp/ue_metrics_log.csv /tmp/prb_decisions.json
```

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    pip install numpy torch websocket-client --break-system-packages
```

Clear the checkpoint only if the action space changed since the last run; otherwise omit `ppo_slice.pt` to resume.

---

## L — Logger (Terminal 7, leave running)

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

Prints `[LOG] subscribed to 8002` then goes silent — correct.

---

## M — Verify traffic before training (Terminal 1, wait ~20 s after L)

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    sh -c "tail -3 /tmp/ue_metrics_log.csv"
```

**Check M:** `dl_brate_bps` (field 14) nonzero on all three rows. If zero, traffic is not flowing — fix before training.

---

## N — Train PPO (Terminal 8)

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/ppo_slice_xapp.py --interval 1 --train
```

Confirm startup prints `[PPO-CTRL] TRAIN` and `[WS] subscribed 8002`.

**Check N (2 min):** the printed `sat=[...]` line should show satisfaction varying across actions, and `r=` taking different values per action. Confirm the decision log populates:

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner cat /tmp/prb_decisions.json
```

Three rnti keys with `prb_min` values = logging correct.

---

## O — Let it train

Approximately 45 minutes. In Terminal 8, watch `mean_reward` trend up, `value_loss` fall, `entropy` fall from ~1.79. The checkpoint auto-saves to `/tmp/ppo_slice.pt` every update.

**When to stop training:** stop once `mean_reward` has plateaued (no upward trend over the last several updates) and `entropy` has clearly declined and stabilized. If both are still moving, let it continue. A typical run is 30–45 min; do not stop before entropy has begun to fall (that indicates the policy is still fully random).

---

## P — Stop + save training (Terminal 8: Ctrl-C, then Terminal 1)

Ctrl-C in Terminal 8 to stop cleanly.

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/ppo_slice.pt        ~/scen_c_run/datasets/ppo_train_checkpoint_${TS}.pt
docker cp python_xapp_runner:/tmp/ppo_train_log.csv   ~/scen_c_run/datasets/ppo_train_log_${TS}.csv
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/ppo_train_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/ppo_train_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/ppo_train_*${TS}*
```

**Check P:** four files with nonzero sizes.

---

## Q — Evaluate PPO (Terminal 8)

Clear the dataset so eval logs cleanly (the checkpoint is preserved):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    rm -f /tmp/ue_metrics_log.csv /tmp/prb_decisions.json
```

Restart the logger so it writes a fresh CSV (Terminal 7: Ctrl-C, then relaunch):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/metrics_logger.py
```

Run eval (frozen greedy policy):

```bash
docker compose -f ~/oran-sc-ric/docker-compose.yml exec python_xapp_runner \
    python3 /opt/xApps/ppo_slice_xapp.py --eval --ckpt /tmp/ppo_slice.pt --interval 1
```

Confirm startup prints `[PPO] loaded checkpoint` and `[PPO-CTRL] EVAL`. If it did not load the checkpoint, the eval is invalid — stop and recheck the path.

**When to stop evaluation:** eval needs enough steps to span the CQI trace range. Run a minimum of 5 minutes (~300 steps); 10 minutes is preferable for a thesis-grade number. Stop earlier only for a quick sanity check, noting the sample size is thin. There is no convergence to wait for — the policy is frozen — so duration is purely about statistical coverage.

---

## R — Save evaluation (Terminal 8: Ctrl-C, then Terminal 1)

Ctrl-C to stop, then:

```bash
TS=$(date +%Y%m%d_%H%M)
docker cp python_xapp_runner:/tmp/ue_metrics_log.csv  ~/scen_c_run/datasets/ppo_eval_dataset_${TS}.csv
docker cp python_xapp_runner:/tmp/prb_decisions.json  ~/scen_c_run/datasets/ppo_eval_prb_${TS}.json
ls -lh ~/scen_c_run/datasets/ppo_eval_*${TS}*
```

---

## S — Compute satisfaction from the eval dataset (Terminal 1)

The logger SLA must match the training SLAs; if it does not, recompute from raw `dl_brate` (field 14) against the true SLAs:

```bash
f=$(ls -t ~/scen_c_run/datasets/ppo_eval_dataset_*.csv | head -1)
for sl in CRITICAL:9000000 PERFORMANCE:8000000 BUSINESS:25000000; do
  name=${sl%:*}; sla=${sl#*:}
  echo -n "$name: "
  awk -F, -v S=$sla -v N=$name 'NR>1 && $6==N{s=$14/S; if(s>1)s=1; sum+=s; n++} END{printf "%.3f (n=%d)\n", sum/n, n}' "$f"
done
```

Expect CRITICAL ≥ PERFORMANCE ≫ BUSINESS — the priority ordering. Adjust the SLA constants in the loop to match the values used during the run.

---

## Quick reference — start/stop summary

|Phase|Start|Stop condition|
|---|---|---|
|Training|stage N|`mean_reward` plateaued, `entropy` fallen and stable (~30–45 min)|
|Evaluation|stage Q|≥5 min for sanity, ≥10 min for thesis-grade (statistical coverage only)|

Checkpoint auto-saves during training; the explicit copy in stage P/R is to preserve the run before the next one overwrites `/tmp`.