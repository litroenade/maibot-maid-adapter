from .api import MaidApi
from .connection import MaidBridgeConnection
from .gateway import MaidGateway
from .planner import MaidPlannerHooks


class MaidBridgeMaidPlugin(MaidPlannerHooks, MaidGateway, MaidApi, MaidBridgeConnection):
    """组合插件注册点，具体功能按入口拆在各自模块。"""
