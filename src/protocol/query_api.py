from copy import deepcopy
from time import time
from typing import Any, Iterable, Mapping

RegistryItem = dict[str, Any]
CatalogRecord = dict[str, Any]

_VALID_KINDS = frozenset({"tools", "skills", "contexts", "tasks", "sites"})
_DEFAULT_SERVER_ID = "__default__"
_DEFAULT_ENDPOINT_ID = "__default__"
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "accesstoken",
        "apikey",
        "authorization",
        "bearer",
        "body",
        "credential",
        "header",
        "history",
        "message",
        "oauth",
        "password",
        "prompt",
        "rawrequest",
        "rawresponse",
        "reference",
        "refreshtoken",
        "request",
        "response",
        "secret",
        "token",
        "value",
    }
)
_CATALOGS: dict[tuple[str, str, str], CatalogRecord] = {}


def clear_catalogs() -> None:
    _CATALOGS.clear()


def store_catalog(
    kind: str,
    items: Iterable[Mapping[str, Any]],
    trace_id: str,
    *,
    server_id: str = _DEFAULT_SERVER_ID,
    endpoint_id: str = _DEFAULT_ENDPOINT_ID,
    registry_id: str = "",
    revision: int = 0,
    source: str = "maidbridge",
    visibility: str = "private",
    expires_at: int | None = None,
) -> None:
    _validate_kind(kind)
    normalized_server_id = _normalize_scope(server_id, "server_id")
    normalized_endpoint_id = _normalize_scope(endpoint_id, "endpoint_id")
    if not isinstance(revision, int) or revision < 0:
        raise ValueError("revision 必须是非负整数")
    _CATALOGS[(kind, normalized_server_id, normalized_endpoint_id)] = {
        "kind": kind,
        "items": [_sanitize_registry_item(item) for item in items],
        "trace_id": trace_id,
        "server_id": normalized_server_id,
        "endpoint_id": normalized_endpoint_id,
        "registry_id": registry_id or f"{kind}:{normalized_server_id}:{normalized_endpoint_id}:{revision}",
        "revision": revision,
        "generated_at": int(time() * 1000),
        "source": source,
        "visibility": visibility,
        "expires_at": expires_at,
    }


def get_catalog(
    kind: str,
    *,
    server_id: str = _DEFAULT_SERVER_ID,
    endpoint_id: str = _DEFAULT_ENDPOINT_ID,
) -> CatalogRecord:
    record = _record_for(kind, server_id=server_id, endpoint_id=endpoint_id)
    return deepcopy(record)


def list_items(
    kind: str,
    *,
    server_id: str = _DEFAULT_SERVER_ID,
    endpoint_id: str = _DEFAULT_ENDPOINT_ID,
) -> list[RegistryItem]:
    return deepcopy(_record_for(kind, server_id=server_id, endpoint_id=endpoint_id)["items"])


def get_item(
    kind: str,
    key: str,
    *,
    server_id: str = _DEFAULT_SERVER_ID,
    endpoint_id: str | None = None,
) -> RegistryItem | None:
    for record in _matching_records(kind, server_id=server_id, endpoint_id=endpoint_id):
        for item in record["items"]:
            if item.get("id") == key or item.get("name") == key:
                return deepcopy(item)
    return None


def search_items(
    kind: str,
    text: str,
    *,
    server_id: str = _DEFAULT_SERVER_ID,
    endpoint_id: str | None = None,
) -> list[RegistryItem]:
    needle = text.casefold()
    matches: list[RegistryItem] = []
    for record in _matching_records(kind, server_id=server_id, endpoint_id=endpoint_id):
        matches.extend(
            deepcopy(item)
            for item in record["items"]
            if _matches_search_text(item, needle)
        )
    return matches


def latest_catalog_scope(kind: str, *, server_id: str = "", endpoint_id: str = "") -> tuple[str, str]:
    _validate_kind(kind)
    records = _matching_records_optional(
        kind,
        server_id=server_id.strip() or None,
        endpoint_id=endpoint_id.strip() or None,
    )
    if records:
        latest = max(records, key=_catalog_order_key)
        return str(latest["server_id"]), str(latest["endpoint_id"])
    return (
        server_id.strip() or _DEFAULT_SERVER_ID,
        endpoint_id.strip() or _DEFAULT_ENDPOINT_ID,
    )


def _record_for(kind: str, *, server_id: str, endpoint_id: str) -> CatalogRecord:
    _validate_kind(kind)
    normalized_server_id = _normalize_scope(server_id, "server_id")
    normalized_endpoint_id = _normalize_scope(endpoint_id, "endpoint_id")
    return _CATALOGS.get(
        (kind, normalized_server_id, normalized_endpoint_id),
        {
            "kind": kind,
            "items": [],
            "trace_id": "",
            "server_id": normalized_server_id,
            "endpoint_id": normalized_endpoint_id,
            "registry_id": "",
            "revision": 0,
            "generated_at": 0,
            "source": "",
            "visibility": "private",
            "expires_at": None,
        },
    )


def _matching_records(kind: str, *, server_id: str, endpoint_id: str | None) -> list[CatalogRecord]:
    _validate_kind(kind)
    normalized_server_id = _normalize_scope(server_id, "server_id")
    if endpoint_id is not None:
        return [_record_for(kind, server_id=normalized_server_id, endpoint_id=endpoint_id)]
    return [
        record
        for (record_kind, record_server_id, _), record in _CATALOGS.items()
        if record_kind == kind and record_server_id == normalized_server_id
    ]


def _matching_records_optional(kind: str, *, server_id: str | None, endpoint_id: str | None) -> list[CatalogRecord]:
    return [
        record
        for (record_kind, record_server_id, record_endpoint_id), record in _CATALOGS.items()
        if record_kind == kind
        and (server_id is None or record_server_id == server_id)
        and (endpoint_id is None or record_endpoint_id == endpoint_id)
    ]


def _catalog_order_key(record: CatalogRecord) -> tuple[int, int, str]:
    return (
        int(record.get("generated_at") or 0),
        int(record.get("revision") or 0),
        str(record.get("registry_id") or ""),
    )


def _validate_kind(kind: str) -> None:
    if kind not in _VALID_KINDS:
        allowed = ", ".join(sorted(_VALID_KINDS))
        raise ValueError(f"注册表类型无效：{kind!r}；可用类型为 {allowed}")


def _normalize_scope(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _matches_search_text(item: Mapping[str, Any], needle: str) -> bool:
    return any(
        needle in str(item.get(field, "")).casefold()
        for field in ("id", "name", "description")
    )


def _sanitize_registry_item(item: Mapping[str, Any]) -> RegistryItem:
    return {
        str(key): _sanitize_registry_value(value)
        for key, value in dict(item).items()
        if not _is_sensitive_registry_key(str(key))
    }


def _sanitize_registry_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_registry_value(nested_value)
            for key, nested_value in dict(value).items()
            if not _is_sensitive_registry_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_registry_value(item) for item in value]
    return deepcopy(value)


def _is_sensitive_registry_key(key: str) -> bool:
    compact = "".join(character for character in key.casefold() if character.isalnum())
    return any(part in compact for part in _SENSITIVE_KEY_PARTS)
