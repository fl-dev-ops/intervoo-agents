"""Fixed generation and evaluation harness for the interview behavior policy.

Each evaluation case is a frozen decision point: a partial interview that ends on a
candidate answer. The composed prompt generates exactly one next interviewer turn, and a
judge scores those turns against Vasanth's observed move policy.
"""

import argparse
import asyncio
import json
import os
import statistics
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent
MARKER = "{{BEHAVIOR_POLICY}}"

WEIGHTS = {
    "wrong_answer_handling": 0.30,
    "probe_selection_fidelity": 0.25,
    "no_reveal_no_lead": 0.20,
    "advance_discipline": 0.15,
    "spoken_style_fidelity": 0.10,
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CriterionScore(StrictModel):
    score: float = Field(ge=0, le=1)
    rationale: str


class Criteria(StrictModel):
    wrong_answer_handling: CriterionScore
    probe_selection_fidelity: CriterionScore
    no_reveal_no_lead: CriterionScore
    advance_discipline: CriterionScore
    spoken_style_fidelity: CriterionScore


class CriterionJudgement(StrictModel):
    applicable: bool
    score: float = Field(ge=0, le=1)
    rationale: str


class CaseCriteria(StrictModel):
    wrong_answer_handling: CriterionJudgement
    probe_selection_fidelity: CriterionJudgement
    no_reveal_no_lead: CriterionJudgement
    advance_discipline: CriterionJudgement
    spoken_style_fidelity: CriterionJudgement


class CaseJudgement(StrictModel):
    criteria: CaseCriteria
    violated_must_not: bool
    critical_issue: str


class CaseScore(StrictModel):
    case_id: str
    score: float = Field(ge=0, le=1)
    violated_must_not: bool
    critical_issue: str


class Evaluation(StrictModel):
    score: float = Field(ge=0, le=1)
    score_stdev: float
    replicates: list[float]
    criteria: Criteria
    case_scores: list[CaseScore]
    strengths: list[str]
    failures: list[str]
    next_experiment: str


def compose_prompt(policy_path: Path) -> str:
    """Rebuild the full interviewer prompt from the frozen shell plus the mutable policy."""
    shell = (ROOT / "prompt_shell.md").read_text(encoding="utf-8")
    if shell.count(MARKER) != 1:
        raise RuntimeError(f"prompt_shell.md must contain {MARKER} exactly once")
    policy = policy_path.read_text(encoding="utf-8").strip()
    if not policy:
        raise RuntimeError("behavior policy is empty")
    prompt = shell.replace(MARKER, policy)
    # The runtime fills these; the benchmark holds them fixed so only the policy varies.
    # The interview plan is no longer a placeholder — it is built at runtime by
    # build_interview_plan — so each case supplies its own active question instead.
    return (
        prompt.replace("{user_name}", "the candidate")
        .replace("{resume_markdown}", "")
        .replace("{additional_context}", '{"role": "Software Engineer"}')
    )


def render_case(case: dict[str, Any]) -> str:
    question = case["active_question"]
    lines = [
        f"Candidate: {case['candidate']}",
        f"Interview phase: {case['phase']}",
        "",
        "Active question from the plan:",
        f"  id: {question['id']}",
        f"  surface: {question['surface']} ({question['answer_mode']} answer)",
        f"  question: {question['asked']}",
    ]
    if "starter_code" in question:
        lines.append("  code shown on the candidate's screen:")
        lines.extend(f"    {line}" for line in question["starter_code"].split("\n"))
    lines += ["", "Conversation so far:"]
    for turn in case["conversation"]:
        speaker = "You" if turn["speaker"] == "interviewer" else "Candidate"
        lines.append(f"  {speaker}: {turn['text']}")
    lines += [
        "",
        "Produce only your next spoken turn, exactly as it should be read aloud. "
        "No stage directions, no tool syntax, no explanation of your choice.",
    ]
    return "\n".join(lines)


async def generate_turn(
    client: AsyncOpenAI, model: str, prompt: str, case: dict[str, Any], seed: int
) -> str:
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        seed=seed,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": render_case(case)},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"Empty generation for {case['id']}")
    return content.strip()


JUDGE_INSTRUCTIONS = """
You are evaluating whether an interviewer prompt reproduces Vasanth's observed probe-and-depth
decisions. Each case is a frozen decision point; you are scoring the single next turn the
prompt produced. Judge the decision, not the polish.

REFERENCE BEHAVIOR
{reference}

You are scoring ONE case. Every criterion score is a quality rating from 0.0 to 1.0 for this
case alone, where 1.0 means the turn handled that dimension exactly as the reference describes.
These are not weights; do not copy any number from these instructions into a score field.

Mark a criterion `applicable: false` when this case does not exercise it — for example, a case
with no incorrect answer does not exercise wrong_answer_handling. A non-applicable criterion is
excluded from aggregation rather than scored as a failure, so do not mark something applicable
just to have an opinion about it. spoken_style_fidelity is always applicable.

CRITERIA
- wrong_answer_handling: keeps an unresolved wrong answer live through discrepancy, a
  counter-question, or one constrained hint; never resolves it by assertion; never lets an
  acknowledgement read as confirmation of an incorrect answer.
- probe_selection_fidelity: picks the move the corpus associates with this answer type
  (definition -> example, vague -> exact scenario, unsupported claim -> justification,
  optimization -> cost or simpler alternative, contradiction -> reconcile, unfinished
  sentence -> minimal continuation cue, strong and reasoned -> harder variation or move on).
- no_reveal_no_lead: does not state the answer, teach the underlying rule, complete the
  candidate's reasoning, or telegraph the direction of the correct answer.
- advance_discipline: does not advance while a wrong answer stands unaddressed, and does not
  grind after a rescue attempt has already failed. Both failure directions are penalised.
- spoken_style_fidelity: one question, TTS-safe plain speech, no markdown or spoken code,
  length appropriate to the move.

Each rationale must quote the specific words in the turn that drove the score.

HARD PENALTIES
- Cap no_reveal_no_lead at 0.10 if the turn states the correct answer or explains the rule the
  candidate failed to produce.
- Cap wrong_answer_handling at 0.30 if an incorrect answer is accepted, confirmed, or left
  behind unaddressed.
- Cap advance_discipline at 0.35 if the turn advances to a new question while this case's
  answer is still incorrect and unresolved, or if it keeps probing a candidate who has already
  spent a rescue attempt and said they do not know.
- Cap probe_selection_fidelity at 0.40 if a probe is added to an answer that already supplied
  a concrete example and its reasoning.
- Cap spoken_style_fidelity at 0.40 if the turn stacks more than one question or contains
  markdown, code, or bracketed ids.
- Set violated_must_not when the turn does any item in this case's must_not list.

Do not reward imitation of accent, fillers, or praise words. A turn is not better because it
contains more of Vasanth's catchphrases.

THE CASE
{case}

THE TURN THE PROMPT PRODUCED
{turn}
""".strip()


async def judge_case(
    client: AsyncOpenAI,
    model: str,
    reference: str,
    case: dict[str, Any],
    turn: str,
    seed: int,
) -> CaseJudgement:
    instructions = JUDGE_INSTRUCTIONS.format(
        reference=reference, case=json.dumps(case, indent=2), turn=turn
    )
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        seed=seed,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": f"Score the turn for case {case['id']}."},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "CaseJudgement",
                "strict": True,
                "schema": CaseJudgement.model_json_schema(),
            },
        },
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"Judge returned an empty response for {case['id']}")
    return CaseJudgement.model_validate_json(content)


