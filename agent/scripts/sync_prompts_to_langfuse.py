"""
Sync diagnostic prompt versions V1–V5 to Langfuse Prompt Management.

Run from the agent/ directory:
    python scripts/sync_prompts_to_langfuse.py

Each version is pushed as a new Langfuse prompt version with the label "v1"–"v5".
Langfuse auto-increments the integer version number; labels are what we use to
identify versions by name (e.g. lf.get_prompt("diagnostic-agent", label="v5")).

Re-running this script will create new versions — it does not overwrite existing ones.
"""

import json
import os
import sys
from pathlib import Path

# Allow running from either agent/ or agent/scripts/
AGENT_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = AGENT_ROOT / "prompts" / "diagnostic"

# Load .env from agent root if present
env_path = AGENT_ROOT / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), value)

from langfuse import Langfuse  # noqa: E402 — import after env is loaded

PROMPT_NAME = "diagnostic-agent"


def _load_versions_from_config(agent_root: Path) -> tuple[list[str], str]:
    """
    Read agents.json to find the active prompt version, and scan the prompts
    directory to build the full list of versions to sync.

    ACTIVE_VERSION = highest version number referenced by any diagnostic-agent entry
    VERSIONS       = sorted list of all v*.md files found in prompts/diagnostic/
    """
    config = json.loads((agent_root / "config" / "agents.json").read_text())
    diagnostic_versions = []
    for entry in config["agents"].values():
        if entry.get("agent_type") == "diagnostic-agent":
            stem = Path(entry["prompt_url"]).stem  # "prompts/diagnostic/v5.md" → "v5"
            if stem.startswith("v") and stem[1:].isdigit():
                diagnostic_versions.append(stem)

    if not diagnostic_versions:
        raise ValueError("No diagnostic-agent entries with versioned prompt_url found in agents.json")

    active = max(diagnostic_versions, key=lambda v: int(v[1:]))

    all_files = sorted(
        (p.stem for p in (agent_root / "prompts" / "diagnostic").glob("v*.md")),
        key=lambda v: int(v[1:]),
    )
    return all_files, active


VERSIONS, ACTIVE_VERSION = _load_versions_from_config(AGENT_ROOT)


def main() -> None:
    lf = Langfuse()

    if not lf.auth_check():
        print("ERROR: Langfuse auth failed. Check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL.")
        sys.exit(1)

    print(f"Active version (from agents.json): {ACTIVE_VERSION}")
    print(f"Versions to sync: {', '.join(VERSIONS)}\n")
    print(f"Connected to Langfuse. Syncing {len(VERSIONS)} prompt versions...\n")

    for version in VERSIONS:
        prompt_path = PROMPTS_DIR / f"{version}.md"
        if not prompt_path.exists():
            print(f"  SKIP {version} — file not found: {prompt_path}")
            continue

        text = prompt_path.read_text(encoding="utf-8")
        char_count = len(text)

        # Check if this exact content is already in Langfuse under this label
        try:
            existing = lf.get_prompt(PROMPT_NAME, label=version, fallback="__not_found__")
            if existing.prompt == text:
                # Content unchanged — but ensure production label is set on ACTIVE_VERSION
                if version == ACTIVE_VERSION:
                    try:
                        lf.create_prompt(
                            name=PROMPT_NAME,
                            prompt=text,
                            labels=[version, "production"],
                            commit_message=f"Pin production label to {version}",
                        )
                        print(f"  = {version}  →  already up to date, pinned [production] label")
                    except Exception:
                        print(f"  = {version}  →  already up to date (production label unchanged)")
                else:
                    print(f"  = {version}  →  already up to date, skipping")
                continue
        except Exception:
            pass  # label doesn't exist yet — proceed to create

        try:
            labels = [version]
            if version == ACTIVE_VERSION:
                labels.append("production")
            result = lf.create_prompt(
                name=PROMPT_NAME,
                prompt=text,
                labels=labels,
                commit_message=f"Sync {version} from git ({char_count} chars)",
            )
            tag = "  [production]" if version == ACTIVE_VERSION else ""
            print(f"  ✓ {version}  →  Langfuse version {result.version}  ({char_count} chars){tag}")
        except Exception as e:
            print(f"  ✗ {version}  →  ERROR: {e}")

    lf.flush()
    print(f"\nDone. Go to Langfuse → Prompt Management → '{PROMPT_NAME}' to verify.")


if __name__ == "__main__":
    main()
