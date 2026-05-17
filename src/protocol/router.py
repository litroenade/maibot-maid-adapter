from dataclasses import dataclass
from typing import Any, Mapping

from ..constants import SERVER_CHAT_MESSAGE_TYPE, SERVER_CHAT_RESPONSE_TYPE
from . import query_api
from .frame import BridgeFrame


@dataclass(frozen=True)
class RouteDecision:
    kind: str
    payload: dict[str, Any]


_REGISTRY_EVENTS = {
    "maid.api.registry.tools": ("tools", "tools"),
    "maid.api.registry.skills": ("skills", "skills"),
    "maid.api.registry.contexts": ("contexts", "contexts"),
    "maid.api.registry.tasks": ("tasks", "tasks"),
    "maid.api.registry.sites": ("sites", "sites"),
}

_AI_CHAIN_EVENTS = frozenset(
    {
        "maid.ai.request.received",
        "maid.ai.prompt.built",
        "maid.ai.llm.client.selected",
        "maid.ai.llm.request",
        "maid.ai.llm.raw_response",
        "maid.ai.tool_calls.proposed",
        "maid.ai.tool_call.decoded",
        "maid.ai.tool_result.added",
        "maid.ai.output.final",
        "maid.ai.output.failure",
        "maid.ai.tts.request",
    }
)
_SERVER_EVENT_PREFIX = "maidbridge.server."


def route_frame(
    frame: BridgeFrame,
    *,
    default_server_id: str = "",
) -> RouteDecision:
    registry_target = _REGISTRY_EVENTS.get(frame.type)
    if registry_target is not None:
        kind, payload_key = registry_target
        return _route_registry_catalog(frame, kind=kind, payload_key=payload_key, default_server_id=default_server_id)
    if frame.type == "bridge.session.ready":
        return _route_session_ready(frame)
    if frame.type == "maid.api.response":
        return _route_domain_response(frame, "api_response")
    if frame.type == SERVER_CHAT_RESPONSE_TYPE:
        return _route_domain_response(frame, "server_chat_response")
    if frame.type == "bridge.error":
        return _route_domain_response(frame, "bridge_error")
    if frame.type == "maid.agent.turn.request":
        return _route_maid_turn(frame)
    if frame.type == SERVER_CHAT_MESSAGE_TYPE:
        return _route_server_chat(frame)
    if frame.type in _AI_CHAIN_EVENTS or frame.type.startswith(_SERVER_EVENT_PREFIX):
        direction_error = _java_to_client_error(frame)
        if direction_error is not None:
            return direction_error
        return RouteDecision(kind="observe", payload={"accepted": frame.type})
    return RouteDecision(kind="bridge_error", payload={"error": f"不支持的帧类型：{frame.type}"})


def _route_session_ready(frame: BridgeFrame) -> RouteDecision:
    direction_error = _java_to_client_error(frame)
    if direction_error is not None:
        return direction_error
    payload = dict(frame.payload)
    features = payload.get("features")
    capabilities = payload.get("capabilities")
    return RouteDecision(
        kind="session_ready",
        payload={
            "accepted": frame.type,
            "trace_id": frame.trace_id,
            "request_id": frame.request_id,
            "reply_to": frame.reply_to,
            "server_id": _payload_server_id(frame),
            "endpoint_id": _payload_endpoint_id(frame),
            "server_name": str(payload.get("server_name") or ""),
            "source_endpoint": frame.source_endpoint,
            "target_endpoint": frame.target_endpoint,
            "schema_version": str(payload.get("schema_version") or ""),
            "features": dict(features) if isinstance(features, Mapping) else {},
            "capabilities": dict(capabilities) if isinstance(capabilities, Mapping) else {},
        },
    )


def _route_domain_response(frame: BridgeFrame, kind: str) -> RouteDecision:
    direction_error = _java_to_client_error(frame)
    if direction_error is not None:
        return direction_error
    payload = dict(frame.payload)
    return RouteDecision(
        kind=kind,
        payload={
            "type": frame.type,
            "reply_to": frame.reply_to or frame.request_id,
            "trace_id": frame.trace_id,
            "payload": payload,
        },
    )


