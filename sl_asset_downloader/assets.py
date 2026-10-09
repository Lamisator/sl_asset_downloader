"""Asset kinds, how to fetch them, and how to turn them into normal files."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from . import inventory as inv

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetKind:
    name: str          # CLI name
    asset_type: int    # LLAssetType
    cap_name: str      # type name used in ViewerAsset URLs ("<name>_id=")
    via_http: bool     # ViewerAsset cap; otherwise UDP inventory transfer
    ext: str           # extension of the raw asset


KINDS: dict[str, AssetKind] = {k.name: k for k in [
    AssetKind("texture", inv.AT_TEXTURE, "texture", True, ".j2c"),
    AssetKind("sound", inv.AT_SOUND, "sound", True, ".ogg"),
    AssetKind("animation", inv.AT_ANIMATION, "animatn", True, ".anim"),
    AssetKind("gesture", inv.AT_GESTURE, "gesture", True, ".gesture"),
    AssetKind("landmark", inv.AT_LANDMARK, "landmark", True, ".landmark"),
    AssetKind("clothing", inv.AT_CLOTHING, "clothing", True, ".wearable"),
    AssetKind("bodypart", inv.AT_BODYPART, "bodypart", True, ".wearable"),
    AssetKind("settings", inv.AT_SETTINGS, "settings", True, ".llsd"),
    AssetKind("material", inv.AT_MATERIAL, "material", True, ".llsd"),
    AssetKind("notecard", inv.AT_NOTECARD, "notecard", False, ".txt"),
    AssetKind("script", inv.AT_LSL_TEXT, "lsltext", False, ".lsl"),
]}
KINDS_BY_TYPE = {k.asset_type: k for k in KINDS.values()}

IMAGE_FORMATS = {
    "png": ("PNG", ".png"),
    "jpg": ("JPEG", ".jpg"),
    "tga": ("TGA", ".tga"),
    "j2c": (None, ".j2c"),
}


def convert_texture(data: bytes, fmt: str) -> tuple[bytes, str]:
    """Convert a JPEG2000 codestream to the requested image format.

    Falls back to the raw .j2c if decoding is not possible."""
    pil_format, ext = IMAGE_FORMATS[fmt]
    if pil_format is None:
        return data, ext
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow is not installed; saving raw .j2c instead of .%s", fmt)
        return data, ".j2c"
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        log.warning("could not decode texture (%s); saving raw .j2c", e)
        return data, ".j2c"
    if pil_format == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA")
    out = io.BytesIO()
    save_args = {"quality": 95} if pil_format == "JPEG" else {}
    img.save(out, pil_format, **save_args)
    return out.getvalue(), ext


_TEXT_LENGTH = re.compile(rb"Text length (\d+)\n")


def notecard_text(data: bytes) -> bytes:
    """Extract the text body from a Linden notecard asset."""
    m = _TEXT_LENGTH.search(data)
    if not m:
        return data
    start = m.end()
    return data[start:start + int(m.group(1))]
