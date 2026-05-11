from collections.abc import Mapping
from typing import Any


def first_non_blank(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def reconnect_attempt(metadata: Mapping[str, Any]) -> int:
    raw_attempt = metadata.get("reconnect_attempt")
    if isinstance(raw_attempt, int):
        return max(0, raw_attempt)
    if isinstance(raw_attempt, str) and raw_attempt.isdecimal():
        return int(raw_attempt)
    return 0
