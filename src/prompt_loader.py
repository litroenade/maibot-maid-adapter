from pathlib import Path


DEFAULT_PROMPT_LOCALE = "zh-CN"
PROMPT_SUFFIX = ".prompt"

_PROMPTS_ROOT = Path(__file__).resolve().parents[1] / "prompts"


def load_prompt(name: str, locale: str = DEFAULT_PROMPT_LOCALE) -> str:
    normalized = _normalize_prompt_name(name)
    path = _resolve_prompt_path(normalized, locale)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"MaidBridge prompt 文件不可读: {path}") from exc
    if not text:
        raise RuntimeError(f"MaidBridge prompt 文件为空: {path}")
    return text


def render_prompt(name: str, locale: str = DEFAULT_PROMPT_LOCALE, **values: object) -> str:
    text = load_prompt(name, locale=locale)
    try:
        return text.format(**values)
    except KeyError as exc:
        raise KeyError(f"MaidBridge prompt '{name}' 缺少占位符参数: {exc.args[0]}") from exc


def _normalize_prompt_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("prompt 名称不能为空")
    if not normalized.replace("_", "").replace("-", "").isalnum():
        raise ValueError(f"prompt 名称非法: {name}")
    return normalized


def _normalize_locale(locale: str) -> str:
    normalized = str(locale or DEFAULT_PROMPT_LOCALE).strip().replace("_", "-")
    if not normalized:
        return DEFAULT_PROMPT_LOCALE
    parts = [part for part in normalized.split("-") if part]
    if not parts:
        return DEFAULT_PROMPT_LOCALE
    normalized_parts = [parts[0].lower()]
    normalized_parts.extend(part.upper() if len(part) == 2 else part for part in parts[1:])
    return "-".join(normalized_parts)


def _resolve_prompt_path(name: str, locale: str) -> Path:
    requested_locale = _normalize_locale(locale)
    for locale_candidate in (requested_locale, DEFAULT_PROMPT_LOCALE):
        path = _PROMPTS_ROOT / locale_candidate / f"{name}{PROMPT_SUFFIX}"
        if path.is_file():
            return path
    return _PROMPTS_ROOT / requested_locale / f"{name}{PROMPT_SUFFIX}"
