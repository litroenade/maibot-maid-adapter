import base64
import hashlib
import inspect
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageSequence

from ..utils import first_non_blank

EXTERNAL_EMOJI_ACTION_TYPE = "show_external_emoji"
PNG_FORMAT = "png"
GIF_FORMAT = "gif"
SUPPORTED_EXTERNAL_EMOJI_FORMATS = (PNG_FORMAT, GIF_FORMAT)
TLM_EMOJI_WIDTH = 24
TLM_EMOJI_HEIGHT = 24
TLM_EMOJI_SIZE = (TLM_EMOJI_WIDTH, TLM_EMOJI_HEIGHT)
RESAMPLE_FILTER = Image.Resampling.LANCZOS


class ExternalEmojiError(ValueError):
    """表情包桥接无法继续时抛出的业务错误。"""


def build_action_from_component(component: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    image = normalize_component_image(component)
    return build_action_from_image(image), image_metadata(image)


def build_action_from_maibot_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    image = normalize_component_image(
        {
            "binary_data_base64": _maibot_payload_base64(payload),
            "hash": first_non_blank(payload.get("hash"), payload.get("file_hash"), payload.get("binary_hash")),
        }
    )
    return build_action_from_image(image), image_metadata(image)


def build_action_from_image(image: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": EXTERNAL_EMOJI_ACTION_TYPE,
        "image": dict(image),
    }


async def select_maibot_emoji_payload(ctx: Any, query_text: str) -> Mapping[str, Any] | None:
    emoji_api = getattr(ctx, "emoji", None)
    if emoji_api is None:
        return None

    normalized_query = str(query_text or "").strip()
    if normalized_query:
        finder = getattr(emoji_api, "get_by_description", None)
        if callable(finder):
            matched = await _call_emoji_api(finder, normalized_query)
            payload = _coerce_maibot_emoji_payload(matched)
            if payload is not None:
                return payload

    random_getter = getattr(emoji_api, "get_random", None)
    if not callable(random_getter):
        return None
    random_emojis = await _call_emoji_api(random_getter, 1)
    if isinstance(random_emojis, list):
        for item in random_emojis:
            payload = _coerce_maibot_emoji_payload(item)
            if payload is not None:
                return payload
        return None
    return _coerce_maibot_emoji_payload(random_emojis)


def normalize_component_image(component: Mapping[str, Any]) -> dict[str, Any]:
    raw_base64 = first_non_blank(component.get("binary_data_base64"))
    if not raw_base64:
        raise ExternalEmojiError("MaiBot 表情包出站消息缺少 binary_data_base64")
    raw_bytes = _decode_base64(raw_base64)

    try:
        with Image.open(BytesIO(raw_bytes)) as source:
            source_format = str(source.format or "").lower()
            if _is_animated_image(source):
                gif_bytes = _convert_animated_image_to_gif(source)
                return _image_payload(
                    image_format=GIF_FORMAT,
                    image_bytes=gif_bytes,
                    source_hash=first_non_blank(component.get("hash")),
                    source_format=source_format,
                    width=TLM_EMOJI_WIDTH,
                    height=TLM_EMOJI_HEIGHT,
                )
            image = _fit_to_tlm_emoji_size(ImageOps.exif_transpose(source).convert("RGBA"))
    except ExternalEmojiError:
        raise
    except Exception as exc:
        raise ExternalEmojiError(f"MaiBot 表情包图片无法解析：{exc}") from exc

    output = BytesIO()
    image.save(output, format=PNG_FORMAT.upper(), optimize=True)
    return _image_payload(
        image_format=PNG_FORMAT,
        image_bytes=output.getvalue(),
        source_hash=first_non_blank(component.get("hash")),
        source_format=source_format,
        width=TLM_EMOJI_WIDTH,
        height=TLM_EMOJI_HEIGHT,
    )


def image_metadata(image: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: image[key]
        for key in ("format", "hash", "source_hash", "source_format", "width", "height", "bytes")
        if key in image
    }


def _coerce_maibot_emoji_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping) and first_non_blank(value.get("base64")):
        return value
    return None


