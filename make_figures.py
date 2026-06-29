#!/usr/bin/env python3
import os, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

D = os.path.expanduser("~/scen_c_run/datasets")
OUT = os.path.expanduser("~/scen_c_run/figures"); os.makedirs(OUT, exist_ok=True)

PPO_EVAL  = f"{D}/ppo_eval_dataset_20260629_1105.csv"
DQN_EVAL  = f"{D}/dqn_eval_dataset_20260629_1114.csv"
PPO_TRAIN = f"{D}/ppo_5act_trainlog_20260628_1305.csv"
DQN_TRAIN = f"{D}/dqn_5act_trainlog_20260628_1318.csv"

SLA = {"CRITICAL": 9_000_000, "PERFORMANCE": 8_000_000, "BUSINESS": 25_000_000}
SLICES = ["CRITICAL", "PERFORMANCE", "BUSINESS"]

C_PPO, C_DQN = "#2563eb", "#dc2626"
C_SLICE = {"CRITICAL": "#dc2626", "PERFORMANCE": "#ea9010", "BUSINESS": "#059669"}

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white", "savefig.facecolor": "white",
    "font.size": 11, "axes.titlesize": 13, "axes.titleweight": "bold",
    "figure.dpi": 150, "axes.grid": True, "grid.alpha": 0.25,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#444",
})

def load(path):
    df = pd.read_csv(path)
    df["dl_brate_bps"] = pd.to_numeric(df["dl_brate_bps"], errors="coerce")
    df["prb_min"]      = pd.to_numeric(df["prb_min"], errors="coerce")
    df["cqi"]          = pd.to_numeric(df["cqi"], errors="coerce")
    df = df[df["dl_brate_bps"] > 0].copy()
    df["sat"] = df.apply(lambda r: min(r["dl_brate_bps"]/SLA.get(r["slice_name"],1), 1.0), axis=1)
    return df

print("loading datasets")
ppo = load(PPO_EVAL); dqn = load(DQN_EVAL)
ppo_m = {s: ppo[ppo["slice_name"]==s]["sat"].mean() for s in SLICES}
dqn_m = {s: dqn[dqn["slice_name"]==s]["sat"].mean() for s in SLICES}

# FIGURE 1: main dashboard
fig = plt.figure(figsize=(18, 10))
fig.suptitle("Slice Aware RL Resource Allocation: Evaluation Results", fontsize=20, fontweight="bold", y=0.98)

ax = fig.add_subplot(2,3,1)
x = np.arange(len(SLICES)); w = 0.36
b1 = ax.bar(x-w/2, [ppo_m[s] for s in SLICES], w, label="PPO", color=C_PPO)
b2 = ax.bar(x+w/2, [dqn_m[s] for s in SLICES], w, label="DQN", color=C_DQN)
ax.axhline(1.0, ls="--", color="#888", lw=1, label="SLA met")
for b in list(b1)+list(b2):
    ax.text(b.get_x()+b.get_width()/2, b.get_height()+0.02, f"{b.get_height():.2f}",
            ha="center", va="bottom", fontsize=9, fontweight="bold")
ax.set_xticks(x); ax.set_xticklabels(SLICES, fontsize=9); ax.set_ylim(0,1.15)
ax.set_ylabel("Mean SLA satisfaction"); ax.set_title("Mean Satisfaction per Slice")
ax.legend(fontsize=9)

ax = fig.add_subplot(2,3,2)
data, labels, colors = [], [], []
for s in SLICES:
    data.append(ppo[ppo["slice_name"]==s]["sat"].values); labels.append(f"{s[:4]} PPO"); colors.append(C_PPO)
    data.append(dqn[dqn["slice_name"]==s]["sat"].values); labels.append(f"{s[:4]} DQN"); colors.append(C_DQN)
parts = ax.violinplot(data, showmeans=True, showextrema=False)
for i,b in enumerate(parts["bodies"]):
    b.set_facecolor(colors[i]); b.set_alpha(0.6)
