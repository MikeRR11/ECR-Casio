"""BLE client for talking to a Casio SR-S820 (and family) cash register.

Reimplements, in Python via `bleak`, the wire protocol used by the
discontinued Android app "CASIO ECR+". See docs/protocol/ for the
reverse-engineered specification this is based on.

STATUS: reconstructed from static analysis of the decompiled app, not yet
validated against a real register. Expect to need small corrections after
the first live test — run with `logging.DEBUG` to see every raw frame.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

from .protocol import framing, jobs, responses

log = logging.getLogger("casio_ecr.ble")

SERVICE_UUID = "0179bbd0-5351-48b5-bf6d-2167639bc867"
CHARACTERISTIC_UUID = "0179bbd1-5351-48b5-bf6d-2167639bc867"

DEFAULT_JOB_TIMEOUT = 5.0
REG_INFO_TIMEOUT = 1800 * 0.05  # ~90s ceiling per addendum (waitcount*50ms), generous
XZ_SALES_BEFORE_TIMEOUT = 72000 * 0.05  # register may print a physical report first


class CasioProtocolError(Exception):
    pass


class CasioTimeoutError(CasioProtocolError):
    pass


class CasioBusyError(CasioProtocolError):
    """Register already has another BLE client connected."""


class CasioLimitedByLawError(CasioProtocolError):
    """Register refused per BLE_PACKET_TYPE_COMMUNICATION_END payload[0]==1."""


@dataclass
class _RxState:
    reassembler: framing.PacketReassembler = field(default_factory=framing.PacketReassembler)
    packets: list[framing.ParsedPacket] = field(default_factory=list)
    event: asyncio.Event = field(default_factory=asyncio.Event)


async def scan_for_register(timeout: float = 10.0) -> list[BLEDevice]:
    """Scan for BLE devices advertising the Casio VSSPP service."""
    devices = await BleakScanner.discover(timeout=timeout, service_uuids=[SERVICE_UUID])
    return list(devices)


class CasioBleClient:
    def __init__(self, address: str) -> None:
        self.address = address
        self._client: BleakClient | None = None
        self._rx = _RxState()

    async def connect(self) -> None:
        log.info("Connecting to %s", self.address)
        self._client = BleakClient(self.address)
        await self._client.connect()
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        log.info("Connected, notifications enabled")

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._write(framing.build_base_packet(framing.STX))
                await self._write(jobs.ble_disconnect_packet())
                # Give the register a brief window to react before we tear
                # down the GATT link ourselves; don't block long or raise
                # on timeout -- a clean close is best-effort, not required.
                try:
                    await asyncio.wait_for(self._rx.event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            except Exception:
                log.debug("disconnect job write failed (continuing)", exc_info=True)
            await asyncio.sleep(0.2)
            await self._client.disconnect()
        self._client = None

    async def __aenter__(self) -> "CasioBleClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    def _on_notify(self, _handle, data: bytearray) -> None:
        raw = bytes(data)
        log.debug("RX raw: %s", raw.hex(" "))
        packets = self._rx.reassembler.feed(raw)
        for p in packets:
            log.debug(
                "RX packet: type=0x%02X seq=0x%02X text=0x%02X payload=%s ok=%s",
                p.type_byte,
                p.seq,
                p.text,
                p.payload.hex(" "),
                p.ok,
            )
            self._rx.packets.append(p)
        if packets:
            self._rx.event.set()

    async def _write(self, frame: bytes) -> None:
        assert self._client is not None
        for chunk in framing.chunk_packet(frame):
            log.debug("TX chunk: %s", chunk.hex(" "))
            await self._client.write_gatt_char(CHARACTERISTIC_UUID, chunk, response=False)
            await asyncio.sleep(framing.CHUNK_PACING_SECONDS)

    async def _wait_packet(self, timeout: float) -> framing.ParsedPacket:
        deadline = time.monotonic() + timeout
        while True:
            if self._rx.packets:
                return self._rx.packets.pop(0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CasioTimeoutError("timed out waiting for register response")
            self._rx.event.clear()
            try:
                await asyncio.wait_for(self._rx.event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise CasioTimeoutError("timed out waiting for register response")

    async def _wait_control(self, expected_type: int, timeout: float) -> framing.ParsedPacket:
        pkt = await self._wait_packet(timeout)
        self._check_error(pkt)
        if pkt.type_byte != expected_type:
            log.warning(
                "expected control 0x%02X, got 0x%02X (payload=%s)",
                expected_type,
                pkt.type_byte,
                pkt.payload.hex(" "),
            )
        return pkt

    @staticmethod
    def _check_error(pkt: framing.ParsedPacket) -> None:
        if pkt.type_byte == framing.BUSY:
            raise CasioBusyError("register is busy (already connected to another client)")
        if pkt.type_byte == framing.COMM_END:
            if pkt.payload and pkt.payload[0] == 1:
                raise CasioLimitedByLawError("register refused connection (regulatory limit)")
            raise CasioProtocolError("register ended communication")

    _CONTROL_TYPES = (framing.ACK, framing.CTS, framing.NACK, framing.STX, framing.ETX, framing.EOT)

    async def _run_simple_job(self, job_packet: bytes, timeout: float = DEFAULT_JOB_TIMEOUT) -> bytes:
        """Send STX then the job packet, and reactively consume whatever
        comes back: control frames (ACK/CTS/NACK) are logged and skipped,
        the first non-control packet is treated as the start of the answer
        payload, accumulated until a TEXT flag of 0x80/0x81 marks the last
        chunk. Deliberately tolerant of exact control-byte ordering, since
        real hardware doesn't follow the naive STX-ACK-job-ACK-ETX shape
        the decompiled app's helper names suggested.
        """
        self._rx.packets.clear()
        await self._write(framing.build_base_packet(framing.STX))
        # Wait for the register's initial control response (observed to be
        # CTS 0x07 in practice, not the ACK 0xF0 the decompiled helper names
        # implied) before sending the job packet -- sending both back-to-back
        # without waiting caused the register to silently drop the job and
        # eventually close the connection (observed on real hardware).
        first = await self._wait_packet(timeout)
        self._check_error(first)
        log.debug("post-STX response: type=0x%02X (proceeding regardless)", first.type_byte)

        await self._write(job_packet)

        payload = bytearray()
        deadline = time.monotonic() + timeout
        got_any_data = False
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                if got_any_data:
                    break
                raise CasioTimeoutError("timed out waiting for register response")
            pkt = await self._wait_packet(remaining)
            self._check_error(pkt)
            if pkt.type_byte in self._CONTROL_TYPES:
                continue
            payload += pkt.payload
            got_any_data = True
            if pkt.text in (framing.TEXT_SINGLE_CHUNK, framing.TEXT_END):
                break
        # NOTE: deliberately NOT sending a trailing ETX here. Real hardware
        # testing showed the register considers the transaction complete
        # once it sends a single/last-chunk data packet (TEXT=0x81/0x80) --
        # an extra unsolicited ETX at that point is out of sequence with
        # the register's own state machine and is suspected to have
        # contributed to a firmware hang during testing. Only send ETX
        # where the per-job flow explicitly requires closing a still-open
        # data block (not yet needed by any operation implemented so far).
        return bytes(payload)

    # -- High-level operations -------------------------------------------------

    async def echo_test(self) -> bool:
        data = await self._run_simple_job(jobs.echo_back_packet())
        return data == jobs.ECHOBACK_DATA

    async def get_reg_info(self) -> responses.RegInfo:
        data = await self._run_simple_job(jobs.reg_info_packet(), timeout=REG_INFO_TIMEOUT)
        info = responses.parse_reg_info(data)
        if info is None:
            raise CasioProtocolError(f"reg info response too short ({len(data)} bytes)")
        return info

    async def get_unsent_count(self) -> int:
        data = await self._run_simple_job(jobs.unsent_data_packet())
        return responses.parse_unsent_count(data)

    async def set_datetime(self, when=None) -> None:
        await self._run_simple_job(jobs.set_datetime_packet(when))

    async def sales_confirm(self) -> responses.SalesConfirm:
        data = await self._run_simple_job(jobs.sales_confirm_packet())
        parsed = responses.parse_sales_confirm(data)
        if parsed is None:
            raise CasioProtocolError(f"sales confirm response too short ({len(data)} bytes)")
        return parsed

    async def read_setting_file(self, file_no: str, timeout: float = 20.0, idle_gap: float = 2.0) -> bytes:
        """Read a raw settings-file blob (jobs 9001 family) from the register.

        This is deliberately conservative/adaptive rather than following the
        exact dealno state table from the decompiled app (which we already
        found diverges from real hardware behavior for simple jobs): after
        sending the file-transfer request, it grants CTS after every data
        packet received and keeps collecting until either an explicit
        COMM_END/EOT-like control byte is seen, or `idle_gap` seconds pass
        with nothing new arriving (register has nothing more to send).

        Returns the raw accumulated bytes (framing already stripped, but
        NOT yet RLE-decompressed or SectionH/I/F-parsed -- see
        docs/protocol/casio_ble_protocol_spec.md section 5 for that).
        """
        self._rx.packets.clear()
        await self._write(framing.build_base_packet(framing.STX))
        first = await self._wait_packet(timeout)
        self._check_error(first)
        log.debug("post-STX response: type=0x%02X", first.type_byte)

        await self._write(jobs.deal_setting_packet(framing.COMP_NON, jobs.TRANSFER_GET_PGM, file_no))
        ack = await self._wait_packet(timeout)
        self._check_error(ack)
        log.debug("post-deal-setting response: type=0x%02X", ack.type_byte)
        # Proactively grant CTS to invite the register to start streaming --
        # observed that it ACKs the request but sends nothing until we do.
        await self._write(framing.build_base_packet(framing.CTS))

        payload = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = min(idle_gap, max(0.0, deadline - time.monotonic()))
            if remaining <= 0:
                break
            try:
                pkt = await self._wait_packet(remaining)
            except CasioTimeoutError:
                break  # idle gap elapsed with no new data -> assume done
            self._check_error(pkt)
            if pkt.type_byte in self._CONTROL_TYPES:
                continue
            payload += pkt.payload
            log.debug("settings data so far: %d bytes", len(payload))
            # Grant permission for more, in case the register is pacing us.
            await self._write(framing.build_base_packet(framing.CTS))
            if pkt.text in (framing.TEXT_SINGLE_CHUNK, framing.TEXT_END):
                break
        return bytes(payload)

    async def xz_report(self, report_type: str = "X") -> str:
        """Trigger an X (read-only) or Z (read+reset) report.

        NOTE: this only performs the trigger handshake (job 0013) and
        returns the result code. The bulk sales-data transfer that follows
        (jobs 9004/9005/9006) is not yet implemented here -- see
        docs/protocol/casio_ble_protocol_addendum.md section 1.3 for the
        remaining state machine to build once the trigger step is confirmed
        working against real hardware.
        """
        data = await self._run_simple_job(jobs.xz_start_packet(report_type))
        result = responses.parse_xz_result(data)
        if result != "00":
            raise CasioProtocolError(f"XZ start failed, result code {result!r}")
        return result
