"""Minimal LLSD XML (de)serializer.

LLSD is the structured data format Second Life uses for capability
(HTTP) traffic. Only the XML flavour is needed here.
"""

from __future__ import annotations

import base64
import datetime as _dt
import uuid
import xml.etree.ElementTree as ET
from typing import Any

CONTENT_TYPE = "application/llsd+xml"


class URI(str):
    """Marks a string that should be serialized as an LLSD <uri>."""


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

def _to_elem(value: Any) -> ET.Element:
    if value is None:
        return ET.Element("undef")
    if isinstance(value, bool):
        el = ET.Element("boolean")
        el.text = "true" if value else "false"
        return el
    if isinstance(value, int):
        el = ET.Element("integer")
        el.text = str(value)
        return el
    if isinstance(value, float):
        el = ET.Element("real")
        el.text = repr(value)
        return el
    if isinstance(value, uuid.UUID):
        el = ET.Element("uuid")
        el.text = str(value)
        return el
    if isinstance(value, URI):
        el = ET.Element("uri")
        el.text = str(value)
        return el
    if isinstance(value, str):
        el = ET.Element("string")
        el.text = value
        return el
    if isinstance(value, (bytes, bytearray)):
        el = ET.Element("binary", encoding="base64")
        el.text = base64.b64encode(bytes(value)).decode("ascii")
        return el
    if isinstance(value, _dt.datetime):
        el = ET.Element("date")
        el.text = value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return el
    if isinstance(value, dict):
        el = ET.Element("map")
        for k, v in value.items():
            key = ET.SubElement(el, "key")
            key.text = str(k)
            el.append(_to_elem(v))
        return el
    if isinstance(value, (list, tuple)):
        el = ET.Element("array")
        for v in value:
            el.append(_to_elem(v))
        return el
    raise TypeError(f"cannot serialize {type(value).__name__} to LLSD")


def format_xml(value: Any) -> bytes:
    root = ET.Element("llsd")
    root.append(_to_elem(value))
    return b'<?xml version="1.0" ?>' + ET.tostring(root, encoding="utf-8")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _from_elem(el: ET.Element) -> Any:
    tag = el.tag
    text = el.text or ""
    if tag == "undef":
        return None
    if tag == "boolean":
        return text.strip().lower() in ("1", "true")
    if tag == "integer":
        return int(text.strip() or 0)
    if tag == "real":
        t = text.strip()
        return float(t) if t else 0.0
    if tag == "uuid":
        t = text.strip()
        return uuid.UUID(t) if t else uuid.UUID(int=0)
    if tag in ("string", "uri"):
        return text
    if tag == "binary":
        return base64.b64decode(text)
    if tag == "date":
        t = text.strip()
        if not t:
            return _dt.datetime.fromtimestamp(0, _dt.timezone.utc)
        return _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    if tag == "map":
        result = {}
        children = list(el)
        for i in range(0, len(children) - 1, 2):
            result[children[i].text or ""] = _from_elem(children[i + 1])
        return result
    if tag == "array":
        return [_from_elem(c) for c in el]
    raise ValueError(f"unknown LLSD element <{tag}>")


def parse_xml(data: bytes) -> Any:
    root = ET.fromstring(data)
    if root.tag != "llsd":
        raise ValueError("not an LLSD document")
    children = list(root)
    if not children:
        return None
    return _from_elem(children[0])
