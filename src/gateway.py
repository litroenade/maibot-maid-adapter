from typing import Any

from maibot_sdk import MessageGateway

from .constants import ADAPTER_STATE_NAME, PLATFORM


class MaidGateway:
    @MessageGateway(
        name=ADAPTER_STATE_NAME,
        route_type="receive",
        platform=PLATFORM,
        protocol="maidbridge",
        description="MaidBridge Minecraft / TouhouLittleMaid 入站消息网关",
    )
    async def handle_maidbridge_gateway(
        self,
        message: dict[str, Any],
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del message, route, metadata, kwargs
        return {
            "success": False,
            "error": "MaidBridge 女仆外部接管不接收 MaiBot 普通出站；pending 轮次必须由截获到的 reply 工具或 no_reply 完成",
        }
