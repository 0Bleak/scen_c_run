# Deep Q-Networks for Slice-Aware Radio Resource Allocation

### A self-contained exposition, from reinforcement learning fundamentals to the deployed xApp

This document presents the Deep Q-Network (DQN) algorithm from first principles and connects each concept to the slice-aware downlink PRB allocator implemented as a near-real-time xApp. It presumes familiarity with Python but no prior knowledge of reinforcement learning. It is intended to be read once in full and thereafter used as a reference.

Parts 1 and 2 establish the same problem setting and Markov Decision Process used by the companion PPO controller; the problem is held identical so the two algorithms may be compared fairly. A reader already familiar with that formulation may proceed to Part 3, where DQN diverges substantially from PPO.

---

## PART 1 — THE PROBLEM SETTING

### 1.1 The class of problem reinforcement learning addresses

Reinforcement learning (RL) concerns systems that must make a decision repeatedly, where each decision influences subsequent conditions, and where feedback on the quality of a decision arrives only _after_ the decision is taken. No supervisor provides the correct answer for each situation; the only signal available is a scalar score.

The resource-allocation task is of exactly this form:

- At each decision interval (one second), the controller partitions PRBs among three slices: CRITICAL, PERFORMANCE, and BUSINESS.
- After the allocation is applied, the outcome is observed as each slice's throughput relative to its SLA.
- No optimal split is provided; the only feedback is a **reward**.

RL learns a _decision policy_ from this trial-and-error feedback. DQN is the algorithm that initiated the deep-RL era (learning to play Atari from raw pixels in 2015) and is the method underlying one of the two controllers studied.

### 1.2 The four fundamental quantities

- **State (s):** the observation at the current instant.
- **Action (a):** the decision taken.
- **Reward (r):** a scalar scoring the immediate result.
- **Policy (π):** the rule mapping state to action — the object being learned.

The interaction loop:

```
observe state  →  select action  →  environment returns reward and next state  →  repeat
```

The objective is to maximize **cumulative reward over time**, not merely the immediate reward.

---

## PART 2 — THE MARKOV DECISION PROCESS

The problem is formalized as a **Markov Decision Process (MDP)**: a state space, an action space, and a reward function. DQN uses the MDP identical to that of the PPO controller, by design, to permit fair comparison.

### 2.1 State — the observation vector (six components)

```
state = [sat_C, sat_P, sat_B, cqi_C, cqi_P, cqi_B]
```

- `sat_C, sat_P, sat_B` — **satisfaction**: delivered throughput divided by SLA target, clipped to 1.5. A value of 1.0 denotes full satisfaction; 0.5 denotes half the requirement.
- `cqi_C, cqi_P, cqi_B` — **channel quality**, normalized CQI/15. Higher values mean each PRB carries more bits.

The state encodes how well each slice is served and the channel condition of each.

### 2.2 Action — the discrete allocation profiles (six choices)

```
PROFILES = [
    [34, 33, 33],   # 0 balanced
    [60, 25, 15],   # 1 favour CRITICAL
    [25, 60, 15],   # 2 favour PERFORMANCE
    [15, 25, 60],   # 3 favour BUSINESS
    [45, 45, 10],   # 4 protect CRITICAL and PERFORMANCE
    [10, 10, 80],   # 5 maximize BUSINESS
]
```

Each profile is the triple `[CRITICAL%, PERFORMANCE%, BUSINESS%]` of the DL PRB quota. The discrete action space is precisely the setting for which DQN is designed, for reasons developed in Part 3.

### 2.3 Reward — the optimization signal

```
reward = 0.6·min(sat_C, 1) + 0.3·min(sat_P, 1) + 0.1·min(sat_B, 1)
if sat_C < 1.0:
    reward -= 0.3
```

- **Weights `[0.6, 0.3, 0.1]`** express slice priority — CRITICAL counts six times BUSINESS.
- **`min(sat, 1)`** removes any reward for over-provisioning beyond the SLA.
- **The −0.3 penalty** sharply deters CRITICAL failure.

Maximum reward is 1.0 (all satisfied, no penalty).

### 2.4 Action masking