async def run_replicate(
    client: AsyncOpenAI,
    model: str,
    judge_model: str,
    prompt: str,
    reference: str,
    cases: list[dict[str, Any]],
    seed: int,
) -> tuple[float, dict[str, str], dict[str, CaseJudgement]]:
    """One full generate-and-judge pass. Returns (weighted score, turns, judgements)."""
    turns = await asyncio.gather(
        *(generate_turn(client, model, prompt, case, seed) for case in cases)
    )
    outputs = {case["id"]: turn for case, turn in zip(cases, turns, strict=True)}
    judgements = await asyncio.gather(
        *(
            judge_case(client, judge_model, reference, case, outputs[case["id"]], seed)
            for case in cases
        )
    )
    by_case = {case["id"]: j for case, j in zip(cases, judgements, strict=True)}

    # A criterion's score is the mean over the cases that exercise it; the total is the
    # weighted sum over criteria that at least one case exercised.
    weighted, total_weight = 0.0, 0.0
    for name, weight in WEIGHTS.items():
        scores = [
            getattr(j.criteria, name).score
            for j in by_case.values()
            if getattr(j.criteria, name).applicable
        ]
        if scores:
            weighted += (sum(scores) / len(scores)) * weight
            total_weight += weight
    score = weighted / total_weight if total_weight else 0.0
    return score, outputs, by_case


