import json
from collections.abc import Mapping
from typing import Any

from maibot_sdk import HookHandler

from .action_planner import plan_external_actions
from .prompt_loader import render_prompt
from .reply_generator import generate_external_reply
from .utils import first_non_blank

MAID_TURN_COMPLETION_HOOK_TIMEOUT_MS = 35_000
PHASE_TIMING_GATE = "timing_gate"
PHASE_PLANNER = "planner"
PHASE_UNKNOWN = "unknown"


class MaidPlannerHooks:
    @HookHandler(
        "maisaka.planner.before_request",
        name="maidbridge_maid_planner_direct_reply",
        mode="blocking",
        order="early",
        timeout_ms=3000,
    )
    async def prepare_maid_planner_request(
        self,
        messages: list[dict[str, Any]] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        service = getattr(self, "_maid_agent_turn_service", None)
        if not _has_pending_maid_session(service, session_id):
            return hook_continue()

        request_phase = _request_phase_from_tool_definitions(tool_definitions or [])
        _remember_maid_request_phase(self, session_id, request_phase)
        if request_phase != PHASE_PLANNER:
            return hook_continue()

        normalized_messages = list(messages or [])
        turn_context = _maid_planner_context(service, session_id)
        normalized_messages.append(
            {
                "role": "system",
                "content": _maidbridge_prompt_patch(turn_context),
            }
        )
        return hook_continue(messages=normalized_messages)

    @HookHandler(
        "maisaka.planner.after_response",
        name="maidbridge_maid_turn_completion",
        mode="blocking",
        order="late",
        timeout_ms=MAID_TURN_COMPLETION_HOOK_TIMEOUT_MS,
    )
    async def complete_maid_turn_after_response(
        self,
        response: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        service = getattr(self, "_maid_agent_turn_service", None)
        if not _has_pending_maid_session(service, session_id):
            return hook_continue()

        request_phase = _pop_maid_request_phase(self, session_id)
        if request_phase == PHASE_TIMING_GATE:
            if _tool_calls_include(tool_calls, "no_reply"):
                result = await service.handle_no_reply_session(session_id, reason="maibot_timing_gate_no_reply")
                if result.get("success"):
                    self.ctx.logger.info(
                        f"MaidBridge 女仆 agent 轮次已由 Timing Gate no_reply 释放 [session_id={session_id}, "
                        f"external_message_id={result.get('external_message_id', '')}]"
                    )
                    return hook_continue(response="", tool_calls=[])
                self.ctx.logger.debug(
                    f"MaidBridge Timing Gate no_reply 未匹配待处理女仆轮次 [session_id={session_id}, "
                    f"error={result.get('error', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            return hook_continue(response="", tool_calls=tool_calls or [])

        if _tool_calls_include(tool_calls, "no_reply"):
            result = await service.handle_no_reply_session(session_id, reason="maibot_no_reply")
            if result.get("success"):
                self.ctx.logger.info(
                    f"MaidBridge 女仆 agent 轮次已由 MaiBot no_reply 释放 [session_id={session_id}, "
                    f"external_message_id={result.get('external_message_id', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            self.ctx.logger.debug(
                f"MaidBridge no_reply 未匹配待处理女仆轮次 [session_id={session_id}, "
                f"error={result.get('error', '')}]"
            )
            return hook_continue(response="", tool_calls=[])

        reply_call = _first_tool_call(tool_calls, "reply")
        if reply_call is not None:
            turn_context = _maid_reply_context(service, session_id)
            action_result = await plan_external_actions(
                ctx=self.ctx,
                settings=self._settings(),
                turn_context=turn_context,
                planner_reasoning=response,
                arguments=_tool_call_arguments(reply_call),
                tool_call_id=_tool_call_id(reply_call),
            )
            planned_actions = list(action_result.get("actions") or []) if action_result.get("success") else []
            action_error = "" if action_result.get("success") else str(action_result.get("error") or "")
            if action_error:
                self.ctx.logger.warning(
                    f"MaidBridge 女仆 agent 动作规划失败，本轮仅回写文本 [session_id={session_id}, error={action_error}]"
                )
            elif planned_actions or action_result.get("warnings"):
                self.ctx.logger.info(
                    f"MaidBridge 女仆 agent 动作规划完成 [session_id={session_id}, "
                    f"actions={len(planned_actions)}, warnings={action_result.get('warnings', [])}]"
                )
            reply_result = await generate_external_reply(
                ctx=self.ctx,
                settings=self._settings(),
                turn_context=turn_context,
                planner_reasoning=response,
                arguments=_tool_call_arguments(reply_call),
                tool_call_id=_tool_call_id(reply_call),
                planned_actions=planned_actions,
                action_error=action_error,
            )
            if not reply_result.get("success"):
                result = await service.handle_no_reply_session(session_id, reason="maibot_external_reply_failed")
                self.ctx.logger.warning(
                    f"MaidBridge reply 工具已被截获，但外部回复生成失败，女仆 agent 轮次按 no_reply 释放 "
                    f"[session_id={session_id}, error={reply_result.get('error', '')}, "
                    f"external_message_id={result.get('external_message_id', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            result = await service.handle_reply_session(
                session_id,
                str(reply_result.get("reply_text") or ""),
                actions=planned_actions,
                reason="maibot_external_reply_generated",
            )
            if result.get("success"):
                self.ctx.logger.info(
                    f"MaidBridge 女仆 agent 轮次已截获 reply 工具并完成外部回写 "
                    f"[session_id={session_id}, msg_id={reply_result.get('target_message_id', '')}, "
                    f"model={reply_result.get('model', '')}, "
                    f"actions={result.get('actions_count', 0)}, external_message_id={result.get('external_message_id', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            self.ctx.logger.warning(
                f"MaidBridge 外部回复回写失败 [session_id={session_id}, error={result.get('error', '')}]"
            )
            return hook_continue(response="", tool_calls=[])

        if _tool_calls_include(tool_calls, "finish"):
            result = await service.handle_no_reply_session(session_id, reason="maibot_finish_without_reply")
            if result.get("success"):
                self.ctx.logger.info(
                    f"MaidBridge 女仆 agent 轮次已由 MaiBot finish 释放 [session_id={session_id}, "
                    f"external_message_id={result.get('external_message_id', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            self.ctx.logger.debug(
                f"MaidBridge finish 未匹配待处理女仆轮次 [session_id={session_id}, error={result.get('error', '')}]"
            )
            return hook_continue(response="", tool_calls=[])

        if first_non_blank(response):
            turn_context = _maid_reply_context(service, session_id)
            action_result = await plan_external_actions(
                ctx=self.ctx,
                settings=self._settings(),
                turn_context=turn_context,
                planner_reasoning=response,
                arguments={},
                tool_call_id="",
            )
            planned_actions = list(action_result.get("actions") or []) if action_result.get("success") else []
            action_error = "" if action_result.get("success") else str(action_result.get("error") or "")
            if action_error:
                self.ctx.logger.warning(
                    f"MaidBridge 女仆 agent 动作规划失败，本轮仅回写文本 [session_id={session_id}, error={action_error}]"
                )
            elif planned_actions or action_result.get("warnings"):
                self.ctx.logger.info(
                    f"MaidBridge 女仆 agent 动作规划完成 [session_id={session_id}, "
                    f"actions={len(planned_actions)}, warnings={action_result.get('warnings', [])}]"
                )
            reply_result = await generate_external_reply(
                ctx=self.ctx,
                settings=self._settings(),
                turn_context=turn_context,
                planner_reasoning=response,
                arguments={},
                tool_call_id="",
                planned_actions=planned_actions,
                action_error=action_error,
            )
            if not reply_result.get("success"):
                result = await service.handle_no_reply_session(session_id, reason="maibot_plain_response_failed")
                self.ctx.logger.warning(
                    f"MaidBridge planner 返回普通文本但外部回复生成失败，女仆 agent 轮次按 no_reply 释放 "
                    f"[session_id={session_id}, error={reply_result.get('error', '')}, "
                    f"external_message_id={result.get('external_message_id', '')}]"
                )
                return hook_continue(response="", tool_calls=[])
            result = await service.handle_reply_session(
                session_id,
                str(reply_result.get("reply_text") or ""),
                actions=planned_actions,
                reason="maibot_plain_response_generated",
            )
            if result.get("success"):
                self.ctx.logger.info(
                    f"MaidBridge 已拦截 planner 普通文本并完成外部回写 "
                    f"[session_id={session_id}, model={reply_result.get('model', '')}, "
                    f"actions={result.get('actions_count', 0)}, external_message_id={result.get('external_message_id', '')}]"
                )
            else:
                self.ctx.logger.warning(
                    f"MaidBridge planner 普通文本回写失败 [session_id={session_id}, error={result.get('error', '')}]"
                )
            return hook_continue(response="", tool_calls=[])

        result = await service.handle_no_reply_session(session_id, reason="maibot_planner_empty")
        if result.get("success"):
            self.ctx.logger.info(
                f"MaidBridge 女仆 agent 轮次已由空 planner 结果释放 [session_id={session_id}, "
                f"external_message_id={result.get('external_message_id', '')}]"
            )
        return hook_continue(response="", tool_calls=[])


def hook_continue(
    *,
    messages: list[dict[str, Any]] | None = None,
    tool_definitions: list[dict[str, Any]] | None = None,
    response: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    custom_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "continue"}
    modified_kwargs: dict[str, Any] = {}
    if messages is not None:
        modified_kwargs["messages"] = messages
    if tool_definitions is not None:
        modified_kwargs["tool_definitions"] = tool_definitions
    if response is not None:
        modified_kwargs["response"] = response
    if tool_calls is not None:
        modified_kwargs["tool_calls"] = tool_calls
    if modified_kwargs:
        result["modified_kwargs"] = modified_kwargs
    if custom_result is not None:
        result["custom_result"] = custom_result
    return result


def _has_pending_maid_session(service: Any, session_id: str) -> bool:
    if service is None:
        return False
    checker = getattr(service, "has_pending_session", None)
    return bool(callable(checker) and checker(session_id))


def _request_phase_from_tool_definitions(tool_definitions: list[dict[str, Any]]) -> str:
    names = {_tool_definition_name(definition) for definition in tool_definitions}
    names.discard("")
    if names & {"continue", "wait", "no_reply"} and not names & {"reply", "finish"}:
        return PHASE_TIMING_GATE
    if names & {"reply", "finish"}:
        return PHASE_PLANNER
    return PHASE_UNKNOWN


def _remember_maid_request_phase(plugin: Any, session_id: str, phase: str) -> None:
    normalized = str(session_id or "").strip()
    if not normalized:
        return
    _maid_request_phase_store(plugin)[normalized] = phase


def _pop_maid_request_phase(plugin: Any, session_id: str) -> str:
    normalized = str(session_id or "").strip()
    if not normalized:
        return PHASE_UNKNOWN
    return str(_maid_request_phase_store(plugin).pop(normalized, PHASE_UNKNOWN) or PHASE_UNKNOWN)


def _maid_request_phase_store(plugin: Any) -> dict[str, str]:
    phases = getattr(plugin, "_maid_request_phase_by_session", None)
    if isinstance(phases, dict):
        return phases
    phases = {}
    setattr(plugin, "_maid_request_phase_by_session", phases)
    return phases


def _maid_planner_context(service: Any, session_id: str) -> dict[str, Any]:
    provider = getattr(service, "planner_context_for_session", None)
    if not callable(provider):
        return {}
    context = provider(session_id)
    return dict(context) if isinstance(context, Mapping) else {}


def _maid_reply_context(service: Any, session_id: str) -> dict[str, Any]:
    provider = getattr(service, "reply_context_for_session", None)
    if not callable(provider):
        return {}
    context = provider(session_id)
    return dict(context) if isinstance(context, Mapping) else {}


def _maidbridge_prompt_patch(turn_context: Mapping[str, Any]) -> str:
    context_json = json.dumps(dict(turn_context), ensure_ascii=False, separators=(",", ":"), default=str)
    return render_prompt("planner_context", maidbridge_context_json=context_json)


def _tool_calls_include(tool_calls: Any, expected_name: str) -> bool:
    if not isinstance(tool_calls, list):
        return False
    expected = expected_name.strip()
    return any(_tool_call_name(tool_call) == expected for tool_call in tool_calls)


def _first_tool_call(tool_calls: Any, expected_name: str) -> Mapping[str, Any] | None:
    if not isinstance(tool_calls, list):
        return None
    expected = expected_name.strip()
    for tool_call in tool_calls:
        if not isinstance(tool_call, Mapping):
            continue
        if _tool_call_name(tool_call) == expected:
            return tool_call
    return None


def _tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    return first_non_blank(tool_call.get("id"), tool_call.get("call_id"), tool_call.get("tool_call_id"))


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        return str(name).strip() if name is not None else ""
    for key in ("name", "func_name", "tool_name"):
        name = tool_call.get(key)
        if name is not None and str(name).strip():
            return str(name).strip()
    return ""


def _tool_call_arguments(tool_call: Any) -> dict[str, Any]:
    if not isinstance(tool_call, Mapping):
        return {}
    function = tool_call.get("function")
    raw_arguments: Any = None
    if isinstance(function, Mapping):
        raw_arguments = function.get("arguments")
    if raw_arguments is None:
        raw_arguments = tool_call.get("arguments")
    if isinstance(raw_arguments, Mapping):
        return dict(raw_arguments)
    if isinstance(raw_arguments, str) and raw_arguments.strip():
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _tool_definition_name(definition: Any) -> str:
    if not isinstance(definition, Mapping):
        return ""
    function = definition.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return str(definition.get("name") or "").strip()
