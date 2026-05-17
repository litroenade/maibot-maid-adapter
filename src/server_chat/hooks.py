import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeGuard

from maibot_sdk import HookHandler
from maibot_sdk.types import HookMode, HookOrder

from ..constants import MAIBOT_REPLYER_TASK, SERVER_CHAT_RESPONSE_TYPE
from ..protocol.frame import build_server_chat_message_frame
from ..utils import first_non_blank
from .service import ServerChatContext, ServerChatService

SERVER_CHAT_REPLY_HOOK_TIMEOUT_MS = 45_000
SERVER_CHAT_UNSUPPORTED_SEND_TOOLS = {"send_emoji", "send_image"}
SERVER_CHAT_NO_OUTPUT_TERMINAL_TOOLS = {"finish", "no_action", "wait"}


class ServerChatPlannerHooks:
    if TYPE_CHECKING:
        _bot_name: str
        _server_chat_service: ServerChatService | None

        @property
        def ctx(self) -> Any:
            raise NotImplementedError

        def _settings(self) -> Any:
            raise NotImplementedError

        async def _send_frame_await_reply(self, frame: Any, *, settings: Any) -> dict[str, Any]:
            raise NotImplementedError

    @HookHandler(
        "maisaka.planner.after_response",
        name="maidbridge_server_chat_reply",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=SERVER_CHAT_REPLY_HOOK_TIMEOUT_MS,
    )
    async def complete_server_chat_after_response(
        self,
        response: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        service = self._server_chat_service
        if not _has_server_chat_session(service, session_id):
            return hook_continue()

        context = service.context_for_session(session_id)
        if context is None:
            return hook_continue()

        should_complete_session = False
        try:
            normalized_tool_calls = list(tool_calls or []) if isinstance(tool_calls, list) else []
            reply_calls = _tool_calls_named(normalized_tool_calls, "reply")
            unsupported_send_calls = _tool_calls_matching(normalized_tool_calls, SERVER_CHAT_UNSUPPORTED_SEND_TOOLS)

            if reply_calls:
                if unsupported_send_calls:
                    self.ctx.logger.warning(
                        f"MaidBridge 服务器群聊只支持文字，已跳过同轮媒体工具 "
                        f"[session_id={session_id}, tools={_tool_call_names(unsupported_send_calls)}]"
                    )
                reply_result = await _generate_server_chat_reply(
                    self,
                    context,
                    planner_reasoning=response,
                    reply_call=reply_calls[0],
                )
                if not reply_result.get("success"):
                    self.ctx.logger.warning(
                        f"MaidBridge 服务器群聊回复生成失败，已阻止原生 send_service "
                        f"[session_id={session_id}, error={reply_result.get('error', '')}]"
                    )
                    should_complete_session = True
                    return hook_continue(response="", tool_calls=[])

                send_result = await _send_server_chat_text(
                    self,
                    context,
                    str(reply_result.get("reply_text") or ""),
                    source="planner_reply_tool",
                    session_id=session_id,
                )
                if send_result.get("success"):
                    self.ctx.logger.info(
                        f"MaidBridge 服务器群聊已接管 reply 工具并完成回写 "
                        f"[session_id={session_id}, external_message_id={send_result.get('external_message_id', '')}]"
                    )
                else:
                    self.ctx.logger.warning(
                        f"MaidBridge 服务器群聊 reply 工具回写失败 "
                        f"[session_id={session_id}, error={send_result.get('error', '')}]"
                    )
                should_complete_session = True
                return hook_continue(response="", tool_calls=[])

            if unsupported_send_calls:
                self.ctx.logger.warning(
                    f"MaidBridge 服务器群聊只支持文字，已阻止媒体工具走原生 send_service "
                    f"[session_id={session_id}, tools={_tool_call_names(unsupported_send_calls)}]"
                )
                should_complete_session = True
                return hook_continue(response="", tool_calls=[])

            if _tool_calls_matching(normalized_tool_calls, SERVER_CHAT_NO_OUTPUT_TERMINAL_TOOLS):
                should_complete_session = True
                return hook_continue()

            if normalized_tool_calls:
                return hook_continue()

            plain_response = _clean_reply_text(first_non_blank(response))
            if plain_response:
                send_result = await _send_server_chat_text(
                    self,
                    context,
                    plain_response,
                    source="planner_plain_response",
                    session_id=session_id,
                )
                if send_result.get("success"):
                    self.ctx.logger.info(
                        f"MaidBridge 服务器群聊已接管 planner 普通文本并完成回写 "
                        f"[session_id={session_id}, external_message_id={send_result.get('external_message_id', '')}]"
                    )
                else:
                    self.ctx.logger.warning(
                        f"MaidBridge 服务器群聊 planner 普通文本回写失败 "
                        f"[session_id={session_id}, error={send_result.get('error', '')}]"
                    )
                should_complete_session = True
                return hook_continue(response="", tool_calls=[])

            should_complete_session = True
            return hook_continue()
        except Exception as exc:
            self.ctx.logger.warning(
                f"MaidBridge 服务器群聊 after_response 处理异常，已阻止原生 send_service "
                f"[session_id={session_id}, error={exc}]"
            )
            should_complete_session = True
            return hook_continue(response="", tool_calls=[])
        finally:
            if should_complete_session:
                service.complete_session(session_id)


async def _generate_server_chat_reply(
    plugin: Any,
    context: ServerChatContext,
    *,
    planner_reasoning: str,
    reply_call: Mapping[str, Any],
) -> dict[str, Any]:
    arguments = _tool_call_arguments(reply_call)
    try:
        result = await plugin.ctx.llm.generate(
            _reply_prompt(
                context,
                planner_reasoning=planner_reasoning,
                arguments=arguments,
                tool_call_id=_tool_call_id(reply_call),
            ),
            model=MAIBOT_REPLYER_TASK,
        )
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
        "model": first_non_blank(result.get("model"), result.get("model_name")),
    }


