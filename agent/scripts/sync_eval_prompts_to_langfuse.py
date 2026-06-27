"""
Sync evaluator prompts to Langfuse Prompt Management.

Run from the agent/ directory:
    python scripts/sync_eval_prompts_to_langfuse.py

Creates 3 LLM-as-judge rubric prompts used by eval/evaluate.py to score sessions.
Related criteria are grouped so each prompt covers one thematic area — balancing
scoring quality against the cost of too many API calls per session.

  eval-probing-quality   (turn-level)  — followup_relevance, depth_probing, premature_closure
  eval-interview-conduct (turn-level)  — neutral_tone, leading_question, graceful_redirect, response_brevity
  eval-session-coherence (session-level) — question_derailment, context_carry_forward

Turn-level prompts receive {{input}} (candidate message) + {{output}} (agent response).
Session-level prompts receive {{conversation}} (full transcript, alternating turns).

Re-running creates new versions — it does not overwrite existing ones.
"""

import os
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]

env_path = AGENT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)

from langfuse import Langfuse  # noqa: E402

EVALUATORS: dict[str, tuple[str, str]] = {
    # (scope, prompt_text)

    "eval-probing-quality": ("turn", """\
You are evaluating an AI voice interviewer's probing behaviour in a CS diagnostic interview.

This interview has two types of rounds:
- Rounds WITH follow-up probes: behavioral and technical-thinking. \
Each main question is followed by exactly 1 required follow-up probe.
- Rounds WITHOUT follow-up probes: screening and career-readiness. \
The agent must move straight to the next question after the candidate answers — probing is never expected.

STEP 1 — Classify the turn type based on the agent response:
- "follow_up_probe"  if the agent's response drills into something specific the candidate just said \
(narrower topic, references a detail from the candidate's answer, no new scenario introduced)
- "main_question"    if the agent's response introduces a new topic, scenario, or question \
(could be the first question, a transition after a follow-up, or any new main question)
- "other"            if the agent's response is a greeting, closing remark, redirect, \
or anything that is not a question

STEP 2 — Score only the applicable dimensions:

IF turn_type == "follow_up_probe":
  followup_relevance (0-10): Does the probe target the specific gap in this candidate's answer?
    10 — Directly references what the candidate said, probes the exact gap \
(missing example, unexplained claim, vague term).
    5–7 — Broadly related to the topic but generic ("Can you elaborate?", "Tell me more.").
    0–3 — Unrelated to what the candidate said, or repeats the original question verbatim.
  depth_probing (0-10): Is the probe appropriately deep given the answer quality?
    10 — Shallow/partial answer: probe pushed for depth. Strong answer: probe extended naturally.
    5–7 — Probe present but misdirected or too surface-level.
    0–3 — Candidate gave a vague or evasive answer; agent's follow-up missed the actual gap.
  premature_closure: null — the agent is actively probing, not closing.

IF turn_type == "main_question":
  followup_relevance: null — no follow-up to evaluate.
  depth_probing: null — cannot assess probing depth on a main question transition.
  premature_closure (0-10): Did the agent move to this new question prematurely?
    IMPORTANT: The silence protocol applies to ALL rounds including screening and career-readiness. \
If the candidate's message was empty, only filler words (um, uh, mm, hmm), or clearly a non-answer \
(no real content), the agent must check in and re-prompt before advancing — even in rounds where \
follow-up probes are not expected. Jumping straight to a new question after a non-answer is always \
premature closure, regardless of round type.
    10 — Candidate gave a real answer and agent correctly transitioned. \
OR candidate gave a non-answer and agent followed the silence protocol \
(check-in → re-prompt) before advancing.
    5–7 — Candidate gave a partial answer and agent moved on slightly early, \
OR agent did a brief check-in but not a full re-prompt before advancing on a non-answer.
    0–3 — Agent asked a new question immediately after a non-answer, empty message, \
or filler-only response (um, uh, mm) with no check-in or re-prompt. \
This applies in ALL rounds.

IF turn_type == "other":
  followup_relevance: null
  depth_probing: null
  premature_closure: null

Candidate message:
{{input}}

Agent response:
{{output}}

Respond with JSON only. Use JSON null (not a string) for inapplicable dimensions:
{
  "turn_type": "follow_up_probe" | "main_question" | "other",
  "followup_relevance": {"score": <0-10>, "reasoning": "<one sentence>"} | null,
  "depth_probing": {"score": <0-10>, "answer_quality": "<shallow|complete|partial>", "reasoning": "<one sentence>"} | null,
  "premature_closure": {"score": <0-10>, "reasoning": "<one sentence>"} | null
}"""),

    "eval-interview-conduct": ("turn", """\
You are evaluating an AI voice interviewer's conduct in a CS diagnostic interview.

Score the following 4 dimensions from 0 to 10 based on this single exchange.

--- NEUTRAL TONE ---
Does the agent stay neutral — neither overly encouraging nor discouraging?
10 — No praise signals ("nice", "great", "excellent", "correct", "well done", "perfect"). \
No discouraging signals. Pure neutrality.
5–7 — One minor slip: a filler affirmation ("okay", "alright") that slightly colours the response \
but is not evaluative.
0–3 — Clear positive or negative evaluation of the candidate's answer aloud, repeated praise, \
or dismissive language.

--- LEADING QUESTION ---
Does the follow-up hint at the correct answer or give information the candidate should provide?
Note: higher score = LESS leading (10 is ideal).
10 — Fully neutral and open-ended. No embedded answer, no hint.
5–7 — Mild hint: phrasing narrows the answer space or implies a direction \
("Would using a hash map help here?").
0–3 — Strongly leading: question contains or implies the answer \
("So you'd use Floyd's cycle detection, right?").

--- GRACEFUL REDIRECT ---
When a candidate goes off-topic or gives an irrelevant answer, does the agent recover cleanly?
If no redirect situation arose, score 10 with reasoning "No redirect needed."
10 — Redirected briefly and warmly, then returned to the interview without dwelling.
5–7 — Redirected but spent too long on off-topic content, or was stilted/robotic.
0–3 — Ignored the derailment, answered the off-topic content, or abandoned the current question.

--- RESPONSE BREVITY ---
Is the agent's spoken response short enough for a voice channel? Responses must be under 30 words.
10 — 30 words or fewer. Concise and natural for voice.
5–7 — 31–60 words. Acceptable but slightly long.
0–4 — Over 60 words. Too long for a voice turn; monologues score 0.

Candidate message:
{{input}}

Agent response:
{{output}}

Respond with JSON only:
{
  "neutral_tone": {"score": <0-10>, "reasoning": "<one sentence>"},
  "leading_question": {"score": <0-10>, "reasoning": "<one sentence>"},
  "graceful_redirect": {"score": <0-10>, "reasoning": "<one sentence>"},
  "response_brevity": {"score": <0-10>, "word_count": <int>, "reasoning": "<one sentence>"}
}"""),

    "eval-session-coherence": ("session", """\
You are evaluating an AI voice interviewer across a full CS diagnostic interview session.

Score the following 2 dimensions from 0 to 10 based on the complete conversation.

--- QUESTION DERAILMENT ---
Does the agent stay within the intended skill domains throughout the session?
The interview must stay focused on: CS technical knowledge, behavioural competency, \
language proficiency, and communication confidence.
Note: higher score = LESS derailment (10 is ideal).
10 — Every agent turn stays within the assessment domains. No drift.
5–7 — One or two turns briefly off-domain but agent returned quickly.
0–3 — Agent introduced or followed off-topic content for multiple turns \
(e.g. salary, company culture, personal advice).

--- CONTEXT CARRY-FORWARD ---
Does the agent take the natural opportunities to reference specific things the candidate said, \
through bridge sentences before questions and follow-up probes within questions?
Bridge sentences and follow-up probes are the two mechanisms available — they are optional \
(the agent should not force a connection when there is none), so the score reflects whether \
the agent used them when a clear callback existed.
Good examples of carry-forward:
  Bridge sentence: "You mentioned Redis — [next question about caching]"
  Follow-up probe: "You said you used a mutex — what happens under high contention?"
  Screening S2 pick: Agent picks the second question based on a specific detail from the first answer.
Poor examples (no carry-forward):
  Bridge: "Let's move on to the next question." (generic, references nothing)
  No bridge at all across a long session despite clear callbacks in the candidate's answers.
10 — Agent explicitly referenced specific candidate words or details at least once in a bridge \
sentence or follow-up probe, in a way that felt natural and contextually accurate.
5–7 — Agent made one weak or vague reference ("you mentioned something about databases") \
or only carried forward in follow-up probes but never in bridge sentences despite opportunities.
0–3 — Agent treated every turn as an isolated script read-out. No specific callbacks to what \
the candidate said. Generic or absent bridges throughout.

Full conversation transcript (format: "Candidate: ..." / "Agent: ..." alternating):
{{conversation}}

Respond with JSON only:
{
  "question_derailment": {"score": <0-10>, "derailment_examples": ["<quote if any, else empty list>"], "reasoning": "<one sentence>"},
  "context_carry_forward": {"score": <0-10>, "carry_forward_examples": ["<quote if any, else empty list>"], "reasoning": "<one sentence>"}
}"""),
}

