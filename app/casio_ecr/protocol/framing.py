"""Low-level packet framing for the Casio ECR BLE protocol.

Reverse-engineered from the discontinued "CASIO ECR+" Android app
(package jp.co.casio.exconnect). See docs/protocol/ for the full spec.

Wire format of a logical packet:

    [LEN_LO][LEN_HI][TYPE][SEQ][TEXT][...payload...][LRC]

- LEN_LO/LEN_HI: little-endian 16-bit length of everything after the
  length header (TYPE+SEQ+TEXT+payload+LRC), i.e. total_frame_len - 2.
- LRC: XOR of every preceding byte in the frame (length header included).

Logical packets are chunked into <=57-byte writes on the BLE characteristic,
paced ~50ms apart. Reassembly on receive is driven purely by the length
header, independent of BLE-level chunk boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass

WRITE_CHUNK_SIZE = 57
CHUNK_PACING_SECONDS = 0.05

# Control byte (TYPE field) values
STX = 0x06  # start transmission
ETX = 0x03  # end transmission
EOT = 0x08  # end of transaction
CTS = 0x07  # clear-to-send / send permission
ACK = 0xF0  # acknowledge
NACK = 0xF1  # checksum error / negative-ack
BUSY = 0xF2  # register busy (in use by another client)
COMM_END = 0xF6  # register-initiated abort

# TEXT byte values (continuation flag)
TEXT_SINGLE_CHUNK = 0x81  # start+end (whole message in one chunk)
TEXT_START = 0x01  # first of several chunks
TEXT_END = 0x80  # last of several chunks
TEXT_MIDDLE = 0x00  # middle chunk
TEXT_DEFAULT = 0x81

JOB_MARKER = 0x01
JOB_MARKER_CONTINUATION = 0x02

COMP_ON = ord("c")  # 0x63
COMP_NON = ord("n")  # 0x6E


def calc_lrc(data: bytes) -> int:
    """XOR checksum of all bytes."""
    x = 0
    for b in data:
        x ^= b
    return x & 0xFF


def _length_header(total_frame_len: int) -> bytes:
    i = total_frame_len - 2
    return bytes([i & 0xFF, (i >> 8) & 0xFF])


def build_base_packet(control: int, text: int = 0x00) -> bytes:
    """5-byte control frame body + LRC = 6-byte frame.

    Used for STX / ETX / EOT / CTS / NACK / plain ACK.
    Body: [0x04][0x00][control][0x00][text]
    """
    body = bytes([0x04, 0x00, control & 0xFF, 0x00, text & 0xFF])
    return body + bytes([calc_lrc(body)])


def build_ack_cts() -> bytes:
    """ACK immediately followed by CTS, concatenated in one payload."""
    return build_base_packet(ACK) + build_base_packet(CTS)


def build_job_packet(job_code: str, data: bytes | None, comp: int = COMP_NON) -> bytes:
    """Build a job-command packet.

    Payload: [JOB_MARKER=0x01][0x00][0x81][comp][':' 'J'][4-digit job code][':'][data...]
    """
    assert len(job_code) == 4 and job_code.isdigit(), "job_code must be 4 ASCII digits"
    body = bytearray()
    body += bytes([JOB_MARKER, 0x00, TEXT_SINGLE_CHUNK, comp & 0xFF])
    body += b":J"
    body += job_code.encode("ascii")
    body += b":"
    if data:
        body += data
    total_len = len(body) + 2 + 1  # length header + LRC
    frame = _length_header(total_len) + bytes(body)
    frame += bytes([calc_lrc(frame)])
    return frame


def build_job_packet2(b: int, b2: int, data: bytes) -> bytes:
    """[LEN][0x01][0x00][0x81][b][':'][b2][data][LRC] — used for writeDealSetting."""
    body = bytearray()
    body += bytes([JOB_MARKER, 0x00, TEXT_SINGLE_CHUNK, b & 0xFF, ord(":"), b2 & 0xFF])
    body += data
    total_len = len(body) + 2 + 1
    frame = _length_header(total_len) + bytes(body)
    frame += bytes([calc_lrc(frame)])
    return frame


def build_job_packet3(seq: int, text: int, data: bytes) -> bytes:
    """[LEN][0x02][seq][text][data][LRC] — continuation packet for chunked sends."""
    body = bytearray()
    body += bytes([JOB_MARKER_CONTINUATION, seq & 0xFF, text & 0xFF])
    body += data
    total_len = len(body) + 2 + 1
    frame = _length_header(total_len) + bytes(body)
    frame += bytes([calc_lrc(frame)])
    return frame


def chunk_packet(frame: bytes, size: int = WRITE_CHUNK_SIZE) -> list[bytes]:
    return [frame[i : i + size] for i in range(0, len(frame), size)]


@dataclass
class ParsedPacket:
    type_byte: int
    seq: int
    text: int
    payload: bytes
    ok: bool  # LRC matched


class PacketReassembler:
    """Mirrors analyzeData()'s SEQ_LEN1 -> SEQ_LEN2 -> SEQ_DATA -> SEQ_SUM state
    machine to reconstruct full logical packets from BLE notification bytes,
    which may arrive in arbitrary chunk sizes independent of WRITE_CHUNK_SIZE.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._buf = bytearray()
        self._packet_len: int | None = None

    def feed(self, chunk: bytes) -> list[ParsedPacket]:
        """Feed raw bytes; returns any fully-reassembled packets found."""
        out: list[ParsedPacket] = []
        self._buf += chunk
        while True:
            if self._packet_len is None:
                if len(self._buf) < 2:
                    break
                lo, hi = self._buf[0], self._buf[1]
                self._packet_len = lo | ((hi << 8) & 0xFF00)
            total_needed = 2 + self._packet_len  # header + body(incl LRC)
            if len(self._buf) < total_needed:
                break
            frame = bytes(self._buf[:total_needed])
            del self._buf[:total_needed]
            self._packet_len = None

            body_and_lrc = frame[2:]
            lrc_expected = calc_lrc(frame[:-1])
            lrc_actual = body_and_lrc[-1]
            ok = lrc_expected == lrc_actual
            payload_area = body_and_lrc[:-1]
            type_byte = payload_area[0] if len(payload_area) > 0 else 0
            seq = payload_area[1] if len(payload_area) > 1 else 0
            text = payload_area[2] if len(payload_area) > 2 else 0
            payload = payload_area[3:] if len(payload_area) > 3 else b""
            out.append(ParsedPacket(type_byte, seq, text, payload, ok))
        return out
