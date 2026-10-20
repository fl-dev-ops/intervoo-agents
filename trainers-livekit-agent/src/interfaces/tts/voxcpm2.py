"""LiveKit adapter for Trainer Twin's VoxCPM2 speech endpoint."""

from __future__ import annotations

import logging
import os
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    get_job_context,
    tts,
)

logger = logging.getLogger(__name__)


class VoxCPM2TTS(tts.TTS):
    def __init__(self, *, endpoint: str, voice: str, api_key: str) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=48_000,
            num_channels=1,
        )
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path != "/v1/audio/speech":
            raise ValueError("TTS_SERVICE_URL must point to an HTTPS VoxCPM2 service")
        if not voice:
            raise ValueError("Olga's VoxCPM2 voice ID is missing")
        if not api_key:
            raise ValueError("TTS_SERVICE_KEY is required for VoxCPM2")
        self.endpoint = endpoint
        self.voice = voice
        self.api_key = api_key
        self.http: aiohttp.ClientSession | None = None
        job_context = get_job_context(required=False)
        if job_context is not None:
            job_context.add_shutdown_callback(self.aclose)

    @property
    def model(self) -> str:
        return "voxcpm2"

    @property
    def provider(self) -> str:
        return "voxcpm2"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return VoxCPM2Stream(tts=self, input_text=text, conn_options=conn_options)

    def session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession()
        return self.http

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.close()
            self.http = None


class VoxCPM2Stream(tts.ChunkedStream):
    async def _run(self, output: tts.AudioEmitter) -> None:
        vox = self._tts
        assert isinstance(vox, VoxCPM2TTS)
        request_id = uuid.uuid4().hex
        started_at = time.perf_counter()
        audio_bytes = 0
        logger.info("[TTS:voxcpm2] start request_id=%s host=%s model=%s", request_id, urlsplit(vox.endpoint).hostname, vox.model)
        try:
            async with vox.session().post(
                vox.endpoint,
                headers={"Authorization": f"Bearer {vox.api_key}"},
                json={"model": vox.model, "voice": vox.voice, "input": self.input_text,
                      "stream": True, "response_format": "pcm"},
                timeout=aiohttp.ClientTimeout(total=60, sock_connect=10),
            ) as response:
                request_id = response.headers.get("X-Request-ID", request_id)
                if response.status != 200:
                    await response.read()
                    raise APIStatusError(
                        "VoxCPM2 TTS request failed",
                        status_code=response.status,
                        request_id=request_id,
                        retryable=response.status in {408, 429} or response.status >= 500,
                    )
                sample_rate = int(response.headers.get("X-Sample-Rate", "48000"))
                output.initialize(
                    request_id=request_id,
                    sample_rate=sample_rate,
                    num_channels=1,
                    mime_type="audio/pcm",
                )
                async for chunk in response.content.iter_any():
                    if chunk:
                        output.push(chunk)
                        audio_bytes += len(chunk)
                if audio_bytes == 0:
                    raise APIConnectionError("VoxCPM2 returned an empty audio stream", retryable=True)
                output.flush()
                logger.info("[TTS:voxcpm2] complete request_id=%s elapsed_ms=%.1f audio_bytes=%d", request_id, (time.perf_counter() - started_at) * 1000, audio_bytes)
        except (APIStatusError, APIConnectionError) as error:
            logger.warning("[TTS:voxcpm2] error request_id=%s elapsed_ms=%.1f error_type=%s error=%s", request_id, (time.perf_counter() - started_at) * 1000, type(error).__name__, error)
            raise
        except TimeoutError as error:
            logger.warning("[TTS:voxcpm2] error request_id=%s elapsed_ms=%.1f error_type=timeout", request_id, (time.perf_counter() - started_at) * 1000)
            raise APITimeoutError(retryable=audio_bytes == 0) from error
        except aiohttp.ClientError as error:
            logger.warning("[TTS:voxcpm2] error request_id=%s elapsed_ms=%.1f error_type=%s error=%s", request_id, (time.perf_counter() - started_at) * 1000, type(error).__name__, error)
            raise APIConnectionError("VoxCPM2 TTS connection failed", retryable=audio_bytes == 0) from error


def build_voxcpm2_tts(*, voice: str) -> VoxCPM2TTS:
    service_url = os.getenv("TTS_SERVICE_URL", "https://tts.trainertwin.com").rstrip("/")
    return VoxCPM2TTS(
        endpoint=f"{service_url}/v1/audio/speech",
        voice=voice,
        api_key=os.getenv("TTS_SERVICE_KEY", "").strip(),
    )