ax.set_xticks(range(1,len(labels)+1)); ax.set_xticklabels(labels, fontsize=7, rotation=30)
ax.set_ylabel("SLA satisfaction"); ax.set_title("Satisfaction Spread"); ax.set_ylim(0,1.1)

ax = fig.add_subplot(2,3,3)
def actcount(df):
    v = df[df["slice_name"]=="CRITICAL"]["prb_min"].dropna().astype(int)
    return v.value_counts().sort_index()
pa, da = actcount(ppo), actcount(dqn)
allk = sorted(set(pa.index)|set(da.index)); xp = np.arange(len(allk))
ax.bar(xp-w/2, [pa.get(k,0) for k in allk], w, label="PPO", color=C_PPO)
ax.bar(xp+w/2, [da.get(k,0) for k in allk], w, label="DQN", color=C_DQN)
ax.set_xticks(xp); ax.set_xticklabels([f"{k}%" for k in allk], fontsize=9)
ax.set_xlabel("PRB given to CRITICAL"); ax.set_ylabel("times chosen")
ax.set_title("Which Allocation the Agent Picked"); ax.legend(fontsize=9)

ax = fig.add_subplot(2,3,4)
try:
    t = pd.read_csv(PPO_TRAIN)
    ax.plot(t["update"], t["mean_reward"], color=C_PPO, lw=2)
    ax.set_xlabel("training update"); ax.set_ylabel("mean reward", color=C_PPO)
    ax2 = ax.twinx(); ax2.plot(t["update"], t["entropy"], color="#888", lw=2)
    ax2.set_ylabel("entropy (exploration)", color="#888"); ax2.grid(False)
    ax.set_title("PPO Training Progress")
except Exception as e:
    ax.text(0.5,0.5,str(e),ha="center"); ax.set_title("PPO Training")

ax = fig.add_subplot(2,3,5)
try:
    t = pd.read_csv(DQN_TRAIN)
    ax.plot(t["step"], t["mean_q"], color=C_DQN, lw=2)
    ax.set_xlabel("training step"); ax.set_ylabel("mean Q value", color=C_DQN)
    ax2 = ax.twinx(); ax2.plot(t["step"], t["loss"], color="#888", lw=1.2, alpha=0.7)
    ax2.set_ylabel("loss", color="#888"); ax2.grid(False)
    ax.set_title("DQN Training Progress")
except Exception as e:
    ax.text(0.5,0.5,str(e),ha="center"); ax.set_title("DQN Training")

ax = fig.add_subplot(2,3,6)
for df,name,col in [(ppo,"PPO",C_PPO),(dqn,"DQN",C_DQN)]:
    c = df[df["slice_name"]=="CRITICAL"].dropna(subset=["prb_min","cqi"])
    g = c.groupby("cqi")["prb_min"].mean()
    ax.plot(g.index, g.values, "o-", color=col, lw=2, ms=7, label=name)
ax.set_xlabel("CQI (channel quality, low is worse)"); ax.set_ylabel("avg PRB given to CRITICAL")
ax.set_title("Does the Agent Adapt to the Channel?"); ax.legend(fontsize=9)

plt.tight_layout(rect=[0,0,1,0.96])
plt.savefig(f"{OUT}/fig1_dashboard.png", bbox_inches="tight")
print("saved fig1_dashboard.png")

# FIGURE 2: satisfaction over time
fig, axes = plt.subplots(3,1, figsize=(16,11))
fig.suptitle("SLA Satisfaction Over the Evaluation Run", fontsize=18, fontweight="bold", y=0.995)
for ax,s in zip(axes,SLICES):
    for df,name,col in [(ppo,"PPO",C_PPO),(dqn,"DQN",C_DQN)]:
        d = df[df["slice_name"]==s].reset_index(drop=True)
        ax.plot(d.index, d["sat"], color=col, lw=1.1, alpha=0.85, label=name)
    ax.axhline(1.0, ls="--", color="#888", lw=1)
    ax.set_title(f"{s}  (SLA target {SLA[s]/1e6:.0f} Mbps)", color=C_SLICE[s])
    ax.set_ylabel("satisfaction"); ax.set_ylim(0,1.15); ax.legend(fontsize=9)