```
CRIT_MIN_FLOOR = 30   # (reduced during specific experiments)
```

When CRITICAL is unsatisfied, profiles allocating it fewer than `CRIT_MIN_FLOOR` percent are forbidden — a hard constraint applied before selection, not part of learning.

---

## PART 3 — THE DQN ALGORITHM

Here DQN and PPO diverge entirely. PPO learns a _policy directly_ (a probability per action). DQN instead learns a **value for each action** and acts greedily upon those values.

### 3.1 The central object: the Q-value

> **Q(s, a)** is the total future reward expected if, from state `s`, action `a` is taken, and optimal actions are taken thereafter.

Q denotes "quality." Were the true Q-value of every (state, action) pair known, the optimal policy would be immediate: in any state, select the action of highest Q-value. This is the entire strategy.

The task of DQN is to _learn_ this Q-function — to train a network that, given a state, outputs a Q-value for each of the six actions; action selection is then the argmax over those outputs.

```
QNetwork:  Linear(6, 64) → Tanh → Linear(64, 64) → Tanh → Linear(64, 6)
           # input: 6-component state    output: 6 Q-values, one per profile
```

The contrast with PPO is fundamental: PPO's actor outputs _probabilities_ (a stochastic policy), whereas DQN's network outputs _values_, and the policy is the deterministic argmax over them. DQN has no separate actor and critic; the Q-network constitutes the entire model.

### 3.2 Training the Q-network: the Bellman equation

Q-values obey a self-consistency relation, the **Bellman equation**, which renders them learnable:

> The value of taking action `a` now equals the immediate reward plus the discounted value of the best action available in the resulting state.

Formally:

```
Q(s, a)  =  r  +  GAMMA · max_{a'} Q(s', a')
```

