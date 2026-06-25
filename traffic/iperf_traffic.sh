#!/bin/bash
S=10.45.0.1
# vue1 CRITICAL : UL 3.13M (5201) / DL 356k (5211)
sudo ip netns exec vue1 iperf3 -c $S -p 5201 -u -b 3.13M -t 0 -l 1200 &
sudo ip netns exec vue1 iperf3 -c $S -p 5211 -u -b 356k  -t 0 -l 1200 -R &
# vue2 PERFORMANCE : UL 3.02M (5202) / DL 311k (5212)
sudo ip netns exec vue2 iperf3 -c $S -p 5202 -u -b 3.02M -t 0 -l 1200 &
sudo ip netns exec vue2 iperf3 -c $S -p 5212 -u -b 311k  -t 0 -l 1200 -R &
# vue3 BUSINESS : UL 2M (5203) / DL 20M (5213)
sudo ip netns exec vue3 iperf3 -c $S -p 5203 -u -b 2M    -t 0 -l 1200 &
sudo ip netns exec vue3 iperf3 -c $S -p 5213 -u -b 20M   -t 0 -l 1200 -R &
echo "[IPERF] 3 UEs UL+DL split-port launched"
