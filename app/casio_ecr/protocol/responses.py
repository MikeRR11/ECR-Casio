"""Response payload parsers for jobs 0008 / 0009 / 0013 / 0014."""
from __future__ import annotations

from dataclasses import dataclass

from . import bcd


@dataclass
class RegInfo:
    serial_no: str
    terminal_no: str
    app_version: str
    booter_version: str
    charset_code: str
    target_code: str
    language_code: str


def parse_reg_info(data: bytes) -> RegInfo | None:
    if len(data) < 40:
        return None
    return RegInfo(
        serial_no=data[0:14].decode("utf-8", errors="replace").strip("\x00"),
        terminal_no=bcd.bcd_to_hexstring(data[14:16]),
        app_version=data[16:26].decode("utf-8", errors="replace").strip("\x00"),
        booter_version=data[26:36].decode("utf-8", errors="replace").strip("\x00"),
        charset_code=f"{data[37]:02d}",
        target_code=f"{data[38]:02d}",
        language_code=f"{data[39]:02d}",
    )


def parse_unsent_count(data: bytes) -> int:
    return int.from_bytes(data[0:4], "little")


def parse_xz_result(data: bytes) -> str:
    """'00' means success."""
    return bcd.bcd_to_hexstring(data[0:1])


@dataclass
class SalesConfirmLine:
    label: str
    quantity: int | None
    amount: int


@dataclass
class SalesConfirm:
    gross: SalesConfirmLine
    net: SalesConfirmLine
    caid: SalesConfirmLine
    chid: SalesConfirmLine
    ckid: SalesConfirmLine
    crid1: SalesConfirmLine
    crid2: SalesConfirmLine
    crid3: SalesConfirmLine
    crid4: SalesConfirmLine


_FIELD_ORDER = ["gross", "net", "caid", "chid", "ckid", "crid1", "crid2", "crid3", "crid4"]


def parse_sales_confirm(data: bytes) -> SalesConfirm | None:
    offset = 0
    lines: dict[str, SalesConfirmLine] = {}
    for i, name in enumerate(_FIELD_ORDER):
        if offset + 12 > len(data):
            return None
        label = data[offset : offset + 12].decode("utf-8", errors="replace").strip("\x00 ")
        offset += 12
        qty = None
        if i < 2:
            if offset + 5 > len(data):
                return None
            qty_bytes = data[offset : offset + 5]
            offset += 5
            qty = 0 if bcd.is_blank_bcd(qty_bytes) else bcd.bcd_to_long(qty_bytes)
        if offset + 5 > len(data):
            return None
        amt_bytes = data[offset : offset + 5]
        offset += 5
        amt = 0 if bcd.is_blank_bcd(amt_bytes) else bcd.bcd_to_long(amt_bytes)
        lines[name] = SalesConfirmLine(label=label, quantity=qty, amount=amt)
    return SalesConfirm(**lines)
