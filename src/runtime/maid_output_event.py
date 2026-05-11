from typing import Any, Mapping

from ..protocol.frame import BridgeFrame


def build_message_out_event_observation(frame: BridgeFrame) -> dict[str, Any]:
    payload = frame.payload
    maid = _mapping(payload.get("maid"))
    message = _mapping(payload.get("message"))
    text = _first_text(
        payload.get("chat_text"),
        payload.get("text"),
        message.get("text"),
        message.get("chat_text"),
        message.get("content"),
    )
    observation: dict[str, Any] = {
        "accepted": frame.type,
        "observed": True,
        "event_id": frame.id,
        "trace_id": frame.trace_id,
    }
    maid_summary = _maid_summary(maid)
    if maid_summary:
        observation["maid"] = maid_summary
    if text:
        observation["text"] = text
    return observation


def _maid_summary(maid: Mapping[str, Any]) -> dict[str, str]:
    summary: dict[str, str] = {}
    for key in ("uuid", "name"):
        value = _first_non_empty(maid.get(key))
        if value:
            summary[key] = value
    return summary


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_non_empty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
