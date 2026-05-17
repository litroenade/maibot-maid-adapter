from typing import TYPE_CHECKING, Any, Mapping

from maibot_sdk import MessageGateway

from ..constants import (
    PLATFORM,
    PROTOCOL,
    SERVER_CHAT_GATEWAY_NAME,
    SERVER_CHAT_RESPONSE_TYPE,
)
from ..protocol.frame import build_server_chat_message_frame
from ..utils import first_non_blank


class ServerChatGateway:
    if TYPE_CHECKING:
        _bot_name: str

        @property
        def ctx(self) -> Any:
            raise NotImplementedError

        def _settings(self) -> Any:
            raise NotImplementedError

        async def _send_frame_await_reply(self, frame: Any, *, settings: Any) -> dict[str, Any]:
            raise NotImplementedError

    @MessageGateway(
        name=SERVER_CHAT_GATEWAY_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol=PROTOCOL,
        description="MaidBridge Minecraft 服务器群聊网关",
    )
    async def handle_maidbridge_server_chat_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        settings = self._settings()
        route = dict(route or {})
        metadata = dict(metadata or {})
        text = _message_text(message)
        if not text:
            return {"success": False, "error": "服务器群聊出站消息没有可发送的文本"}

        additional_config = _additional_config(message)
        room_id = first_non_blank(
            route.get("scope"),
            additional_config.get("minecraft_room_id"),
            additional_config.get("platform_io_scope"),
        )
        room_name = first_non_blank(additional_config.get("minecraft_room_name"), room_id)
        if not room_id:
            return {"success": False, "error": "服务器群聊出站缺少 room_id"}

        bot_name = first_non_blank(self._bot_name, settings.agent_id)
        frame = build_server_chat_message_frame(
            room_id=room_id,
            room_name=room_name,
            text=text,
            kind="member",
            speaker_id=settings.agent_id,
            speaker_name=bot_name,
            metadata={
                "message_id": first_non_blank(message.get("message_id")),
                "route": route,
                "gateway_metadata": metadata,
            },
            deadline_ms=settings.request_timeout_ms,
        )
        reply = await self._send_frame_await_reply(frame, settings=settings)
        payload = reply.get("payload") if isinstance(reply, Mapping) else {}
        if reply.get("type") != SERVER_CHAT_RESPONSE_TYPE:
            error = _response_error(payload) or f"服务器群聊回写返回了异常响应：{reply.get('type')}"
            return {"success": False, "error": error}
        if isinstance(payload, Mapping) and payload.get("error"):
            return {"success": False, "error": str(payload.get("error"))}
        return {
            "success": True,
            "external_message_id": frame.id,
            "metadata": {
                "maidbridge": {
                    "room_id": room_id,
                    "room_name": room_name,
                    "kind": "member",
                }
            },
        }


def _additional_config(message: dict[str, Any]) -> dict[str, Any]:
    message_info = message.get("message_info")
    if not isinstance(message_info, Mapping):
        return {}
    additional_config = message_info.get("additional_config")
    return dict(additional_config) if isinstance(additional_config, Mapping) else {}


def _message_text(message: dict[str, Any]) -> str:
    raw_message = message.get("raw_message")
    parts: list[str] = []
    if isinstance(raw_message, list):
        for component in raw_message:
            if not isinstance(component, Mapping):
                continue
            if str(component.get("type") or "").strip().lower() != "text":
                continue
            text = first_non_blank(component.get("data"))
            if text:
                parts.append(text)
    processed_text = first_non_blank(message.get("processed_plain_text"))
    if processed_text == "[表情包]":
        processed_text = ""
    return first_non_blank("\n".join(parts), processed_text)


def _response_error(payload: Any) -> str:
    if isinstance(payload, Mapping):
        return str(payload.get("error") or "")
    return ""
