from typing import TYPE_CHECKING, Any

from maibot_sdk import MessageGateway

from ..constants import (
    ADAPTER_STATE_NAME,
    PLATFORM,
    PROTOCOL,
)
from .external_emoji import ExternalEmojiError, build_action_from_component
from .turn_service import MaidTurnService


class MaidGateway:
    if TYPE_CHECKING:
        _maid_turn_service: MaidTurnService | None
        _state: Any

        @property
        def ctx(self) -> Any:
            raise NotImplementedError

    @MessageGateway(
        name=ADAPTER_STATE_NAME,
        route_type="duplex",
        platform=PLATFORM,
        protocol=PROTOCOL,
        description="MaidBridge Minecraft / TouhouLittleMaid 消息网关",
    )
    async def handle_maidbridge_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del metadata, kwargs
        service = self._maid_turn_service
        if service is None:
            return {"success": False, "error": "MaidBridge 没有找到匹配当前出站消息的女仆 pending 回合"}
        pending_context = service.context_for_outbound(message, route or {})
        if pending_context is None:
            return {"success": False, "error": "MaidBridge 没有找到匹配当前出站消息的女仆 pending 回合"}

        text_parts, emoji_components = _message_components(message)
        reply_text = "\n".join(text_parts).strip()
        actions: list[dict[str, Any]] = []
        emoji_metadata: list[dict[str, Any]] = []
        emoji_enabled = _external_emoji_enabled(self)

        if emoji_components and not emoji_enabled:
            self.ctx.logger.warning(
                f"MaidBridge Java 侧未启用外部表情气泡，已跳过 MaiBot 表情 action "
                f"[session_id={pending_context.session_id}, turn_id={pending_context.turn_id}, count={len(emoji_components)}]"
            )
        elif emoji_components:
            actions, emoji_metadata = _build_emoji_actions(self, pending_context, emoji_components)

        if reply_text:
            result = await service.handle_reply_session(
                pending_context.session_id,
                reply_text,
                actions=actions,
            )
        else:
            result = await service.handle_no_reply_session(
                pending_context.session_id,
                reason="maibot_gateway_emoji_outbound" if actions else "maibot_gateway_empty_outbound",
                actions=actions,
            )

        if result.get("success"):
            return {
                "success": True,
                "external_message_id": str(result.get("external_message_id") or pending_context.turn_id),
                "metadata": {
                    "maidbridge": {
                        "turn_id": pending_context.turn_id,
                        "emoji": emoji_metadata,
                        "actions_count": result.get("actions_count", len(actions)),
                    }
                },
            }
        return {"success": False, "error": str(result.get("error") or "MaidBridge 出站回写失败")}


def _build_emoji_actions(self: Any, pending_context: Any, emoji_components: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actions: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    for component in emoji_components:
        try:
            action, item_metadata = build_action_from_component(component)
        except ExternalEmojiError as exc:
            self.ctx.logger.warning(
                f"MaidBridge 表情包出站组件无法桥接，已跳过该组件 "
                f"[session_id={pending_context.session_id}, turn_id={pending_context.turn_id}, error={exc}]"
            )
            continue
        actions.append(action)
        metadata.append(item_metadata)
    return actions, metadata


def _message_components(message: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    raw_message = message.get("raw_message")
    text_parts: list[str] = []
    emoji_components: list[dict[str, Any]] = []
    if isinstance(raw_message, list):
        for component in raw_message:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "").strip().lower()
            if component_type == "text":
                text = str(component.get("data") or "").strip()
                if text:
                    text_parts.append(text)
            elif component_type == "emoji":
                emoji_components.append(component)
    if not text_parts:
        processed_text = str(message.get("processed_plain_text") or "").strip()
        if processed_text and processed_text != "[表情包]":
            text_parts.append(processed_text)
    return text_parts, emoji_components


def _external_emoji_enabled(plugin: Any) -> bool:
    return bool(plugin._state.external_emoji_enabled())
