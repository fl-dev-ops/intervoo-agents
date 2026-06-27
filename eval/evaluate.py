#!/usr/bin/env python3
"""
Batch evaluation script for diagnostic interview sessions.

Run from the eval/ directory:
    python evaluate.py                                          # last 50 sessions, all evals
    python evaluate.py --since 2026-06-15 --per-round 5        # 5 per round from last week
    python evaluate.py --evals probing-quality                  # only probing metrics
    python evaluate.py --prompt-version v5                      # filter by prompt version
    python evaluate.py --session-id <room-id>                   # single session

--evals accepts short names: probing-quality, conduct, coherence
  (or full names: eval-probing-quality, eval-interview-conduct, eval-session-coherence)

Scores are written back to Langfuse and appear in the Scores tab per session.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Load agent/.env before importing any SDK
EVAL_DIR = Path(__file__).resolve().parent
AGENT_ROOT = EVAL_DIR.parent / "agent"
env_path = AGENT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)

from langfuse import Langfuse  # noqa: E402
from openai import OpenAI  # noqa: E402

HAIKU_MODEL: str  # set in main() after env is loaded

# agent_id → prompt version derived from agents.json (fallback for pre-deployment traces)
def _build_agent_version_map(agent_root: Path) -> dict[str, str]:
    try:
        cfg = json.loads((agent_root / "config" / "agents.json").read_text())
        result = {}
        for agent_id, entry in cfg.get("agents", {}).items():
            stem = Path(entry.get("prompt_url", "")).stem
            if stem.startswith("v") and stem[1:].isdigit():
                result[agent_id] = stem
        return result
    except Exception:
        return {}

_AGENT_VERSION_MAP: dict[str, str] = _build_agent_version_map(AGENT_ROOT)

# Words that indicate the candidate produced no real answer
_FILLER_ONLY = frozenset({"um", "uh", "mm", "hmm", "ah", "oh", "err", "erm"})
# Phrases that indicate the agent followed the silence protocol
_SILENCE_PROTOCOL_PHRASES = [
    "take your time", "no rush", "there's no rush", "let me rephrase",
    "let me ask that differently", "let's move on", "let's continue",
    "how about we", "would you like", "take a moment", "let me try",
]

TURN_EVALS = ["eval-probing-quality", "eval-interview-conduct"]
SESSION_EVALS = ["eval-session-coherence"]

# Short-name aliases for --evals flag
EVAL_ALIASES = {
    "probing-quality":    "eval-probing-quality",
    "probing":            "eval-probing-quality",
    "conduct":            "eval-interview-conduct",
    "interview-conduct":  "eval-interview-conduct",
    "coherence":          "eval-session-coherence",
    "session-coherence":  "eval-session-coherence",
}

ALL_ROUNDS = ["screening", "behavioral", "technical-thinking", "career-readiness"]

TURN_DIMENSIONS = {
    "eval-probing-quality": ["followup_relevance", "depth_probing", "premature_closure"],
    "eval-interview-conduct": ["neutral_tone", "leading_question", "graceful_redirect", "response_brevity"],
}
SESSION_DIMENSIONS = {
    "eval-session-coherence": ["question_derailment", "context_carry_forward"],
}


# ---------------------------------------------------------------------------
# Turn extraction
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict) and "text" in c
        )
    return str(content)


def _parse_json_field(value):
    """Parse a field that may be a JSON string, dict, or list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _detect_round(observations: list) -> str:
    """Extract current_round from the system prompt in any observation's input."""
    for obs in sorted(observations, key=lambda o: o.start_time or ""):
        messages = _parse_json_field(obs.input)
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                m = re.search(r"active round is [`'\"]?([\w-]+)[`'\"]?", msg.get("content", ""), re.I)
                if m:
                    # normalise british/american spelling
                    return m.group(1).replace("behavioural", "behavioral")
    return "unknown"


