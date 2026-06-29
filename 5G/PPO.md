# Proximal Policy Optimization for Slice-Aware Radio Resource Allocation

### A self-contained exposition, from reinforcement learning fundamentals to the deployed xApp

This document presents Proximal Policy Optimization (PPO) from first principles and connects each concept to the slice-aware downlink PRB allocator implemented as a near-real-time xApp. It presumes familiarity with Python but no prior knowledge of reinforcement learning. It is intended to be read once in full and thereafter used as a reference.

---

## PART 1 — THE PROBLEM SETTING

### 1.1 The class of problem reinforcement learning addresses

Reinforcement learning (RL) concerns systems that must make a decision repeatedly, where each decision influences subsequent conditions, and where feedback on the quality of a decision arrives only _after_ the decision is taken. Crucially, no supervisor provides the correct answer for each situation; the only signal available is a scalar score.

The resource-allocation task studied here is of exactly this form:

- At each decision interval (one second), the controller must partition radio resources (physical resource blocks, PRBs) among three network slices: CRITICAL, PERFORMANCE, and BUSINESS.
- After the allocation is applied, the outcome is observed as the throughput each slice achieved relative to its service-level agreement (SLA).
- No ground-truth optimal split is provided; the only feedback is a **reward**, a single number describing how good the outcome was.

RL is the family of methods that learns a _decision policy_ from such trial-and-error feedback. PPO is a modern, stable, policy-gradient RL algorithm and is the method underlying the controller described in this document.

### 1.2 The four fundamental quantities

Every RL formulation rests on four objects:

- **State (s):** the observation of the environment at the current instant — a snapshot.
- **Action (a):** the decision taken in response.
- **Reward (r):** a scalar describing the immediate quality of the result.
- **Policy (π):** the rule mapping a state to an action. The policy is the object being learned: _given this state, which action should be taken?_

The interaction proceeds as a loop:

```
observe state  →  policy selects action  →  environment returns reward and next state  →  repeat
```

The objective of RL is to find a policy that maximizes **cumulative reward over time**, rather than merely the immediate reward. This temporal dimension is what distinguishes RL from ordinary prediction: a choice that is locally attractive may be globally detrimental.

---

## PART 2 — THE MARKOV DECISION PROCESS

RL problems are formalized as a **Markov Decision Process (MDP)**. The Markov property states that the next state depends only on the current state and action, not on the full history. An MDP is specified by its state space, action space, and reward function. The formulation used by the controller follows.

### 2.1 State — the observation vector (six components)

```
state = [sat_C, sat_P, sat_B, cqi_C, cqi_P, cqi_B]
```

Six real-valued components:

- `sat_C, sat_P, sat_B` — the **satisfaction** of each slice, defined as delivered throughput divided by the slice's SLA target and clipped to a maximum of 1.5.
    - A value of 1.0 indicates the slice receives exactly its SLA — full satisfaction.
    - A value of 0.5 indicates the slice receives only half of its requirement.
- `cqi_C, cqi_P, cqi_B` — the **channel quality** of each slice, normalized to the range 0–1 (raw CQI spans 0–15; the normalized value is CQI/15).
    - High CQI corresponds to favourable radio conditions, in which each PRB carries more bits.
    - Low CQI corresponds to poor conditions, in which the same allocation delivers less.

The state therefore encodes how well each slice is currently served and the channel condition of each — the information required to decide how resources should be reallocated.

### 2.2 Action — the discrete allocation profiles (six choices)

The controller does not set arbitrary PRB percentages. It selects one of six predefined **profiles**, constituting a discrete action space (a finite menu rather than a continuous control):

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

Each profile is the triple `[CRITICAL%, PERFORMANCE%, BUSINESS%]` of the downlink PRB minimum-ratio quota, summing to 100. Selecting a profile causes an E2SM-RC control message to be issued to the DU scheduler specifying the minimum PRB ratios. The discrete nature of the action space motivates the use of a **Categorical** distribution over the six choices within PPO.

### 2.3 Reward — the optimization signal

```
reward = 0.6·min(sat_C, 1) + 0.3·min(sat_P, 1) + 0.1·min(sat_B, 1)
if sat_C < 1.0:
    reward -= 0.3      # penalty for failing CRITICAL
```

The reward encodes the priority structure of the system:

- **Weights `[0.6, 0.3, 0.1]`** — CRITICAL satisfaction contributes six times as much as BUSINESS satisfaction. The relative weighting is the mechanism by which slice priority is expressed; the learned policy will protect whichever slice is weighted most heavily.
- **`min(sat, 1)`** — satisfaction is capped at 1.0 within the reward. Delivering throughput beyond a slice's SLA yields no additional reward, discouraging the wasteful over-provisioning of an already-satisfied slice.
- **The penalty** — an additional −0.3 whenever CRITICAL falls below its SLA, providing a sharp deterrent on the highest-priority slice.

The maximum attainable reward is 0.6 + 0.3 + 0.1 = 1.0, corresponding to all slices satisfied with no penalty.