def _reply_prompt(
    context: ServerChatContext,
    *,
    planner_reasoning: str,
    arguments: Mapping[str, Any],
    tool_call_id: str,
) -> list[dict[str, str]]:
    payload = {
        "room": {
            "id": context.room_id,
            "name": context.room_name,
        },
        "speaker": {
            "id": context.speaker_id,
            "name": context.speaker_name,
        },
        "message": {
            "id": context.message_id,
            "text": context.text,
        },
        "bot_name": context.bot_name,
        "planner_decision": first_non_blank(planner_reasoning),
        "reply_tool": {
            "call_id": first_non_blank(tool_call_id),
            "arguments": dict(arguments),
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你负责把 MaiSaka planner 的回复意图改写成一条 Minecraft 服务器群聊里的普通成员发言。"
                "只输出最终可见文本，不要输出分析、JSON、Markdown、引号、字段名或说话人前缀。"
                "优先使用玩家消息的语言；玩家使用中文时输出简体中文。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        },
    ]


async def _send_server_chat_text(
    plugin: Any,
    context: ServerChatContext,
    text: str,
    *,
    source: str,
    session_id: str,
) -> dict[str, Any]:
    settings = plugin._settings()
    frame = build_server_chat_message_frame(
        room_id=context.room_id,
        room_name=context.room_name,
        text=text,
        kind="member",
        speaker_id=settings.agent_id,
        speaker_name=first_non_blank(plugin._bot_name, settings.agent_id),
        metadata={
            "source": source,
            "session_id": session_id,
            "incoming_message_id": context.message_id,
        },
        deadline_ms=settings.request_timeout_ms,
    )
    reply = await plugin._send_frame_await_reply(frame, settings=settings)
    payload = reply.get("payload") if isinstance(reply, Mapping) else {}
    if reply.get("type") != SERVER_CHAT_RESPONSE_TYPE:
        return {"success": False, "error": f"服务器群聊回写返回了异常响应：{reply.get('type')}"}
    if isinstance(payload, Mapping) and payload.get("error"):
        return {"success": False, "error": str(payload.get("error"))}
    return {"success": True, "external_message_id": frame.id}


def hook_continue(
    *,
    response: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"action": "continue"}
    modified_kwargs: dict[str, Any] = {}
    if response is not None:
        modified_kwargs["response"] = response
    if tool_calls is not None:
        modified_kwargs["tool_calls"] = tool_calls
    if modified_kwargs:
        result["modified_kwargs"] = modified_kwargs
    return result


def _has_server_chat_session(service: ServerChatService | None, session_id: str) -> TypeGuard[ServerChatService]:
    if service is None:
        return False
    return bool(service.has_session(session_id))


def _tool_calls_named(tool_calls: list[dict[str, Any]], expected_name: str) -> list[Mapping[str, Any]]:
    expected = expected_name.strip()
    return [
        tool_call
        for tool_call in tool_calls
        if isinstance(tool_call, Mapping) and _tool_call_name(tool_call) == expected
    ]


def _tool_calls_matching(tool_calls: list[dict[str, Any]], expected_names: set[str]) -> list[Mapping[str, Any]]:
    return [
        tool_call
        for tool_call in tool_calls
        if isinstance(tool_call, Mapping) and _tool_call_name(tool_call) in expected_names
    ]


def _tool_call_names(tool_calls: list[Mapping[str, Any]]) -> list[str]:
    return [name for tool_call in tool_calls if (name := _tool_call_name(tool_call))]


def _tool_call_id(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    return first_non_blank(tool_call.get("id"))


def _tool_call_name(tool_call: Any) -> str:
    if not isinstance(tool_call, Mapping):
        return ""
    function = tool_call.get("function")
    if isinstance(function, Mapping):
        return first_non_blank(function.get("name"))
    return first_non_blank(tool_call.get("name"))


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


def _clean_reply_text(text: str) -> str:
    cleaned = first_non_blank(text)
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    parsed = _reply_from_json(cleaned)
    if parsed:
        cleaned = parsed
    quote_pairs = {'"': '"', "'": "'", "“": "”", "‘": "’"}
    if len(cleaned) >= 2 and cleaned[0] in quote_pairs and cleaned[-1] == quote_pairs[cleaned[0]]:
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
