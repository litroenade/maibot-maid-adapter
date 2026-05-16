import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Any

from ..constants import DEFAULT_MAX_MESSAGE_BYTES

RawCallback = Callable[[str], object]
LifecycleCallback = Callable[[], object]


class AioHttpWebSocketBridgeTransport:
    def __init__(
        self,
        url: str,
        *,
        access_token: str = "",
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if not url:
            raise ValueError("url 不能为空")
        self.url = url
        self.access_token = access_token
        self.max_message_bytes = max_message_bytes
        self._session: Any | None = None
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._close_emitted = False
        self._raw_callbacks: list[RawCallback] = []
        self._close_callbacks: list[LifecycleCallback] = []

    def on_raw(self, callback: RawCallback) -> None:
        self._raw_callbacks.append(callback)

    def on_close(self, callback: LifecycleCallback) -> None:
        self._close_callbacks.append(callback)

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("传输层已停止")
        try:
            import aiohttp
        except ImportError as exc:
            raise RuntimeError("AioHttpWebSocketBridgeTransport 需要安装 aiohttp") from exc
        session = aiohttp.ClientSession()
        self._session = session
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else None
        kwargs: dict[str, Any] = {"max_msg_size": self.max_message_bytes}
        if headers:
            kwargs["headers"] = headers
        self._websocket = await session.ws_connect(self.url, **kwargs)
        self._started = True
        self._close_emitted = False
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._close_resources()
        if self._reader_task and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._started = False
        await self._emit_close_once()

    async def send(self, raw: str) -> None:
        if not self._started or self._websocket is None:
            raise RuntimeError("传输层尚未启动")
        await self._websocket.send_str(raw)

    async def _reader_loop(self) -> None:
        try:
            websocket = self._websocket
            if websocket is None:
                return
            async for message in websocket:
                if self._is_text_message(message):
                    raw = str(message.data)
                    if len(raw.encode("utf-8")) > self.max_message_bytes:
                        await self._close_resources()
                        break
                    for callback in list(self._raw_callbacks):
                        await _maybe_await(callback(raw))
                    continue
                if self._is_terminal_message(message):
                    break
        finally:
            self._started = False
            await self._close_resources()
            await self._emit_close_once()

    async def _close_resources(self) -> None:
        websocket = self._websocket
        session = self._session
        self._websocket = None
        self._session = None
        if websocket is not None:
            await websocket.close()
        if session is not None:
            await session.close()

    def _is_text_message(self, message: object) -> bool:
        return self._message_type_name(message) == "TEXT"

    def _is_terminal_message(self, message: object) -> bool:
        return self._message_type_name(message) in {"CLOSE", "CLOSED", "ERROR"}

    def _message_type_name(self, message: object) -> str:
        message_type = getattr(message, "type", None)
        return str(getattr(message_type, "name", message_type))

    async def _emit_close_once(self) -> None:
        if self._close_emitted:
            return
        self._close_emitted = True
        for callback in list(self._close_callbacks):
            await _maybe_await(callback())


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value
