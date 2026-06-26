#!/bin/bash
# STRESS: DL offers exceed what PRB allocation can deliver per slice.
# With 52 PRBs total at MCS28:
#   CRITICAL gets min 10 PRBs -> ~5.4M max DL -> offer 8M -> never satisfied
#   PERFORMANCE gets min 10 PRBs -> ~5.4M max DL -> offer 8M -> never satisfied  
#   BUSINESS gets min 10 PRBs -> ~5.4M max DL -> offer 8M -> never satisfied
# Sum of offers = 24M but each slice individually can't hit 8M with shared PRBs
# -> genuine per-slice contention, impossible to satisfy all simultaneously
S=10.45.0.1
sleep 5
sudo ip netns exec vue1 iperf3 -c $S -p 5201 -u -b 500k -t 0 -l 800 &
sudo ip netns exec vue2 iperf3 -c $S -p 5202 -u -b 500k -t 0 -l 800 &
sudo ip netns exec vue3 iperf3 -c $S -p 5203 -u -b 500k -t 0 -l 800 &
sudo ip netns exec vue1 iperf3 -c $S -p 5211 -u -b 8M -t 0 -l 800 -R &
sudo ip netns exec vue2 iperf3 -c $S -p 5212 -u -b 8M -t 0 -l 800 -R &
sudo ip netns exec vue3 iperf3 -c $S -p 5213 -u -b 8M -t 0 -l 800 -R &
echo "[IPERF STRESS] DL=3x8M UL=3x500k launched"