SUPERSEDED = [
    "eval-followup-relevance",
    "eval-depth-probing",
    "eval-premature-closure",
    "eval-leading-question",
    "eval-neutral-tone",
    "eval-graceful-redirect",
    "eval-response-brevity",
    "eval-question-derailment",
    "eval-context-carry-forward",
    "eval-persona-adherence",
    "eval-followup-quality",
    "eval-guardrail-adherence",
]


def main() -> None:
    lf = Langfuse()

    if not lf.auth_check():
        print("ERROR: Langfuse auth failed. Check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL.")
        sys.exit(1)

    print(f"Connected to Langfuse. Syncing {len(EVALUATORS)} evaluator prompts...\n")

    for name, (scope, prompt_text) in EVALUATORS.items():
        try:
            result = lf.create_prompt(
                name=name,
                prompt=prompt_text,
                labels=["production"],
                commit_message="Initial grouped evaluator prompt",
            )
            print(f"  ✓ [{scope:7s}] {name}  →  Langfuse version {result.version}")
        except Exception as e:
            print(f"  ✗ [{scope:7s}] {name}  →  ERROR: {e}")

    lf.flush()
    print(f"\nDone. {len(EVALUATORS)} prompts pushed to Langfuse → Prompt Management.")
    print("\nSuperseded individual prompts (archive via Langfuse UI when ready):")
    for name in SUPERSEDED:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