def _maibot_payload_base64(payload: Mapping[str, Any]) -> str:
    raw_base64 = first_non_blank(payload.get("base64"), payload.get("binary_data_base64"))
    if raw_base64:
        return raw_base64

    file_path = first_non_blank(payload.get("full_path"), payload.get("file_path"), payload.get("path"))
    if not file_path:
        raise ExternalEmojiError("MaiBot 表情包缺少 base64 图片数据或本地文件路径")
    try:
        return base64.b64encode(Path(file_path).expanduser().read_bytes()).decode("ascii")
    except OSError as exc:
        raise ExternalEmojiError(f"读取 MaiBot 表情包文件失败：{exc}") from exc


async def _call_emoji_api(method: Any, *args: Any) -> Any:
    result = method(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _decode_base64(raw_base64: str) -> bytes:
    payload = raw_base64.split(",", 1)[1] if raw_base64.startswith("data:") and "," in raw_base64 else raw_base64
    try:
        return base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ExternalEmojiError("MaiBot 表情包 binary_data_base64 不是合法 Base64") from exc


def _image_payload(
    *,
    image_format: str,
    image_bytes: bytes,
    source_hash: str,
    source_format: str,
    width: int,
    height: int,
) -> dict[str, Any]:
    return {
        "format": image_format,
        "data_base64": base64.b64encode(image_bytes).decode("ascii"),
        "hash": hashlib.sha256(image_bytes).hexdigest(),
        "source_hash": source_hash,
        "source_format": source_format,
        "width": width,
        "height": height,
        "bytes": len(image_bytes),
    }


def _is_animated_image(source: Image.Image) -> bool:
    if bool(getattr(source, "is_animated", False)):
        return True
    try:
        return int(getattr(source, "n_frames", 1) or 1) > 1
    except (TypeError, ValueError):
        return False


def _convert_animated_image_to_gif(source: Image.Image) -> bytes:
    frames: list[Image.Image] = []
    durations: list[int] = []
    disposals: list[int] = []
    for frame in ImageSequence.Iterator(source):
        frames.append(_fit_to_tlm_emoji_size(ImageOps.exif_transpose(frame).convert("RGBA")))
        if (duration := _frame_duration_ms(frame, source)) is not None:
            durations.append(duration)
        if (disposal := _frame_disposal(frame)) is not None:
            disposals.append(disposal)
    if not frames:
        raise ExternalEmojiError("MaiBot 动图表情包没有可转换的帧")

    output = BytesIO()
    save_kwargs: dict[str, Any] = {
        "format": GIF_FORMAT.upper(),
        "save_all": True,
        "append_images": frames[1:],
        "loop": _gif_loop_count(source),
        "optimize": True,
    }
    if len(durations) == len(frames):
        save_kwargs["duration"] = durations
    if len(disposals) == len(frames):
        save_kwargs["disposal"] = disposals
    frames[0].save(output, **save_kwargs)
    return output.getvalue()


def _fit_to_tlm_emoji_size(image: Image.Image) -> Image.Image:
    resized = ImageOps.contain(image, TLM_EMOJI_SIZE, RESAMPLE_FILTER)
    canvas = Image.new("RGBA", TLM_EMOJI_SIZE, (0, 0, 0, 0))
    left = (TLM_EMOJI_WIDTH - resized.width) // 2
    top = (TLM_EMOJI_HEIGHT - resized.height) // 2
    canvas.alpha_composite(resized, (left, top))
    return canvas


def _frame_duration_ms(frame: Image.Image, source: Image.Image) -> int | None:
    raw_duration = frame.info.get("duration", source.info.get("duration"))
    if raw_duration is None:
        return None
    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _frame_disposal(frame: Image.Image) -> int | None:
    raw_disposal = getattr(frame, "disposal_method", None)
    if raw_disposal is None:
        raw_disposal = frame.info.get("disposal")
    if raw_disposal is None:
        return None
    try:
        disposal = int(raw_disposal)
    except (TypeError, ValueError):
        return None
    return disposal if disposal >= 0 else None


def _gif_loop_count(source: Image.Image) -> int:
    try:
        return max(0, int(source.info.get("loop", 0) or 0))
    except (TypeError, ValueError):
        return 0
