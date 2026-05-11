from dataclasses import dataclass
from typing import Any

from ..constants import DEFAULT_MAX_MESSAGE_BYTES
from .state import BridgeRuntimeState


@dataclass(frozen=True)
class RuntimeBundle:
    ctx: Any
    transport: Any
    state: BridgeRuntimeState | None = None
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES
    enable_message_out_events: bool = False
    maid_agent_turn_handler: Any | None = None


def build_runtime_bundle(
    *,
    ctx: Any,
    transport: Any,
    state: BridgeRuntimeState | None = None,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    enable_message_out_events: bool = False,
    maid_agent_turn_handler: Any | None = None,
) -> RuntimeBundle:
    return RuntimeBundle(
        ctx=ctx,
        transport=transport,
        state=state,
        max_message_bytes=max_message_bytes,
        enable_message_out_events=enable_message_out_events,
        maid_agent_turn_handler=maid_agent_turn_handler,
    )