This furnishes a **target**. After taking action `a`, receiving reward `r`, and arriving in state `s'`, the network's estimate of `Q(s, a)` should equal `r + GAMMA · (best Q-value in s')`. Any discrepancy is an error to be minimized. Repeated minimization drives the Q-values toward their true values.

```
q_curr = q(s).gather(a)                       # current estimate of Q(s,a)
q_next = q_tgt(s2).gather(a_next)             # value of the best action in s'
q_tgt  = r + GAMMA · q_next · (1 − d)          # the Bellman target
loss   = huber_loss(q_curr, q_tgt)
```

`GAMMA = 0.99` discounts the future. The factor `(1 − d)` removes the future term at episode termination; in a continuous control task this rarely applies.

### 3.3 Three stabilizing mechanisms

Plain Q-learning with a neural network is unstable and frequently diverges. The contribution of DQN was three mechanisms that render it stable; all three are employed by the controller.

**(a) Experience replay — learning from a stored memory rather than only the latest transition.**

```
buffer = ReplayBuffer(capacity = 10,000)
```

Rather than training on each transition as it occurs and discarding it (as PPO does), DQN stores every transition `(s, a, r, s', done)` in a large buffer and trains on **random mini-batches** drawn from it:

```
BATCH_SIZE = 64
s, a, r, s2, d = buffer.sample(64)   # 64 random past transitions
```

The benefits are twofold. First, it **breaks temporal correlation**: consecutive transitions are highly similar, and training on them in sequence biases the network; random sampling de-correlates the data. Second, it **reuses experience**: each transition may be learned from many times, making DQN **off-policy** and considerably more _sample-efficient_ than PPO. Where each second of data corresponds to a live radio run, this efficiency is a material advantage.

This is the principal practical distinction from PPO: PPO consumes each batch once and discards it, whereas DQN retains and replays.

**(b) Target network — a slowly-updated copy providing stable targets.**

```
q_tgt = QNetwork()                              # a second, slowly-updated copy
if steps % TARGET_UPDATE == 0:                  # every 50 steps
    q_tgt.load_state_dict(q.state_dict())
```

In the Bellman target `r + GAMMA · max Q(s')`, the network is used to compute its own training target. As the network changes, the target moves, producing a feedback loop that causes oscillation or divergence. The remedy is a **second copy** of the network, updated only periodically (every `TARGET_UPDATE = 50` steps), used to compute the targets. The target then remains fixed between updates, stabilizing training.

**(c) Double DQN — preventing systematic overestimation.**

```
a_next = q(s2).argmax()                         # main network selects the next action
q_next = q_tgt(s2).gather(a_next)               # target network evaluates it
```

A subtle defect in plain DQN: using the maximum to both select and evaluate the next action causes systematic overestimation of Q-values, since the maximum of noisy estimates is biased upward. **Double DQN** separates the two operations: the main network _selects_ the next action and the target network _evaluates_ it. This decoupling removes the optimistic bias and constitutes the modern, correct formulation.

### 3.4 Exploration: epsilon-greedy

PPO explores by remaining stochastic (the entropy bonus). DQN's policy is deterministic argmax and therefore requires a distinct exploration mechanism: **epsilon-greedy**.

```
EPS_START, EPS_END, EPS_DECAY = 1.0, 0.05, 500

eps = EPS_END + (EPS_START − EPS_END) · exp(−steps / EPS_DECAY)

if random() < eps:
    return random_valid_action()      # explore
else:
    return argmax(Q-values)           # exploit
```

`epsilon` is the probability of acting randomly. It begins at 1.0 — the initial steps are entirely random, populating the replay buffer with varied experience — and decays toward 0.05 over approximately 500 steps as the learned Q-values become trustworthy. A residual 5% random rate persists, so exploration never ceases entirely.

A practical consequence: the initial phase of a training run (until epsilon has decayed, roughly step 350 onward) is dominated by exploration, during which the Q-values are not yet reliable and the selected profiles are largely random. A DQN policy should not be assessed before epsilon has decayed.

### 3.5 The onset of training

```
TRAIN_START = 128
```

Training does not begin until the replay buffer holds at least 128 transitions. The first 128 steps merely collect data — under high epsilon, hence largely random — after which gradient updates commence. This prevents training on a small, unrepresentative buffer.

---

## PART 4 — THE PER-INTERVAL CONTROL CYCLE

The following sequence executes each second within the control loop:

1. **Observation.** Per-slice CQI and throughput are read from the metrics feed.
    
2. **State and reward construction.** The six-component state and the reward are computed from current satisfaction.
    
3. **Storing the previous transition.** The reward just observed results from the _previous_ interval's action; the transition `(prev_state, prev_action, reward, current_state)` is stored in the replay buffer.
    
4. **Conditional update.** Once the buffer holds at least 128 transitions, a random batch of 64 is sampled, Bellman targets are computed with the target network (Double-DQN selection), the Huber loss is minimized, and every 50 steps the target network is synchronized. The checkpoint is saved and metrics logged.
    
5. **Action selection.** The mask excludes CRITICAL-starving profiles where applicable; epsilon-greedy then selects a random action with probability `epsilon`, otherwise the argmax of the Q-values.
    
6. **Application.** The chosen profile is issued via E2SM-RC and recorded to the decision log.
    
7. **Retention.** The current (state, action) pair is retained for the subsequent interval's transition.
    

### Training versus evaluation

```
# Training:   epsilon-greedy (occasionally random, for exploration)
# Evaluation: pure argmax (epsilon effectively zero, frozen)
```

During evaluation, exploration is disabled and the highest-Q action is always taken. This frozen greedy policy is the appropriate basis for performance comparison.

---

## PART 5 — INTERPRETING RESULTS

A DQN training log records:

```
ts, step, epsilon, loss, mean_q
```

- **`epsilon`** should decline from 1.0 toward 0.05, indicating progression from exploration to exploitation. Early high-epsilon steps are expected to appear random.
- **`loss`** (Huber loss between the Q-estimate and the Bellman target) should generally decrease and stabilize. Transient spikes accompany target-network synchronization (the target shifts). Persistently high loss indicates the network cannot fit the targets, often due to a structureless reward.
- **`mean_q`** is the mean Q-value of the batch; it typically rises early and then plateaus. A convergence curve rising from approximately 0.16 to a stable plateau near 28, accompanied by falling loss, indicates a consistent value function has been learned.

A caution: a clean convergence curve (falling loss, rising and plateauing mean_q) demonstrates that _a_ consistent Q-function has been learned, but does not by itself establish that the learned policy is _useful_. If the reward varies along only one dimension, DQN converges to maximizing that dimension and the curve appears ideal while the policy remains trivial. The convergence curve must therefore be paired with a behavioural check: whether the evaluation policy alters its action sensibly in response to state changes.

### The decisive behavioural test

The strongest evidence of a genuine, channel-aware, priority-protecting policy is the relationship between channel quality and allocation: whether the controller allocates **more PRBs to CRITICAL when CRITICAL's CQI degrades**, and fewer when the channel is favourable (a favourable channel allowing CRITICAL to meet its SLA with fewer PRBs). Such a relationship constitutes a meaningful result.

---

## PART 6 — PPO COMPARED WITH DQN

||**PPO**|**DQN**|
|---|---|---|
|What is learned|a policy (action probabilities) directly|Q-values; policy is the argmax over them|
|Architecture|actor and critic (two heads)|a single Q-network|
|On/off-policy|**on**-policy (data used once, then discarded)|**off**-policy (replay buffer, data reused)|
|Sample efficiency|lower (fresh data required per update)|**higher** (past experience replayed)|
|Exploration|entropy bonus (remains stochastic)|epsilon-greedy (random with probability ε)|
|Action type|discrete or continuous|**discrete only**|
|Stability mechanism|clipped policy updates|target network and experience replay|
|Update cadence|every 32 steps, six epochs per batch|every step (after 128), on a random batch|
|Character|stable, robust, tolerant to tuning|sample-efficient, low inference latency|

**Rationale for studying both.** This constitutes the core algorithmic comparison. The literature is divided: certain works favour PPO for its online training stability, whereas offline approaches favour DQN for its low inference latency once trained. Running both on an identical MDP and testbed permits a direct comparison under matched conditions. The distinguishing contribution relative to offline approaches is that both algorithms are trained online, in a closed loop, on live hardware.

A behavioural observation suitable for discussion: DQN frequently produces a sharper, more decisive policy — allocating more aggressively to priority slices and sacrificing best-effort traffic more readily — whereas PPO tends toward a smoother, more balanced allocation. Neither is categorically superior; the difference reflects the distinct optimization characters of the two algorithms and is itself a worthwhile observation.

---

## PART 7 — SUMMARY

DQN (Deep Q-Network) is an off-policy, value-based reinforcement-learning algorithm. A single network learns the **Q-value** — the expected total future reward — of each discrete PRB-allocation profile given the current state of per-slice satisfaction and channel quality. It is trained toward the **Bellman target** (immediate reward plus the discounted value of the best subsequent action) using three stabilizing mechanisms: an **experience-replay buffer** that de-correlates and reuses past transitions, conferring sample efficiency valuable when each second of testbed data is costly; a slowly-updated **target network** that holds training targets stable; and **Double-DQN** action selection that prevents value overestimation. Exploration proceeds by **epsilon-greedy** selection, initially random and decaying toward near-greedy. Action selection reduces to taking the highest-Q action. The reward function, weighted 0.6/0.3/0.1 toward CRITICAL with a penalty for CRITICAL failure, encodes slice priority, so that the learned policy protects high-priority traffic while permitting BUSINESS to absorb residual capacity.

---

## APPENDIX — Hyperparameter reference

|Symbol|Value|Meaning|
|---|---|---|
|`BUFFER_CAP`|10,000|replay buffer size|
|`BATCH_SIZE`|64|random transitions per gradient step|
|`GAMMA`|0.99|discount on future reward|
|`LR`|1e-3|Adam learning rate|
|`TARGET_UPDATE`|50|steps between target-network synchronizations|
|`EPS_START → EPS_END`|1.0 → 0.05|exploration probability range|
|`EPS_DECAY`|500|steps over which epsilon decays|
|`TRAIN_START`|128|minimum buffer size before training begins|
|hidden width|64|neurons per hidden layer|
|activation|Tanh|nonlinearity|
|loss|Huber|robust regression loss for the Bellman error|