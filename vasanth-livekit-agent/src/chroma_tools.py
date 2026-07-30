"""Chroma-backed interview-question retrieval.

Builds the interview plan at runtime from the Chroma Cloud question bank
(collection `vasanth-questions`), sized by the candidate's experience. The count
strategy lives here so counts are deterministic rather than left to the LLM.

Experience has no dedicated field in the bank, so `difficulty_level` is used as a
proxy. Coding inventory is thin, so `coding` and `code-output` share one pool and
each bucket relaxes its filters (domain, then difficulty) to reach its target.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, MutableMapping
from typing import Any

from livekit.agents import RunContext, function_tool

logger = logging.getLogger(__name__)

_LOG = "[EXT-API:chroma]"

SUPPORTED_LANGUAGES = ("java", "javascript", "python")
DEFAULT_DOMAINS = ["react", "javascript"]

# Main-question counts per experience band (see AGENTS strategy).
_COUNTS = {
    "0-3": {"verbal": 6, "coding": 2, "machine": 3},
    "4-8": {"verbal": 5, "coding": 2, "machine": 2},
}
# difficulty_level proxy for experience.
_DIFFICULTIES = {
    "0-3": ["easy", "medium"],
    "4-8": ["medium", "hard"],
}
# question_type values per bucket. coding + code-output share one pool.
_BUCKET_TYPES = {
    "verbal": ["verbal"],
    "coding": ["coding", "code-output"],
    "machine": ["machine-coding"],
}
_BUCKET_ORDER = ["verbal", "coding", "machine"]

USERDATA_CHROMA_CLIENT = "chroma_client"
USERDATA_CHROMA_COLLECTION = "chroma_collection"

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


# --- connection ------------------------------------------------------------


def _chroma_settings() -> dict[str, str]:
    return {
        "api_key": os.getenv("CHROMA_API_KEY", ""),
        "tenant": os.getenv("CHROMA_TENANT", ""),
        "database": os.getenv("CHROMA_DATABASE", ""),
        "collection": os.getenv("CHROMA_COLLECTION", ""),
    }


def chroma_configured() -> bool:
    return all(_chroma_settings().values())


def get_cached_collection(userdata: MutableMapping[str, Any]) -> Any:
    """Return a cached Chroma collection, creating the client/collection once.

    Cached in `proc.userdata` so it is reused across jobs in the same process.
    """
    collection = userdata.get(USERDATA_CHROMA_COLLECTION)
    if collection is not None:
        return collection

    settings = _chroma_settings()
    if not all(settings.values()):
        raise ValueError(
            "Chroma not configured: set CHROMA_API_KEY, CHROMA_TENANT, "
            "CHROMA_DATABASE and CHROMA_COLLECTION"
        )

    import chromadb

    client = userdata.get(USERDATA_CHROMA_CLIENT)
    if client is None:
        logger.info(
            "%s connecting CloudClient tenant=%r database=%r",
            _LOG,
            settings["tenant"],
            settings["database"],
        )
        client = chromadb.CloudClient(
            api_key=settings["api_key"],
            tenant=settings["tenant"],
            database=settings["database"],
        )
        userdata[USERDATA_CHROMA_CLIENT] = client

    collection = client.get_collection(settings["collection"])
    userdata[USERDATA_CHROMA_COLLECTION] = collection
    return collection


def prewarm_chroma(userdata: MutableMapping[str, Any]) -> None:
    """Connect + run a dummy query so the ONNX embedder downloads before a session."""
    if not chroma_configured():
        return
    try:
        collection = get_cached_collection(userdata)
        collection.query(query_texts=["prewarm"], n_results=1)
        logger.info("%s prewarm complete", _LOG)
    except Exception as exc:
        logger.warning("%s prewarm failed: %s", _LOG, exc)


# --- retrieval -------------------------------------------------------------


def _build_where(
    types: list[str],
    difficulties: list[str] | None,
    domains: list[str] | None,
) -> dict[str, Any] | None:
    clauses: list[dict[str, Any]] = []
    if types:
        clauses.append({"question_type": {"$in": list(types)}})
    if difficulties:
        clauses.append({"difficulty_level": {"$in": list(difficulties)}})
    if domains:
        clauses.append({"domain": {"$in": list(domains)}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _query(
    collection: Any,
    *,
    query_text: str,
    types: list[str],
    difficulties: list[str] | None,
    domains: list[str] | None,
    n: int,
    exclude_ids: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    where = _build_where(types, difficulties, domains)
    started = time.monotonic()
    result = collection.query(
        query_texts=[query_text or "interview"],
        n_results=n + len(exclude_ids),
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    logger.info(
        "%s query types=%s diff=%s domains=%s n=%d took=%dms",
        _LOG,
        types,
        difficulties,
        domains,
        n,
        int((time.monotonic() - started) * 1000),
    )
    ids = (result.get("ids") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    rows: list[tuple[str, dict[str, Any]]] = []
    for qid, meta in zip(ids, metas, strict=False):
        if not isinstance(qid, str) or qid in exclude_ids:
            continue
        rows.append((qid, meta or {}))
    return rows


def _fill_bucket(
    collection: Any,
    *,
    query_text: str,
    types: list[str],
    difficulties: list[str],
    domains: list[str],
    target: int,
    exclude_ids: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    """Fetch up to `target` rows, relaxing domain then difficulty when short."""
    picked: dict[str, dict[str, Any]] = {}
    ladder = [
        (domains, difficulties),  # exact
        (None, difficulties),  # relax domain
        (None, None),  # relax difficulty too
    ]
    for dom, diff in ladder:
        if len(picked) >= target:
            break
        rows = _query(
            collection,
            query_text=query_text,
            types=types,
            difficulties=diff,
            domains=dom,
            n=target,
            exclude_ids=exclude_ids | set(picked),
        )
        for qid, meta in rows:
            if qid in picked:
                continue
            picked[qid] = meta
            if len(picked) >= target:
                break
    return list(picked.items())[:target]


# --- normalization (matches editor_tools / interview_evaluator schema) ------


def _spoken(text: str) -> str:
    stripped = _FENCE_RE.sub("", text).strip()
    return stripped or text.strip()


def _language(meta: dict[str, Any]) -> str:
    lang = str(meta.get("code_language") or "").lower()
    if lang in SUPPORTED_LANGUAGES:
        return lang
    if lang in ("jsx", "tsx", "ts"):
        return "javascript"
    return "javascript"


def _starter_code(meta: dict[str, Any]) -> str:
    raw = meta.get("record_json")
    if not isinstance(raw, str):
        return ""
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    code = record.get("code") if isinstance(record, dict) else None
    if isinstance(code, dict) and isinstance(code.get("content"), str):
        return code["content"].strip()
    return ""


def _normalize(qid: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    text = str(meta.get("question") or "").strip()
    if not text:
        return None
    qtype = str(meta.get("question_type") or "verbal")
    if qtype in ("coding", "machine-coding"):
        surface, answer_mode = "code", "surface"
    elif qtype == "code-output":
        surface, answer_mode = "code", "verbal"
    else:
        surface, answer_mode = "verbal", "verbal"
    return {
        "id": qid,
        "text": text,
        "spokenText": _spoken(text),
        "surface": surface,
        "answerMode": answer_mode,
        "language": _language(meta),
        "starterCode": _starter_code(meta) if surface == "code" else "",
        "questionType": qtype,
        "difficulty": str(meta.get("difficulty_level") or ""),
    }


def _band(years_experience: object) -> str:
    try:
        years = int(years_experience)
    except (ValueError, TypeError):
        years = 0
    return "0-3" if years <= 3 else "4-8"


def build_plan(
    collection: Any,
    *,
    years_experience: int,
    domains: list[str] | None,
    focus: str,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    band = _band(years_experience)
    counts = _COUNTS[band]
    difficulties = _DIFFICULTIES[band]

    doms = [
        d.strip().lower()
        for d in (domains or DEFAULT_DOMAINS)
        if isinstance(d, str) and d.strip()
    ] or DEFAULT_DOMAINS
    query_text = (focus or " ".join(doms)).strip()

    ordered: list[dict[str, Any]] = []
    used: set[str] = set()
    for bucket in _BUCKET_ORDER:
        rows = _fill_bucket(
            collection,
            query_text=query_text,
            types=_BUCKET_TYPES[bucket],
            difficulties=difficulties,
            domains=doms,
            target=counts[bucket],
            exclude_ids=used,
        )
        for qid, meta in rows:
            normalized = _normalize(qid, meta)
            if normalized is None:
                continue
            ordered.append(normalized)
            used.add(qid)
    return band, counts, ordered


# --- tool ------------------------------------------------------------------


def build_interview_plan_tool(
    *,
    get_collection: Callable[[], Any],
    register_plan: Callable[[list[dict[str, Any]]], None],
):
    """Return a function tool that builds the interview plan from the question bank."""

    @function_tool(
        name="build_interview_plan",
        description=(
            "Build the interview question plan from the question bank, sized by the "
            "candidate's experience. Call this ONCE after the introduction, once you "
            "know the candidate's years of experience and primary tech area, and before "
            "any mark_question_started or open_question_editor call. Pass "
            "years_experience as a number, domains such as [\"react\", \"javascript\"], "
            "and a focus string of the candidate's key skills, technologies, and project "
            "topics from the resume (never their name). Returns an ordered questions "
            "list; ask each returned question in order using mark_question_started for "
            "verbal ones or open_question_editor for code ones, passing the returned id."
        ),
    )
    async def build_interview_plan(
        context: RunContext,
        years_experience: int,
        domains: list[str] | None = None,
        focus: str = "",
    ) -> dict[str, object]:
        try:
            collection = get_collection()
        except Exception as exc:
            logger.error("%s collection unavailable: %s", _LOG, exc)
            return {
                "status": "error",
                "message": "Question bank is unavailable right now.",
            }

        try:
            band, counts, ordered = await asyncio.to_thread(
                build_plan,
                collection,
                years_experience=years_experience,
                domains=domains,
                focus=focus,
            )
        except Exception as exc:
            logger.error("%s build_plan failed: %s", _LOG, exc)
            return {"status": "error", "message": "Could not build the interview plan."}

        if not ordered:
            return {
                "status": "empty",
                "message": "No questions matched. Continue the interview verbally.",
            }

        register_plan(ordered)

        got = {bucket: 0 for bucket in _BUCKET_ORDER}
        for question in ordered:
            qtype = question["questionType"]
            if qtype == "machine-coding":
                got["machine"] += 1
            elif qtype in ("coding", "code-output"):
                got["coding"] += 1
            else:
                got["verbal"] += 1

        logger.info(
            "%s plan band=%s target=%s got=%s total=%d",
            _LOG,
            band,
            counts,
            got,
            len(ordered),
        )

        return {
            "status": "ok",
            "experience_band": band,
            "counts": got,
            "questions": [
                {
                    "id": q["id"],
                    "type": q["questionType"],
                    "surface": q["surface"],
                    "answer_mode": q["answerMode"],
                    "question": q["spokenText"],
                }
                for q in ordered
            ],
            "message": (
                f"Plan ready: {got['verbal']} verbal, {got['coding']} coding, "
                f"{got['machine']} machine-coding. Ask them in the given order by id."
            ),
        }

    return build_interview_plan
