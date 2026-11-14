"""Measure the memory recall threshold on synthetic memory/query pairs (no database needed).

    docker compose run --rm tools python -m app.launcher.memory_calibrate

One synthetic user holds all MEMORIES. Each RELEVANT query has exactly one memory it should recall
(paraphrased, few shared words). Each UNRELATED query should recall nothing: ordinary chat turns and
company-brain questions a user asks every day. Memories are embedded as passages and queries with
the query prefix, exactly as app/launcher/memory.py does. For each threshold, recall keeps the top
RECALL_K results scoring at or above it:
  precision = correct memories returned / all memories returned (over every query)
  recall    = relevant queries whose memory was returned / relevant queries
"""

import numpy as np

from ..embed import embed_passages, embed_query
from ..rerank import _model as _cross_encoder
from . import memory

MEMORIES = [
    "I prefer short answers, three sentences at most.",
    "My desk is 4B on the second floor of the Leeds office.",
    "I manage the Northern dispatch team of six drivers.",
    "Send me weekly supplier reports as a one-page PDF.",
    "My manager is Ada and our one-to-one is on Thursdays.",
    "I am allergic to peanuts, so keep that in mind when planning team lunches.",
    "I work Monday to Thursday; Fridays are my day off.",
    "Call me Pri, not Priya.",
    "The Lisbon offsite budget owner is Ada.",
    "I write in British English: use colour and organise.",
    "My main customer accounts are Halden Foods and Corvo Retail.",
    "I am learning Portuguese before the Lisbon trip.",
    "Draft emails to suppliers in a formal tone.",
    "I keep my notes in the Ops-Notes folder of the shared drive.",
    "My laptop is a ThinkPad and I use two external monitors.",
    "Remind me that the forklift certification renewal is due in March.",
    "I prefer meetings before 11am.",
    "My team's standup is at 9:15 every weekday.",
    "I don't drink coffee; I drink green tea.",
    "Use metric units in everything you write for me.",
]
RELEVANT = [  # (query, index into MEMORIES)
    ("Keep it brief please, how do I reset my password?", 0),
    ("Where do I sit?", 1),
    ("How many drivers are on my team?", 2),
    ("What format do I want the supplier summary in?", 3),
    ("When is my next catch-up with my boss?", 4),
    ("Book a lunch for the team, anything I should avoid?", 5),
    ("Can we schedule the review for Friday?", 6),
    ("What name should you use for me?", 7),
    ("Who controls the money for the Portugal trip?", 8),
    ("Which spelling conventions do I use?", 9),
    ("Which clients do I look after?", 10),
    ("What language am I studying?", 11),
    ("Write a note to our packaging vendor about the late delivery", 12),
    ("Where do I keep my notes?", 13),
    ("What computer setup do I have?", 14),
    ("When does the forklift licence need renewing?", 15),
    ("When should I schedule the planning meeting?", 16),
    ("What time is my daily standup?", 17),
    ("Should I bring coffee for you?", 18),
    ("How heavy is the pallet in kilograms or pounds?", 19),
]
UNRELATED = [
    "When are invoices payable?",
    "How many days of annual leave do staff get?",
    "What is the returns policy for damaged goods?",
    "Summarise the attached spreadsheet",
    "Translate 'good morning' into German",
    "What's 17 times 23?",
    "Explain how a VPN works",
    "Who approves overtime for warehouse staff?",
    "Write a haiku about autumn",
    "What is the capital of Australia?",
]
THRESHOLDS = [round(x, 2) for x in np.arange(0.50, 0.86, 0.025)]


def main() -> None:
    mem = np.stack(embed_passages(MEMORIES))
    mem /= np.linalg.norm(mem, axis=1, keepdims=True)

    def scores(q: str) -> np.ndarray:
        v = embed_query(q)
        return mem @ (v / np.linalg.norm(v))

    rel = [(scores(q), i) for q, i in RELEVANT]
    unrel = [scores(q) for q in UNRELATED]
    print("relevant pair: own score / best other score")
    for (s, i), (q, _) in zip(rel, RELEVANT):
        other = max(x for j, x in enumerate(s) if j != i)
        print(f"  {s[i]:.3f} / {other:.3f}  {q}")
    print("unrelated query: best score")
    for s, q in zip(unrel, UNRELATED):
        print(f"  {s.max():.3f}  {q}")
    print(f"\nthreshold  precision  recall  returned  (top {memory.RECALL_K})")
    for t in THRESHOLDS:
        returned = correct = hit = 0
        for s, i in rel:
            top = [j for j in np.argsort(-s)[: memory.RECALL_K] if s[j] >= t]
            returned += len(top)
            correct += i in top
            hit += i in top
        for s in unrel:
            returned += sum(1 for j in np.argsort(-s)[: memory.RECALL_K] if s[j] >= t)
        p = correct / returned if returned else 1.0
        print(f"  {t:.3f}     {p:.2f}       {hit / len(rel):.2f}    {returned}")

    # Cross-encoder over the vector top-CANDIDATES, as memory.recall does.
    def ce(q: str) -> dict[int, float]:
        cand = list(np.argsort(-scores(q))[: memory.RECALL_CANDIDATES])
        return dict(zip(cand, map(float, _cross_encoder().rerank(q, [MEMORIES[j] for j in cand])), strict=True))

    rel_ce = [(ce(q), i) for q, i in RELEVANT]
    unrel_ce = [ce(q) for q in UNRELATED]
    print("\ncross-encoder: relevant own / best other;  unrelated best")
    for (c, i), (q, _) in zip(rel_ce, RELEVANT, strict=True):
        print(f"  {c.get(i, float('nan')):6.2f} / {max(v for j, v in c.items() if j != i):6.2f}  {q}")
    for c, q in zip(unrel_ce, UNRELATED, strict=True):
        print(f"  best {max(c.values()):6.2f}  {q}")
    print(f"\nCE threshold  precision  recall  returned  (top {memory.RECALL_K} of {memory.RECALL_CANDIDATES})")
    for t in [x / 2 for x in range(-16, 9)]:
        returned = hit = 0
        for c, i in rel_ce:
            top = [j for j, v in sorted(c.items(), key=lambda kv: -kv[1])[: memory.RECALL_K] if v >= t]
            returned += len(top)
            hit += i in top
        for c in unrel_ce:
            returned += sum(1 for _, v in sorted(c.items(), key=lambda kv: -kv[1])[: memory.RECALL_K] if v >= t)
        print(f"  {t:6.1f}       {hit / returned if returned else 1.0:.2f}       {hit / len(rel_ce):.2f}    {returned}")

    print(f"\nCE threshold + margin (keep v >= threshold and v >= best - margin), top {memory.RECALL_K}")
    print("  threshold  margin  precision  recall  returned")
    for t in (-8.0, -7.0, -6.0):
        for m in (2.0, 3.0, 4.0, 99.0):
            returned = hit = 0
            for c, i in rel_ce + [(c, None) for c in unrel_ce]:
                ranked = sorted(c.items(), key=lambda kv: -kv[1])[: memory.RECALL_K]
                best = ranked[0][1]
                top = [j for j, v in ranked if v >= t and v >= best - m]
                returned += len(top)
                hit += i in top
            print(f"  {t:6.1f}    {m:5.1f}   {hit / returned if returned else 1.0:.2f}       {hit / len(rel_ce):.2f}    {returned}")


if __name__ == "__main__":
    main()
