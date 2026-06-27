#!/usr/bin/env python3
"""
Unified report: session-level and candidate-level view of all eval + latency metrics.

Usage:
  python report.py --discover [--since DATE]          # list all sessions/candidates
  python report.py --user-ids UID1,UID2 [--since DATE] [--export FILE.csv]
  python report.py --sessions SID1,SID2 [--export FILE.csv]
  python report.py --since DATE [--export FILE.csv]   # all sessions since DATE

Metrics exported:
  followup_relevance, depth_probing, premature_closure, silence_protocol
  avg_llm_ttft_ms, avg_llm_duration_ms, avg_llm_tokens_per_second
  avg_tts_ttfb_ms, avg_tts_duration_ms
  stt_audio_seconds, tts_characters, tts_audio_seconds

Each candidate may have up to 4 sessions (one per round).
Session ID = room name = one round of the diagnostic interview.
Candidate key = the middle part of diagnostic_<key>_<timestamp>.
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

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

# Metrics we track
TURN_METRICS = ["followup_relevance", "depth_probing", "premature_closure", "silence_protocol"]
# Session-level quality (written once per session by evaluate.py coherence evaluator)
SESSION_QUALITY_METRICS = ["context_carry_forward", "question_derailment"]
# Session-level usage/latency (written by server.py at session end)
SESSION_METRICS = [
    "avg_llm_ttft_ms", "avg_llm_duration_ms", "avg_llm_tokens_per_second",
    "avg_tts_ttfb_ms", "avg_tts_duration_ms", "avg_stt_duration_ms",
    "total_prompt_tokens", "total_completion_tokens", "total_llm_turns",
    "stt_audio_seconds", "tts_characters", "tts_audio_seconds",
]
ALL_SESSION_METRICS = SESSION_QUALITY_METRICS + SESSION_METRICS
ALL_METRICS = TURN_METRICS + ALL_SESSION_METRICS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_field(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _detect_round(observations: list) -> str:
    for obs in sorted(observations, key=lambda o: o.start_time or ""):
        messages = _parse_json_field(obs.input)
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                m = re.search(r"active round is [`'\"]?([\w-]+)[`'\"]?", msg.get("content", ""), re.I)
                if m:
                    return m.group(1).replace("behavioural", "behavioral")
    return "unknown"


def _candidate_key(session_id: str) -> str:
    """Extract candidate identifier from session ID."""
    m = re.match(r"diagnostic_(.+)_(\d{10,})$", session_id)
    return m.group(1) if m else session_id


def _session_date(session_id: str) -> str:
    """Extract date from timestamp in session ID."""
    m = re.match(r".*_(\d{10,})$", session_id)
    if m:
        ts_ms = int(m.group(1))
        ts_s = ts_ms / 1000 if ts_ms > 1e12 else ts_ms
        try:
            return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    return ""


def _get_prompt_version(trace) -> str:
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


def _fmt(val, decimals=1) -> str:
    if val is None:
        return "-"
    if isinstance(val, float) and val != val:  # NaN
        return "-"
    return f"{val:.{decimals}f}"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_sessions(
    lf: Langfuse,
    from_start_time: datetime | None = None,
    target_sessions: list[str] | None = None,
    target_user_ids: list[str] | None = None,
    fetch_limit: int = 200,
) -> list[dict]:
    """
    Return a list of session dicts:
      {session_id, candidate_key, user_id, round, date, prompt_version,
       trace_ids: [str], observations: [obs]}
    """
    all_obs: list = []
    cursor = None
    while len(all_obs) < fetch_limit:
        kwargs: dict = {
            "type": "GENERATION",
            "fields": "io",
            "limit": min(100, fetch_limit - len(all_obs)),
        }
        if from_start_time:
            kwargs["from_start_time"] = from_start_time
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

    # Group by trace_id
    groups_by_trace: dict[str, list] = defaultdict(list)
    for o in all_obs:
        if o.input and o.output:
            groups_by_trace[o.trace_id].append(o)

    if not groups_by_trace:
        return []

    # Resolve session_id + user_id + round for each trace
    sessions: dict[str, dict] = {}  # session_id → session dict
    print(f"  Resolving {len(groups_by_trace)} traces...", flush=True)

    for trace_id, obs_list in groups_by_trace.items():
        try:
            trace = lf.api.trace.get(trace_id)
        except Exception as e:
            print(f"    WARN: trace {trace_id[:12]}: {e}")
            continue

        sid = trace.session_id or trace_id
        uid = trace.user_id or "anonymous"

        # Apply filters early to avoid unnecessary work
        if target_sessions and sid not in target_sessions:
            continue
        if target_user_ids and uid not in target_user_ids:
            continue

        pv = _get_prompt_version(trace)

        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "candidate_key": _candidate_key(sid),
                "user_id": uid,
                "round": _detect_round(obs_list),
                "date": _session_date(sid),
                "prompt_version": pv,
                "trace_ids": [],
                "observations": [],
                "_obs_ids": set(),
            }

        sessions[sid]["trace_ids"].append(trace_id)
        existing = sessions[sid]["_obs_ids"]
        for o in obs_list:
            if o.id not in existing:
                sessions[sid]["observations"].append(o)
                existing.add(o.id)

    return list(sessions.values())


def fetch_scores_for_session(lf: Langfuse, session: dict) -> dict[str, list]:
    """
    Fetch all scores for a session by querying per-trace.
    Returns {metric_name: [float]} — one value per score entry.
    Paginates to avoid the 100-score API cap silently truncating results.
    """
    scores: dict[str, list] = defaultdict(list)

    # Probing quality / silence scores — written with trace_id by evaluate.py
    for tid in session["trace_ids"]:
        try:
            cursor = None
            while True:
                kwargs: dict = {"trace_id": tid, "limit": 100}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = lf.api.scores.get_many(**kwargs)
                for s in resp.data:
                    if s.value is not None:
                        scores[s.name].append(float(s.value))
                cursor = getattr(resp.meta, "cursor", None)
                if not cursor:
                    break
        except Exception as e:
            print(f"    WARN: scores for trace {tid[:12]}: {e}")

    # Session-level usage scores — written with trace_id=room_name by server.py
    try:
        cursor = None
        while True:
            kwargs = {"session_id": session["session_id"], "limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            resp = lf.api.scores.get_many(**kwargs)
            for s in resp.data:
                # Only include session-level metrics (avoid double-counting trace scores)
                if s.name in SESSION_METRICS and s.value is not None:
                    scores[s.name].append(float(s.value))
            cursor = getattr(resp.meta, "cursor", None)
            if not cursor:
                break
    except Exception:
        pass

    return dict(scores)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _avg(values: list) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def build_session_row(session: dict, scores: dict[str, list]) -> dict:
    turns = len(set(o.id for o in session["observations"]))
    row: dict = {
        "session_id": session["session_id"],
        "candidate_key": session["candidate_key"],
        "user_id": session["user_id"],
        "round": session["round"],
        "date": session["date"],
        "prompt_version": session["prompt_version"],
        "turns": turns,
    }
    for metric in ALL_METRICS:
        vals = scores.get(metric, [])
        if metric in TURN_METRICS + SESSION_QUALITY_METRICS:
            # average across all values (per-turn or single session score)
            row[metric] = _avg(vals)
        else:
            # session-level usage/latency: take first/only value
            row[metric] = vals[0] if vals else None
    return row


def build_candidate_rows(session_rows: list[dict]) -> list[dict]:
    """Aggregate session rows by candidate_key → one row per candidate."""
    by_candidate: dict[str, list] = defaultdict(list)
    for r in session_rows:
        by_candidate[r["candidate_key"]].append(r)

    rows = []
    for ckey, sessions in sorted(by_candidate.items()):
        rounds = [s["round"] for s in sessions]
        user_id = sessions[0]["user_id"]
        row: dict = {
            "candidate_key": ckey,
            "user_id": user_id,
            "rounds": ",".join(sorted(rounds)),
            "total_sessions": len(sessions),
            "dates": ",".join(sorted(set(s["date"][:10] for s in sessions if s["date"]))),
        }
        total_turns = sum(s["turns"] for s in sessions)
        row["total_turns"] = total_turns

        for metric in ALL_METRICS:
            all_vals = [s[metric] for s in sessions if s[metric] is not None]
            if metric in ("tts_characters", "stt_audio_seconds", "tts_audio_seconds"):
                row[metric] = sum(v for v in all_vals) if all_vals else None
            else:
                row[metric] = _avg(all_vals)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

QUALITY_COLS = ["followup_relevance", "depth_probing", "premature_closure", "silence_protocol",
                "context_carry_forward", "question_derailment"]
LATENCY_COLS = ["avg_llm_ttft_ms", "avg_llm_duration_ms", "avg_tts_ttfb_ms", "avg_tts_duration_ms",
                "avg_stt_duration_ms"]
TOKEN_COLS = ["total_prompt_tokens", "total_completion_tokens", "total_llm_turns"]
USAGE_COLS = ["stt_audio_seconds", "tts_characters", "tts_audio_seconds"]


def print_session_table(rows: list[dict]) -> None:
    if not rows:
        print("  No sessions found.")
        return

    hdr = f"{'SESSION ID':<50} {'CANDIDATE':<22} {'USER':<22} {'ROUND':<18} {'DATE':<17} {'V':<7} {'TURNS':<6}"
    for c in QUALITY_COLS + LATENCY_COLS + TOKEN_COLS + USAGE_COLS:
        hdr += f" {c[:14]:<14}"
    print(hdr)
    print("-" * (len(hdr) + 20))

    for r in sorted(rows, key=lambda x: (x["candidate_key"], x["date"])):
        line = (
            f"{r['session_id']:<50} "
            f"{r['candidate_key'][:20]:<22} "
            f"{str(r['user_id'])[:20]:<22} "
            f"{r['round']:<18} "
            f"{r['date']:<17} "
            f"{r['prompt_version']:<7} "
            f"{r['turns']:<6}"
        )
        for c in QUALITY_COLS:
            line += f" {_fmt(r.get(c)):<14}"
        for c in LATENCY_COLS:
            line += f" {_fmt(r.get(c), 0):<14}"
        for c in TOKEN_COLS:
            line += f" {_fmt(r.get(c), 0):<14}"
        for c in USAGE_COLS:
            line += f" {_fmt(r.get(c)):<14}"
        print(line)


def print_candidate_table(rows: list[dict]) -> None:
    if not rows:
        print("  No candidates found.")
        return

    hdr = f"{'CANDIDATE':<22} {'USER ID':<25} {'ROUNDS':<40} {'SESS':<5} {'TURNS':<6}"
    for c in QUALITY_COLS + LATENCY_COLS + TOKEN_COLS + USAGE_COLS:
        hdr += f" {c[:14]:<14}"
    print(hdr)
    print("-" * (len(hdr) + 20))

    for r in rows:
        line = (
            f"{r['candidate_key'][:20]:<22} "
            f"{str(r['user_id'])[:23]:<25} "
            f"{r['rounds'][:38]:<40} "
            f"{r['total_sessions']:<5} "
            f"{r['total_turns']:<6}"
        )
        for c in QUALITY_COLS:
            line += f" {_fmt(r.get(c)):<14}"
        for c in LATENCY_COLS:
            line += f" {_fmt(r.get(c), 0):<14}"
        for c in TOKEN_COLS:
            line += f" {_fmt(r.get(c), 0):<14}"
        for c in USAGE_COLS:
            line += f" {_fmt(r.get(c)):<14}"
        print(line)


def print_discover_table(sessions: list[dict]) -> None:
    print(f"\n{'SESSION ID':<55} {'CANDIDATE KEY':<28} {'USER ID':<30} {'ROUND':<20} {'DATE':<17} {'TRACES'}")
    print("-" * 160)
    by_candidate: dict[str, list] = defaultdict(list)
    for s in sessions:
        by_candidate[s["candidate_key"]].append(s)
    for ckey, items in sorted(by_candidate.items()):
        for s in sorted(items, key=lambda x: x["date"]):
            print(
                f"{s['session_id']:<55} "
                f"{s['candidate_key'][:26]:<28} "
                f"{str(s['user_id'])[:28]:<30} "
                f"{s['round']:<20} "
                f"{s['date']:<17} "
                f"{len(s['trace_ids'])}"
            )
        print()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(session_rows: list[dict], candidate_rows: list[dict], filepath: str) -> None:
    path = Path(filepath)
    session_cols = (
        ["session_id", "candidate_key", "user_id", "round", "date", "prompt_version", "turns"]
        + ALL_METRICS
    )
    candidate_cols = (
        ["candidate_key", "user_id", "rounds", "total_sessions", "total_turns"]
        + ALL_METRICS
    )

    session_file = path.with_name(path.stem + "_sessions" + path.suffix)
    candidate_file = path.with_name(path.stem + "_candidates" + path.suffix)

    def write_csv(rows, cols, out_path):
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                out_row = {}
                for col in cols:
                    val = row.get(col)
                    out_row[col] = "" if val is None else (f"{val:.4f}" if isinstance(val, float) else val)
                writer.writerow(out_row)
        print(f"  Written: {out_path}  ({len(rows)} rows)")

    write_csv(session_rows, session_cols, session_file)
    write_csv(candidate_rows, candidate_cols, candidate_file)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Report on diagnostic interview eval + latency metrics.")
    parser.add_argument("--discover", action="store_true", help="List all sessions/candidates without scoring")
    parser.add_argument("--since", help="Filter sessions from this date, e.g. 2026-06-14")
    parser.add_argument("--user-ids", help="Comma-separated Langfuse user IDs to filter")
    parser.add_argument("--sessions", help="Comma-separated session IDs to filter")
    parser.add_argument("--limit", type=int, default=200, help="Max observations to fetch (default 200)")
    parser.add_argument("--export", help="Base path for CSV export, e.g. report.csv → report_sessions.csv + report_candidates.csv")
    args = parser.parse_args()

    lf = Langfuse()
    if not lf.auth_check():
        print("ERROR: Langfuse auth failed.")
        sys.exit(1)

    from_start_time: datetime | None = None
    if args.since:
        from_start_time = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    target_sessions: list[str] | None = None
    if args.sessions:
        target_sessions = [s.strip() for s in args.sessions.split(",")]
        # Auto-derive from_start_time from the earliest session timestamp so bulk
        # fetch starts from that date rather than fetching only the most-recent obs
        if not from_start_time:
            timestamps = []
            for sid in target_sessions:
                m = re.match(r".*_(\d{10,})$", sid)
                if m:
                    ts_ms = int(m.group(1))
                    ts_s = ts_ms / 1000 if ts_ms > 1e12 else ts_ms
                    timestamps.append(ts_s)
            if timestamps:
                from_start_time = datetime.fromtimestamp(min(timestamps) - 3600, tz=timezone.utc)
        # Use a larger pool to ensure we get past any noise sessions in the same window
        if args.limit == 200:
            args.limit = 2000

    target_user_ids: list[str] | None = None
    if args.user_ids:
        target_user_ids = [u.strip() for u in args.user_ids.split(",")]

    print(f"\nFetching sessions{' since ' + args.since if args.since else ''}...")
    sessions = fetch_all_sessions(
        lf,
        from_start_time=from_start_time,
        target_sessions=target_sessions,
        target_user_ids=target_user_ids,
        fetch_limit=args.limit,
    )

    if not sessions:
        print("No sessions found matching the given filters.")
        return

    print(f"  Found {len(sessions)} sessions across "
          f"{len(set(s['candidate_key'] for s in sessions))} candidates\n")

    if args.discover:
        print_discover_table(sessions)
        print(f"\nTip: use --user-ids UID1,UID2 or --sessions SID1,SID2 to generate a scored report.")
        return

    # Fetch scores for each session
    print("Fetching scores per session...")
    session_rows = []
    for s in sessions:
        print(f"  {s['session_id'][:48]}  [{s['round']}]", end="  ", flush=True)
        scores = fetch_scores_for_session(lf, s)
        score_count = sum(len(v) for v in scores.values())
        print(f"{score_count} scores")
        session_rows.append(build_session_row(s, scores))

    candidate_rows = build_candidate_rows(session_rows)

    lf.flush()

    # Print session-level table
    print(f"\n{'='*80}")
    print("SESSION-LEVEL REPORT")
    print(f"{'='*80}\n")
    print_session_table(session_rows)

    # Print candidate-level table
    print(f"\n{'='*80}")
    print("CANDIDATE-LEVEL REPORT")
    print(f"{'='*80}\n")
    print_candidate_table(candidate_rows)

    # Export
    if args.export:
        print(f"\nExporting to CSV...")
        export_csv(session_rows, candidate_rows, args.export)
    else:
        print(f"\nTip: add --export report.csv to save both tables to CSV files.")


if __name__ == "__main__":
    main()
