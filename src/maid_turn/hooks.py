import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, TypeGuard

from maibot_sdk import HookHandler
from maibot_sdk.types import HookMode, HookOrder

from .action_planner import plan_external_actions
from .external_emoji import (
    ExternalEmojiError,
    build_action_from_maibot_payload,
    select_maibot_emoji_payload,
)
from ..prompt_loader import render_prompt
from .reply_generator import generate_external_reply
from .turn_service import MaidTurnService
from ..utils import first_non_blank

MAID_TURN_COMPLETION_HOOK_TIMEOUT_MS = 35_000
MAID_TURN_COMPLETION_CLEANUP_MARGIN_MS = 5_000
PHASE_TIMING_GATE = "timing_gate"
PHASE_PLANNER = "planner"
PHASE_UNKNOWN = "unknown"


class MaidPlannerHooks:
    if TYPE_CHECKING:
        ctx: Any
        _maid_turn_service: MaidTurnService | None

    @HookHandler(
        "maisaka.planner.before_request",
        name="maidbridge_maid_planner_direct_reply",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
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
        service = self._maid_turn_service
        if not _has_pending_maid_session(service, session_id):
            return hook_continue()

        request_phase = _request_phase_from_tool_definitions(tool_definitions or [])
        turn_context = service.begin_request_session(session_id, request_phase)
        if request_phase != PHASE_PLANNER:
            return hook_continue()

        normalized_messages = list(messages or [])
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
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
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
        service = self._maid_turn_service
        normalized_tool_calls = list(tool_calls or []) if isinstance(tool_calls, list) else []
        if not _has_pending_maid_session(service, session_id):
            tool_call_names = [name for tool_call in normalized_tool_calls if (name := _tool_call_name(tool_call))]
            if {"reply", "send_emoji"} & set(tool_call_names):
                if _is_known_maid_session(service, session_id):
                    self.ctx.logger.warning(
                        f"MaidBridge 已拦截已完成会话的 MaiBot 原生工具调用，避免走原生发送服务 "
                        f"[session_id={session_id}, tool_calls={tool_call_names}]"
                    )
                    return hook_continue(response="", tool_calls=[])
                self.ctx.logger.warning(
                    f"MaidBridge planner hook 未匹配待处理女仆回合，MaiBot 原生工具将继续执行 "
                    f"[session_id={session_id}, tool_calls={tool_call_names}]"
                )
            return hook_continue()

        request_phase = service.request_phase_for_session(session_id)
        cleanup_budget = max(1, MAID_TURN_COMPLETION_HOOK_TIMEOUT_MS - MAID_TURN_COMPLETION_CLEANUP_MARGIN_MS)
        deadline = asyncio.get_running_loop().time() + cleanup_budget / 1000
        try:
            return await _complete_maid_turn_after_response_impl(
                self,
                service,
                response=response,
                tool_calls=normalized_tool_calls,
                session_id=session_id,
                request_phase=request_phase,
                deadline=deadline,
            )
        except asyncio.CancelledError as exc:
            await _release_after_hook_error(self, service, session_id, "maibot_hook_cancelled", exc)
            return hook_continue(response="", tool_calls=[])
        except Exception as exc:
            await _release_after_hook_error(self, service, session_id, "maibot_hook_error", exc)
            return hook_continue(response="", tool_calls=[])

    @HookHandler(
        "emoji.maisaka.after_select",
        name="maidbridge_maid_emoji_after_select",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=4500,
    )
    async def bridge_maid_emoji_after_select(
        self,
        stream_id: str = "",
        selected_emoji: dict[str, Any] | None = None,
        selected_emoji_hash: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        service = self._maid_turn_service
        if not _has_pending_maid_session(service, stream_id):
            if _is_known_maid_session(service, stream_id):
                self.ctx.logger.warning(
                    f"MaidBridge 已拦截已完成会话的 MaiBot 原生表情发送，避免走原生发送服务 "
                    f"[session_id={stream_id}, selected_emoji_hash={selected_emoji_hash}]"
                )
                return hook_abort(abort_message="MaidBridge 已接管 MaiBot 表情发送")
            return hook_continue()
        return await _complete_selected_emoji_and_abort_native_send(
            self,
            service,
            stream_id=stream_id,
            selected_emoji=selected_emoji,
            selected_emoji_hash=selected_emoji_hash,
        )


async def _complete_maid_turn_after_response_impl(
    plugin: Any,
    service: Any,
    *,
    response: str,
    tool_calls: list[dict[str, Any]],
    session_id: str,
    request_phase: str,
    deadline: float,
) -> dict[str, Any]:
    if request_phase == PHASE_TIMING_GATE:
        if _tool_calls_named(tool_calls, "no_reply"):
            return await _complete_no_reply_and_continue(
                plugin,
                service,
                session_id,
                reason="maibot_timing_gate_no_reply",
                success_log="MaidBridge 女仆回合已由 Timing Gate no_reply 释放",
            )
        return hook_continue(response="", tool_calls=tool_calls)

    emoji_result: dict[str, Any] | None = None
    emoji_calls = _tool_calls_named(tool_calls, "send_emoji")
    if emoji_calls:
        emoji_result = dict(
            await _await_with_deadline(
                _bridge_send_emoji_tools(plugin, service, session_id, response, emoji_calls),
                deadline,
            )
        )
        if emoji_result.get("success"):
            plugin.ctx.logger.info(
                f"MaidBridge 已接管 MaiBot send_emoji 工具并桥接为女仆表情气泡 "
                f"[session_id={session_id}, count={emoji_result.get('count', 0)}, metadata={emoji_result.get('metadata', [])}]"
            )
        else:
            plugin.ctx.logger.warning(
                f"MaidBridge 接管 MaiBot send_emoji 工具失败，本轮跳过表情包 "
                f"[session_id={session_id}, error={emoji_result.get('error', '')}]"
        )
        tool_calls = _without_tool_calls(tool_calls, "send_emoji")

    if _tool_calls_named(tool_calls, "no_reply"):
        return await _complete_no_reply_and_continue(
            plugin,
            service,
            session_id,
            reason="maibot_no_reply",
            success_log="MaidBridge 女仆回合已由 MaiBot no_reply 释放",
        )

    reply_calls = _tool_calls_named(tool_calls, "reply")
    if reply_calls:
        return await _complete_external_reply(plugin, service, response, session_id, deadline, reply_call=reply_calls[0])

    if _tool_calls_named(tool_calls, "finish"):
        return await _complete_no_reply_and_continue(
            plugin,
            service,
            session_id,
            reason="maibot_finish_without_reply",
            success_log="MaidBridge 女仆回合已由 MaiBot finish 释放",
        )

    if emoji_result is not None and not tool_calls:
        reason = "maibot_direct_emoji_outbound" if emoji_result.get("success") else "maibot_direct_emoji_failed"
        return await _complete_no_reply_and_continue(
            plugin,
            service,
            session_id,
            reason=reason,
            success_log="MaidBridge send_emoji 单独出站已按 no_reply 合并提交",
        )

    if first_non_blank(response):
        return await _complete_external_reply(plugin, service, response, session_id, deadline)

    return await _complete_no_reply_and_continue(
        plugin,
        service,
        session_id,
        reason="maibot_planner_empty",
        success_log="MaidBridge 女仆回合已由空 planner 结果释放",
    )


async def _complete_external_reply(
    plugin: Any,
    service: Any,
    response: str,
    session_id: str,
    deadline: float,
    *,
    reply_call: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    turn_context = service.reply_context_for_session(session_id)
    arguments = _tool_call_arguments(reply_call)
    tool_call_id = _tool_call_id(reply_call)
    action_result = await _await_with_deadline(
        plan_external_actions(
            ctx=plugin.ctx,
            settings=plugin._settings(),
            turn_context=turn_context,
            planner_reasoning=response,
            arguments=arguments,
            tool_call_id=tool_call_id,
        ),
        deadline,
    )
    planned_actions = list(action_result.get("actions") or []) if action_result.get("success") else []
    action_error = "" if action_result.get("success") else str(action_result.get("error") or "")
    _log_action_planning(plugin, session_id, planned_actions, action_result, action_error)

    reply_result = await _await_with_deadline(
        generate_external_reply(
            ctx=plugin.ctx,
            settings=plugin._settings(),
            turn_context=turn_context,
            planner_reasoning=response,
            arguments=arguments,
            tool_call_id=tool_call_id,
            planned_actions=planned_actions,
            action_error=action_error,
        ),
        deadline,
    )
    if not reply_result.get("success"):
        reason = "maibot_external_reply_failed" if reply_call is not None else "maibot_plain_response_failed"
        result = await service.handle_no_reply_session(session_id, reason=reason)
        source = "reply 工具" if reply_call is not None else "planner 普通文本"
        plugin.ctx.logger.warning(
            f"MaidBridge {source} 已被截获，但外部回复生成失败，女仆回合按 no_reply 释放 "
            f"[session_id={session_id}, error={reply_result.get('error', '')}, "
            f"external_message_id={result.get('external_message_id', '')}]"
        )
        return hook_continue(response="", tool_calls=[])

    result = await service.handle_reply_session(
        session_id,
        str(reply_result.get("reply_text") or ""),
        actions=planned_actions,
    )
    if result.get("success"):
        if reply_call is not None:
            plugin.ctx.logger.info(
                f"MaidBridge 女仆回合已截获 reply 工具并完成外部回写 "
                f"[session_id={session_id}, msg_id={reply_result.get('target_message_id', '')}, "
                f"model={reply_result.get('model', '')}, "
                f"actions={result.get('actions_count', 0)}, external_message_id={result.get('external_message_id', '')}]"
            )
        else:
            plugin.ctx.logger.info(
                f"MaidBridge 已拦截 planner 普通文本并完成外部回写 "
                f"[session_id={session_id}, model={reply_result.get('model', '')}, "
                f"actions={result.get('actions_count', 0)}, external_message_id={result.get('external_message_id', '')}]"
            )
    else:
        source = "reply 工具" if reply_call is not None else "planner 普通文本"
        plugin.ctx.logger.warning(f"MaidBridge {source} 回写失败 [session_id={session_id}, error={result.get('error', '')}]")
    return hook_continue(response="", tool_calls=[])


async def _complete_no_reply_and_continue(
    plugin: Any,
    service: Any,
    session_id: str,
    *,
    reason: str,
    success_log: str,
) -> dict[str, Any]:
    result = await service.handle_no_reply_session(session_id, reason=reason)
    if result.get("success"):
        plugin.ctx.logger.info(
            f"{success_log} [session_id={session_id}, external_message_id={result.get('external_message_id', '')}, "
            f"actions={result.get('actions_count', 0)}]"
        )
    else:
        plugin.ctx.logger.debug(
            f"MaidBridge no_reply 未匹配待处理女仆回合 [session_id={session_id}, reason={reason}, error={result.get('error', '')}]"
        )
    return hook_continue(response="", tool_calls=[])


async def _release_after_hook_error(plugin: Any, service: Any, session_id: str, reason: str, exc: BaseException) -> None:
    result: dict[str, Any] = {"success": False, "error": ""}
    with suppress(Exception):
        result = await service.handle_no_reply_session(session_id, reason=reason)
    plugin.ctx.logger.warning(
        f"MaidBridge planner hook 异常，已清空 tool_calls 并尝试按 no_reply 释放当前女仆回合 "
        f"[session_id={session_id}, reason={reason}, released={bool(result.get('success'))}, "
        f"external_message_id={result.get('external_message_id', '')}, error={exc}]"
    )


async def _bridge_send_emoji_tools(
    plugin: Any,
    service: Any,
    session_id: str,
    response: str,
    send_emoji_calls: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not _external_emoji_enabled(plugin):
        return {"success": False, "error": "Java 侧未启用外部女仆表情气泡"}

    metadata: list[dict[str, Any]] = []
    errors: list[str] = []
    for send_emoji_call in send_emoji_calls:
        result = await _bridge_send_emoji_tool(plugin, service, session_id, response, send_emoji_call)
        if result.get("success"):
            metadata.append(dict(result.get("metadata") or {}))
        else:
            errors.append(str(result.get("error") or "未知错误"))
    if metadata:
        return {"success": True, "count": len(metadata), "metadata": metadata}
    return {"success": False, "error": "；".join(errors) if errors else "没有可处理的 send_emoji 工具调用"}


async def _bridge_send_emoji_tool(
    plugin: Any,
    service: Any,
    session_id: str,
    response: str,
    send_emoji_call: Mapping[str, Any],
) -> dict[str, Any]:
    arguments = _tool_call_arguments(send_emoji_call)
    turn_context = service.reply_context_for_session(session_id)
    raw_message_context = turn_context.get("message")
    message_context = dict(raw_message_context) if isinstance(raw_message_context, Mapping) else {}
    query_text = first_non_blank(
        arguments.get("emotion"),
        arguments.get("description"),
        arguments.get("query"),
        arguments.get("reason"),
        response,
        message_context.get("text"),
    )
    try:
        emoji_payload = await select_maibot_emoji_payload(plugin.ctx, query_text)
    except Exception as exc:
        return {"success": False, "error": f"MaiBot 表情包能力调用失败：{exc}"}
    if emoji_payload is None:
        return {"success": False, "error": "MaiBot 表情包库没有返回可桥接的表情包"}

    try:
        action, metadata = build_action_from_maibot_payload(emoji_payload)
    except ExternalEmojiError as exc:
        return {"success": False, "error": str(exc)}

    add_result = service.add_outbound_action(session_id, action)
    if not add_result.get("success"):
        return {"success": False, "error": str(add_result.get("error") or "MaidBridge 表情包暂存失败")}
    return {"success": True, "metadata": metadata}


async def _complete_selected_emoji_and_abort_native_send(
    plugin: Any,
    service: Any,
    *,
    stream_id: str,
    selected_emoji: Mapping[str, Any] | None,
    selected_emoji_hash: str,
) -> dict[str, Any]:
    abort_message = "MaidBridge 已接管 MaiBot 表情发送"
    if not _external_emoji_enabled(plugin):
        result = await service.handle_no_reply_session(stream_id, reason="maibot_emoji_hook_unsupported")
        plugin.ctx.logger.warning(
            f"MaidBridge 已中止 MaiBot 原生表情发送，Java 侧未启用外部表情气泡 "
            f"[session_id={stream_id}, selected_emoji_hash={selected_emoji_hash}, "
            f"external_message_id={result.get('external_message_id', '')}]"
        )
        return hook_abort(abort_message=abort_message)

    if not isinstance(selected_emoji, Mapping):
        result = await service.handle_no_reply_session(stream_id, reason="maibot_emoji_hook_missing_payload")
        plugin.ctx.logger.warning(
            f"MaidBridge 已中止 MaiBot 原生表情发送，但 Hook 未提供可桥接表情 "
            f"[session_id={stream_id}, selected_emoji_hash={selected_emoji_hash}, "
            f"external_message_id={result.get('external_message_id', '')}]"
        )
        return hook_abort(abort_message=abort_message)

    try:
        action, metadata = build_action_from_maibot_payload(selected_emoji)
    except ExternalEmojiError as exc:
        result = await service.handle_no_reply_session(stream_id, reason="maibot_emoji_hook_failed")
        plugin.ctx.logger.warning(
            f"MaidBridge 已中止 MaiBot 原生表情发送，但表情无法桥接 "
            f"[session_id={stream_id}, selected_emoji_hash={selected_emoji_hash}, "
            f"external_message_id={result.get('external_message_id', '')}, error={exc}]"
        )
        return hook_abort(abort_message=abort_message)

    result = await service.handle_no_reply_session(
        stream_id,
        reason="maibot_emoji_hook_outbound",
        actions=[action],
    )
    if result.get("success"):
        plugin.ctx.logger.info(
            f"MaidBridge 已在表情选择后接管 MaiBot send_emoji 并回写 Java "
            f"[session_id={stream_id}, external_message_id={result.get('external_message_id', '')}, metadata={metadata}]"
        )
    else:
        plugin.ctx.logger.warning(
            f"MaidBridge 表情选择 Hook 未能回写 Java，已中止 MaiBot 原生表情发送 "
            f"[session_id={stream_id}, selected_emoji_hash={selected_emoji_hash}, error={result.get('error', '')}]"
        )
    return hook_abort(abort_message=abort_message)


async def _await_with_deadline(awaitable: Any, deadline: float) -> Any:
    timeout = deadline - asyncio.get_running_loop().time()
    if timeout <= 0:
        closer = getattr(awaitable, "close", None)
        if callable(closer):
            closer()
        raise asyncio.TimeoutError("MaidBridge planner hook 已接近超时，保留时间用于释放 pending 回合")
    return await asyncio.wait_for(awaitable, timeout=timeout)


def _log_action_planning(
    plugin: Any,
    session_id: str,
    planned_actions: list[dict[str, Any]],
    action_result: Mapping[str, Any],
    action_error: str,
) -> None:
    if action_error:
        plugin.ctx.logger.warning(
            f"MaidBridge 女仆回合动作规划失败，本轮仅回写文本 [session_id={session_id}, error={action_error}]"
        )
    elif planned_actions or action_result.get("warnings"):
        plugin.ctx.logger.info(
            f"MaidBridge 女仆回合动作规划完成 [session_id={session_id}, "
            f"actions={len(planned_actions)}, warnings={action_result.get('warnings', [])}]"
        )


def hook_continue(
    *,
    messages: list[dict[str, Any]] | None = None,
    response: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "continue"}
    modified_kwargs: dict[str, Any] = {}
    if messages is not None:
        modified_kwargs["messages"] = messages
    if response is not None:
        modified_kwargs["response"] = response
    if tool_calls is not None:
        modified_kwargs["tool_calls"] = tool_calls
    if modified_kwargs:
        result["modified_kwargs"] = modified_kwargs
    return result


def hook_abort(*, abort_message: str) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "abort"}
    if abort_message:
        result["modified_kwargs"] = {"abort_message": abort_message}
    return result


def _has_pending_maid_session(service: MaidTurnService | None, session_id: str) -> TypeGuard[MaidTurnService]:
    if service is None:
        return False
    return bool(service.has_pending_session(session_id))


def _is_known_maid_session(service: MaidTurnService | None, session_id: str) -> TypeGuard[MaidTurnService]:
    if service is None:
        return False
    return bool(service.is_known_session(session_id))


def _request_phase_from_tool_definitions(tool_definitions: list[dict[str, Any]]) -> str:
    names = {_tool_definition_name(definition) for definition in tool_definitions}
    names.discard("")
    if names & {"continue", "wait", "no_reply"} and not names & {"reply", "finish"}:
        return PHASE_TIMING_GATE
    if names & {"reply", "finish"}:
        return PHASE_PLANNER
    return PHASE_UNKNOWN


def _maidbridge_prompt_patch(turn_context: Mapping[str, Any]) -> str:
    context_json = json.dumps(dict(turn_context), ensure_ascii=False, separators=(",", ":"), default=str)
    return render_prompt("planner_context", maidbridge_context_json=context_json)


def _without_tool_calls(tool_calls: list[dict[str, Any]], removed_name: str) -> list[dict[str, Any]]:
    return [
        tool_call
        for tool_call in tool_calls
        if _tool_call_name(tool_call) != removed_name
    ]


def _tool_calls_named(tool_calls: list[dict[str, Any]], expected_name: str) -> list[Mapping[str, Any]]:
    expected = expected_name.strip()
    return [
        tool_call
        for tool_call in tool_calls
        if isinstance(tool_call, Mapping) and _tool_call_name(tool_call) == expected
    ]


def _tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    return first_non_blank(tool_call.get("id"))


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        name = function.get("name")
        return str(name).strip() if name is not None else ""
    return str(tool_call.get("name") or "").strip()


def _tool_call_arguments(tool_call: Any) -> dict[str, Any]:
    if not isinstance(tool_call, Mapping):
        return {}
    function = tool_call.get("function")
    raw_arguments: Any = None
    if isinstance(function, Mapping):
        raw_arguments = function.get("arguments")
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
    return ""


def _external_emoji_enabled(plugin: Any) -> bool:
    return bool(plugin._state.external_emoji_enabled())
