import json
from collections.abc import Mapping
from typing import Any

from ..prompt_loader import render_prompt
from ..utils import first_non_blank


async def plan_external_actions(
    *,
    ctx: Any,
    settings: Any,
    turn_context: Mapping[str, Any],
    planner_reasoning: str,
    arguments: Mapping[str, Any],
    tool_call_id: str,
) -> dict[str, Any]:
    if not settings.enable_agent_actions:
        return {"success": True, "actions": [], "reason": "maid_turn_actions_disabled"}
    action_defs = _available_actions(
        turn_context,
        include_emoji_bubbles=settings.enable_agent_emoji_bubbles,
    )
    if not action_defs:
        return {"success": True, "actions": [], "reason": "no_java_actions"}

    try:
        result = await ctx.llm.generate(
            _action_prompt(
                turn_context=turn_context,
                planner_reasoning=planner_reasoning,
                arguments=arguments,
                tool_call_id=tool_call_id,
            ),
            model=settings.action_planning_model,
            temperature=float(settings.action_planning_temperature),
            max_tokens=int(settings.action_planning_max_tokens),
        )
    except Exception as exc:
        return {"success": False, "actions": [], "error": str(exc)}
    if not isinstance(result, Mapping):
        return {"success": False, "actions": [], "error": "动作规划器返回了非对象结果"}
    if result.get("success") is False:
        return {"success": False, "actions": [], "error": first_non_blank(result.get("error"), "动作规划器返回失败")}

    parsed = _json_payload(first_non_blank(result.get("response"), result.get("content")))
    if not isinstance(parsed, Mapping):
        return {"success": False, "actions": [], "error": "动作规划器未返回 JSON 对象"}
    actions, warnings = _validated_actions(parsed.get("actions"), action_defs, _nearby_entity_ids(turn_context))
    return {
        "success": True,
        "actions": actions,
        "warnings": warnings,
        "model": first_non_blank(result.get("model"), result.get("model_name")),
    }


def _action_prompt(
    *,
    turn_context: Mapping[str, Any],
    planner_reasoning: str,
    arguments: Mapping[str, Any],
    tool_call_id: str,
) -> list[dict[str, str]]:
    payload = {
        "turn_context": dict(turn_context),
        "planner_decision": first_non_blank(planner_reasoning),
        "reply_tool": {
            "call_id": first_non_blank(tool_call_id),
            "arguments": dict(arguments),
        },
    }
    return [
        {
            "role": "system",
            "content": render_prompt("action_planner"),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        },
    ]


