# Research Summary

## Task

Optimize the probe-and-depth decisions in `prompts/interview/vasanth.md` so the live agent
handles wrong answers, shallow answers, and strong answers the way Vasanth does in the
published corpus.

## Domain

Prompt engineering, behavioral policy.

## Trigger

A real session exposed the failure. For `console.log(a); var a = 5;` the candidate answered
"5". The agent replied "Good.", accepted "hoisting" as the justification, said "Good. Let's
start from that.", and advanced to the next question. When the candidate later asked about it,
the agent announced: *"The code will actually output 'undefined' because of the hoisting
behavior with the var keyword."*

Three defects in one exchange: an incorrect answer acknowledged as if correct, the interview
advancing while the error stood unresolved, and the error finally resolved by assertion rather
than by discrepancy.

## Key Findings

- **The prompt contradicts itself.** Line 166 says an incorrect answer "calls for a gentle
  correction", while line 134 requires a counter-question "without revealing the evaluation"
  and line 247 says "Never teach, reveal an answer... or suggest an answer direction". The
  model resolved the conflict by taking the permissive reading. Fixing this contradiction is
  the obvious first experiment.
- **The wrong-answer loop is absent.** `grep -niE "run the code|execute"` over the prompt
  returns nothing, yet the corpus analysis names execution as his resolution path for wrong
  predictions: commit → run → observe discrepancy → ask the candidate to deduce why. The app
  already has `/api/code/run` and a Monaco editor, unused for this.
- **Teaching is his rarest move, not his default.** In the corpus, "teach or reveal" accounts
  for roughly 4% of identifiable next moves, behind deepening (37%), practical evidence (26%),
  and hint-and-retry (14%) — and it appears after a rescue attempt has failed.
- **Acknowledgement is pacing, not a verdict.** His "correct" can mean "I follow you" or
  "continue". The prompt already states this, but the live agent still used "Good." in a way
  that read as confirming a wrong answer.
- **Over-probing is a real failure direction too.** He protects coverage and feedback time in
  every session, so an optimizer rewarded only for probing harder would drift away from him.

## Current Baseline

Run 001, weighted score **0.475**.

| Criterion | Score |
|---|---:|
| wrong_answer_handling | 0.50 |
| probe_selection_fidelity | 0.50 |
| no_reveal_no_lead | 0.25 |
| advance_discipline | 0.50 |
| spoken_style_fidelity | 0.75 |

The benchmark reproduces the production failure. On
`wrong_answer_with_plausible_justification` the shipped policy generated:

> "Hmm. Actually, in this case, the output will be undefined. Can you explain why that
> might be?"

Three of eight cases violate their `must_not` list: revealing the answer to a plausible-but-
wrong justification, failing to surface a self-contradiction, and probing an answer that was
already complete.

An earlier baseline of 0.375 came from two mis-specified cases, both corrected before this
number was recorded:

- `stuck_after_one_rescue` originally forbade "a full explanation", which the judge read as
  forbidding any explanation — penalising the policy for the one place the corpus says
  teaching is correct. The constraint now names a full mechanism walkthrough and a
  two-sentence ceiling, and the case scores 0.80.
- `strong_answer_with_reasoning` left the measurement method unstated, so probing for it was
  arguably legitimate new depth rather than redundancy. The candidate's answer now covers
  the method, the window, and both directions of the crossover, leaving nothing to probe.

## Evaluation Design

Decision-point cases rather than a simulated candidate. Each case is a frozen partial
interview ending on a candidate answer; the composed prompt produces exactly one next turn,
and the judge scores that move. This keeps runs deterministic (temperature 0, fixed cases) and
makes a score change attributable to the prompt change rather than to simulation drift.

Only `workspace/current/behavior_policy.md` is mutable — the Question and Probe Strategy and
Interview Behavior sections. The voice rules, introduction flow, editor and whiteboard tool
rules, time-boxing, and `finish_interview` handling stay frozen in `prompt_shell.md`, because
a loop scored only on probe quality could silently regress tool orchestration. The split was
verified lossless: shell plus policy recomposes to the original file with an identical line
multiset.

## Recommended Evaluation Dimensions

- **wrong_answer_handling** (0.30): the specific defect that triggered this work.
- **probe_selection_fidelity** (0.25): whether the move matches the corpus for that answer type.
- **no_reveal_no_lead** (0.20): the rarest real move, most over-used by the current prompt.
- **advance_discipline** (0.15): penalises both advancing past an unresolved error and
  grinding after a failed rescue.
- **spoken_style_fidelity** (0.10): deliberately the smallest weight — style is not fidelity.

## Common Pitfalls

- Optimizing toward more probing without the `strong_answer_with_reasoning` counterweight
  produces an interviewer who never closes a thread.
- Rewarding catchphrases ("Wonderful. Wonderful.") is imitation, not decision fidelity; the
  judge is instructed not to credit it.
- The judge initially echoed the criterion weights back as scores because the weights appeared
  inline in its instructions. Weights were removed from the judge text and are applied by the
  harness; keep them out.
- Generation and judging share one model by default, so the score carries self-preference
  bias. `JUDGE_MODEL` can be set to a different model to test sensitivity.
- The score is an LLM-judge metric over eight cases, not human ground truth. Treat a win as a
  hypothesis to validate in a live session.
