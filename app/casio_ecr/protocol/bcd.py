"""Binary-coded-decimal helpers matching BinaryCodedDecimalTool.java."""
from __future__ import annotations


def sub_bcd(data: bytes, start_digit: int = 0, num_digits: int | None = None) -> str:
    """Decode packed BCD nibbles into a decimal-digit string.

    Nibble value 0xF renders as '-' (sign). Two digits per byte, high nibble first.
    """
    digits = []
    for byte in data:
        digits.append((byte >> 4) & 0xF)
        digits.append(byte & 0xF)
    if num_digits is not None:
        digits = digits[start_digit : start_digit + num_digits]
    elif start_digit:
        digits = digits[start_digit:]
    out = []
    for d in digits:
        out.append("-" if d == 0xF else str(d))
    return "".join(out)


def bcd_to_long(data: bytes) -> int:
    s = sub_bcd(data)
    negative = s.startswith("-")
    s = s.lstrip("-") or "0"
    val = int(s)
    return -val if negative else val


def bcd_to_hexstring(data: bytes) -> str:
    """convertBCDtoString(): NOT decimal BCD, just hex-formats each byte."""
    return "".join(f"{b:02x}" for b in data)


def is_blank_bcd(data: bytes) -> bool:
    """Sentinel used by SalesConfirmDef parsing: every byte has the sign bit
    set (i.e. every byte >= 0x80 when read unsigned, or negative as a signed
    Java byte) means the field is blank/unpopulated -> treat as 0.
    """
    return all((b & 0x80) != 0 for b in data) if data else True


def read_short_be(data: bytes, offset: int = 0) -> int:
    return (data[offset] << 8) | data[offset + 1]


def read_int_be(data: bytes, offset: int = 0) -> int:
    return int.from_bytes(data[offset : offset + 4], "big", signed=False)