### 2.4 Action masking — excluding inadmissible actions

```
CRIT_MIN_FLOOR = 30   # (reduced during specific experiments)
```

When CRITICAL is unsatisfied, the mask forbids any profile that would allocate CRITICAL fewer than `CRIT_MIN_FLOOR` percent of PRBs. This is a hard constraint — an already-starved CRITICAL slice may not be starved further — applied prior to action selection. It is not part of the learning process but a safeguard layered upon it.

---

## PART 3 — THE PPO ALGORITHM

PPO is a **policy-gradient** algorithm employing an **actor-critic** architecture. Three notions require development: the policy gradient, the actor-critic structure, and the "proximal" mechanism from which the algorithm takes its name.

### 3.1 The actor-critic architecture

```
body  : Linear(6, 64) → Tanh → Linear(64, 64) → Tanh     # shared trunk
pi    : Linear(64, 6)    # ACTOR  — action preferences
v     : Linear(64, 1)    # CRITIC — state value estimate
```

- The **actor** (`pi`) represents the policy. Given the six-component state, it produces six values (logits), one preference per action; a softmax converts these into a probability distribution over the profiles.
- The **critic** (`v`) estimates the **value** of a state: the total reward expected from that state onward under the current policy. It produces a single number.

The critic is necessary because policy improvement requires knowing whether an action performed _better or worse than expected_. The critic supplies the expected baseline; without it, fortunate outcomes cannot be distinguished from skillful ones.

The two heads share a trunk because both require an understanding of the state; only the final layers differ.

### 3.2 The central quantity: advantage

The pivotal concept in PPO is the **advantage**:

> Advantage measures how much better an action proved to be relative to the value the critic expected from that state.

If the critic estimated a state's value as 0.8 and the action led to outcomes worth 0.95, the advantage is +0.15 — the action exceeded expectation and should be made more probable. If outcomes were worth 0.7, the advantage is −0.1, and the action should be made less probable.

PPO estimates advantage via **Generalized Advantage Estimation (GAE)**, which combines short- and long-horizon reward signals:

```
for t in reversed(range(T)):
    delta = r[t] + GAMMA·v[t+1] − v[t]      # one-step temporal-difference error
    gae   = delta + GAMMA·LAM·gae           # discounted accumulation
    adv[t] = gae
```

- `GAMMA = 0.99` is the discount factor; future rewards are valued almost as highly as immediate ones, producing a farsighted policy.
- `LAM = 0.95` is the GAE smoothing parameter, trading bias against variance in the advantage estimate.
- `delta` is the **temporal-difference error**: the observed reward plus the discounted value of the resulting state, minus the value expected at the current state — the per-step surprise.

Advantages are then normalized:

```
adv = (adv − mean(adv)) / (std(adv) + 1e-8)
```

Normalization maintains a consistent learning-signal scale irrespective of reward magnitude. A consequence worth noting: if every action yields an identical reward, the advantages are uniformly near zero, normalization amplifies the residual noise to unit variance, and no meaningful policy gradient exists. A reward that is flat with respect to the action is therefore fatal to learning — which is why a contention regime, in which the action measurably affects the reward, is essential.

### 3.3 The proximal mechanism

A naive policy improvement pushes the policy strongly toward actions with positive advantage. This is unstable: an excessive update can move the policy to a far worse region, from which it may not recover. PPO constrains the magnitude of each update through a **clipped objective**:

```
ratio = exp(new_logp − old_logp)               # change in action likelihood
s1 = ratio · advantage
s2 = clamp(ratio, 1−CLIP, 1+CLIP) · advantage   # CLIP = 0.2
policy_loss = −mean(min(s1, s2))
```

The `ratio` quantifies how much the updated policy has altered its preference for an action relative to the policy that gathered the data. If an update attempts to shift an action's probability by more than ±20% (`CLIP = 0.2`), the objective clips the contribution, removing the incentive for further movement. This constraint keeps each update **proximal** to the preceding policy and is the source of PPO's stability.

### 3.4 The composite loss

```
loss = policy_loss + VF_COEF·value_loss − ENT_COEF·entropy
```

Three components, each weighted:

- **`policy_loss`** — the clipped actor objective, which improves the policy.
- **`value_loss`** (`VF_COEF = 0.5`) — trains the critic to predict returns accurately via `(v − return)²`. An accurate critic yields accurate advantages.
- **`entropy`** (`ENT_COEF = 0.03`) — rewards randomness in the policy. Entropy is high when the policy is uncertain (probability spread across actions) and low when confident (one action dominant). Subtracting entropy from the loss rewards the optimizer for remaining somewhat stochastic, preventing premature commitment to a single action before adequate exploration. This term constitutes the exploration mechanism: early in training it sustains trial of all six profiles, and it declines naturally as the policy sharpens.

The entropy value is informative during training. It begins near ln(6) ≈ 1.79 — maximal uncertainty over six actions — and declines as the policy commits. Persistence at 1.79 indicates the policy has found no reason to prefer any action, implying the reward provides no gradient.

