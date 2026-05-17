from dataclasses import dataclass
from hashlib import md5
from time import time
from typing import Any, Mapping

from ..constants import PLATFORM, SERVER_CHAT_GATEWAY_NAME, SERVER_CHAT_MESSAGE_TYPE
from ..protocol.frame import BridgeFrame, BridgeProtocolError
from ..utils import first_non_blank


@dataclass(frozen=True)
class ServerChatContext:
    message_id: str
    account_id: str
    room_id: str
    room_name: str
    speaker_id: str
    speaker_name: str
    text: str
    bot_name: str
    session_id: str


class ServerChatService:
    def __init__(
        self,
        *,
        ctx: Any,
        settings: Any,
        bot_name: str = "",
    ) -> None:
        self._ctx = ctx
        self._settings = settings
        self._bot_name = bot_name
        self._contexts_by_session_id: dict[str, list[ServerChatContext]] = {}

    async def handle(self, frame: BridgeFrame) -> bool:
        context = parse_server_chat_context(frame, self._settings, bot_name=self._bot_name)
        self._register_context(context)
        accepted = await self._ctx.gateway.route_message(
            gateway_name=SERVER_CHAT_GATEWAY_NAME,
            message=build_maibot_message(context),
            route_metadata=build_route_metadata(context),
            external_message_id=context.message_id,
            dedupe_key=f"maidbridge-server-chat:{context.message_id}",
        )
        if not accepted:
            self._unregister_context(context)
            self._ctx.logger.warning(
                f"MaidBridge 服务器群聊消息被 MaiBot gateway 拒绝 "
                f"[message_id={context.message_id}, room_id={context.room_id}, speaker_id={context.speaker_id}]"
            )
        return bool(accepted)

    def context_for_session(self, session_id: str) -> ServerChatContext | None:
        batch = self._batch_for_session(session_id)
        return batch[0] if batch else None

    def has_session(self, session_id: str) -> bool:
        return self.context_for_session(session_id) is not None

    def complete_session(self, session_id: str) -> None:
        context = self.context_for_session(session_id)
        if context is not None:
            self._unregister_context(context)

    def _register_context(self, context: ServerChatContext) -> None:
        self._contexts_by_session_id.setdefault(context.session_id, []).append(context)

    def _unregister_context(self, context: ServerChatContext) -> None:
        items = self._contexts_by_session_id.get(context.session_id)
        if not items:
            return
        retained = [item for item in items if item.message_id != context.message_id]
        if retained:
            self._contexts_by_session_id[context.session_id] = retained
        else:
            self._contexts_by_session_id.pop(context.session_id, None)

    def _batch_for_session(self, session_id: str) -> list[ServerChatContext]:
        normalized = first_non_blank(session_id)
        if not normalized:
            return []
        return list(self._contexts_by_session_id.get(normalized) or [])


def parse_server_chat_context(frame: BridgeFrame, settings: Any, *, bot_name: str = "") -> ServerChatContext:
    if frame.type != SERVER_CHAT_MESSAGE_TYPE:
        raise BridgeProtocolError(f"不支持的服务器群聊帧类型：{frame.type}")
    payload = frame.payload
    message = _mapping(payload.get("message"))
    room = _mapping(payload.get("room"))
    speaker = _mapping(payload.get("speaker"))
    text = first_non_blank(message.get("text"))
    room_id = first_non_blank(room.get("id"))
    speaker_id = first_non_blank(speaker.get("id"))
    if not text:
        raise BridgeProtocolError("服务器群聊 message.text 不能为空")
    if not room_id:
        raise BridgeProtocolError("服务器群聊 room.id 不能为空")
    if not speaker_id:
        raise BridgeProtocolError("服务器群聊 speaker.id 不能为空")

    account_id = first_non_blank(getattr(settings, "server_id", ""))
    room_name = first_non_blank(room.get("name"), room_id)
    resolved_bot_name = first_non_blank(bot_name, getattr(settings, "agent_id", ""))
    return ServerChatContext(
        message_id=first_non_blank(frame.id, frame.request_id),
        account_id=account_id,
        room_id=room_id,
        room_name=room_name,
        speaker_id=speaker_id,
        speaker_name=first_non_blank(speaker.get("name"), speaker_id),
        text=text,
        bot_name=resolved_bot_name,
        session_id=_server_chat_session_id(platform=PLATFORM, account_id=account_id, room_id=room_id),
    )


def build_maibot_message(context: ServerChatContext) -> dict[str, Any]:
    mentioned = _is_mentioned(context.text, context.bot_name)
    additional_config = {
        "platform_io_account_id": context.account_id,
        "platform_io_scope": context.room_id,
        "maidbridge_server_chat": True,
        "minecraft_room_id": context.room_id,
        "minecraft_room_name": context.room_name,
        "speaker_uuid": context.speaker_id,
        "speaker_name": context.speaker_name,
        "bot_name": context.bot_name,
        "is_mentioned": 1.0 if mentioned else 0.0,
    }
    return {
        "message_id": context.message_id,
        "timestamp": str(time()),
        "platform": PLATFORM,
        "message_info": {
            "user_info": {
                "user_id": context.speaker_id,
                "user_nickname": context.speaker_name,
                "user_cardname": context.speaker_name,
            },
            "group_info": {
                "group_id": context.room_id,
                "group_name": context.room_name,
            },
            "additional_config": additional_config,
        },
        "raw_message": [{"type": "text", "data": context.text}],
        "processed_plain_text": context.text,
        "is_mentioned": mentioned,
        "is_at": False,
        "session_id": context.session_id,
    }


def build_route_metadata(context: ServerChatContext) -> dict[str, Any]:
    return {
        "platform_io_account_id": context.account_id,
        "platform_io_scope": context.room_id,
        "minecraft_room_id": context.room_id,
        "minecraft_room_name": context.room_name,
        "speaker_uuid": context.speaker_id,
        "speaker_name": context.speaker_name,
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _is_mentioned(text: str, bot_name: str) -> bool:
    name = first_non_blank(bot_name)
    return bool(name and name in text)


def _server_chat_session_id(*, platform: str, account_id: str, room_id: str) -> str:
    components = [platform]
    if account_id:
        components.append(f"account:{account_id}")
    if room_id:
        components.append(f"scope:{room_id}")
        components.append(room_id)
    return md5("_".join(components).encode()).hexdigest()
