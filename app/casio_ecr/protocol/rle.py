"""Run-length encoding used for compressed CASIO physical sales-data records.

Ported from jp.co.casio.exconnect.common.RunLengthEncoding. Two independent
escape schemes, both active for sales-data blocks (zero_rle=True, space_rle=True):

- 0xFE marks a run of 0x00 bytes (count in the following byte; count==0 means
  the 0xFE itself was a literal byte in the original data).
- 0xFD marks a run of 0x20 (space) bytes, same convention.
"""
from __future__ import annotations

ZERO_MARKER = 0xFE
SPACE_MARKER = 0xFD


def rle_compress(zero_rle: bool, space_rle: bool, data: bytes) -> bytes:
    if not data:
        return b""
    if not zero_rle and not space_rle:
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if zero_rle and b == ZERO_MARKER:
            out += bytes([ZERO_MARKER, 0x00])
            i += 1
        elif space_rle and b == SPACE_MARKER:
            out += bytes([SPACE_MARKER, 0x00])
            i += 1
        elif zero_rle and b == 0x00:
            run = 0
            while i < n and run < 255 and data[i] == 0x00:
                i += 1
                run += 1
            if run > 1:
                out += bytes([ZERO_MARKER, run])
            else:
                out.append(0x00)
        elif space_rle and b == 0x20:
            run = 0
            while i < n and run < 255 and data[i] == 0x20:
                i += 1
                run += 1
            if run > 1:
                out += bytes([SPACE_MARKER, run])
            else:
                out.append(0x20)
        else:
            out.append(b)
            i += 1
    return bytes(out)


def rle_uncompress(zero_rle: bool, space_rle: bool, data: bytes | None, out_len: int) -> bytes | None:
    if data is None:
        return None
    if not zero_rle and not space_rle:
        return data if len(data) == out_len else None
    out = bytearray(out_len)
    i, j, k = 0, 0, 0
    n = len(data)
    while j < out_len and i < n:
        b = data[i]
        if zero_rle and b == ZERO_MARKER:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            if nxt == 0x00:
                out[k] = ZERO_MARKER
                k += 1
                j += 1
            else:
                out[k : k + nxt] = bytes([0x00]) * nxt
                k += nxt
                j += nxt
            i += 1
        elif space_rle and b == SPACE_MARKER:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            if nxt == 0x00:
                out[k] = SPACE_MARKER
                k += 1
                j += 1
            else:
                out[k : k + nxt] = bytes([0x20]) * nxt
                k += nxt
                j += nxt
            i += 1
        else:
            out[k] = b
            k += 1
            j += 1
            i += 1
    return bytes(out) if j == out_len else None


def rle_uncompress_record(
    zero_rle: bool, space_rle: bool, data: bytes, offset: int, out_len: int
) -> tuple[bytes, int]:
    """Decompress exactly one ``out_len``-byte record starting at ``data[offset]``.

    Unlike ``rle_uncompress`` (which expands a whole buffer to a known total
    length), this expands a single record and reports where the *next* record
    begins, so a stream of variable-length compressed records can be walked one
    at a time. Returns ``(record_bytes, next_offset)``.

    Raises ``ValueError`` if the input ends before ``out_len`` output bytes are
    produced (truncated/corrupt stream).
    """
    if not zero_rle and not space_rle:
        end = offset + out_len
        if end > len(data):
            raise ValueError(f"uncompressed record runs past end of data ({end} > {len(data)})")
        return bytes(data[offset:end]), end

    out = bytearray()
    i, n = offset, len(data)
    while len(out) < out_len and i < n:
        b = data[i]
        if zero_rle and b == ZERO_MARKER:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            i += 1
            if nxt == 0x00:
                out.append(ZERO_MARKER)
            else:
                out += bytes([0x00]) * nxt
        elif space_rle and b == SPACE_MARKER:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            i += 1
            if nxt == 0x00:
                out.append(SPACE_MARKER)
            else:
                out += bytes([0x20]) * nxt
        else:
            out.append(b)
            i += 1
    if len(out) < out_len:
        raise ValueError(f"truncated RLE record: got {len(out)} of {out_len} bytes")
    # A well-formed per-record stream never encodes a run crossing the record
    # boundary, so overshoot shouldn't happen; truncate defensively if it does.
    return bytes(out[:out_len]), i