def _is_non_answer(text: str) -> bool:
    """True if the candidate message is empty or contains only filler words."""
    words = [w.strip(".,!?") for w in text.lower().split()]
    return len(words) == 0 or all(w in _FILLER_ONLY for w in words)


def _score_silence_handling(agent_response: str) -> tuple[int, str]:
    """
    Heuristic: did the agent follow the silence protocol after a non-answer?
    Returns (score 0-10, reasoning).
    """
    text = agent_response.lower()
    followed = any(phrase in text for phrase in _SILENCE_PROTOCOL_PHRASES)
    has_new_question = "?" in agent_response
    if followed:
        return 10, "Agent used check-in or re-prompt phrase before advancing."
    elif has_new_question:
        return 2, "Agent asked a new question without any silence protocol check-in."
    else:
        return 5, "Agent response unclear — may or may not have followed silence protocol."


def extract_turns(observations: list) -> list[dict]:
    """
    Return list of {candidate_msg, agent_response, observation_id, trace_id}
    sorted by start_time. One entry per GENERATION observation with input+output.
    """
    turns = []
    for obs in sorted(observations, key=lambda o: o.start_time or ""):
        if not obs.input or not obs.output:
            continue

        messages = _parse_json_field(obs.input)
        if isinstance(messages, dict):
            messages = messages.get("messages", [])
        if not isinstance(messages, list):
            continue

        # Last user message = candidate's turn
        candidate_msg = None
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                candidate_msg = _extract_text(msg.get("content", "")).strip()
                break

        if not candidate_msg:
            continue

        # Agent response from output
        output = _parse_json_field(obs.output)
        if isinstance(output, dict):
            choices = output.get("choices", [])
            if choices:
                output = choices[0].get("message", {}).get("content", "")
            else:
                # {"role": "assistant", "content": "..."}
                output = output.get("content", str(output))
        agent_response = str(output).strip()

        if not agent_response:
            continue

        turns.append({
            "candidate_msg": candidate_msg,
            "agent_response": agent_response,
            "observation_id": obs.id,
            "trace_id": obs.trace_id,
        })

    return turns


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def call_haiku(openrouter: OpenAI, prompt_text: str) -> dict:
    response = openrouter.chat.completions.create(
        model=HAIKU_MODEL,
        messages=[{"role": "user", "content": prompt_text}],
        temperature=0,
        max_tokens=512,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _write_dim_scores(lf: Langfuse, result: dict, dimensions: list,
                      trace_id: str, observation_id: str | None = None) -> dict[str, float]:
    written = {}
    for dim in dimensions:
        dim_data = result.get(dim, {})
        if dim_data is None:
            continue
        if isinstance(dim_data, dict):
            score_val = dim_data.get("score")
            reasoning = dim_data.get("reasoning", "")
        else:
            score_val = dim_data
            reasoning = ""

        if score_val is None:
            continue

        score_id = f"{(observation_id or trace_id)[:16]}-{dim}"
        lf.create_score(
            score_id=score_id,
            trace_id=trace_id,
            observation_id=observation_id,  # links score to the exact turn
            name=dim,
            value=float(score_val),
            data_type="NUMERIC",
            comment=str(reasoning) if reasoning else None,
        )
        written[dim] = float(score_val)
    return written


def score_turns(lf, openrouter, prompts_cache, session_id, turns,
                active_evals: list[str] | None = None) -> dict[str, list]:
    """Run turn-level evaluators for every turn. Returns {dim: [scores]}."""
    evals_to_run = [e for e in TURN_EVALS if e in (active_evals or TURN_EVALS)]
    dim_scores: dict[str, list] = defaultdict(list)

    for turn in turns:
        # Deterministic silence-protocol check — no LLM call needed
        if _is_non_answer(turn["candidate_msg"]):
            if active_evals is None or "silence_protocol" in active_evals:
                score, reasoning = _score_silence_handling(turn["agent_response"])
                try:
                    lf.create_score(
                        score_id=f"{turn['observation_id'][:16]}-silence_protocol",
                        trace_id=turn["trace_id"],
                        observation_id=turn["observation_id"],
                        name="silence_protocol",
                        value=float(score),
                        data_type="NUMERIC",
                        comment=f"Non-answer detected. {reasoning}",
                    )
                    dim_scores["silence_protocol"].append(float(score))
                except Exception as e:
                    print(f"    WARN [silence_protocol] obs={turn['observation_id'][:8]}: {e}")

        for eval_name in evals_to_run:
            rendered = prompts_cache[eval_name].compile(
                input=turn["candidate_msg"],
                output=turn["agent_response"],
            )
            try:
                result = call_haiku(openrouter, rendered)
            except Exception as e:
                print(f"    WARN [{eval_name}] obs={turn['observation_id'][:8]}: {e}")
                continue

            # Log turn classification for probing-quality so you can see the distribution
            if eval_name == "eval-probing-quality":
                turn_type = result.get("turn_type", "unknown")
                print(f"      [{turn_type}] obs={turn['observation_id'][:8]}")

            written = _write_dim_scores(
                lf, result, TURN_DIMENSIONS[eval_name],
                trace_id=turn["trace_id"],
                observation_id=turn["observation_id"],
            )
            for dim, val in written.items():
                dim_scores[dim].append(val)

    return dim_scores


def score_session_coherence(lf, openrouter, prompts_cache, session_id, turns,
                            active_evals: list[str] | None = None) -> dict[str, float]:
    """Run session-level evaluator once. Returns {dim: score}."""
    if not turns:
        return {}
    if active_evals is not None and "eval-session-coherence" not in active_evals:
        return {}

    conversation = "\n".join(
        f"Candidate: {t['candidate_msg']}\nAgent: {t['agent_response']}"
        for t in turns
    )

    rendered = prompts_cache["eval-session-coherence"].compile(conversation=conversation)
    try:
        result = call_haiku(openrouter, rendered)
    except Exception as e:
        print(f"    WARN [eval-session-coherence] session={session_id[:16]}: {e}")
        return {}

    # Use the first turn's trace_id for session-level scores
    trace_id = turns[0]["trace_id"]
    return _write_dim_scores(
        lf, result, SESSION_DIMENSIONS["eval-session-coherence"],
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Session discovery (observation-centric — avoids slow trace.list API)
# ---------------------------------------------------------------------------

def fetch_trace_groups(lf: Langfuse, limit: int,
                       target_session_id: str | None = None,
                       from_start_time: datetime | None = None) -> list[dict]:
    """
    Return up to `limit` trace groups, each:
      {trace_id, session_id, prompt_version, round, observations}

    Fetches recent GENERATION observations (optionally from a start date),
    groups by trace_id, then resolves session_id + round via trace.get().
    """
    if target_session_id:
        obs_resp = lf.api.observations.get_many(
            type="GENERATION", fields="io", limit=500,
            from_start_time=from_start_time,
        )
        all_obs = [o for o in obs_resp.data if o.input and o.output]
        groups_by_trace: dict[str, list] = defaultdict(list)
        for o in all_obs:
            groups_by_trace[o.trace_id].append(o)

        result = []
        for trace_id, obs_list in groups_by_trace.items():
            try:
                trace = lf.api.trace.get(trace_id)
            except Exception:
                continue
            if trace.session_id == target_session_id:
                pv = _get_prompt_version(trace)
                result.append({
                    "trace_id": trace_id,
                    "session_id": trace.session_id,
                    "prompt_version": pv,
                    "round": _detect_round(obs_list),
                    "observations": obs_list,
                })
        return result

    # General case: fetch recent GENERATION obs, group into traces
    all_obs: list = []
    cursor = None
    max_obs = limit * 20  # generous pool — many may be filtered by round
    while len(all_obs) < max_obs:
        kwargs = {
            "type": "GENERATION", "fields": "io",
            "limit": min(100, max_obs - len(all_obs)),
            "from_start_time": from_start_time,
        }
        if cursor:
            kwargs["cursor"] = cursor
        resp = lf.api.observations.get_many(**kwargs)
        batch = list(resp.data)
        if not batch:
            break
        all_obs.extend(batch)
        cursor = getattr(resp.meta, "cursor", None)
        if not cursor:
            break

    # Group by trace_id (keep only obs with input+output)
    groups_by_trace: dict[str, list] = defaultdict(list)
    for o in all_obs:
        if o.input and o.output:
            groups_by_trace[o.trace_id].append(o)

    if not groups_by_trace:
        return []

    # Resolve session_id + round for each trace, up to limit
    result = []
    for trace_id in list(groups_by_trace.keys())[:limit]:
        try:
            trace = lf.api.trace.get(trace_id)
        except Exception as e:
            print(f"  WARN: could not fetch trace {trace_id[:16]}: {e}")
            continue
        obs_list = groups_by_trace[trace_id]
        pv = _get_prompt_version(trace)
        result.append({
            "trace_id": trace_id,
            "session_id": trace.session_id or trace_id,
            "prompt_version": pv,
            "round": _detect_round(obs_list),
            "observations": obs_list,
        })

    return result


def _get_prompt_version(trace) -> str:
    """Extract prompt_version from trace metadata attributes.
    Falls back to deriving from agent_id via agents.json when not explicitly set."""
    meta = getattr(trace, "metadata", {}) or {}
    attrs = meta.get("attributes", {}) if isinstance(meta, dict) else {}
    # Prefer explicitly set prompt_version (new sessions after deployment)
    v = attrs.get("prompt_version") or meta.get("prompt_version")
    if v:
        return str(v)
    # Fall back: derive from agent_id via agents.json mapping
    agent_id = attrs.get("agent_id") or meta.get("agent_id")
    if agent_id and agent_id in _AGENT_VERSION_MAP:
        return _AGENT_VERSION_MAP[agent_id]
    return "unknown"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Score diagnostic sessions via Langfuse + OpenRouter.")
    parser.add_argument("--limit", type=int, default=50, help="Max traces to fetch (default 50; overridden by --per-round)")
    parser.add_argument("--per-round", type=int, help="Cap sessions per round (e.g. 5 → up to 20 total across 4 rounds)")
    parser.add_argument("--since", help="Only score sessions on or after this date, e.g. 2026-06-15")
    parser.add_argument("--rounds", help="Comma-separated round filter, e.g. screening,behavioral (default: all 4)")
    parser.add_argument("--evals", help="Comma-separated evaluators to run, e.g. probing-quality (default: all)")
    parser.add_argument("--prompt-version", help="Only score sessions with this prompt version, e.g. v5")
    parser.add_argument("--session-id", help="Score a single session by ID")
    args = parser.parse_args()

    global HAIKU_MODEL
    HAIKU_MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-haiku-4-5-20251001")

    # --- Resolve --since ---
    from_start_time: datetime | None = None
    if args.since:
        from_start_time = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # --- Resolve --rounds ---
    round_filter: list[str] | None = None
    if args.rounds:
        round_filter = [r.strip() for r in args.rounds.split(",")]

    # --- Resolve --evals to full prompt names ---
    active_evals: list[str] | None = None
    if args.evals:
        active_evals = []
        for token in args.evals.split(","):
            token = token.strip()
            resolved = EVAL_ALIASES.get(token, token)  # fallback = token itself (already full name)
            active_evals.append(resolved)

    # --- Adjust fetch limit when --per-round is used ---
    fetch_limit = args.limit
    if args.per_round:
        num_rounds = len(round_filter) if round_filter else len(ALL_ROUNDS)
        fetch_limit = args.per_round * num_rounds * 4  # generous pool for filtering

    lf = Langfuse()
    if not lf.auth_check():
        print("ERROR: Langfuse auth failed. Check LANGFUSE_* env vars.")
        sys.exit(1)

    openrouter = OpenAI(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api/v1",
    )
    print(f"Eval model: {HAIKU_MODEL}")
    if from_start_time:
        print(f"Date filter: sessions from {args.since} onwards")
    if round_filter:
        print(f"Round filter: {', '.join(round_filter)}")
    if active_evals:
        print(f"Evals: {', '.join(active_evals)}")

    # Pre-fetch only the evaluator prompts we'll actually use
    print("\nLoading evaluator prompts...")
    prompts_cache = {}
    evals_needed = active_evals if active_evals else (TURN_EVALS + SESSION_EVALS)
    for name in evals_needed:
        if name in TURN_DIMENSIONS or name in SESSION_DIMENSIONS:
            prompts_cache[name] = lf.get_prompt(name, label="production")
            print(f"  ✓ {name}")

    # Discover sessions/traces
    print(f"\nDiscovering sessions (fetch_limit={fetch_limit})...")
    trace_groups = fetch_trace_groups(
        lf, limit=fetch_limit,
        target_session_id=args.session_id,
        from_start_time=from_start_time,
    )
    print(f"  Found {len(trace_groups)} raw traces")

    # Apply per-round cap: keep up to --per-round sessions per round type
    if args.per_round:
        counts_per_round: dict[str, int] = defaultdict(int)
        filtered = []
        target_rounds = set(round_filter) if round_filter else set(ALL_ROUNDS)
        for group in trace_groups:
            r = group["round"]
            if r not in target_rounds:
                continue
            if counts_per_round[r] < args.per_round:
                filtered.append(group)
                counts_per_round[r] += 1
        trace_groups = filtered
        print(f"  After per-round cap ({args.per_round}/round): {len(trace_groups)} sessions")
        for r, n in sorted(counts_per_round.items()):
            print(f"    {r:<22} {n}")
    elif round_filter:
        trace_groups = [g for g in trace_groups if g["round"] in round_filter]
        print(f"  After round filter: {len(trace_groups)} sessions")

    print()

    # Score each session
    all_scores: dict[str, list] = defaultdict(list)
    scored_count = 0
    skipped_count = 0

    for group in trace_groups:
        session_id = group["session_id"]
        prompt_version = group["prompt_version"]
        round_name = group["round"]

        if args.prompt_version and prompt_version != args.prompt_version:
            skipped_count += 1
            continue

        turns = extract_turns(group["observations"])
        if not turns:
            skipped_count += 1
            continue

        print(f"Session {session_id}  version={prompt_version}  round={round_name}  turns={len(turns)}")

        turn_dim_scores = score_turns(lf, openrouter, prompts_cache, session_id, turns, active_evals)
        session_dim_scores = score_session_coherence(lf, openrouter, prompts_cache, session_id, turns, active_evals)

        session_summary = {}
        for dim, vals in turn_dim_scores.items():
            avg = sum(vals) / len(vals)
            session_summary[dim] = avg
            all_scores[dim].extend(vals)
        for dim, val in session_dim_scores.items():
            session_summary[dim] = val
            all_scores[dim].append(val)

        for dim, avg in sorted(session_summary.items()):
            print(f"  {dim:<32} {avg:.1f}")

        scored_count += 1

    lf.flush()

    # Aggregate summary
    version_str = f"prompt_version={args.prompt_version}" if args.prompt_version else "all versions"
    print(f"\n{'='*60}")
    print(f"AGGREGATE  {scored_count} sessions scored  |  {skipped_count} skipped  |  {version_str}")
    print(f"{'='*60}")
    for dim, vals in sorted(all_scores.items()):
        print(f"  {dim:<32} {sum(vals)/len(vals):.2f}  (n={len(vals)})")


if __name__ == "__main__":
    main()
