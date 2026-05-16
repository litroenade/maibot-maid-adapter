import json
from collections.abc import Mapping
from typing import Any

from ..constants import MAIBOT_REPLYER_TASK
from ..prompt_loader import render_prompt
from ..utils import first_non_blank


async def generate_external_reply(
    *,
    ctx: Any,
    settings: Any,
    turn_context: Mapping[str, Any],
    planner_reasoning: str,
    arguments: Mapping[str, Any],
    tool_call_id: str,
    planned_actions: list[dict[str, Any]] | None = None,
    action_error: str = "",
) -> dict[str, Any]:
    _ = settings
    prompt = _reply_prompt(
        turn_context=turn_context,
        planner_reasoning=planner_reasoning,
        arguments=arguments,
        tool_call_id=tool_call_id,
        planned_actions=planned_actions or [],
        action_error=action_error,
    )
    try:
        result = await ctx.llm.generate(prompt, model=MAIBOT_REPLYER_TASK)
    except Exception as exc:
        return {"success": False, "error": f"回复生成能力调用失败：{exc}"}
    if not isinstance(result, Mapping):
        return {"success": False, "error": "回复生成器返回了非对象结果"}
    if result.get("success") is False:
        return {"success": False, "error": first_non_blank(result.get("error"), "回复生成器返回失败")}

    reply_text = _clean_reply_text(first_non_blank(result.get("response"), result.get("content")))
    if not reply_text:
        return {"success": False, "error": "回复生成器返回空文本"}
    return {
        "success": True,
        "reply_text": reply_text,
        "target_message_id": _target_message_id(arguments),
        "model": first_non_blank(result.get("model"), result.get("model_name")),
    }


def _reply_prompt(
    *,
    turn_context: Mapping[str, Any],
    planner_reasoning: str,
    arguments: Mapping[str, Any],
    tool_call_id: str,
    planned_actions: list[dict[str, Any]],
    action_error: str,
) -> list[dict[str, str]]:
    payload = {
        "turn_context": dict(turn_context),
        "planner_decision": first_non_blank(planner_reasoning),
        "planned_java_actions": planned_actions,
        "action_error": first_non_blank(action_error),
        "reply_tool": {
            "call_id": first_non_blank(tool_call_id),
            "target_message_id": _target_message_id(arguments),
            "arguments": dict(arguments),
        },
    }
    return [
        {
            "role": "system",
            "content": render_prompt("reply_generator"),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        },
    ]


def _target_message_id(arguments: Mapping[str, Any]) -> str:
    return first_non_blank(
        arguments.get("msg_id"),
        arguments.get("message_id"),
        arguments.get("target_message_id"),
    )


def _clean_reply_text(text: str) -> str:
    cleaned = first_non_blank(text)
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    parsed = _reply_from_json(cleaned)
    if parsed:
        cleaned = parsed
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"', "“", "”"}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _reply_from_json(text: str) -> str:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, Mapping):
        return ""
    return first_non_blank(parsed.get("reply"), parsed.get("text"), parsed.get("content"))
