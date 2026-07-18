"""Offline round-trip tests for the RLE codec and the SectionH/I/F parser.

Zero hardware risk: builds synthetic FileAll blobs matching the documented
CASIO physical-file layout, then checks the parser recovers the movement
detail. Run with:  python app/tests/test_salesfile.py   (or pytest).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from casio_ecr.protocol import rle, salesfile  # noqa: E402


# --- helpers to build a synthetic file -------------------------------------

def bcd(value: int, nbytes: int) -> bytes:
    """Encode a non-negative int as packed BCD in `nbytes` bytes (2 digits/byte)."""
    digits = str(value).rjust(nbytes * 2, "0")[-nbytes * 2 :]
    return bytes((int(digits[i]) << 4) | int(digits[i + 1]) for i in range(0, len(digits), 2))


def name_field(text: str, width: int = 12) -> bytes:
    return text.encode("latin-1").ljust(width, b" ")[:width]


# DEPT (FNO 5) record schema: name(13,12) + qty(1,5) + amount(2,5) => 22 bytes.
DEPT_SCHEMA = [(13, 12), (1, 5), (2, 5)]
DEPT_REC_LEN = 22


def dept_record(name: str, qty: int, amount: int) -> bytes:
    return name_field(name) + bcd(qty, 5) + bcd(amount, 5)


def section_h() -> bytes:
    h = bytearray(32)
    h[0] = salesfile.MAGIC_H
    h[1] = 0x01  # machine
    h[2] = 0x02  # id
    h[5:10] = bytes([0x25, 0x07, 0x18, 0x14, 0x30])  # datetime raw (yy mm dd hh mm)
    return bytes(h)


def section_i(fno: int, schema: list[tuple[int, int]]) -> bytes:
    body = bytearray()
    body.append(salesfile.MAGIC_I)
    fixed_len = 10 + 3 * len(schema)          # total SectionI byte count
    m_len = fixed_len - 3                       # header_len = mLEN + 3
    body += m_len.to_bytes(2, "big")
    body += fno.to_bytes(2, "big")
    body.append(0)                              # skip
    body.append(len(schema))                    # mALL
    body.append(0)                              # mPGM
    body.append(0)                              # skip
    body.append(0)                              # mCAL
    for fid, flen in schema:
        body += fid.to_bytes(2, "big")
        body.append(flen)
    assert len(body) == fixed_len
    return bytes(body)


def section_f(fno: int, rec_len: int, records: list[bytes], compressed: bool) -> bytes:
    start_rec, end_rec = 1, len(records)
    hdr = bytearray()
    hdr.append(salesfile.MAGIC_F)
    hdr += fno.to_bytes(2, "big")
    hdr.append(0)                               # skip
    hdr += bytes([0x00, 0x01])                   # zcounter
    hdr += rec_len.to_bytes(4, "big")
    hdr += start_rec.to_bytes(4, "big")
    hdr += end_rec.to_bytes(4, "big")
    assert len(hdr) == salesfile.SECTION_F_HEADER_LEN
    out = bytearray(hdr)
    for rec in records:
        assert len(rec) == rec_len
        out += rle.rle_compress(True, True, rec) if compressed else rec
    return bytes(out)


def build_file(compressed: bool) -> bytes:
    recs = [
        dept_record("DEPT01", 3, 15000),
        dept_record("DEPT02", 7, 42000),
        dept_record("BEBIDAS", 12, 108000),
    ]
    return (
        section_h()
        + section_i(5, DEPT_SCHEMA)
        + section_f(5, DEPT_REC_LEN, recs, compressed)
    )


# --- tests -----------------------------------------------------------------

def test_rle_record_roundtrip():
    original = dept_record("DEPT01", 3, 15000)  # has 0x00 runs -> exercises markers
    comp = rle.rle_compress(True, True, original)
    assert comp != original  # runs of zeros must have compressed
    rec, nxt = rle.rle_uncompress_record(True, True, comp, 0, len(original))
    assert rec == original
    assert nxt == len(comp)


def test_rle_two_records_stream():
    r1 = dept_record("A", 1, 100)
    r2 = dept_record("B", 2, 200)
    stream = rle.rle_compress(True, True, r1) + rle.rle_compress(True, True, r2)
    out1, off = rle.rle_uncompress_record(True, True, stream, 0, len(r1))
    out2, off = rle.rle_uncompress_record(True, True, stream, off, len(r2))
    assert out1 == r1 and out2 == r2
    assert off == len(stream)


def _check_parsed(sf: salesfile.SalesFile):
    assert sf.machine == 1 and sf.ident == 2
    dept = sf.block(5)
    assert dept is not None, "expected a DEPT (FNO 5) block"
    assert dept.label == "Departamentos"
    assert len(dept.records) == 3
    r0, r1, r2 = dept.records
    assert r0.label == "DEPT01" and r0.qty == 3 and r0.amount == 15000
    assert r1.label == "DEPT02" and r1.qty == 7 and r1.amount == 42000
    assert r2.label == "BEBIDAS" and r2.qty == 12 and r2.amount == 108000


def test_parse_uncompressed():
    _check_parsed(salesfile.parse_sales_file(build_file(False), records_compressed=False))


def test_parse_compressed():
    _check_parsed(salesfile.parse_sales_file(build_file(True), records_compressed=True))


def test_parse_auto_compressed():
    sf, was_comp = salesfile.parse_sales_file_auto(build_file(True))
    _check_parsed(sf)
    assert was_comp is True  # compressed stream must be detected as compressed


def _run_all():
    fns = [g for name, g in sorted(globals().items()) if name.startswith("test_") and callable(g)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed.")


if __name__ == "__main__":
    _run_all()
