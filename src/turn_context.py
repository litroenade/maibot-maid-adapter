from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import md5
from time import time
from typing import Any

from .constants import PLATFORM
from .protocol.frame import BridgeFrame, BridgeProtocolError
from .utils import first_non_blank


@dataclass(frozen=True)
class TurnContext:
    frame: BridgeFrame
    turn_id: str
    maid_uuid: str
    bot_name: str
    speaker_uuid: str
    speaker_name: str
    text: str
    scope: str
    session_id: str
    maid: dict[str, Any]
    speaker: dict[str, Any]
    state: dict[str, Any]
    action_context: dict[str, Any]
    actions: list[dict[str, Any]]


def parse_turn_context(frame: BridgeFrame, settings: Any, *, bot_name: str = "") -> TurnContext:
    if frame.type != "maid.agent.turn.request":
        raise BridgeProtocolError(f"不支持的女仆 agent 轮次帧类型：{frame.type}")
    payload = frame.payload
    turn_id = first_non_blank(payload.get("turn_id"), frame.request_id, frame.id)
    maid = _mapping(payload.get("maid"))
    maid_uuid = first_non_blank(maid.get("uuid"))
    if not turn_id:
        raise BridgeProtocolError("maid.agent.turn.request turn_id 不能为空")
    if not maid_uuid:
        raise BridgeProtocolError("maid.agent.turn.request maid.uuid 不能为空")
    text = _message_text(payload.get("message"))
    if not text:
        raise BridgeProtocolError("maid.agent.turn.request message 文本不能为空")
    speaker = _mapping(payload.get("speaker"))
    scope = f"maid:{maid_uuid}"
    resolved_bot_name = first_non_blank(
        bot_name,
        getattr(settings, "agent_id", ""),
        "maibot",
    )
    return TurnContext(
        frame=frame,
        turn_id=turn_id,
        maid_uuid=maid_uuid,
        bot_name=resolved_bot_name,
        speaker_uuid=first_non_blank(speaker.get("uuid"), speaker.get("source_member_id"), "minecraft-user"),
        speaker_name=first_non_blank(speaker.get("name"), speaker.get("nickname"), "Player"),
        text=text,
        scope=scope,
        session_id=_maisaka_session_id(
            platform=PLATFORM,
            group_id=scope,
            account_id=maid_uuid,
            scope=scope,
        ),
        maid=maid,
        speaker=speaker,
        state=_mapping(payload.get("state")),
        action_context=_mapping(payload.get("action_context")),
        actions=_mapping_list(payload.get("actions")),
    )


def build_maibot_message(context: TurnContext) -> dict[str, Any]:
    additional_config = {
        "platform_io_account_id": context.maid_uuid,
        "platform_io_scope": context.scope,
        "maidbridge_turn_id": context.turn_id,
        "maid_uuid": context.maid_uuid,
        "bot_name": context.bot_name,
        "minecraft_channel_id": context.scope,
        "minecraft_channel_name": context.bot_name,
        "speaker_uuid": context.speaker_uuid,
        "speaker_name": context.speaker_name,
        "maid_state": dict(context.state),
        "maid_action_context": dict(context.action_context),
    }
    return {
        "message_id": context.turn_id,
        "timestamp": str(time()),
        "platform": PLATFORM,
        "message_info": {
            "user_info": {
                "user_id": context.speaker_uuid,
                "user_nickname": context.speaker_name,
                "user_cardname": context.speaker_name,
            },
            "group_info": {
                "group_id": context.scope,
                "group_name": context.bot_name,
            },
            "additional_config": additional_config,
        },
        "raw_message": [{"type": "text", "data": context.text}],
        "processed_plain_text": context.text,
        "session_id": context.session_id,
    }


