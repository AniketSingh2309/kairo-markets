"""Minimal protobuf wire-format decoder for Yahoo's ``PricingData`` message.

Yahoo's streaming feed sends base64-encoded protobuf. Only the fields we use
are mapped; unknown fields are skipped, so new upstream fields are harmless.
Pure Python on purpose: the ``protobuf`` package ships native code that some
locked-down Windows machines block.
"""

from __future__ import annotations

import base64
import struct
from typing import Any

# field number -> (name, kind). Kinds: string, float, double, sint64, int32.
PRICING_FIELDS: dict[int, tuple[str, str]] = {
    1: ("id", "string"),
    2: ("price", "float"),
    3: ("time", "sint64"),            # epoch milliseconds
    4: ("currency", "string"),
    5: ("exchange", "string"),
    6: ("quote_type", "int32"),
    7: ("market_hours", "int32"),     # 0 pre, 1 regular, 2 post, 3 extended
    8: ("change_percent", "float"),
    9: ("day_volume", "sint64"),
    10: ("day_high", "float"),
    11: ("day_low", "float"),
    12: ("change", "float"),
    13: ("short_name", "string"),
    15: ("open_price", "float"),
    16: ("previous_close", "float"),
    22: ("last_size", "sint64"),
    23: ("bid", "float"),
    25: ("ask", "float"),
}


class ProtobufDecodeError(ValueError):
    pass


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ProtobufDecodeError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtobufDecodeError("varint too long")


def _zigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def decode(buf: bytes, fields: dict[int, tuple[str, str]] = PRICING_FIELDS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wire = key >> 3, key & 7
        name, kind = fields.get(number, (None, None))
        if wire == 0:  # varint
            raw, pos = _varint(buf, pos)
            if name:
                out[name] = _zigzag(raw) if kind == "sint64" else (raw - (1 << 64) if raw >= 1 << 63 else raw)
        elif wire == 1:  # 64-bit
            if pos + 8 > len(buf):
                raise ProtobufDecodeError("truncated fixed64")
            if name:
                out[name] = struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        elif wire == 2:  # length-delimited
            length, pos = _varint(buf, pos)
            if pos + length > len(buf):
                raise ProtobufDecodeError("truncated bytes field")
            if name:
                out[name] = buf[pos : pos + length].decode("utf-8", errors="replace")
            pos += length
        elif wire == 5:  # 32-bit
            if pos + 4 > len(buf):
                raise ProtobufDecodeError("truncated fixed32")
            if name:
                out[name] = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        else:
            raise ProtobufDecodeError(f"unsupported wire type {wire}")
    return out


def decode_pricing_message(b64: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(b64, validate=False)
    except (ValueError, TypeError) as exc:
        raise ProtobufDecodeError(f"invalid base64: {exc}") from exc
    return decode(raw)
