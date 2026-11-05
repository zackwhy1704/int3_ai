# Refusal threshold: measurement plan

**Status: plan only. Not run. Nothing is tuned by this document.** Run needs the owner's approval.

## Why

`REFUSE_THRESHOLD = 0.0` (`backend/app/answer.py:16`) was measured on the 21 seed questions: supported top
scores 2.56..9.69, unsupported -11.43..-6.91, per its comment. All of those supported questions are specific.

In the hosted pilot, priya asked "What is our holiday policy?" and was refused, though the company-wide
`holidays.md` ranks first. Identical scores on the local stack and the VM (top-document cross-encoder):

| Question | Score | Result at 0.0 |
|---|---|---|
| What is our holiday policy? | -5.83 | refused |
| holiday policy | -3.41 | refused |
| How many days of annual leave do full-time staff get? | 9.69 | answered |

So vague questions fall in the gap (-6.91..2.56), which the original set never sampled. We don't know how
wide the false-refusal problem is, or whether any threshold fixes it without letting unsupported questions
through. "What is our parental leave policy?" (unsupported) has the same shape as the holiday question, so
this is the hard case.

## Questions (30, labelled before any scoring)

Add a `vague` list to `seed/brindlewood/questions.yaml`, with an expected answer fragment for every
answerable question, so correctness can be checked mechanically.

| Group | n | Source |
|---|---|---|
| Answerable, specific | 11 | the existing `answerable` list |
| Answerable, vague | 9 | new, below |
| Unsupported, off-topic | 5 | existing |
| Unsupported, on-topic | 5 | existing (parental leave, courier, pension, auditor, WiFi) |

New vague questions: "What is our holiday policy?", "What's the travel policy?", "Tell me about refunds",
"How does pricing work?", "What's the security policy?", "How does onboarding work?", "What are our payment
terms?", "What's the returns process?", "What do I need to know about warehouse safety?".

Freeze this list before the first run. If it changes after seeing scores, say so in the report.

## Method

Extend `backend/app/calibrate.py` (a new `--sweep` mode), against a **throwaway local stack**, never the
VM. No Hermes in the loop: it tests the retrieval and refusal layer directly.

1. **Scores (no model spend).** For each question, record the top cosine, top cross-encoder score and top
   document, using the union of scopes as `calibrate.py` does, so "supported" means supported somewhere.
2. **Sweep (no model spend).** For thresholds -12.0 to +10.0 in 0.5 steps, count from those scores:
   false refusals (specific and vague separately) and false passes (off-topic and on-topic separately).
3. **Answers with the gate open (model spend).** A one-off script calls the model once per question with
   the threshold removed, because a pass at the threshold isn't a correct answer: the model has its own
   `supported` flag, and citations are structural. Record `supported`, citations, and whether the expected
   fragment appears. Bypass `answer_cache`, so no result is served from it or written to it.
4. **Scope check (no model spend beyond step 3's rate).** Repeat the answerable-vague questions as priya and
   marcus, and check `canaries`: a lower threshold must not change what leaks, which should be nothing,
   because the scope filter runs before ranking.

## What gets reported

- A table of all 30 questions: group, top score, top document, refused at 0.0, model `supported`, fragment found.
- The sweep curve, and the width of the gap between the lowest answerable and highest unsupported score,
  specific and vague separately.
- Effective end-to-end outcome per threshold: gate result combined with the model's `supported`.
- What was not measured: n is small, one synthetic corpus, one reranker model, no real agent rephrasing.

## Decision rule (fixed in advance)

- A change is proposed only if some threshold has **zero false passes end to end** across all 10 unsupported
  questions **and** answers more vague questions correctly.
- If the groups overlap, the threshold stays, and the report says so. The remaining options are product changes
  for the owner, such as Hermes rephrasing a vague question or the refusal suggesting what to ask.
- Any change is the owner's decision and needs the existing refusal tests plus a re-run of the permission tests.
  A tuned pass is not a result.

## Expected cost

Steps 1, 2 and the sweep use the local embedding and reranker models: no model spend.

Step 3 plus step 4: about 30 + 18 = 48 model calls. At an estimated 2,000 input and 200 output tokens per call,
that's about 96,000 input and 9,600 output tokens. At the gateway's placeholder price of $3/$15 per million
(`gateway/limits.py:47-50`, **unverified**), about **$0.43**. **Proposed cap: $1**, stopping if reached.
Token counts are estimates; the report will state the real ones. Compute is local; expect under an hour.
