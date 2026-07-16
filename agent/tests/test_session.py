from __future__ import annotations

from types import SimpleNamespace

import pytest

import session as session_module
from session import build_agent_session


@pytest.fixture
def fake_session_dependencies(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    calls: dict[str, dict] = {}

    def fake_assemblyai_stt(**kwargs):
        calls["stt"] = kwargs
        return object()

    monkeypatch.setattr(session_module.assemblyai, "STT", fake_assemblyai_stt)
    monkeypatch.setattr(
        session_module.sarvam,
        "TTS",
        lambda **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        session_module.openai.LLM,
        "with_openrouter",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        session_module,
        "AgentSession",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )
    return calls


def test_session_uses_assemblyai_universal_3_5_pro(
    fake_session_dependencies: dict[str, dict],
) -> None:
    build_agent_session(
        tts_speaker="test",
        tts_dict_id=None,
        turn_detector=object(),
    )

    assert fake_session_dependencies["stt"] == {
        "model": "universal-3-5-pro",
        "min_turn_silence": 100,
        "max_turn_silence": 1000,
        "vad_threshold": 0.3,
    }


def test_diagnostic_session_disables_preemptive_generation(
    fake_session_dependencies: dict[str, dict],
) -> None:
    built = build_agent_session(
        tts_speaker="test",
        tts_dict_id=None,
        turn_detector=object(),
        disable_preemptive_generation=True,
    )

    assert built.turn_handling["preemptive_generation"] == {"enabled": False}
    assert built.turn_handling["interruption"] == {
        "mode": "adaptive",
        "min_duration": 0.5,
        "resume_false_interruption": True,
    }