axes[-1].set_xlabel("decision step")
plt.tight_layout(rect=[0,0,1,0.97])
plt.savefig(f"{OUT}/fig2_satisfaction_over_time.png", bbox_inches="tight")
print("saved fig2_satisfaction_over_time.png")

# FIGURE 3: channel adaptation detail
fig, axes = plt.subplots(1,2, figsize=(16,6))
fig.suptitle("PRB Given to CRITICAL at Each Channel Quality Level", fontsize=17, fontweight="bold", y=1.0)
for ax,(df,name,col) in zip(axes,[(ppo,"PPO",C_PPO),(dqn,"DQN",C_DQN)]):
    c = df[df["slice_name"]=="CRITICAL"].dropna(subset=["prb_min","cqi"])
    cqis = sorted(c["cqi"].unique())
    box = [c[c["cqi"]==q]["prb_min"].values for q in cqis]
    bp = ax.boxplot(box, positions=cqis, widths=0.6, patch_artist=True)
    for b in bp["boxes"]: b.set_facecolor(col); b.set_alpha(0.5)
    for m in bp["medians"]: m.set_color("#222")
    g = c.groupby("cqi")["prb_min"].mean()
    ax.plot(g.index, g.values, "o-", color="#ea9010", lw=2, ms=6, label="average")
    ax.set_title(name, color=col); ax.set_xlabel("CQI"); ax.set_ylabel("PRB given to CRITICAL")
    ax.legend(fontsize=9)
plt.tight_layout()
plt.savefig(f"{OUT}/fig3_channel_adaptation.png", bbox_inches="tight")
print("saved fig3_channel_adaptation.png")

# FIGURE 4: throughput vs SLA
fig, axes = plt.subplots(1,3, figsize=(17,5.5))
fig.suptitle("Delivered Throughput Compared to SLA Target", fontsize=17, fontweight="bold", y=1.02)
for ax,s in zip(axes,SLICES):
    for df,name,col in [(ppo,"PPO",C_PPO),(dqn,"DQN",C_DQN)]:
        v = df[df["slice_name"]==s]["dl_brate_bps"].values/1e6
        ax.hist(v, bins=30, alpha=0.5, color=col, label=name)
    ax.axvline(SLA[s]/1e6, ls="--", color="#ea9010", lw=2, label="SLA target")
    ax.set_title(s, color=C_SLICE[s]); ax.set_xlabel("delivered downlink (Mbps)"); ax.set_ylabel("count")
    ax.legend(fontsize=8)
plt.tight_layout()
plt.savefig(f"{OUT}/fig4_throughput_vs_sla.png", bbox_inches="tight")
print("saved fig4_throughput_vs_sla.png")

# FIGURE 5: summary table
fig, ax = plt.subplots(figsize=(9,3.2)); ax.axis("off")
rows = [["Slice","SLA (Mbps)","PPO satisfaction","DQN satisfaction"]]
for s in SLICES:
    rows.append([s, f"{SLA[s]/1e6:.0f}", f"{ppo_m[s]:.3f}", f"{dqn_m[s]:.3f}"])
tbl = ax.table(cellText=rows, loc="center", cellLoc="center")
tbl.auto_set_font_size(False); tbl.set_fontsize(12); tbl.scale(1,2)
for (r,cc),cell in tbl.get_celld().items():
    cell.set_edgecolor("#ccc")
    if r==0: cell.set_facecolor("#2563eb"); cell.set_text_props(weight="bold", color="white")
    else: cell.set_facecolor("white" if r%2 else "#f3f4f6")
ax.set_title("Evaluation Summary", fontweight="bold", pad=20)
plt.savefig(f"{OUT}/fig5_summary_table.png", bbox_inches="tight")
print("saved fig5_summary_table.png")

print("\nall figures saved to", OUT)