def build_route_metadata(context: TurnContext) -> dict[str, Any]:
    return {
        "platform_io_account_id": context.maid_uuid,
        "platform_io_scope": context.scope,
        "maidbridge_turn_id": context.turn_id,
        "maid_uuid": context.maid_uuid,
        "minecraft_channel_id": context.scope,
        "minecraft_channel_name": context.bot_name,
        "bot_name": context.bot_name,
        "speaker_uuid": context.speaker_uuid,
        "speaker_name": context.speaker_name,
    }


def build_planner_context(context: TurnContext) -> dict[str, Any]:
    maid = {"name": context.bot_name}
    if model_id := first_non_blank(context.maid.get("model_id")):
        maid["model_id"] = model_id
    payload: dict[str, Any] = {
        "turn_id": context.turn_id,
        "maid": maid,
        "speaker": _selected_mapping(context.speaker, "uuid", "name", "language"),
        "state": dict(context.state),
    }
    if context.action_context:
        payload["action_context"] = dict(context.action_context)
    actions = _capability_summary(context.actions)
    if actions:
        payload["actions"] = actions
    return payload


def build_reply_generation_context(context: TurnContext) -> dict[str, Any]:
    payload = build_planner_context(context)
    payload["message"] = {
        "text": context.text,
        "turn_id": context.turn_id,
    }
    payload["route"] = {
        "platform": PLATFORM,
        "session_id": context.session_id,
        "scope": context.scope,
        "platform_io_account_id": context.maid_uuid,
        "speaker_uuid": context.speaker_uuid,
    }
    return payload


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _message_text(message: Any) -> str:
    if isinstance(message, Mapping):
        return first_non_blank(message.get("text"), message.get("plain_text"), message.get("content"))
    return first_non_blank(message)


def _selected_mapping(source: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        result[key] = value
    return result


def _capability_summary(items: list[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
    if not items:
        return {}
    selected = [_capability_item_summary(item) for item in items[:limit]]
    payload: dict[str, Any] = {"count": len(items), "items": selected}
    omitted = len(items) - len(selected)
    if omitted > 0:
        payload["omitted_count"] = omitted
    return payload


def _capability_item_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("id", "name", "summary", "description"):
        value = first_non_blank(item.get(key))
        if value:
            summary[key] = value
    parameters = item.get("parameters")
    if isinstance(parameters, Mapping):
        summary["parameters"] = _compact_schema(parameters)
    choices = item.get("choices")
    if isinstance(choices, list):
        compact_choices = _compact_choices(choices)
        if compact_choices:
            summary["choices"] = compact_choices
    return summary


def _compact_schema(schema: Mapping[str, Any], *, max_chars: int = 1200) -> dict[str, Any]:
    payload = dict(schema)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= max_chars:
        return payload
    properties = payload.get("properties")
    compact: dict[str, Any] = {}
    if type_name := first_non_blank(payload.get("type")):
        compact["type"] = type_name
    required = payload.get("required")
    if isinstance(required, list):
        compact["required"] = [str(item) for item in required]
    if isinstance(properties, Mapping):
        compact["properties"] = {
            str(key): _compact_property_schema(value)
            for key, value in list(properties.items())[:20]
        }
    return compact


def _compact_property_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, Mapping):
        return {}
    result: dict[str, Any] = {}
    if type_name := first_non_blank(schema.get("type")):
        result["type"] = type_name
    if description := first_non_blank(schema.get("description")):
        result["description"] = description
    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        result["enum"] = [str(item) for item in enum_values[:80] if str(item).strip()]
    return result


def _compact_choices(choices: list[Any], *, limit: int = 80) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for choice in choices:
        if len(result) >= limit:
            break
        if not isinstance(choice, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in ("id", "name", "summary", "description"):
            value = first_non_blank(choice.get(key))
            if value:
                item[key] = value
        if item:
            result.append(item)
    return result


def _maisaka_session_id(*, platform: str, group_id: str, account_id: str, scope: str) -> str:
    components = [platform]
    if account_id:
        components.append(f"account:{account_id}")
    if scope:
        components.append(f"scope:{scope}")
    components.append(group_id)
    return md5("_".join(components).encode()).hexdigest()
