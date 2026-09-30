"""Avatar provider integration."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, Protocol

from livekit import rtc
from livekit.agents import AgentSession
from livekit.plugins import anam, did, keyframe, liveavatar, simli
from livekit.plugins.anam import api as _anam_api

logger = logging.getLogger(__name__)

SIMLI_ROOM_LIFETIME_SECONDS = 1 * 60 * 60
PERSONA_ID_PREFIX = "persona-id:"

# The installed Anam plugin (1.6.6) only serializes ``avatarId`` in the
# session-token payload, but Anam's API expects saved personas to be
# referenced as ``personaId``. Profiles pass persona ids prefixed with
# ``PERSONA_ID_PREFIX``; swap the key before the request leaves the process.
_ANAM_POST = _anam_api.AnamAPI._post


async def _anam_post_with_persona_support(
    self: _anam_api.AnamAPI,
    endpoint: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    if endpoint.endswith("/auth/session-token") and isinstance(payload, dict):
        persona = payload.get("personaConfig")
        if isinstance(persona, dict):
            avatar_id = persona.get("avatarId")
            if isinstance(avatar_id, str) and avatar_id.startswith(PERSONA_ID_PREFIX):
                persona["personaId"] = avatar_id.split(":", 1)[1]
                del persona["avatarId"]
    return await _ANAM_POST(self, endpoint, payload, headers)


_anam_api.AnamAPI._post = _anam_post_with_persona_support


class AvatarConfigurationError(ValueError):
    pass


class AvatarSession(Protocol):
    async def start(self, agent_session: AgentSession, room: rtc.Room) -> None: ...


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AvatarConfigurationError(f"{name} is required")
    return value


def _build_anam(
    *,
    persona_id: str | None = None,
    avatar_id: str | None = None,
    persona_name: str | None = None,
) -> AvatarSession:
    api_key = _required_env("ANAM_API_KEY")
    effective_persona_id = (persona_id or "").strip()
    effective_avatar_id = (avatar_id or os.getenv("ANAM_AVATAR_ID", "")).strip()
    effective_name = (
        persona_name or os.getenv("ANAM_PERSONA_NAME", "")
    ).strip() or "Vasanth"
    if not effective_avatar_id and not effective_persona_id:
        raise AvatarConfigurationError(
            "ANAM_AVATAR_ID or a profile avatar persona_id is required"
        )
    if effective_persona_id and effective_avatar_id:
        logger.warning(
            "Both avatar_id and persona_id configured; persona_id takes precedence"
        )
    persona_config = anam.PersonaConfig(
        name=effective_name,
        avatarId=(
            f"{PERSONA_ID_PREFIX}{effective_persona_id}"
            if effective_persona_id
            else effective_avatar_id
        ),
    )
    return anam.AvatarSession(
        persona_config=persona_config,
        api_key=api_key,
    )


def _build_did() -> AvatarSession:
    api_key = _required_env("DID_API_KEY")
    agent_id = _required_env("DID_AGENT_ID")
    return did.AvatarSession(
        agent_id=agent_id,
        api_key=api_key,
    )


def _build_keyframe(
    *,
    persona_id: str | None = None,
    persona_slug: str | None = None,
) -> AvatarSession:
    api_key = _required_env("KEYFRAME_API_KEY")
    eff_id = (persona_id or os.getenv("KEYFRAME_PERSONA_ID", "")).strip() or None
    eff_slug = (persona_slug or os.getenv("KEYFRAME_PERSONA_SLUG", "")).strip() or None

    if not eff_id and not eff_slug:
        raise AvatarConfigurationError(
            "KEYFRAME_PERSONA_ID or KEYFRAME_PERSONA_SLUG is required"
        )
    if eff_id and eff_slug:
        logger.warning(
            "Both KEYFRAME_PERSONA_ID and KEYFRAME_PERSONA_SLUG provided; persona_id takes precedence"
        )
        eff_slug = None

    if eff_id:
        return keyframe.AvatarSession(persona_id=eff_id, api_key=api_key)
    return keyframe.AvatarSession(persona_slug=eff_slug, api_key=api_key)


def _build_liveavatar() -> AvatarSession:
    return liveavatar.AvatarSession(
        api_key=_required_env("LIVEAVATAR_API_KEY"),
        avatar_id=_required_env("LIVEAVATAR_AVATAR_ID"),
    )


def _build_simli() -> AvatarSession:
    api_key = _required_env("SIMLI_API_KEY")
    face_id = _required_env("SIMLI_FACE_ID")
    emotion_id = os.getenv("SIMLI_EMOTION_ID")
    config = (
        simli.SimliConfig(
            api_key=api_key,
            face_id=face_id,
            emotion_id=emotion_id,
            max_session_length=SIMLI_ROOM_LIFETIME_SECONDS,
            max_idle_time=SIMLI_ROOM_LIFETIME_SECONDS,
        )
        if emotion_id
        else simli.SimliConfig(
            api_key=api_key,
            face_id=face_id,
            max_session_length=SIMLI_ROOM_LIFETIME_SECONDS,
            max_idle_time=SIMLI_ROOM_LIFETIME_SECONDS,
        )
    )
    return simli.AvatarSession(simli_config=config)


ProviderFactory = Callable[[], AvatarSession]

PROVIDER_FACTORIES: dict[str, ProviderFactory] = {
    "anam": _build_anam,
    "d-id": _build_did,
    "did": _build_did,
    "keyframe": _build_keyframe,
    "liveavatar": _build_liveavatar,
    "simli": _build_simli,
}

DISABLED_PROVIDERS = {
    "hedra": (
        "Hedra sunset its Realtime Avatar service on April 15, 2026; "
        "the LiveKit Hedra plugin no longer functions"
    ),
}


def create_avatar_session(
    provider_name: str | None = None,
    *,
    persona_id: str | None = None,
    avatar_id: str | None = None,
    persona_name: str | None = None,
) -> tuple[str, AvatarSession] | None:
    provider = (
        provider_name if provider_name is not None else os.getenv("AVATAR_PROVIDER", "")
    ).strip().lower()

    if provider in {"", "none", "off", "disabled"}:
        return None
    if provider in DISABLED_PROVIDERS:
        raise AvatarConfigurationError(DISABLED_PROVIDERS[provider])

    factory = PROVIDER_FACTORIES.get(provider)
    if factory is None:
        supported = ", ".join(sorted(PROVIDER_FACTORIES))
        raise AvatarConfigurationError(
            f"Unsupported AVATAR_PROVIDER={provider!r}; supported providers: {supported}"
        )
    if provider == "anam":
        return provider, _build_anam(
            persona_id=persona_id, avatar_id=avatar_id, persona_name=persona_name
        )
    if provider == "keyframe":
        return provider, _build_keyframe(
            persona_id=persona_id,
            persona_slug=os.getenv("KEYFRAME_PERSONA_SLUG"),
        )
    return provider, factory()


async def start_avatar(
    session: AgentSession,
    room: rtc.Room,
    *,
    enabled: bool,
    provider_name: str | None = None,
    persona_id: str | None = None,
    avatar_id: str | None = None,
    persona_name: str | None = None,
) -> bool:
    if not enabled:
        return False

    try:
        if (
            provider_name is not None
            or persona_id is not None
            or avatar_id is not None
            or persona_name is not None
        ):
            configured_avatar = create_avatar_session(
                provider_name,
                persona_id=persona_id,
                avatar_id=avatar_id,
                persona_name=persona_name,
            )
        else:
            configured_avatar = create_avatar_session()
    except AvatarConfigurationError as error:
        logger.warning("Avatar unavailable: %s; using audio-only agent", error)
        return False

    if configured_avatar is None:
        logger.info(
            "Avatar requested but AVATAR_PROVIDER is disabled; using audio-only agent"
        )
        return False

    provider, avatar = configured_avatar
    try:
        await avatar.start(session, room)
    except Exception:
        logger.exception(
            "Avatar provider %s failed to start; using audio-only agent",
            provider,
        )
        return False

    logger.info("Avatar provider %s started for room=%s", provider, room.name)
    return True
