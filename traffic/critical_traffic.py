#!/usr/bin/env python3
# CRITICAL slice traffic. ato carries video_surv profile (300/3000 + 9000 UL burst)
# to stress the slice; name 'ato' and port 6003 kept everywhere by design.
import socket, struct, threading, time, random, argparse
from collections import deque

class App:
    def __init__(self, name, port, ip):
        self.name=name; self.s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setblocking(False); self.dest=(ip, port); self.running=True
        self.ul=0; self.dl=0; self.pu=0; self.pd=0; self.ulb=0.0; self.dlb=0.0
        self.cur_ul=0.0; self.cur_dl=0.0
        self.lat=deque(maxlen=100); self.lastping=0
    def tick(self, ul_bps, dl_bps, dt):
        now=time.perf_counter()
        self.ulb+=(ul_bps/8.0)*dt
        if self.ulb>=1.0:
            sz=min(int(self.ulb),60000)
            try: self.s.sendto(bytes(sz),self.dest); self.ul+=sz; self.pu+=1
            except OSError: pass
            self.ulb-=int(self.ulb)
        self.dlb+=(dl_bps/8.0)*dt
        if self.dlb>=1.0:
            sz=min(int(self.dlb),60000)
            try: self.s.sendto(struct.pack("!I",sz)+b"DL_REQ",self.dest)
            except OSError: pass
            self.dlb-=int(self.dlb)
        if now-self.lastping>=1.0:
            try: self.s.sendto(struct.pack("!d",now)+b"PING",self.dest); self.lastping=now
            except OSError: pass
        while True:
            try:
                d,_=self.s.recvfrom(65535)
                if len(d)>=12 and d[8:12]==b"PONG":
                    self.lat.append((now-struct.unpack("!d",d[:8])[0])*1000)
                else: self.dl+=len(d); self.pd+=1
            except OSError: break
    def avg(self): return sum(self.lat)/len(self.lat) if self.lat else 0.0

def run(app, ul, dl, ms, dur):
    app.cur_ul=ul; app.cur_dl=dl
    iv=ms/1000.0; nt=time.perf_counter()+iv
    end=time.perf_counter()+dur if dur!=float('inf') else float('inf')
    while app.running and time.perf_counter()<end:
        app.tick(ul,dl,iv); now=time.perf_counter()
        s=nt-now
        if s>0.0001: time.sleep(s)
        nt+=iv
        if nt<now: nt=now+iv
    app.cur_ul=0.0; app.cur_dl=0.0

def voice(app):
    while app.running:
        run(app,23850,23850,20,30)
        if not app.running: break
        time.sleep(random.uniform(30,90))
def etcs(app):
    run(app,1250,5000,20,float('inf'))
def ato(app):
    # STRESS: video_surv profile, still named 'ato', port 6003.
    while app.running:
        run(app,3000000,300000,25,120)        # normal: 3Mbps UL / 300kbps DL
        if not app.running: break
        run(app,9000000,10000,25,25)           # emergency UL burst
def remote_engine_ctrl(app):
    while app.running:
        run(app,100000,25000,25,1)
        if not app.running: break
        time.sleep(59)
def pub_warn(app):
    while app.running:
        run(app,2000,2000,20,10)
        if not app.running: break
        time.sleep(random.uniform(60,180))

def latlog(apps,out,iv=1):
    import csv
    csv.writer(open(out,"w",newline="")).writerow(["timestamp","app","rtt_avg_ms","samples"])
    while any(a.running for a in apps):
        time.sleep(iv); ts=round(time.time(),3)
        w=csv.writer(open(out,"a",newline=""))
        for a in apps:
            if a.lat: w.writerow([ts,a.name,round(a.avg(),2),len(a.lat)])
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--server",default="10.45.0.1")
    p.add_argument("--duration",type=int,default=3600)
    p.add_argument("--ue",default="critical")
    p.add_argument("--latency_log",default="/tmp/latency_critical.csv")
    a=p.parse_args()
    defs=[("voice",voice,6001),("etcs",etcs,6002),("ato",ato,6003),
          ("remote_engine_ctrl",remote_engine_ctrl,6004),("pub_warn",pub_warn,6005)]
    apps=[]
    for name,fn,port in defs:
        app=App(name,port,a.server); apps.append(app)
        threading.Thread(target=fn,args=(app,),daemon=True).start()
    threading.Thread(target=latlog,args=(apps,a.latency_log),daemon=True).start()
    print(f"[CRITICAL/{a.ue}] -> {a.server} ({a.duration}s) ato=video_surv stress profile")
    try: time.sleep(a.duration)
    except KeyboardInterrupt: pass
    for x in apps: x.running=False
    time.sleep(2)

if __name__=="__main__": main()