### 3.5 On-policy operation and the rollout buffer

PPO is **on-policy**: it learns only from data generated by the current policy and cannot reuse past experience. It therefore proceeds in cycles:

1. Execute the current policy for `ROLLOUT = 32` steps, recording each (state, action, log-probability, value, reward).
2. Compute advantages across those steps.
3. Update the networks for `EPOCHS = 6` passes over the batch.
4. Discard the data, collect fresh data under the improved policy, and repeat.

```
ROLLOUT, EPOCHS = 32, 6
```

Each recorded "update" corresponds to one such cycle. As each cycle spans 32 one-second steps, an update occurs approximately every 32 seconds.

---

## PART 4 — THE PER-INTERVAL CONTROL CYCLE

The following sequence executes at each one-second interval within the control loop:

1. **Observation.** Per-slice metrics (CQI, delivered throughput) are read from the metrics feed.
    
2. **State and reward construction.** The six-component state and the reward are computed from current satisfaction.
    
3. **Closing the previous transition.** The reward just observed is the consequence of the action applied in the _previous_ interval. The previous (state, action) pair is therefore associated with the present reward and stored in the rollout buffer. This temporal attribution is subtle and correct; an error here silently disables learning.
    
4. **Conditional update.** Once 32 transitions accumulate, the PPO update executes (GAE, then the clipped objective over six epochs), the checkpoint is saved, metrics are logged, and the buffer is cleared.
    
5. **Action selection.** The mask excludes CRITICAL-starving profiles where applicable; the policy then samples an action from the actor's distribution (during training) or selects the most probable action (during evaluation).
    
6. **Application.** The chosen profile is issued to the DU via E2SM-RC and recorded to the decision log.
    
7. **Retention.** The current (state, action) pair is retained so that the subsequent interval's reward can be attributed to it.
    

### Training versus evaluation

```
# Training:   a = sample(distribution)   — exploratory
# Evaluation: a = argmax(logits)         — greedy, frozen
```

During training the policy samples actions, occasionally selecting suboptimal ones to maintain exploration. During evaluation the weights are frozen and the most probable action is always taken. Evaluation measures the final policy's quality without exploration noise, and is therefore the appropriate basis for performance comparison.

---

## PART 5 — INTERPRETING RESULTS

A PPO training log records:

```
ts, update, mean_reward, policy_loss, value_loss, entropy
```

- **`mean_reward`** should trend upward across updates, with noise. It is the principal indicator of policy improvement.
- **`policy_loss`** fluctuates near zero and is _not_ a "lower-is-better" quantity; it is a clipped surrogate and should not be read as a supervised loss. Small fluctuations are expected.
- **`value_loss`** should decrease as the critic learns to predict returns. Divergence indicates the critic is struggling, often because the reward signal lacks structure.
- **`entropy`** should decline from approximately 1.79 as the policy commits. Failure to decline indicates the policy found no basis for preferring any action — the reward is flat with respect to the action.

A healthy run exhibits rising reward, falling value loss, and falling entropy. A degenerate run exhibits flat reward and entropy fixed near 1.79, indicating the environment grants the action no leverage over the reward.

### Policy collapse

If, during evaluation, the controller selects the same one or two actions regardless of state, the policy is **state-independent** — it has settled on a single profile that scores acceptably. This is not invariably a failure (the optimal policy is occasionally near-constant), but a strong policy should alter its action in response to state changes, for instance favouring CRITICAL more heavily when CRITICAL's channel quality degrades. The diagnostic is whether the action varies with the state in a sensible manner.

---

## PART 6 — SUMMARY

PPO is an on-policy, actor-critic, policy-gradient reinforcement-learning algorithm. An actor network maps the current state — per-slice satisfaction and channel quality — to a probability distribution over a discrete set of PRB-allocation profiles, while a critic network estimates the value of each state to provide a baseline. After every 32 decision-steps, PPO estimates each action's advantage relative to the critic's expectation using Generalized Advantage Estimation, then adjusts the actor to render above-average actions more probable, while _clipping_ each update so that the policy cannot move excessively in a single step — the property responsible for its stability. An entropy bonus sustains exploration until commitment is warranted. The reward function, weighted 0.6/0.3/0.1 toward CRITICAL with a penalty for CRITICAL failure, encodes slice priority, so that the learned policy protects high-priority traffic while permitting BUSINESS to absorb residual capacity.

---

## APPENDIX — Hyperparameter reference

|Symbol|Value|Meaning|
|---|---|---|
|`ROLLOUT`|32|steps collected before each update|
|`EPOCHS`|6|gradient passes per update over the batch|
|`GAMMA`|0.99|discount on future reward|
|`LAM`|0.95|GAE bias/variance smoothing|
|`CLIP`|0.2|maximum policy change per update|
|`LR`|3e-4|Adam learning rate|
|`ENT_COEF`|0.03|strength of the entropy (exploration) bonus|
|`VF_COEF`|0.5|weight of the critic's value loss|
|hidden width|64|neurons per hidden layer|
|activation|Tanh|nonlinearity|