def _route_registry_catalog(
    frame: BridgeFrame,
    *,
    kind: str,
    payload_key: str,
    default_server_id: str,
) -> RouteDecision:
    direction_error = _java_to_client_error(frame)
    if direction_error is not None:
        return direction_error
    raw_items = frame.payload.get(payload_key)
    if not isinstance(raw_items, list):
        return RouteDecision(kind="bridge_error", payload={"error": f"{payload_key} 必须是列表"})
    items = [dict(item) for item in raw_items if isinstance(item, Mapping)]
    if len(items) != len(raw_items):
        return RouteDecision(kind="bridge_error", payload={"error": f"{payload_key} 的条目必须是对象"})
    revision = frame.payload.get("revision", 0)
    if not isinstance(revision, int) or revision < 0:
        return RouteDecision(kind="bridge_error", payload={"error": "revision 必须是非负整数"})
    registry_id = frame.payload.get("registry_id", "")
    if not isinstance(registry_id, str) or not registry_id.strip():
        return RouteDecision(kind="bridge_error", payload={"error": "registry_id 不能为空"})
    server_id = _first_non_empty(frame.payload.get("server_id"), frame.source_endpoint, default_server_id)
    endpoint_id = _first_non_empty(frame.payload.get("endpoint_id"), frame.source_endpoint, server_id)
    query_api.store_catalog(
        kind,
        items,
        trace_id=frame.trace_id,
        server_id=server_id,
        endpoint_id=endpoint_id,
        registry_id=registry_id,
        revision=revision,
        source="maidbridge",
        visibility=_first_non_empty(frame.payload.get("visibility"), "private"),
    )
    return RouteDecision(kind="observe", payload={"updated": kind, "count": len(items)})


def _java_to_client_error(frame: BridgeFrame) -> RouteDecision | None:
    if frame.direction == "java_to_client":
        return None
    return RouteDecision(kind="bridge_error", payload={"error": f"{frame.type} 的 direction 必须是 java_to_client"})


def _route_maid_turn(frame: BridgeFrame) -> RouteDecision:
    direction_error = _java_to_client_error(frame)
    if direction_error is not None:
        return direction_error
    message = _message_text(frame.payload.get("message"))
    if not message.strip():
        return RouteDecision(kind="bridge_error", payload={"error": "女仆回合 message 必须是非空字符串"})
    maid = frame.payload.get("maid") if isinstance(frame.payload.get("maid"), Mapping) else {}
    turn_id = _first_non_empty(frame.payload.get("turn_id"), frame.request_id, frame.id)
    maid_uuid = _first_non_empty(maid.get("uuid") if isinstance(maid, Mapping) else "")
    if not turn_id:
        return RouteDecision(kind="bridge_error", payload={"error": "女仆回合 turn_id 不能为空"})
    if not maid_uuid:
        return RouteDecision(kind="bridge_error", payload={"error": "payload.maid.uuid 不能为空"})
    return RouteDecision(
        kind="maid_turn",
        payload={
            "accepted": frame.type,
            "request_id": frame.request_id,
            "turn_id": turn_id,
            "message": message,
            "maid": {"uuid": maid_uuid},
        },
    )


def _route_server_chat(frame: BridgeFrame) -> RouteDecision:
    direction_error = _java_to_client_error(frame)
    if direction_error is not None:
        return direction_error
    return RouteDecision(
        kind="server_chat",
        payload={
            "accepted": frame.type,
            "request_id": frame.request_id,
        },
    )


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _payload_server_id(frame: BridgeFrame) -> str:
    return _first_non_empty(frame.payload.get("server_id"), frame.source_endpoint)


def _payload_endpoint_id(frame: BridgeFrame) -> str:
    return _first_non_empty(frame.payload.get("endpoint_id"), frame.source_endpoint)


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, Mapping):
        return _first_non_empty(message.get("text"), message.get("chat_text"), message.get("content"))
    return ""
