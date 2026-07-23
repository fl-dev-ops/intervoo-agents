"""Custom Qwen voice-clone TTS backed by a non-streaming HTTP endpoint.

Wraps the JarvisLabs-hosted "vasanth-best" voice clone. The endpoint accepts a
JSON body ``{"text": ..., "language": ...}`` and streams back raw PCM audio,
which we forward to LiveKit's AudioEmitter chunk by chunk.
"""

from __future__ import annotations

import uuid

import aiohttp
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectionError,
    APIConnectOptions,
    APIStatusError,
    APITimeoutError,
    tts,
)

_SAMPLE_RATE = 24_000
_NUM_CHANNELS = 1


class QwenTTS(tts.TTS):
    def __init__(self, endpoint: str) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_SAMPLE_RATE,
            num_channels=_NUM_CHANNELS,
        )
        self.endpoint = endpoint
        self.http: aiohttp.ClientSession | None = None

    @property
    def model(self) -> str:
        return "vasanth-best"

    @property
    def provider(self) -> str:
        return "qwen"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return QwenStream(tts=self, input_text=text, conn_options=conn_options)

    def session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession()
        return self.http

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.close()


class QwenStream(tts.ChunkedStream):
    async def _run(self, output: tts.AudioEmitter) -> None:
        qwen = self._tts
        assert isinstance(qwen, QwenTTS)
        try:
            async with qwen.session().post(
                qwen.endpoint,
                json={"text": self.input_text, "language": "English"},
                timeout=aiohttp.ClientTimeout(
                    total=300, sock_connect=self._conn_options.timeout
                ),
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    raise APIStatusError(
                        body,
                        status_code=response.status,
                        body=body,
                        retryable=response.status >= 500,
                    )

                output.initialize(
                    request_id=uuid.uuid4().hex,
                    sample_rate=_SAMPLE_RATE,
                    num_channels=_NUM_CHANNELS,
                    mime_type="audio/pcm",
                )
                async for chunk in response.content.iter_any():
                    output.push(chunk)
                output.flush()
        except TimeoutError as error:
            raise APITimeoutError() from error
        except aiohttp.ClientError as error:
            raise APIConnectionError(str(error)) from error
