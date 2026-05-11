import asyncio
from typing import Any
from uuid import uuid4

from maibot_sdk import API

from .constants import ADAPTER_STATE_NAME, CLIENT_TO_JAVA, DEFAULT_ENDPOINT_ID, PLATFORM, PROTOCOL
from .protocol import query_api
from .protocol.frame import BridgeProtocolError, build_ai_event_frame


class MaidApi:
    @API("status", description="获取 MaidBridge 适配器运行状态", version="1", public=True)
    async def get_status(self) -> dict[str, Any]:
        settings = self._settings()
        return {
            "enabled": bool(settings.enabled),
            "connected": bool(self._state.connected),
            "server_id": self._state.server_id or settings.server_id,
            "connection_id": self._state.connection_id,
            "transport_active": self._transport is not None,
            "router_active": self._router is not None,
            "pending_request_count": len(self._state.pending_requests),
            "endpoint_count": len(self._state.endpoints),
            "websocket_role": self._websocket_role(settings),
            "websocket_url": self._websocket_url(settings),
            "protocol": PROTOCOL,
            "adapter_name": ADAPTER_STATE_NAME,
            "platform": PLATFORM,
            "max_message_bytes": settings.max_message_bytes,
            "request_timeout_ms": settings.request_timeout_ms,
            "enable_message_out_events": bool(settings.enable_message_out_events),
        }

    @API("pending_requests", description="列出等待响应的 MaidBridge 请求帧", version="1", public=True)
    async def pending_requests(self) -> list[dict[str, Any]]:
        return [request.snapshot() for _, request in sorted(self._state.pending_requests.items())]

    @API("maid_query", description="发送 MaidBridge 查询帧并等待 maid.api.response", version="1", public=True)
    async def maid_query(
        self,
        event_type: str,
        payload: dict[str, Any],
        server_id: str = "",
        endpoint_id: str = "",
        deadline_ms: int = 0,
    ) -> dict[str, Any]:
        return await self._send_maid_frame(
            event_type,
            payload,
            server_id=server_id,
            endpoint_id=endpoint_id,
            deadline_ms=deadline_ms,
        )

    @API("maid_call", description="发送 MaidBridge 调用帧并等待 maid.api.response", version="1", public=True)
    async def maid_call(
        self,
        event_type: str,
        payload: dict[str, Any],
        server_id: str = "",
        endpoint_id: str = "",
        deadline_ms: int = 0,
    ) -> dict[str, Any]:
        return await self._send_maid_frame(
            event_type,
            payload,
            server_id=server_id,
            endpoint_id=endpoint_id,
            deadline_ms=deadline_ms,
        )

    @API("maid_message", description="发送 MaidBridge maid.message.in 帧", version="1", public=True)
    async def maid_message(
        self,
        text: str,
        source_member_id: str = "",
        maid_uuid: str = "",
        server_id: str = "",
        endpoint_id: str = "",
        deadline_ms: int = 0,
    ) -> dict[str, Any]:
        payload = {
            "text": text,
            "client_info": {
                "source_member_id": source_member_id,
                "mode": "maid_message",
            },
            "maid": {
                "uuid": maid_uuid,
            },
        }
        return await self._send_maid_frame(
            "maid.message.in",
            payload,
            server_id=server_id,
            endpoint_id=endpoint_id,
            deadline_ms=deadline_ms,
        )

    @API("registry_catalog", description="获取 MaidBridge registry 能力目录", version="1", public=True)
    async def get_registry_catalog(
        self,
        kind: str,
        server_id: str = "",
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        resolved_server_id, resolved_endpoint_id = self._resolve_registry_scope(kind, server_id, endpoint_id)
        return query_api.get_catalog(
            kind,
            server_id=resolved_server_id,
            endpoint_id=resolved_endpoint_id,
        )

    @API("registry_list", description="列出 MaidBridge 注册表条目", version="1", public=True)
    async def list_registry_items(
        self,
        kind: str,
        server_id: str = "",
        endpoint_id: str = "",
    ) -> list[dict[str, Any]]:
        resolved_server_id, resolved_endpoint_id = self._resolve_registry_scope(kind, server_id, endpoint_id)
        return query_api.list_items(
            kind,
            server_id=resolved_server_id,
            endpoint_id=resolved_endpoint_id,
        )

    @API("registry_get", description="获取单个 MaidBridge 注册表条目", version="1", public=True)
    async def get_registry_item(
        self,
        kind: str,
        key: str,
        server_id: str = "",
        endpoint_id: str = "",
    ) -> dict[str, Any] | None:
        resolved_server_id, resolved_endpoint_id = self._resolve_registry_scope(kind, server_id, endpoint_id)
        return query_api.get_item(
            kind,
            key,
            server_id=resolved_server_id,
            endpoint_id=resolved_endpoint_id,
        )

    @API("registry_search", description="搜索 MaidBridge 注册表条目", version="1", public=True)
    async def search_registry_items(
        self,
        kind: str,
        text: str,
        server_id: str = "",
        endpoint_id: str = "",
    ) -> list[dict[str, Any]]:
        resolved_server_id, resolved_endpoint_id = self._resolve_registry_scope(kind, server_id, endpoint_id)
        return query_api.search_items(
            kind,
            text,
            server_id=resolved_server_id,
            endpoint_id=resolved_endpoint_id,
        )

    @API("endpoints", description="列出 MaidBridge 端点注册信息", version="1", public=True)
    async def list_endpoints(self) -> list[dict[str, Any]]:
        return [
            {
                **endpoint,
                "features": dict(endpoint.get("features", {})),
                "capabilities": dict(endpoint.get("capabilities", {})),
            }
            for _, endpoint in sorted(self._state.endpoints.items())
        ]

    def _resolve_server_id(self, server_id: str) -> str:
        normalized = server_id.strip()
        return normalized or self._settings().server_id

    def _resolve_endpoint_id(self, endpoint_id: str) -> str:
        normalized = endpoint_id.strip()
        return normalized or DEFAULT_ENDPOINT_ID

    def _resolve_registry_scope(self, kind: str, server_id: str, endpoint_id: str) -> tuple[str, str]:
        normalized_server_id = server_id.strip()
        normalized_endpoint_id = endpoint_id.strip()
        if normalized_server_id or normalized_endpoint_id:
            return query_api.latest_catalog_scope(kind, server_id=normalized_server_id, endpoint_id=normalized_endpoint_id)
        registered_scope = self._registered_registry_scope(kind)
        if registered_scope is not None:
            return registered_scope
        return query_api.latest_catalog_scope(kind)

    def _registered_registry_scope(self, kind: str) -> tuple[str, str] | None:
        candidates: list[dict[str, Any]] = []
        registered_scopes: list[tuple[str, str]] = []
        for _, endpoint in sorted(self._state.endpoints.items()):
            if not isinstance(endpoint, dict):
                continue
            server_id = str(endpoint.get("server_id") or "").strip()
            endpoint_id = str(endpoint.get("endpoint_id") or "").strip()
            if not server_id or not endpoint_id:
                continue
            registered_scopes.append((server_id, endpoint_id))
            catalog = query_api.get_catalog(kind, server_id=server_id, endpoint_id=endpoint_id)
            if int(catalog.get("generated_at") or 0) > 0:
                candidates.append(catalog)
        if not candidates:
            return registered_scopes[0] if registered_scopes else None
        latest = max(candidates, key=self._catalog_order)
        return str(latest["server_id"]), str(latest["endpoint_id"])

    @staticmethod
    def _catalog_order(catalog: dict[str, Any]) -> tuple[int, int, str]:
        return (
            int(catalog.get("generated_at") or 0),
            int(catalog.get("revision") or 0),
            str(catalog.get("registry_id") or ""),
        )

    async def _send_maid_frame(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        server_id: str,
        endpoint_id: str,
        deadline_ms: int,
    ) -> dict[str, Any]:
        settings = self._settings()
        if not self._state.connected:
            return {"error": "MaidBridge 传输层未连接"}
        if self._transport is None:
            return {"error": "MaidBridge 传输发送循环未启动"}
        payload = self._payload_with_default_maid(event_type, payload, settings)
        maid = payload.get("maid") if isinstance(payload.get("maid"), dict) else {}
        request_id = f"maibot-maid-{uuid4()}"
        is_api_request = event_type.startswith("maid.api.") and not event_type.startswith("maid.api.registry.")
        frame = build_ai_event_frame(
            event_type=event_type,
            event_id=request_id,
            request_id=request_id,
            trace_id=f"trace-{uuid4()}",
            server_id=self._resolve_server_id(server_id) if is_api_request else "",
            endpoint_id=self._resolve_endpoint_id(endpoint_id) if is_api_request else "",
            payload=payload,
            deadline_ms=deadline_ms or settings.request_timeout_ms,
            maid_uuid=str(maid.get("uuid") or ""),
            direction=CLIENT_TO_JAVA,
        )
        reply = await self._send_frame_await_reply(frame, settings=settings)
        return reply["payload"]

    def _payload_with_default_maid(
        self,
        event_type: str,
        payload: dict[str, Any],
        settings: Any,
    ) -> dict[str, Any]:
        prepared = dict(payload)
        if event_type == "maid.message.in" or (
            event_type.startswith("maid.api.") and not event_type.startswith("maid.api.registry.")
        ):
            maid = dict(prepared.get("maid")) if isinstance(prepared.get("maid"), dict) else {}
            if "maid_uuid" in prepared or "maid_entity_id" in prepared:
                raise BridgeProtocolError(
                    "payload.maid.uuid 必须放在 maid 对象内，不支持根级 maid_uuid/maid_entity_id"
                )
            if not str(maid.get("uuid") or "").strip() and settings.default_maid_uuid:
                maid["uuid"] = settings.default_maid_uuid
            if maid:
                prepared["maid"] = maid
        return prepared

    async def _send_frame_await_reply(self, frame: Any, *, settings: Any) -> dict[str, Any]:
        if self._transport is None:
            self.ctx.logger.warning("MaidBridge 请求未发送：传输发送循环未启动")
            return {
                "type": "bridge.error",
                "payload": {"error": "MaidBridge 传输发送循环未启动"},
            }
        future = asyncio.get_running_loop().create_future()
        self._state.add_pending(
            self._pending_request(
                request_id=frame.id,
                trace_id=frame.trace_id,
                deadline_ms=frame.deadline_ms,
                future=future,
                frame_type=frame.type,
            )
        )
        self.ctx.logger.debug(
            f"MaidBridge 待响应请求已登记 [request_id={frame.id}, trace_id={frame.trace_id}, "
            f"type={frame.type}, deadline_ms={frame.deadline_ms}]"
        )
        try:
            await self._transport.send(frame.dumps(max_bytes=settings.max_message_bytes))
            self.ctx.logger.debug(
                f"MaidBridge 帧已发送 [request_id={frame.id}, trace_id={frame.trace_id}, type={frame.type}]"
            )
            return await asyncio.wait_for(future, timeout=frame.deadline_ms / 1000)
        except TimeoutError:
            self._state.pending_requests.pop(frame.id, None)
            self.ctx.logger.warning(
                f"MaidBridge 待响应请求超时 [request_id={frame.id}, trace_id={frame.trace_id}, "
                f"type={frame.type}, deadline_ms={frame.deadline_ms}]"
            )
            return {
                "type": "bridge.error",
                "reply_to": frame.id,
                "trace_id": frame.trace_id,
                "payload": {"error": f"MaidBridge 请求 {frame.id} 超时"},
            }
        except Exception as exc:
            self._state.pending_requests.pop(frame.id, None)
            self.ctx.logger.warning(
                f"MaidBridge 请求等待回复时失败 [request_id={frame.id}, "
                f"trace_id={frame.trace_id}, error={exc}]"
            )
            raise

    async def _send_frame(self, frame: Any) -> None:
        if self._transport is None:
            raise RuntimeError("MaidBridge 传输发送循环未启动")
        settings = self._settings()
        await self._transport.send(frame.dumps(max_bytes=settings.max_message_bytes))