def _available_actions(
    turn_context: Mapping[str, Any],
    *,
    include_emoji_bubbles: bool = True,
) -> dict[str, Mapping[str, Any]]:
    actions = turn_context.get("actions")
    if not isinstance(actions, Mapping):
        return {}
    items = actions.get("items")
    if not isinstance(items, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        action_id = _canonical_action_type(first_non_blank(item.get("id"), item.get("name")))
        if action_id == "show_emoji_bubble" and not include_emoji_bubbles:
            continue
        if action_id:
            result[action_id] = item
    return result


def _validated_actions(raw_actions: Any, action_defs: Mapping[str, Mapping[str, Any]], nearby_entity_ids: set[int]) -> tuple[list[dict[str, Any]], list[str]]:
    if raw_actions is None:
        return [], []
    if not isinstance(raw_actions, list):
        return [], ["actions 不是列表"]
    actions: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw_action in enumerate(raw_actions):
        if not isinstance(raw_action, Mapping):
            warnings.append(f"第 {index} 个 action 不是对象")
            continue
        action_type = _canonical_action_type(first_non_blank(
            raw_action.get("type"),
            raw_action.get("action"),
            raw_action.get("tool_id"),
            raw_action.get("name"),
            raw_action.get("id"),
        ))
        if action_type not in action_defs:
            warnings.append(f"未知 action 类型：{action_type}")
            continue
        action = _normalize_action(action_type, raw_action, action_defs[action_type], nearby_entity_ids)
        if action:
            actions.append(action)
        else:
            warnings.append(f"action 参数无效：{action_type}")
    return actions, warnings


def _normalize_action(
    action_type: str,
    raw_action: Mapping[str, Any],
    action_def: Mapping[str, Any],
    nearby_entity_ids: set[int],
) -> dict[str, Any]:
    parameters = _action_parameters(raw_action)
    if action_type == "switch_sit":
        value = _bool_value(parameters, "sit", "value", "enabled")
        return {"type": action_type, "sit": value} if value is not None else {}
    if action_type == "switch_follow_state":
        value = _bool_value(parameters, "follow", "value", "enabled")
        return {"type": action_type, "follow": value} if value is not None else {}
    if action_type == "switch_schedule":
        schedule = first_non_blank(parameters.get("schedule"), parameters.get("value")).upper()
        allowed = _enum_values(action_def, "schedule")
        if schedule and allowed and schedule in allowed:
            return {"type": action_type, "schedule": schedule}
        return {}
    if action_type == "switch_work_task":
        task_id = first_non_blank(parameters.get("task_id"), parameters.get("value"))
        allowed = _task_ids(action_def)
        if not task_id or not allowed or task_id not in allowed:
            return {}
        action: dict[str, Any] = {"type": action_type, "task_id": task_id}
        entity_id = _int_value(parameters, "entity_id", "target_entity_id")
        if entity_id is not None and entity_id in nearby_entity_ids:
            action["entity_id"] = entity_id
        return action
    if action_type == "show_emoji_bubble":
        kind = _emoji_kind(parameters)
        allowed = _enum_values(action_def, "kind")
        if allowed and kind not in allowed:
            return {}
        return {"type": action_type, "kind": kind}
    return {}


def _action_parameters(raw_action: Mapping[str, Any]) -> dict[str, Any]:
    parameters = raw_action.get("parameters")
    if isinstance(parameters, Mapping):
        # LLM 常按协议外壳输出 id + parameters；id 表示动作名，不能再当作工作任务 ID。
        payload = dict(parameters)
        for key, value in raw_action.items():
            if key in {"parameters", "id", "type", "action", "tool_id", "name"}:
                continue
            payload.setdefault(key, value)
        return payload
    return dict(raw_action)


def _canonical_action_type(raw: str) -> str:
    normalized = first_non_blank(raw).lower().replace("-", "_").replace(".", "_")
    return {
        "sit": "switch_sit",
        "set_sit": "switch_sit",
        "sitting": "switch_sit",
        "follow": "switch_follow_state",
        "set_follow": "switch_follow_state",
        "following": "switch_follow_state",
        "schedule": "switch_schedule",
        "set_schedule": "switch_schedule",
        "task": "switch_work_task",
        "work": "switch_work_task",
        "work_task": "switch_work_task",
        "set_task": "switch_work_task",
        "emoji": "show_emoji_bubble",
        "emoji_bubble": "show_emoji_bubble",
        "show_emoji": "show_emoji_bubble",
        "show_maid_emoji": "show_emoji_bubble",
    }.get(normalized, normalized)


def _emoji_kind(parameters: Mapping[str, Any]) -> str:
    raw = first_non_blank(parameters.get("kind"), parameters.get("emoji_kind"), parameters.get("mode"), parameters.get("value"), "image")
    normalized = raw.lower().replace("-", "_").replace(".", "_")
    if normalized in {"kaomoji", "text", "routine_kaomoji"}:
        return "kaomoji"
    return "image"


def _bool_value(source: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            return value
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in {"true", "yes", "1", "on"}:
            return True
        if text in {"false", "no", "0", "off"}:
            return False
    return None


def _int_value(source: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            continue
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _task_ids(action_def: Mapping[str, Any]) -> set[str]:
    values = set(_enum_values(action_def, "task_id"))
    choices = action_def.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, Mapping):
                choice_id = first_non_blank(choice.get("id"))
                if choice_id:
                    values.add(choice_id)
    return values


def _enum_values(action_def: Mapping[str, Any], property_name: str) -> set[str]:
    parameters = action_def.get("parameters")
    if not isinstance(parameters, Mapping):
        return set()
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    property_schema = properties.get(property_name)
    if not isinstance(property_schema, Mapping):
        return set()
    raw_enum = property_schema.get("enum")
    if not isinstance(raw_enum, list):
        return set()
    return {first_non_blank(item) for item in raw_enum if first_non_blank(item)}


def _nearby_entity_ids(turn_context: Mapping[str, Any]) -> set[int]:
    action_context = turn_context.get("action_context")
    if not isinstance(action_context, Mapping):
        return set()
    return _entity_ids_from(action_context.get("nearby_living_entities"))


def _entity_ids_from(entities: Any) -> set[int]:
    if not isinstance(entities, list):
        return set()
    ids: set[int] = set()
    for entity in entities:
        if not isinstance(entity, Mapping):
            continue
        entity_id = _int_value(entity, "entity_id")
        if entity_id is not None:
            ids.add(entity_id)
    return ids


def _json_payload(text: str) -> Any:
    cleaned = first_non_blank(text)
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
