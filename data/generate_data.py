"""
Generate a reproducible, SIMULATED multi-channel customer-journey dataset
with a KNOWN ground truth of each channel's incremental effect.

Why simulated? This is a portfolio/practice project. Real journey logs are
private, and in real life the true causal contribution of a channel is never
observable. By simulating the data I can generate paths the way a real funnel
behaves (awareness -> consideration -> harvest of existing demand) AND keep the
real incremental effect of every touch, so attribution models can be graded
against the truth.

Data-generating process (what the analyst would NOT know in real life):
  1. Each user has a baseline probability p0 of converting even with no ads
     (existing brand demand).
  2. Paths follow a first-order Markov chain between channels. Awareness
     channels (Programmatic, Meta) often lead to Brand Search later: the
     classic "demand harvesting" pattern that inflates last-click.
  3. Every touch has a small causal lift e_c on conversion probability,
     with diminishing returns for repeated touches of the same channel
     (ad fatigue). P(convert) = 1 - (1 - p0) * prod(1 - e_touch).

Outputs (data/):
  touchpoints.csv   : user_id, touch_time, channel            (messy on purpose)
  conversions.csv   : user_id, conversion_time, revenue
  channel_spend.csv : channel, spend, spend_source
  ground_truth.csv  : true incremental conversions per channel (validation only)

Run:  python data/generate_data.py
"""
import numpy as np
import pandas as pd
from pathlib import Path

SEED = 42
N_USERS = 60_000
START, LAST_FIRST_TOUCH = pd.Timestamp("2025-07-01"), pd.Timestamp("2025-09-15")
OUT = Path(__file__).parent
rng = np.random.default_rng(SEED)

CHANNELS = ["Programmatic", "Meta Ads", "Google Search (Generic)",
            "Google Search (Brand)", "Email", "Organic / SEO"]
IDX = {c: i for i, c in enumerate(CHANNELS)}

# Causal lift of ONE touch on conversion probability (hidden from the analyst)
LIFT = {"Programmatic": 0.008, "Meta Ads": 0.017, "Google Search (Generic)": 0.030,
        "Google Search (Brand)": 0.009, "Email": 0.027, "Organic / SEO": 0.015}
FATIGUE = 0.6          # k-th touch from the same channel has lift * FATIGUE**(k-1)

# First touch distribution (Email only for subscribers, handled below)
START_P = np.array([0.28, 0.27, 0.17, 0.04, 0.04, 0.20])

# Transition matrix between channels (rows = current channel)
#            Prog  Meta  GenS  BrdS  Email Org
T = np.array([[0.20, 0.15, 0.18, 0.30, 0.05, 0.12],   # Programmatic
              [0.08, 0.17, 0.18, 0.30, 0.07, 0.20],   # Meta Ads
              [0.10, 0.13, 0.20, 0.30, 0.12, 0.15],   # Google Search (Generic)
              [0.10, 0.15, 0.10, 0.20, 0.25, 0.20],   # Google Search (Brand)
              [0.08, 0.10, 0.12, 0.30, 0.20, 0.20],   # Email
              [0.10, 0.12, 0.18, 0.28, 0.12, 0.20]])  # Organic / SEO
assert np.allclose(T.sum(1), 1) and np.isclose(START_P.sum(), 1)

P_CONTINUE_FIRST, CONTINUE_DECAY, MAX_STEPS = 0.62, 0.90, 8


def sample_channel(probs, subscribed):
    p = probs.copy()
    if not subscribed:                       # non-subscribers never receive email
        p[IDX["Email"]] = 0.0
        p /= p.sum()
    return rng.choice(len(CHANNELS), p=p)


def conv_prob(p0, channel_list, skip=None):
    """Noisy-OR conversion probability; `skip` removes a channel's touches (counterfactual)."""
    seen, survive = {}, 1.0 - p0
    for c in channel_list:
        if c == skip:
            continue
        k = seen.get(c, 0)
        survive *= 1.0 - LIFT[c] * FATIGUE ** k
        seen[c] = k + 1
    return 1.0 - survive


touch_rows, conv_rows = [], []
true_inc = {c: 0.0 for c in CHANNELS}
baseline_expected = 0.0
span_days = (LAST_FIRST_TOUCH - START).days

