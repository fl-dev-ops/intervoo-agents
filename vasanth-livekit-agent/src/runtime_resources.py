from __future__ import annotations

import logging
from collections.abc import MutableMapping
from pathlib import Path
from profile import AgentProfile, load_profile_catalog
from typing import Any

from livekit.agents import JobProcess
from livekit.agents.inference import TurnDetector

from prompt import load_prompt
from recording_config import RecordingConfig, build_recording_config

logger = logging.getLogger(__name__)

USERDATA_TURN_DETECTOR = "turn_detector"
USERDATA_PROFILE_CATALOG = "profile_catalog"
USERDATA_RECORDING_CONFIG = "recording_config"


def prewarm_runtime_resources(
    proc: JobProcess,
    *,
    profile_config_path: Path,
) -> None:
    userdata = proc.userdata

    try:
        userdata[USERDATA_TURN_DETECTOR] = TurnDetector(version="v1-mini")
    except RuntimeError as e:
        logger.info("Turn detector prewarm deferred until job context: %s", e)

    profile_catalog = load_profile_catalog(profile_config_path)
    userdata[USERDATA_PROFILE_CATALOG] = profile_catalog
    userdata[USERDATA_RECORDING_CONFIG] = build_recording_config()

    for profile in profile_catalog.values():
        try:
            load_prompt(profile.prompt_url)
        except Exception as e:
            logger.warning(
                "Failed to prewarm prompt for agent_id=%s: %s",
                profile.id,
                e,
            )

    logger.info(
        "Runtime resources prewarmed: profiles=%s",
        sorted(profile_catalog.keys()),
    )


def get_profile_catalog(
    userdata: MutableMapping[str, Any],
    *,
    fallback_path: Path,
) -> dict[str, AgentProfile]:
    catalog = userdata.get(USERDATA_PROFILE_CATALOG)
    if isinstance(catalog, dict):
        return catalog
    catalog = load_profile_catalog(fallback_path)
    userdata[USERDATA_PROFILE_CATALOG] = catalog
    return catalog


def get_recording_config(userdata: MutableMapping[str, Any]) -> RecordingConfig:
    config = userdata.get(USERDATA_RECORDING_CONFIG)
    if isinstance(config, RecordingConfig):
        return config
    config = build_recording_config()
    userdata[USERDATA_RECORDING_CONFIG] = config
    return config


def get_prewarmed_turn_detector(userdata: MutableMapping[str, Any]) -> Any | None:
    return userdata.get(USERDATA_TURN_DETECTOR)


def get_or_create_turn_detector(userdata: MutableMapping[str, Any]) -> Any:
    turn_detector = userdata.get(USERDATA_TURN_DETECTOR)
    if turn_detector is None:
        turn_detector = TurnDetector(version="v1-mini")
        userdata[USERDATA_TURN_DETECTOR] = turn_detector
    return turn_detector
