from types import SimpleNamespace
from typing import Any

import pytest

from src.maid_turn import hooks as hooks_module
from src.maid_turn.hooks import MaidPlannerHooks


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _MaidTurnService:
    def __init__(self, *, pending: bool, known: bool) -> None:
        self.pending = pending
        self.known = known

    def has_pending_session(self, session_id: str) -> bool:
        return self.pending and session_id == "maid-session"

    def is_known_session(self, session_id: str) -> bool:
        return self.known and session_id == "maid-session"


def _plugin(service: _MaidTurnService) -> MaidPlannerHooks:
    plugin: Any = MaidPlannerHooks()
    plugin.ctx = SimpleNamespace(logger=_Logger())
    plugin._maid_turn_service = service
    return plugin


@pytest.mark.asyncio
async def test_unknown_session_send_emoji_keeps_native_pipeline() -> None:
    plugin = _plugin(_MaidTurnService(pending=False, known=False))

    result = await plugin.complete_maid_turn_after_response(
        response="",
        tool_calls=[{"function": {"name": "send_emoji", "arguments": "{}"}}],
        session_id="normal-session",
    )

    assert result == {"action": "continue"}


@pytest.mark.asyncio
async def test_known_completed_session_send_emoji_is_cleared_before_native_send() -> None:
    plugin = _plugin(_MaidTurnService(pending=False, known=True))

    result = await plugin.complete_maid_turn_after_response(
        response="",
        tool_calls=[{"function": {"name": "send_emoji", "arguments": "{}"}}],
        session_id="maid-session",
    )

    assert result == {
        "action": "continue",
        "modified_kwargs": {
            "response": "",
            "tool_calls": [],
        },
    }
    assert "避免走原生发送服务" in plugin.ctx.logger.warnings[0]


@pytest.mark.asyncio
async def test_known_completed_session_after_select_aborts_native_emoji_send() -> None:
    plugin = _plugin(_MaidTurnService(pending=False, known=True))

    result = await plugin.bridge_maid_emoji_after_select(
        stream_id="maid-session",
        selected_emoji={"hash": "hash"},
        selected_emoji_hash="hash",
    )

    assert result == {
        "action": "abort",
        "modified_kwargs": {
            "abort_message": "MaidBridge 已接管 MaiBot 表情发送",
        },
    }
    assert "避免走原生发送服务" in plugin.ctx.logger.warnings[0]


@pytest.mark.asyncio
async def test_pending_session_after_select_still_uses_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin(_MaidTurnService(pending=True, known=True))
    captured: dict[str, Any] = {}

    async def fake_complete_selected_emoji_and_abort_native_send(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"action": "abort", "modified_kwargs": {"abort_message": "bridge"}}

    monkeypatch.setattr(
        hooks_module,
        "_complete_selected_emoji_and_abort_native_send",
        fake_complete_selected_emoji_and_abort_native_send,
    )

    result = await plugin.bridge_maid_emoji_after_select(
        stream_id="maid-session",
        selected_emoji={"hash": "hash"},
        selected_emoji_hash="hash",
    )

    assert result == {"action": "abort", "modified_kwargs": {"abort_message": "bridge"}}
    assert captured["kwargs"]["stream_id"] == "maid-session"
    assert captured["kwargs"]["selected_emoji_hash"] == "hash"
    assert plugin.ctx.logger.warnings == []