for u in range(N_USERS):
    uid = f"U{u + 1:06d}"
    subscribed = rng.random() < 0.30
    p0 = rng.beta(1, 90)                                   # mean ~1.1%
    t = START + pd.Timedelta(days=float(rng.uniform(0, span_days)))

    path = [sample_channel(START_P, subscribed)]
    times = [t]
    p_cont = P_CONTINUE_FIRST
    while len(path) < MAX_STEPS and rng.random() < p_cont:
        path.append(sample_channel(T[path[-1]], subscribed))
        times.append(times[-1] + pd.Timedelta(days=float(rng.exponential(3.0))))
        p_cont *= CONTINUE_DECAY

    names = [CHANNELS[i] for i in path]
    p_full = conv_prob(p0, names)

    # ground truth: expected conversions lost if channel c's touches never happened
    baseline_expected += p0
    for c in set(names):
        true_inc[c] += p_full - conv_prob(p0, names, skip=c)

    for tm, nm in zip(times, names):
        touch_rows.append((uid, tm.floor("min"), nm))

    if rng.random() < p_full:
        conv_time = times[-1] + pd.Timedelta(days=float(rng.uniform(0.02, 2.0)))
        revenue = round(float(rng.lognormal(np.log(62), 0.45)), 2)
        conv_rows.append((uid, conv_time.floor("min"), revenue))

touch = pd.DataFrame(touch_rows, columns=["user_id", "touch_time", "channel"])
conv = pd.DataFrame(conv_rows, columns=["user_id", "conversion_time", "revenue"])

# --- deliberately messy data ------------------------------------------------
# 1) inconsistent channel naming (casing / whitespace / aliases)
messy = touch.sample(frac=0.015, random_state=SEED).index
alias = {"Meta Ads": ["meta ads", "Meta Ads ", "META ADS"],
         "Programmatic": ["programmatic", " Programmatic"],
         "Email": ["email", "E-mail"],
         "Organic / SEO": ["organic / seo", "Organic/SEO"],
         "Google Search (Generic)": ["google search (generic)"],
         "Google Search (Brand)": ["google search (brand)"]}
touch.loc[messy, "channel"] = [rng.choice(alias[c]) for c in touch.loc[messy, "channel"]]

# 2) exact duplicate rows (tracking fired twice)
touch = pd.concat([touch, touch.sample(frac=0.005, random_state=SEED)])

# 3) touches logged AFTER the conversion (e.g. post-purchase email) for ~5% of buyers
late = conv.sample(frac=0.05, random_state=SEED)
late_rows = pd.DataFrame({"user_id": late.user_id,
                          "touch_time": late.conversion_time + pd.Timedelta(hours=6),
                          "channel": "Email"})
touch = pd.concat([touch, late_rows]).sort_values(["touch_time", "user_id"]).reset_index(drop=True)

# --- spend: week-4 mix of my paid-media-budget-optimization project, x3 months ---
spend = pd.DataFrame([
    ("Programmatic",            13200, "Simulated: DV360 + TTD + Display share (44%) of $30k"),
    ("Meta Ads",                 9000, "Simulated: Meta share (30%) of $30k"),
    ("Google Search (Generic)",  6000, "Simulated: Search share (20%) of $30k"),
    ("Google Search (Brand)",    1800, "Simulated: Search share (6%) of $30k"),
    ("Email",                     600, "Assumed: ESP + creative cost"),
    ("Organic / SEO",            1500, "Assumed: content + tooling cost"),
], columns=["channel", "spend", "spend_source"])

truth = pd.DataFrame({"channel": CHANNELS,
                      "true_incremental_conversions": [true_inc[c] for c in CHANNELS]})
truth.loc[len(truth)] = ["(baseline: would convert anyway)", baseline_expected]

touch.to_csv(OUT / "touchpoints.csv", index=False)
conv.to_csv(OUT / "conversions.csv", index=False)
spend.to_csv(OUT / "channel_spend.csv", index=False)
truth.to_csv(OUT / "ground_truth.csv", index=False)

print(f"touchpoints.csv : {len(touch):,} rows | users: {touch.user_id.nunique():,}")
print(f"conversions.csv : {len(conv):,} rows | conv. rate {len(conv) / N_USERS:.2%}")
print(f"expected baseline conversions: {baseline_expected:,.0f} "
      f"({baseline_expected / len(conv):.0%} of observed)")
