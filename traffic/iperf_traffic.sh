for p in 5211 5212 5213; do iperf3 -s -p $p -D; done
#!/bin/bash
S=10.45.0.1
# reduced packet size -l 800 prevents RLC AM segmentation bug in srsUE
sudo ip netns exec vue1 iperf3 -c $S -p 5211 -u -b 100k  -t 0 -l 800 -R &
sudo ip netns exec vue2 iperf3 -c $S -p 5212 -u -b 100k  -t 0 -l 800 -R &
sudo ip netns exec vue3 iperf3 -c $S -p 5213 -u -b 20M   -t 0 -l 800 -R &
echo "[IPERF] 3 UEs UL+DL l=800 launched"
