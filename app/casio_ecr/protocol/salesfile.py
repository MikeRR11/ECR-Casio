"""Parser for the CASIO physical sales-data file streamed over BLE.

An X/Z report transfer (job 0013 -> 9004/9005/9006) returns one "FileAll"
blob whose bytes this module turns into structured movement detail:
per-department, per-PLU, hourly, clerk, electronic-journal, and
fixed-totalizer records. This is the "detalle de movimientos" the sales
confirm job (0014) cannot give -- 0014 only returns the 9 cumulative
totals, whereas the report file carries every line the register would
print on a paper X/Z.

Layout (see docs/protocol/casio_ble_protocol_spec.md section 5):

    [SectionH: 32 bytes]
    [SectionI: schema for FNO n][SectionF header][records...] ...repeat...

SectionI is self-describing: it declares, per file number (FNO), the exact
list of {field-id, byte-length} that every data record of that file uses.
So we never hard-code record layouts -- we read the schema, then slice each
record by it.

Records may arrive RLE-compressed off BLE (each record compressed to its
own variable-length blob, expanded back to the SectionF header's fixed
record length). Whether the live register compresses is not yet confirmed
against hardware, so `parse_sales_file(..., records_compressed=...)` is a
parameter; when unknown, `parse_sales_file_auto()` tries both and keeps
whichever yields clean section magic bytes throughout.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import bcd, rle

# --- Section magic bytes ---------------------------------------------------
MAGIC_H = 0x68  # 'h' file header
MAGIC_I = 0x69  # 'i' per-file schema descriptor
MAGIC_F = 0x66  # 'f' record block header

SECTION_H_LEN = 32
SECTION_F_HEADER_LEN = 18

# --- FieldID -> (name, kind) ----------------------------------------------
# kind: "bcd" numeric, "char" text, "raw" passthrough hex.
_FIELD = {
    1: ("qty", "bcd"),
    2: ("amount", "bcd"),
    8: ("gross_amount", "bcd"),
    9: ("net_qty", "bcd"),
    10: ("net_amount", "bcd"),
    13: ("name", "char"),
    14: ("base", "raw"),
    15: ("price", "bcd"),
    16: ("link", "bcd"),
    23: ("function_code", "bcd"),
    31: ("clerk_no", "bcd"),
    34: ("obr_code", "raw"),
    37: ("update_date", "raw"),
    122: ("hourly_start", "bcd"),
    123: ("hourly_end", "bcd"),
    124: ("hourly_cust", "bcd"),
    148: ("attend_date", "raw"),
    149: ("attend_time", "raw"),
    152: ("attend_clerk", "bcd"),
    191: ("ej_type", "bcd"),
    192: ("ej_data", "raw"),
    193: ("ej_dummy", "bcd"),
}

# --- FNO -> human label ----------------------------------------------------
FNO_LABELS = {
    0: "Ventas diarias",
    1: "Totalizadores fijos",
    2: "Teclas de funcion",
    4: "PLU (articulos)",
    5: "Departamentos",
    6: "Grupos",
    9: "Por hora",
    10: "Ventas mensuales",
    11: "Empleados",
    15: "Indice de cuentas",
    19: "Asistencia",
    20: "Gran total",
    48: "Diario electronico",
}


class SalesFileError(Exception):
    pass


@dataclass
class RecordField:
    fid: int
    name: str
    kind: str
    raw: bytes
    value: object  # int for bcd, str for char, hex-str for raw


@dataclass
class Record:
    rec_no: int
    fields: list[RecordField]

    def get(self, name: str):
        for f in self.fields:
            if f.name == name:
                return f.value
        return None

    @property
    def label(self) -> str | None:
        v = self.get("name")
        return v.strip() if isinstance(v, str) else None

    @property
    def qty(self) -> int | None:
        v = self.get("qty")
        return v if isinstance(v, int) else None

    @property
    def amount(self) -> int | None:
        v = self.get("amount")
        return v if isinstance(v, int) else None


@dataclass
class FileBlock:
    fno: int
    label: str
    z_counter: str
    start_rec: int
    end_rec: int
    rec_len: int
    schema: list[tuple[int, int]]  # [(fid, length), ...]
    records: list[Record] = field(default_factory=list)


@dataclass
class SalesFile:
    machine: int
    ident: int
    datetime_raw: str
    blocks: list[FileBlock] = field(default_factory=list)

    def block(self, fno: int) -> FileBlock | None:
        for b in self.blocks:
            if b.fno == fno:
                return b
        return None


def _decode_field(fid: int, raw: bytes) -> RecordField:
    name, kind = _FIELD.get(fid, (f"field_{fid}", "raw"))
    if kind == "bcd":
        value: object = 0 if bcd.is_blank_bcd(raw) else bcd.bcd_to_long(raw)
    elif kind == "char":
        value = raw.decode("latin-1", errors="replace").rstrip("\x00 ")
    else:
        value = raw.hex()
    return RecordField(fid=fid, name=name, kind=kind, raw=raw, value=value)


def _parse_section_i(data: bytes, off: int) -> tuple[int, list[tuple[int, int]], int]:
    """Return (fno, schema, next_off) for a SectionI at data[off]."""
    if data[off] != MAGIC_I:
        raise SalesFileError(f"expected SectionI magic 0x69 at {off}, got 0x{data[off]:02x}")
    m_len = bcd.read_short_be(data, off + 1)
    fno = bcd.read_short_be(data, off + 3)
    m_all = data[off + 6]   # #fields for SALES files
    m_pgm = data[off + 7]   # #fields for SETTING files
    num_fields = m_all if m_all else m_pgm
    schema: list[tuple[int, int]] = []
    p = off + 10
    for _ in range(num_fields):
        if p + 3 > len(data):
            break
        fid = bcd.read_short_be(data, p)
        flen = data[p + 2]
        schema.append((fid, flen))
        p += 3
    header_len = m_len + 3  # authoritative advance per SectionI.mHeaderLength
    next_off = off + header_len
    # Guard against a bogus/short header_len that would stall or rewind us.
    if next_off <= off:
        next_off = p
    return fno, schema, next_off


def _parse_section_f(
    data: bytes, off: int, schema: list[tuple[int, int]], records_compressed: bool
) -> tuple[FileBlock, int]:
    if data[off] != MAGIC_F:
        raise SalesFileError(f"expected SectionF magic 0x66 at {off}, got 0x{data[off]:02x}")
    fno = bcd.read_short_be(data, off + 1)
    z_counter = bcd.bcd_to_hexstring(data[off + 4 : off + 6])
    rec_len = bcd.read_int_be(data, off + 6)
    start_rec = bcd.read_int_be(data, off + 10)
    end_rec = bcd.read_int_be(data, off + 14)
    p = off + SECTION_F_HEADER_LEN

    n_records = end_rec - start_rec + 1 if end_rec >= start_rec else 0
    schema_width = sum(flen for _, flen in schema)
    if schema_width and rec_len != schema_width:
        # SectionI schema and SectionF record length disagree -- trust the
        # SectionF length for slicing but keep parsing; flagged by caller/tests.
        pass

    records: list[Record] = []
    for i in range(n_records):
        if records_compressed:
            rec_bytes, p = rle.rle_uncompress_record(True, True, data, p, rec_len)
        else:
            rec_bytes = data[p : p + rec_len]
            p += rec_len
        records.append(_slice_record(start_rec + i, rec_bytes, schema))

    block = FileBlock(
        fno=fno,
        label=FNO_LABELS.get(fno, f"FNO {fno}"),
        z_counter=z_counter,
        start_rec=start_rec,
        end_rec=end_rec,
        rec_len=rec_len,
        schema=schema,
        records=records,
    )
    return block, p


def _slice_record(rec_no: int, rec_bytes: bytes, schema: list[tuple[int, int]]) -> Record:
    fields: list[RecordField] = []
    q = 0
    for fid, flen in schema:
        raw = rec_bytes[q : q + flen]
        fields.append(_decode_field(fid, raw))
        q += flen
    return Record(rec_no=rec_no, fields=fields)


def parse_sales_file(data: bytes, records_compressed: bool = True) -> SalesFile:
    """Parse a raw FileAll blob into structured movement detail.

    `records_compressed` selects whether SectionFData records are RLE-encoded
    (as suspected off BLE) or stored fixed-width. Use `parse_sales_file_auto`
    if unsure.
    """
    if len(data) < SECTION_H_LEN:
        raise SalesFileError(f"data too short for SectionH ({len(data)} bytes)")
    if data[0] != MAGIC_H:
        raise SalesFileError(f"bad SectionH magic: 0x{data[0]:02x} (expected 0x68)")

    machine = data[1]
    ident = data[2]
    datetime_raw = bcd.bcd_to_hexstring(data[5:10])

    sf = SalesFile(machine=machine, ident=ident, datetime_raw=datetime_raw)

    off = SECTION_H_LEN
    current_schema: list[tuple[int, int]] = []
    n = len(data)
    while off < n:
        magic = data[off]
        if magic == MAGIC_I:
            _, current_schema, off = _parse_section_i(data, off)
        elif magic == MAGIC_F:
            block, off = _parse_section_f(data, off, current_schema, records_compressed)
            sf.blocks.append(block)
        elif magic in (0x00, 0x1A, 0x03, 0x08):  # padding / trailing control bytes
            break
        else:
            raise SalesFileError(f"unknown section magic 0x{magic:02x} at offset {off}")
    return sf


def parse_sales_file_auto(data: bytes) -> tuple[SalesFile, bool]:
    """Try compressed then uncompressed; return (parsed, was_compressed).

    Picks whichever interpretation parses the whole stream without a section
    magic mismatch. Compressed is tried first because that's the suspected
    on-BLE encoding.
    """
    last_err: Exception | None = None
    for compressed in (True, False):
        try:
            sf = parse_sales_file(data, records_compressed=compressed)
            if sf.blocks:
                return sf, compressed
        except Exception as e:  # noqa: BLE001 - want to fall through to the other mode
            last_err = e
    if last_err:
        raise last_err
    raise SalesFileError("no data blocks found in either compression mode")
