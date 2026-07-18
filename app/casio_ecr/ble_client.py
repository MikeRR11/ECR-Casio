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


class CasioDisconnectedError(CasioProtocolError):
    """The register dropped the BLE link mid-operation (it does this while
    printing a physical report; reconnect and keep waiting)."""


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
    def __init__(self, address: str, prefer_power_optimized: bool = False) -> None:
        self.address = address
        self._client: BleakClient | None = None
        self._rx = _RxState()
        self._disconnected = False
        self._prefer_power_optimized = prefer_power_optimized
        self._conn_param_request = None  # hold the WinRT request object alive

    def _apply_power_optimized(self) -> None:
        """Ask Windows for the PowerOptimized BLE connection parameters (longest
        LinkTimeout preset + a slower connection interval). bleak doesn't expose
        this; we reach the WinRT BluetoothLEDevice bleak connected with and hold
        the returned request object so the preference stays active. Best-effort:
        the register (peripheral) may impose its own params."""
        try:
            from winrt.windows.devices.bluetooth import BluetoothLEPreferredConnectionParameters as P
            dev = getattr(self._client._backend, "_requester", None)  # type: ignore[union-attr]
            if dev is None:
                log.warning("PowerOptimized: no WinRT device handle available on this bleak backend")
                return
            self._conn_param_request = dev.request_preferred_connection_parameters(P.power_optimized)
            log.info(
                "PowerOptimized conn params requested (link_timeout=%s, interval=%s-%s)",
                P.power_optimized.link_timeout,
                P.power_optimized.min_connection_interval,
                P.power_optimized.max_connection_interval,
            )
        except Exception as e:  # noqa: BLE001 - purely a best-effort optimization
            log.warning("PowerOptimized request failed (continuing without it): %s", e)

    def _on_disconnect(self, _client) -> None:
        log.info("BLE link dropped by register/stack")
        self._disconnected = True
        # Wake any _wait_packet() caller so it can notice the drop.
        self._rx.event.set()

    async def connect(self) -> None:
        log.info("Connecting to %s", self.address)
        self._disconnected = False
        self._client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
        await self._client.connect()
        if self._prefer_power_optimized:
            self._apply_power_optimized()
        await self._client.start_notify(CHARACTERISTIC_UUID, self._on_notify)
        log.info("Connected, notifications enabled")

    async def reconnect_until(self, deadline: float, retry_gap: float = 2.0) -> bool:
        """Keep trying to reconnect until `deadline` (time.monotonic-based).

        Used when the register drops the link while printing a report: it
        stops advertising during the print, then its BT module comes back.
        Returns True once reconnected, False if the deadline passed.
        """
        try:
            if self._client:
                await self._client.disconnect()
        except Exception:
            pass
        self._client = None
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                self._rx.reassembler = framing.PacketReassembler()  # fresh link, fresh framing
                await self.connect()
                log.info("Reconnected on attempt %d", attempt)
                return True
            except Exception as e:  # noqa: BLE001 - device likely not advertising yet
                log.debug("reconnect attempt %d failed: %s", attempt, e)
                self._client = None
                await asyncio.sleep(retry_gap)
        return False

    async def disconnect(self) -> None:
        if self._disconnected:
            self._client = None
            return
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
            if self._disconnected:
                raise CasioDisconnectedError("BLE link dropped while waiting for a packet")
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

    async def receive_xz_report(
        self,
        report_type: str = "X",
        remote_file: str = jobs.FILENO_REMOTE_DAILY,
        overall_timeout: float = 900.0,
        idle_gap: float = 6.0,
        before_timeout: float = 600.0,
        datetime_first: bool = False,  # deprecated: see NOTE below (replay bug)
    ) -> bytes:
        """Trigger an X/Z report AND receive the full sales-data file streamed
        back (the "detalle de movimientos": per-dept/PLU/hourly/EJ/totalizers).

        Flow (docs/protocol addendum section 1.3): job 0013 trigger -> the
        register initiates its own transfer (phases 9004/9005/9006). We stay on
        the SAME connection for the whole exchange (this is one logical
        transaction, not the "two jobs on one connection" replay bug), grant
        CTS to pace the register's streaming, and accumulate every data payload
        until the register signals ETX/EOT or goes idle.

        SAFETY:
        - `report_type="X"` is READ-ONLY: it reads totals without clearing them.
          Prefer X for all investigation.
        - `report_type="Z"` READS AND RESETS the register's totals (closes the
          day). Only pass Z when a real day-close is intended.
        - Never sends an unsolicited trailing ETX (implicated in the past
          firmware hang); it only ACKs a register-sent ETX.

        Returns the raw FileAll bytes (framing stripped; parse with
        `protocol.salesfile.parse_sales_file_auto`). This method is
        deliberately tolerant/adaptive and logs every packet so a first live
        run captures diagnostics even if the exact pacing needs tuning.
        """
        report_type = report_type.upper()
        self._rx.packets.clear()

        # NOTE: the original app pushes date/time (job 0001) before a DAILY
        # remote report (spec 3.1c). Doing that in THIS connection triggers the
        # known replay bug (register repeats the previous job's empty answer,
        # observed live 2026-07-18: trigger result came back '??'). If wanted,
        # do the datetime push in its OWN connection before calling this
        # method (pull_report.py does exactly that); datetime_first here must
        # stay False-equivalent and is kept only for API compatibility.

        # -- Phase 1: 0013 trigger, proven simple-job shape (STX -> wait -> job) --
        # Generous post-STX wait: a freshly-woken BT module took 5.4s to send
        # its first CTS (observed live), blowing the default 5s budget.
        await self._write(framing.build_base_packet(framing.STX))
        first = await self._wait_packet(15.0)
        self._check_error(first)
        log.debug("post-STX response: type=0x%02X", first.type_byte)
        await self._write(jobs.xz_start_packet(report_type, remote_file))

        trigger = bytearray()
        deadline = time.monotonic() + DEFAULT_JOB_TIMEOUT
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            pkt = await self._wait_packet(remaining)
            self._check_error(pkt)
            if pkt.type_byte in self._CONTROL_TYPES:
                continue
            trigger += pkt.payload
            if pkt.text in (framing.TEXT_SINGLE_CHUNK, framing.TEXT_END):
                break
        code = responses.parse_xz_result(trigger) if trigger else "??"
        log.info("XZ trigger result code: %r (report_type=%s file=%s)", code, report_type, remote_file)
        if code != "00":
            raise CasioProtocolError(f"XZ start failed, result code {code!r}")

        # Close OUR trigger transaction with ETX. The decompiled app's state
        # table sends writeEndTrans() at dealno 3 for job 0013 (addendum
        # section 1.3), and without it the register waits ~13s after its "00"
        # ack, emits modem-mode garbage ("+++A") and closes the GATT session
        # (observed live 2026-07-18) -- it needs the trigger transaction closed
        # before it opens its own data-transfer transaction (startmode=2 in
        # the app = same connection, no reconnect). NOTE: this ETX is specific
        # to the XZ flow; simple jobs must still NOT send a trailing ETX.
        await self._write(framing.build_base_packet(framing.ETX))
        log.info("XZ: trigger transaction closed with ETX, waiting for register STX")

        # -- Phase 9004: wait for the register to OPEN its own data transfer --
        # The register initiates by sending STX; we must NOT send anything yet.
        # (Sending a premature CTS on this reused link just makes the register
        # replay its trigger-ack 0xF5/0x00 -- observed live 2026-07-18 as a
        # stream of single 0x00 bytes.) CRITICAL: the register PRINTS the full
        # paper report FIRST and only then streams -- the original app budgets
        # 72000 x 50ms (~60 min!) for this phase (addendum section 1.1), and
        # giving up early + disconnecting makes the register fail with
        # "Bluetooth Fin Error 5900" / screen "E220 Disp. Bluetooth no
        # conectado" when it finally tries to send (observed live 2026-07-18).
        # If the register never opens a transfer at all, check that
        # "X data -> mobile" / "Z data -> mobile" is YES on the register
        # (PGM -> [Bluetooth] -> Functions), else it only prints.
        payload = bytearray()
        overall_deadline = time.monotonic() + overall_timeout
        before_deadline = time.monotonic() + before_timeout
        started = False
        while time.monotonic() < before_deadline:
            try:
                pkt = await self._wait_packet(min(2.0, before_deadline - time.monotonic()))
            except CasioTimeoutError:
                continue
            except CasioDisconnectedError:
                # EXPECTED mid-flow: the register drops the BLE link while it
                # prints the paper report (module resets, stops advertising),
                # then comes back and STREAMS on a fresh connection. Keep
                # reconnecting until it reappears, then resume waiting for its
                # STX. (Observed live 2026-07-18; matches the app tolerating
                # status-133 disconnects with "BleScan ReStart".)
                log.info("XZ receive: register dropped the link (printing) -- reconnecting...")
                if not await self.reconnect_until(before_deadline):
                    log.warning("XZ receive: could not reconnect before deadline")
                    break
                continue
            self._check_error(pkt)
            if pkt.type_byte == framing.STX:
                started = True
                log.info("XZ receive: register opened its data transfer (STX)")
                break
            log.debug("XZ receive: waiting for register STX, ignoring 0x%02X", pkt.type_byte)
        if not started:
            log.warning(
                "XZ receive: register never opened a data transfer (no STX in %.0fs). "
                "The report ran (result 00) but was NOT streamed -- enable "
                "'X data -> mobile' / 'Z data -> mobile' on the register "
                "(PGM -> [Bluetooth] -> Functions). 0 bytes.",
                before_timeout,
            )
            return b""

        # -- Phase 9005: CTS-paced receive until the register closes with ETX/EOT --
        await self._write(framing.build_base_packet(framing.CTS))
        while time.monotonic() < overall_deadline:
            try:
                pkt = await self._wait_packet(idle_gap)
            except CasioTimeoutError:
                log.info("XZ receive: idle gap elapsed, assuming transfer complete (%d bytes)", len(payload))
                break
            except CasioDisconnectedError:
                log.warning("XZ receive: link dropped mid-transfer, keeping %d partial bytes", len(payload))
                break
            self._check_error(pkt)
            t = pkt.type_byte
            if t == framing.STX:
                await self._write(framing.build_base_packet(framing.CTS))
                continue
            if t in (framing.ETX, framing.EOT):
                log.info("XZ receive: register sent 0x%02X (end), %d bytes total", t, len(payload))
                await self._write(framing.build_base_packet(framing.ACK))
                break
            if t in self._CONTROL_TYPES:
                continue
            payload += pkt.payload
            log.debug("XZ receive: +%d bytes (total %d, text=0x%02X)", len(pkt.payload), len(payload), pkt.text)
            await self._write(framing.build_base_packet(framing.CTS))
        return bytes(payload)