async def main_async(args: argparse.Namespace) -> None:
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY or OPENAI_API_KEY")

    prompt = compose_prompt(args.policy)
    cases = json.loads((ROOT / "evaluation_cases.json").read_text(encoding="utf-8"))
    reference = (ROOT / "vasanth_reference.md").read_text(encoding="utf-8")
    model = os.getenv("MODEL", "openai/gpt-4o")
    judge_model = os.getenv("JUDGE_MODEL", model)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
        timeout=120,
    )

    # The provider is not deterministic at temperature 0, so a single pass cannot separate a
    # real change from run-to-run noise. Replicate and report the mean with its spread.
    replicate_count = int(os.getenv("REPLICATES", "5"))
    results = await asyncio.gather(
        *(
            run_replicate(client, model, judge_model, prompt, reference, cases, seed)
            for seed in range(1, replicate_count + 1)
        )
    )
    scores = [r[0] for r in results]
    mean = statistics.fmean(scores)
    stdev = statistics.stdev(scores) if len(scores) > 1 else 0.0

    _, last_outputs, last_judgements = results[-1]
    criteria_means: dict[str, Any] = {}
    for name in WEIGHTS:
        per_replicate = [
            statistics.fmean(vals)
            for _, _, judgements in results
            if (
                vals := [
                    getattr(j.criteria, name).score
                    for j in judgements.values()
                    if getattr(j.criteria, name).applicable
                ]
            )
        ]
        criteria_means[name] = {
            "score": round(statistics.fmean(per_replicate), 4)
            if per_replicate
            else 0.0,
            "rationale": f"mean over {len(per_replicate)} replicates of the cases that exercise it",
        }

    case_scores = []
    for case in cases:
        per_replicate = [
            statistics.fmean(
                [
                    c.score
                    for c in [
                        getattr(judgements[case["id"]].criteria, n) for n in WEIGHTS
                    ]
                    if c.applicable
                ]
            )
            for _, _, judgements in results
        ]
        violations = sum(1 for _, _, j in results if j[case["id"]].violated_must_not)
        case_scores.append(
            CaseScore(
                case_id=case["id"],
                score=round(statistics.fmean(per_replicate), 4),
                violated_must_not=violations > len(results) / 2,
                critical_issue=(
                    f"must_not violated in {violations}/{len(results)} replicates: "
                    + last_judgements[case["id"]].critical_issue
                    if violations
                    else last_judgements[case["id"]].critical_issue
                ),
            )
        )

    evaluation = Evaluation(
        score=round(mean, 4),
        score_stdev=round(stdev, 4),
        replicates=[round(s, 4) for s in scores],
        criteria=Criteria.model_validate(criteria_means),
        case_scores=case_scores,
        strengths=[],
        failures=[c.critical_issue for c in case_scores if c.violated_must_not],
        next_experiment="",
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "outputs.json").write_text(
        json.dumps(last_outputs, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "score.json").write_text(
        evaluation.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "composed_prompt.md").write_text(prompt, encoding="utf-8")
    print(evaluation.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
