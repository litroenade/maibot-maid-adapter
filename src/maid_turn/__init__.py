"""外部女仆回合接管链路。

这里放置和 MaiBot 对话回合接管直接相关的代码；协议、连接和运行时路由仍留在上层包中。
"""

from .hooks import MaidPlannerHooks
from .outbound_gateway import MaidGateway
from .turn_service import MaidTurnService

__all__ = [
    "MaidGateway",
    "MaidPlannerHooks",
    "MaidTurnService",
]
