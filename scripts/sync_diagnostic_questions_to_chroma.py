#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    import chromadb
except ImportError:  # pragma: no cover - depends on runtime environment
    chromadb = None


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT_DIR / "CS-diagnostic-agent" / "diagnostic-questions.json"
DEFAULT_COLLECTION = "diagnostic_questions"
DEFAULT_DOMAIN = "computer_science"
DEFAULT_CONTENT_TYPE = "diagnostic_question"
DEFAULT_BATCH_SIZE = 100


def _non_empty_string(value: Any, field: str, index: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Record {index} has invalid {field!r}")
    return value.strip()


def _required_int(value: Any, field: str, index: int) -> int:
    if not isinstance(value, int):
        raise ValueError(f"Record {index} has invalid {field!r}")
    return value


def _normalize_question_type(value: Any, index: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"Record {index} has invalid 'question_type'")
    normalized = [
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ]
    if not normalized:
        raise ValueError(f"Record {index} has empty 'question_type'")
    return normalized


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, list):
        raise ValueError(f"{path} must contain a JSON array")
    return payload


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_record(
    record: dict[str, Any],
    *,
    index: int,
    domain: str,
    content_type: str,
    source_file: str,
    version: str,
) -> tuple[str, str, dict[str, Any]]:
    question_id = _non_empty_string(record.get("id"), "id", index)
    text = _non_empty_string(record.get("text"), "text", index)
    category = _non_empty_string(record.get("category"), "category", index).lower()
    difficulty = _non_empty_string(
        record.get("difficulty_level"), "difficulty_level", index
    ).lower()
    band = _required_int(record.get("band"), "band", index)
    question_types = _normalize_question_type(record.get("question_type"), index)

    metadata: dict[str, Any] = {
        "question_id": question_id,
        "content_type": content_type,
        "domain": domain,
        "category": category,
        "difficulty_level": difficulty,
        "band": band,
        "question_type": ",".join(question_types),
        "question_type_json": json.dumps(question_types, ensure_ascii=True),
        "source_file": source_file,
        "version": version,
    }

    for optional_key in ("topic", "subtopic", "source", "language"):
        value = record.get(optional_key)
        if isinstance(value, str) and value.strip():
            metadata[optional_key] = value.strip()

    return question_id, text, metadata


def _chunks(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def build_chroma_payload(
    records: list[dict[str, Any]],
    *,
    domain: str,
    content_type: str,
    source_file: str,
    version: str,
) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Record {index} must be an object")
        record_id, document, metadata = _normalize_record(
            record,
            index=index,
            domain=domain,
            content_type=content_type,
            source_file=source_file,
            version=version,
        )
        if record_id in seen_ids:
            raise ValueError(f"Duplicate record id: {record_id}")
        seen_ids.add(record_id)
        ids.append(record_id)
        documents.append(document)
        metadatas.append(metadata)

    return ids, documents, metadatas


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def sync_to_chroma(
    *,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict[str, Any]],
    collection_name: str,
    batch_size: int,
) -> None:
    if chromadb is None:
        raise RuntimeError(
            "chromadb is not installed. Run this with the CS-diagnostic-agent "
            "environment, for example: cd CS-diagnostic-agent && uv run "
            "python ../scripts/sync_diagnostic_questions_to_chroma.py"
        )

    client = chromadb.CloudClient(
        api_key=_required_env("CHROMA_API_KEY"),
        tenant=_required_env("CHROMA_TENANT"),
        database=_required_env("CHROMA_DATABASE"),
    )
    collection = client.get_or_create_collection(collection_name)

    for id_batch in _chunks(ids, batch_size):
        start = ids.index(id_batch[0])
        end = start + len(id_batch)
        collection.upsert(
            ids=id_batch,
            documents=documents[start:end],
            metadatas=metadatas[start:end],
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync diagnostic question JSON records to a Chroma Cloud collection."
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument(
        "--collection",
        default=os.getenv("CHROMA_COLLECTION", DEFAULT_COLLECTION),
    )
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--content-type", default=DEFAULT_CONTENT_TYPE)
    parser.add_argument("--version", default="")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    records = _load_json_records(source)
    version = args.version.strip() or _source_hash(source)[:12]
    ids, documents, metadatas = build_chroma_payload(
        records,
        domain=args.domain,
        content_type=args.content_type,
        source_file=str(source.relative_to(ROOT_DIR)),
        version=version,
    )

    print(
        f"Prepared {len(ids)} records for collection {args.collection!r} from {source}"
    )
    print(f"Version: {version}")

    if args.dry_run:
        print("Dry run complete; no records were written.")
        return 0

    sync_to_chroma(
        ids=ids,
        documents=documents,
        metadatas=metadatas,
        collection_name=args.collection,
        batch_size=max(args.batch_size, 1),
    )
    print(f"Synced {len(ids)} records to Chroma collection {args.collection!r}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
