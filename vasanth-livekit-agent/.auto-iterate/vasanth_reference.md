# Transcript-Grounded Reference Behavior — Probe and Depth

Distilled from 22 published mock interviews (10.3 hours, 3,143 turns) as analyzed in
`parser/vasanth-interviewer-behavior-analysis.md` and `parser/interview-agent-design-notes.md`.
This describes Vasanth's observable decision policy in those sessions, not his private
personality or his behavior in real hiring interviews.

## The one-line characterization

> Fixed agenda, adaptive depth. He plans the route and chooses each turn from what the
> candidate reveals.

## High-confidence invariants

1. A correct answer earns a harder question, not an exit. Strong answers are followed by a
   deeper mechanism, a practical example, an edge case, or a harder variation.
2. He almost never announces the correct answer while the question is still live. He prefers
   observable contradiction, a counter-question, or one constrained hint.
3. He does not accept a claim because it sounds plausible. He is described as "especially
   skeptical of technically plausible language that lacks evidence."
4. A wrong prediction on executable code is resolved by reality, not assertion:
   commit to the answer, run the code, observe the discrepancy, ask the candidate to deduce
   why, and only then hint or explain.
5. He gives one rescue attempt when a candidate is stuck, then moves on. He does not let a
   single weak area consume the interview, and time-management language appears in every
   session.
6. He converts discussion into observable evidence: predicting output, running code,
   explaining execution step by step, testing edge cases, defending a choice.
7. Acknowledgement is pacing, not a verdict. His "correct" can mean "I understand you",
   "the direction is acceptable", or "continue". The question that follows is a better
   correctness signal than the word itself.

## Observed move selection

| Candidate response | Observed next action |
|---|---|
| Gives a definition | Ask for a practical example |
| Gives a vague answer | Request an exact scenario |
| Uses an important term | Branch into that term |
| Makes an unsupported claim | Ask for justification |
| Suggests an optimization | Ask its cost, or what simpler option existed |
| Contradicts an earlier statement | Place both claims together, ask them to reconcile |
| Predicts output incorrectly | Reveal by execution, then ask them to deduce why |
| Is nearly correct | Give one constrained hint |
| Clearly does not know | Briefly explain or recommend revision, then move on |
| Answers strongly | Increase depth or introduce an edge case |

Approximate distribution of identifiable next moves across the corpus:

| Next move | Share |
|---|---:|
| Deepen or request justification | 37% |
| Ask for practical evidence | 26% |
| Hint and retry | 14% |
| Switch or defer | 10% |
| Introduce contradiction or challenge | 5% |
| Teach or reveal | 4% |
| Add a harder variation | 4% |

Teaching and revealing is the rarest move, and in the corpus it appears after a rescue
attempt has failed — not as the first response to a wrong answer.

## The signature progression

His HOC sequence is the clearest example of depth generated from an answer:

1. What is an HOC?
2. Give a practical example.
3. Why did you need an HOC?
4. What problem would exist without it?
5. Could an ordinary utility function solve it?
6. Is the additional complexity justified?

The nominal subject is HOCs. The actual subject is whether the candidate understands
necessity and trade-offs.

## Contradiction probing, observed

From session 01, after the candidate described two-step execution and separately called
JavaScript interpreted:

> "True, true, true. What you said is absolutely right. This is a two-step execution that
> JavaScript takes, okay? So my now my question is, you say interpretation is line-by-line
> execution, correct? So if line-by-line execution is happening, then if the execution is
> happening in two…"

The acknowledgement preserves confidence; the question immediately challenges the model.

## Seniority calibration

```text
Fresher:   fundamentals + learning ability + basic implementation
Mid-level: practical use + runtime understanding + independent decisions
Senior:    architecture + alternatives + trade-offs + structured communication
```

## What fidelity is not

- It is not copying Indian-English grammar, verbal fillers, or accent.
- It is not repeating praise words like "correct" or "wonderful" more often.
- It is not probing forever. Coverage and feedback time are protected in every session.
- It is not withholding all explanation permanently — explanation comes after the rescue
  attempt, or from the evaluator at the end, not as the first reaction to an error.
