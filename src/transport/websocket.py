import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Any, Protocol

from ..constants import DEFAULT_MAX_MESSAGE_BYTES

RawCallback = Callable[[str], object]
LifecycleCallback = Callable[[], object]


class BridgeTransport(Protocol):
    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def send(self, raw: str) -> None:
        raise NotImplementedError

    def on_raw(self, callback: RawCallback) -> None:
        raise NotImplementedError

    def on_open(self, callback: LifecycleCallback) -> None:
        raise NotImplementedError

    def on_close(self, callback: LifecycleCallback) -> None:
        raise NotImplementedError


class InMemoryBridgeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self._started = False
        self._stopped = False
        self._raw_callbacks: list[RawCallback] = []
        self._open_callbacks: list[LifecycleCallback] = []
        self._close_callbacks: list[LifecycleCallback] = []

    def on_raw(self, callback: RawCallback) -> None:
        self._raw_callbacks.append(callback)

    def on_open(self, callback: LifecycleCallback) -> None:
        self._open_callbacks.append(callback)

    def on_close(self, callback: LifecycleCallback) -> None:
        self._close_callbacks.append(callback)

    async def start(self) -> None:
        self._ensure_not_stopped()
        if self._started:
            return
        self._started = True
        await self.emit_open()

    async def stop(self) -> None:
        self._ensure_not_stopped()
        await self.emit_close()
        self._started = False
        self._stopped = True

    async def send(self, raw: str) -> None:
        self._ensure_active()
        self.sent.append(raw)

    async def emit_open(self) -> None:
        self._ensure_not_stopped()
        for callback in list(self._open_callbacks):
            await _maybe_await(callback())

    async def emit_close(self) -> None:
        self._ensure_not_stopped()
        for callback in list(self._close_callbacks):
            await _maybe_await(callback())

    def _ensure_active(self) -> None:
        self._ensure_not_stopped()
        if not self._started:
            raise RuntimeError("传输层尚未启动")

    def _ensure_not_stopped(self) -> None:
        if self._stopped:
            raise RuntimeError("传输层已停止")


class AioHttpWebSocketBridgeTransport:
    def __init__(
        self,
        url: str,
        *,
        access_token: str = "",
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        session_factory: Callable[[], object] | None = None,
    ) -> None:
        if not url:
            raise ValueError("url 不能为空")
        self.url = url
        self.access_token = access_token
        self.max_message_bytes = max_message_bytes
        self._session_factory = session_factory
        self._session: Any | None = None
        self._websocket: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._connected = False
        self._close_emitted = False
        self._raw_callbacks: list[RawCallback] = []
        self._open_callbacks: list[LifecycleCallback] = []
        self._close_callbacks: list[LifecycleCallback] = []

    def on_raw(self, callback: RawCallback) -> None:
        self._raw_callbacks.append(callback)

    def on_open(self, callback: LifecycleCallback) -> None:
        self._open_callbacks.append(callback)

    def on_close(self, callback: LifecycleCallback) -> None:
        self._close_callbacks.append(callback)

    async def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("传输层已停止")
        self._session = self._create_session()
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else None
        kwargs: dict[str, object] = {"max_msg_size": self.max_message_bytes}
        if headers:
            kwargs["headers"] = headers
        self._websocket = await self._session.ws_connect(self.url, **kwargs)
        self._started = True
        self._close_emitted = False
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._emit_open()

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

    def _create_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - 依赖运行时环境。
            raise RuntimeError("AioHttpWebSocketBridgeTransport 需要安装 aiohttp") from exc
        return aiohttp.ClientSession()

    async def _reader_loop(self) -> None:
        try:
            async for message in self._websocket:
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

    async def _emit_open(self) -> None:
        self._connected = True
        for callback in list(self._open_callbacks):
            await _maybe_await(callback())

    async def _emit_close_once(self) -> None:
        if not self._connected or self._close_emitted:
            return
        self._close_emitted = True
        self._connected = False
        for callback in list(self._close_callbacks):
            await _maybe_await(callback())


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value
