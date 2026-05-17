import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..constants import (
    CLIENT_TO_JAVA,
    DEFAULT_CLIENT_ENDPOINT_ID,
    DEFAULT_DEADLINE_MS,
    DEFAULT_JAVA_ENDPOINT_ID,
    DEFAULT_MAX_MESSAGE_BYTES,
    JAVA_TO_CLIENT,
    PROTOCOL,
    SERVER_CHAT_MESSAGE_TYPE,
)


class BridgeProtocolError(ValueError):
    """MaidBridge 协议载荷格式错误或存在安全风险时抛出。"""


def _require_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BridgeProtocolError(f"{key} 必须是非空字符串")
    return value.strip()


def _optional_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BridgeProtocolError(f"{key} 必须是字符串")
    return value.strip()


def _require_mapping(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, Mapping):
        raise BridgeProtocolError(f"{key} 必须是对象")
    return dict(value)


def _optional_list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise BridgeProtocolError(f"{key} 必须是列表")
    return [dict(item) if isinstance(item, Mapping) else item for item in value]


def _first_non_blank(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _require_direction(data: Mapping[str, Any]) -> str:
    direction = _require_string(data, "direction")
    if direction not in {CLIENT_TO_JAVA, JAVA_TO_CLIENT}:
        raise BridgeProtocolError(f"不支持的 direction：{direction}")
    return direction


def _put_mapping_value(payload: dict[str, Any], key: str, values: Mapping[str, Any]) -> None:
    clean_values = {name: value for name, value in values.items() if _first_non_blank(value)}
    if not clean_values:
        return
    current = payload.get(key)
    merged = dict(current) if isinstance(current, Mapping) else {}
    for name, value in clean_values.items():
        merged.setdefault(name, value)
    payload[key] = merged


def _payload_with_structured_identity(
    payload: Mapping[str, Any],
    *,
    maid_uuid: str = "",
    owner_uuid: str = "",
    player_uuid: str = "",
    dimension: str = "",
) -> dict[str, Any]:
    normalized = dict(payload)
    _put_mapping_value(normalized, "maid", {"uuid": maid_uuid, "owner_uuid": owner_uuid})
    _put_mapping_value(normalized, "sender", {"uuid": player_uuid})
    if dimension:
        raw_state = normalized.get("maid_state")
        state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
        raw_location = state.get("location")
        location = dict(raw_location) if isinstance(raw_location, Mapping) else {}
        location.setdefault("dimension", dimension)
        state["location"] = location
        normalized["maid_state"] = state
    return normalized


def _is_maid_api_request_event(event_type: str) -> bool:
    return event_type.startswith("maid.api.") and not event_type.startswith("maid.api.registry.")


def _requires_maid_uuid(event_type: str) -> bool:
    return (
        event_type == "maid.agent.turn.complete"
        or _is_maid_api_request_event(event_type)
    )


def _require_payload_maid_uuid(payload: Mapping[str, Any]) -> None:
    raw_maid = payload.get("maid")
    maid = raw_maid if isinstance(raw_maid, Mapping) else {}
    if not _first_non_blank(maid.get("uuid")):
        raise BridgeProtocolError("payload.maid.uuid 不能为空")


def _put_maid_api_scope(payload: dict[str, Any], *, server_id: str, endpoint_id: str) -> None:
    if server_id:
        payload.setdefault("server_id", server_id)
    if endpoint_id:
        payload.setdefault("endpoint_id", endpoint_id)


def _reject_non_api_scope(event_type: str, *, server_id: str, endpoint_id: str) -> None:
    if _is_maid_api_request_event(event_type):
        return
    if _first_non_blank(server_id, endpoint_id):
        raise BridgeProtocolError("server_id 和 endpoint_id 只支持 maid.api 请求事件")


def _validate_turn_complete_payload(payload: Mapping[str, Any]) -> None:
    turn_id = _first_non_blank(payload.get("turn_id"))
    if not turn_id:
        raise BridgeProtocolError("payload.turn_id 不能为空")
    _require_payload_maid_uuid(payload)
    outcome = _first_non_blank(payload.get("outcome"))
    if outcome == "reply":
        reply = payload.get("reply")
        if not isinstance(reply, Mapping) or not _first_non_blank(reply.get("text")):
            raise BridgeProtocolError("payload.reply.text 不能为空")
        actions = payload.get("actions", [])
        if not isinstance(actions, list):
            raise BridgeProtocolError("payload.actions 必须是列表")
        return
    if outcome == "no_reply":
        actions = payload.get("actions", [])
        if not isinstance(actions, list):
            raise BridgeProtocolError("payload.actions 必须是列表")
        return
    raise BridgeProtocolError(f"不支持的 maid.agent.turn.complete outcome：{outcome}")


def _validate_server_chat_payload(payload: Mapping[str, Any]) -> None:
    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise BridgeProtocolError("payload.message 必须是对象")
    kind = _first_non_blank(message.get("kind"))
    if kind not in {"member", "system"}:
        raise BridgeProtocolError("payload.message.kind 只支持 member 或 system")
    if not _first_non_blank(message.get("text")):
        raise BridgeProtocolError("payload.message.text 不能为空")
    room = payload.get("room")
    if not isinstance(room, Mapping) or not _first_non_blank(room.get("id")) or not _first_non_blank(room.get("name")):
        raise BridgeProtocolError("payload.room.id 和 payload.room.name 不能为空")
    if kind == "member":
        speaker = payload.get("speaker")
        if not isinstance(speaker, Mapping) or not _first_non_blank(speaker.get("name")):
            raise BridgeProtocolError("payload.speaker.name 不能为空")


@dataclass(frozen=True)
class BridgeFrame:
    """Java、传输层和 Python 处理器共享的线级帧。"""

    protocol: str
    type: str
    id: str
    trace_id: str
    deadline_ms: int
    payload: dict[str, Any]
    request_id: str = ""
    reply_to: str = ""
    direction: str = JAVA_TO_CLIENT
    source_endpoint: str = DEFAULT_JAVA_ENDPOINT_ID
    target_endpoint: str = DEFAULT_CLIENT_ENDPOINT_ID

    def to_dict(self) -> dict[str, Any]:
        data = {
            "protocol": self.protocol,
            "type": self.type,
            "id": self.id,
            "direction": self.direction,
            "source_endpoint": self.source_endpoint,
            "target_endpoint": self.target_endpoint,
            "payload": self.payload,
        }
        if self.request_id and self.request_id != self.id:
            data["request_id"] = self.request_id
        if self.trace_id and self.trace_id != self.id:
            data["trace_id"] = self.trace_id
        if self.reply_to:
            data["reply_to"] = self.reply_to
        return data

    def dumps(self, *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> str:
        encoded = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_bytes:
            raise BridgeProtocolError("帧超过最大消息字节数")
        return encoded

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BridgeFrame":
        protocol = data.get("protocol")
        if protocol != PROTOCOL:
            raise BridgeProtocolError(f"不支持的协议：{protocol}")
        deadline_ms = data.get("deadline_ms", DEFAULT_DEADLINE_MS)
        if not isinstance(deadline_ms, int) or deadline_ms <= 0:
            raise BridgeProtocolError("deadline_ms 必须是正整数")
        payload = _require_mapping(data, "payload")
        frame_id = _require_string(data, "id")
        frame = cls(
            protocol=protocol,
            type=_require_string(data, "type"),
            id=frame_id,
            request_id=_optional_string(data, "request_id") or frame_id,
            reply_to=_optional_string(data, "reply_to"),
            direction=_require_direction(data),
            source_endpoint=_optional_string(data, "source_endpoint") or DEFAULT_JAVA_ENDPOINT_ID,
            target_endpoint=_optional_string(data, "target_endpoint") or DEFAULT_CLIENT_ENDPOINT_ID,
            trace_id=_optional_string(data, "trace_id") or frame_id,
            deadline_ms=deadline_ms,
            payload=payload,
        )
        frame.validate()
        return frame

    @classmethod
    def loads(cls, raw: str | bytes, *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> "BridgeFrame":
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        if len(raw_bytes) > max_bytes:
            raise BridgeProtocolError("帧超过最大消息字节数")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeProtocolError("帧不是合法 JSON") from exc
        if not isinstance(data, Mapping):
            raise BridgeProtocolError("帧根节点必须是对象")
        return cls.from_dict(data)

    def validate(self) -> None:
        if self.type == "maid.agent.turn.complete":
            _validate_turn_complete_payload(self.payload)
        if self.type == SERVER_CHAT_MESSAGE_TYPE:
            _validate_server_chat_payload(self.payload)
        if _requires_maid_uuid(self.type):
            _require_payload_maid_uuid(self.payload)


def build_ai_event_frame(
    *,
    event_type: str,
    event_id: str,
    request_id: str,
    trace_id: str,
    payload: Mapping[str, Any],
    server_id: str = "",
    endpoint_id: str = "",
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    maid_uuid: str = "",
    player_uuid: str = "",
    owner_uuid: str = "",
    dimension: str = "",
    direction: str = CLIENT_TO_JAVA,
    source_endpoint: str = DEFAULT_CLIENT_ENDPOINT_ID,
    target_endpoint: str = DEFAULT_JAVA_ENDPOINT_ID,
) -> BridgeFrame:
    """创建事件帧；是否等待业务响应由调用方决定。"""
    _reject_non_api_scope(event_type, server_id=server_id, endpoint_id=endpoint_id)
    normalized_payload = _payload_with_structured_identity(
        payload,
        maid_uuid=maid_uuid,
        owner_uuid=owner_uuid,
        player_uuid=player_uuid,
        dimension=dimension,
    )
    if _is_maid_api_request_event(event_type):
        _put_maid_api_scope(normalized_payload, server_id=server_id, endpoint_id=endpoint_id)
    frame = BridgeFrame(
        protocol=PROTOCOL,
        type=event_type,
        id=event_id,
        request_id=request_id or event_id,
        trace_id=trace_id or event_id,
        deadline_ms=deadline_ms,
        direction=direction,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        payload=normalized_payload,
    )
    BridgeFrame.from_dict(frame.to_dict())
    return frame


def build_session_initialize_frame(
    *,
    client_id: str,
    agent_id: str = "",
    agent_name: str = "",
    maid_uuid: str = "",
    roles: Iterable[str],
    subscriptions: Iterable[str],
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    trace_id: str = "",
    source_endpoint: str = DEFAULT_CLIENT_ENDPOINT_ID,
    target_endpoint: str = DEFAULT_JAVA_ENDPOINT_ID,
) -> BridgeFrame:
    frame_id = client_id.strip()
    if not frame_id:
        raise BridgeProtocolError("client_id 不能为空")
    normalized_agent_id = _first_non_blank(agent_id)
    normalized_agent_name = _first_non_blank(agent_name, normalized_agent_id)
    payload: dict[str, Any] = {
        "client_id": frame_id,
        "roles": [str(role) for role in roles if str(role).strip()],
        "subscriptions": [str(item) for item in subscriptions if str(item).strip()],
    }
    if normalized_agent_id or normalized_agent_name:
        payload["agent"] = {
            key: value
            for key, value in {"id": normalized_agent_id, "name": normalized_agent_name}.items()
            if value
        }
    normalized_maid_uuid = _first_non_blank(maid_uuid)
    if normalized_maid_uuid:
        payload["maid"] = {"uuid": normalized_maid_uuid}
    frame = BridgeFrame(
        protocol=PROTOCOL,
        type="bridge.session.initialize",
        id=frame_id,
        request_id=frame_id,
        trace_id=trace_id or frame_id,
        deadline_ms=deadline_ms,
        direction=CLIENT_TO_JAVA,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        payload=payload,
    )
    BridgeFrame.from_dict(frame.to_dict())
    return frame


def build_maid_agent_turn_complete_frame(
    *,
    turn_id: str,
    maid_uuid: str,
    outcome: str,
    reply_text: str = "",
    tts_text: str = "",
    reason: str = "",
    history_policy: str = "append",
    actions: Iterable[Mapping[str, Any]] | None = None,
    agent_id: str = "",
    agent_name: str = "",
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    trace_id: str = "",
    reply_to: str = "",
    source_endpoint: str = DEFAULT_CLIENT_ENDPOINT_ID,
    target_endpoint: str = DEFAULT_JAVA_ENDPOINT_ID,
) -> BridgeFrame:
    normalized_turn_id = _first_non_blank(turn_id)
    if not normalized_turn_id:
        raise BridgeProtocolError("turn_id 不能为空")
    payload: dict[str, Any] = {
        "turn_id": normalized_turn_id,
        "maid": {"uuid": _first_non_blank(maid_uuid)},
        "outcome": _first_non_blank(outcome),
    }
    normalized_agent_id = _first_non_blank(agent_id)
    normalized_agent_name = _first_non_blank(agent_name)
    if normalized_agent_id or normalized_agent_name:
        payload["agent"] = {
            key: value
            for key, value in {"id": normalized_agent_id, "name": normalized_agent_name}.items()
            if value
        }
    if payload["outcome"] == "reply":
        text = _first_non_blank(reply_text)
        if not text:
            raise BridgeProtocolError("reply_text 不能为空")
        reply = {"text": text}
        if _first_non_blank(tts_text):
            reply["tts_text"] = _first_non_blank(tts_text)
        payload["reply"] = reply
        payload["history"] = {"policy": _first_non_blank(history_policy, "append")}
        payload["actions"] = [dict(action) for action in actions or []]
    elif payload["outcome"] == "no_reply":
        payload["reason"] = _first_non_blank(reason, "no_reply")
        payload["actions"] = [dict(action) for action in actions or []]
    else:
        raise BridgeProtocolError(f"不支持的 maid.agent.turn.complete outcome：{payload['outcome']}")
    frame = BridgeFrame(
        protocol=PROTOCOL,
        type="maid.agent.turn.complete",
        id=normalized_turn_id,
        request_id=normalized_turn_id,
        reply_to=_first_non_blank(reply_to),
        trace_id=_first_non_blank(trace_id, normalized_turn_id),
        deadline_ms=deadline_ms,
        direction=CLIENT_TO_JAVA,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        payload=payload,
    )
    BridgeFrame.from_dict(frame.to_dict())
    return frame


def build_server_chat_message_frame(
    *,
    room_id: str,
    room_name: str,
    text: str,
    kind: str = "member",
    speaker_id: str = "",
    speaker_name: str = "",
    metadata: Mapping[str, Any] | None = None,
    deadline_ms: int = DEFAULT_DEADLINE_MS,
    trace_id: str = "",
    source_endpoint: str = DEFAULT_CLIENT_ENDPOINT_ID,
    target_endpoint: str = DEFAULT_JAVA_ENDPOINT_ID,
) -> BridgeFrame:
    normalized_kind = _first_non_blank(kind)
    if normalized_kind not in {"member", "system"}:
        raise BridgeProtocolError("服务器群聊消息 kind 只支持 member 或 system")
    payload: dict[str, Any] = {
        "message": {
            "kind": normalized_kind,
            "text": _first_non_blank(text),
        },
        "room": {
            "id": _first_non_blank(room_id),
            "name": _first_non_blank(room_name),
        },
        "metadata": dict(metadata or {}),
    }
    if normalized_kind == "member":
        payload["speaker"] = {
            "id": _first_non_blank(speaker_id),
            "name": _first_non_blank(speaker_name),
        }
    frame_id = _first_non_blank(str(payload["metadata"].get("message_id") or ""))
    if not frame_id:
        from uuid import uuid4

        frame_id = f"server-chat-{uuid4()}"
    frame = BridgeFrame(
        protocol=PROTOCOL,
        type=SERVER_CHAT_MESSAGE_TYPE,
        id=frame_id,
        request_id=frame_id,
        trace_id=_first_non_blank(trace_id, f"trace-{frame_id}"),
        deadline_ms=deadline_ms,
        direction=CLIENT_TO_JAVA,
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        payload=payload,
    )
    BridgeFrame.from_dict(frame.to_dict())
    return frame
