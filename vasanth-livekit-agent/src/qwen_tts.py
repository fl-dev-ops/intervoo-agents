"""LiveKit TTS adapter for the vLLM-Omni Qwen3-TTS Speech API."""

from __future__ import annotations

import logging
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

_SAMPLE_RATE = 24_000
_NUM_CHANNELS = 1
_PCM_CONTENT_TYPE = "audio/pcm"

logger = logging.getLogger(__name__)


class QwenTTS(tts.TTS):
    def __init__(
        self,
        *,
        endpoint: str,
        model_label: str,
        language: str,
        api_key: str,
        connect_timeout: float,
        total_timeout: float,
        max_retries: int,
        retry_interval: float,
        voice: str | None = None,
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_SAMPLE_RATE,
            num_channels=_NUM_CHANNELS,
        )
        parsed_endpoint = urlsplit(endpoint)
        if (
            parsed_endpoint.scheme not in {"http", "https"}
            or not parsed_endpoint.hostname
            or parsed_endpoint.path != "/v1/audio/speech/streaming"
            or parsed_endpoint.query
            or parsed_endpoint.fragment
        ):
            raise ValueError(
                "QWEN_TTS_ENDPOINT must be an HTTP(S) URL ending at "
                "/v1/audio/speech/streaming"
            )
        if not api_key.strip():
            raise ValueError("QWEN_TTS_API_KEY is required")
        if not language.strip():
            raise ValueError("QWEN_TTS_LANGUAGE is required")
        if connect_timeout <= 0 or total_timeout <= 0:
            raise ValueError("Qwen TTS timeouts must be greater than zero")
        if max_retries < 0:
            raise ValueError("QWEN_TTS_MAX_RETRIES must be non-negative")
        if retry_interval <= 0:
            raise ValueError("QWEN_TTS_RETRY_INTERVAL_SECONDS must be positive")

        self.endpoint = endpoint
        self.model_label = model_label
        self.language = language
        self.api_key = api_key
        self.connect_timeout = connect_timeout
        self.total_timeout = total_timeout
        self.connect_options = APIConnectOptions(
            max_retry=max_retries,
            retry_interval=retry_interval,
            timeout=connect_timeout,
        )
        self.http: aiohttp.ClientSession | None = None

        job_context = get_job_context(required=False)
        if job_context is not None:
            job_context.add_shutdown_callback(self.aclose)

    @property
    def model(self) -> str:
        return self.model_label

    @property
    def provider(self) -> str:
        return "qwen"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return QwenStream(
            tts=self,
            input_text=text,
            conn_options=self.connect_options,
        )

    def session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession()
        return self.http

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.close()
            self.http = None


class QwenStream(tts.ChunkedStream):
    async def _run(self, output: tts.AudioEmitter) -> None:
        qwen = self._tts
        assert isinstance(qwen, QwenTTS)
        request_id = uuid.uuid4().hex
        started_at = time.perf_counter()
        emitted_bytes = 0
        first_audio_at: float | None = None

        logger.info(
            "[TTS:qwen] start request_id=%s host=%s model=%s",
            request_id,
            urlsplit(qwen.endpoint).hostname,
            qwen.model,
        )
        try:
            async with qwen.session().post(
                qwen.endpoint,
                headers={"Authorization": f"Bearer {qwen.api_key}"},
                json={"text": self.input_text, "language": qwen.language},
                timeout=aiohttp.ClientTimeout(
                    total=qwen.total_timeout,
                    sock_connect=qwen.connect_timeout,
                ),
            ) as response:
                server_request_id = response.headers.get("X-Request-ID")
                if server_request_id:
                    request_id = server_request_id
                if response.status != 200:
                    await response.read()
                    raise APIStatusError(
                        "Qwen TTS request failed",
                        status_code=response.status,
                        request_id=request_id,
                        retryable=response.status in {408, 429}
                        or response.status >= 500,
                    )

                media_type = (
                    response.headers.get("Content-Type", "")
                    .partition(";")[0]
                    .strip()
                    .lower()
                )
                if media_type not in {_PCM_CONTENT_TYPE, "application/octet-stream"}:
                    raise APIStatusError(
                        "Qwen TTS returned an unexpected content type",
                        status_code=502,
                        request_id=request_id,
                        retryable=False,
                    )

                output.initialize(
                    request_id=request_id,
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    mime_type=_PCM_CONTENT_TYPE,
                )
                async for chunk in response.content.iter_any():
                    if not chunk:
                        continue
                    if first_audio_at is None:
                        first_audio_at = time.perf_counter()
                        logger.info(
                            "[TTS:qwen] first audio request_id=%s ttfa_ms=%.1f",
                            request_id,
                            (first_audio_at - started_at) * 1000,
                        )
                    output.push(chunk)
                    emitted_bytes += len(chunk)

                if emitted_bytes == 0:
                    raise APIConnectionError(
                        "Qwen TTS returned an empty audio stream",
                        retryable=True,
                    )
                output.flush()
                elapsed_ms = (time.perf_counter() - started_at) * 1000
                ttfa_ms = (
                    (first_audio_at - started_at) * 1000
                    if first_audio_at is not None
                    else -1
                )
                logger.info(
                    "[TTS:qwen] complete request_id=%s elapsed_ms=%.1f "
                    "ttfa_ms=%.1f audio_bytes=%d",
                    request_id,
                    elapsed_ms,
                    ttfa_ms,
                    emitted_bytes,
                )
        except APIStatusError:
            logger.exception(
                "[TTS:qwen] error request_id=%s elapsed_ms=%.1f audio_bytes=%d",
                request_id,
                (time.perf_counter() - started_at) * 1000,
                emitted_bytes,
            )
            raise
        except APIConnectionError:
            logger.warning(
                "[TTS:qwen] error request_id=%s elapsed_ms=%.1f audio_bytes=%d",
                request_id,
                (time.perf_counter() - started_at) * 1000,
                emitted_bytes,
            )
            raise
        except TimeoutError as error:
            logger.warning(
                "[TTS:qwen] error request_id=%s elapsed_ms=%.1f "
                "audio_bytes=%d error_type=timeout",
                request_id,
                (time.perf_counter() - started_at) * 1000,
                emitted_bytes,
            )
            raise APITimeoutError(retryable=emitted_bytes == 0) from error
        except aiohttp.ClientError as error:
            logger.warning(
                "[TTS:qwen] error request_id=%s elapsed_ms=%.1f "
                "audio_bytes=%d error_type=%s",
                request_id,
                (time.perf_counter() - started_at) * 1000,
                emitted_bytes,
                type(error).__name__,
            )
            raise APIConnectionError(
                "Qwen TTS connection failed",
                retryable=emitted_bytes == 0,
            ) from error
