"""Supervised live capture of a full X/Z sales report over BLE.

Run this ONLY while you (the owner) are watching the register -- it drives
the reverse-engineered bulk-transfer path (job 0013 -> 9004/9005/9006) that
has NOT yet been confirmed against real hardware. See
docs/protocol/live_findings.md for the firmware-hang history and safety rules.

Default is an X report, which is READ-ONLY (reads totals without clearing
them). A Z report READS AND RESETS the register's totals (day close) -- you
must pass `--z` explicitly and it will ask for confirmation.

Whatever bytes come back are saved RAW to app/data/captures/ *before* any
parsing is attempted, so a first run still yields diagnostic data even if the
transfer stalls or the parse fails. Parsing then runs offline on that file and
can be re-run without touching the register:

    python pull_report.py X                 # daily X report (read-only)
    python pull_report.py X --file 6        # time-card remote report
    python pull_report.py --parse app/data/captures/xz_X_20260718_2201.bin
    python pull_report.py Z --z             # DAY CLOSE (resets totals!)
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
from pathlib import Path

from casio_ecr.ble_client import CasioBleClient, CasioProtocolError
from casio_ecr.protocol import jobs, salesfile

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pull_report")

DEFAULT_ADDRESS = "08:00:74:57:30:EF"
CAPTURE_DIR = Path(__file__).resolve().parent / "data" / "captures"


def _summarize(sf: salesfile.SalesFile, was_compressed: bool) -> None:
    print("\n===== REPORTE PARSEADO =====")
    print(f"  compresion RLE: {'si' if was_compressed else 'no'}")
    print(f"  maquina={sf.machine} id={sf.ident} datetime_raw={sf.datetime_raw}")
    if not sf.blocks:
        print("  (sin bloques de datos)")
        return
    for b in sf.blocks:
        print(f"\n  -- {b.label} (FNO {b.fno}) - {len(b.records)} registros, rec_len={b.rec_len} --")
        for r in b.records[:40]:
            name = r.label or f"rec{r.rec_no}"
            qty = r.qty if r.qty is not None else ""
            amt = r.amount if r.amount is not None else ""
            print(f"     {name:<16} qty={qty!s:<8} amount={amt}")
        if len(b.records) > 40:
            print(f"     ... (+{len(b.records) - 40} mas)")


def parse_file(path: Path) -> None:
    data = path.read_bytes()
    print(f"Leyendo {path} ({len(data)} bytes)")
    try:
        sf, was_comp = salesfile.parse_sales_file_auto(data)
        _summarize(sf, was_comp)
    except Exception as e:  # noqa: BLE001
        print(f"\n  PARSE FALLO: {e}")
        print("  Los bytes crudos se conservan; se puede iterar el parser offline.")
        raise


async def capture(address: str, report_type: str, remote_file: str, with_datetime: bool = False) -> Path | None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = CAPTURE_DIR / f"xz_{report_type}_{stamp}.bin"

    print(f"Conectando a {address} ... (reporte {report_type}, remote file {remote_file})")
    raw = b""
    try:
        # Daily-report prerequisite (spec 3.1c): push date/time first, in its
        # OWN connection -- sharing a connection with the 0013 trigger hits the
        # replay bug (register repeats the previous answer). Then wait ~2s like
        # the original app before triggering.
        # NOTE: the app pushes datetime before a daily report, but doing a
        # connect/0016-disconnect/reconnect cycle right before the trigger
        # destabilizes the register's BT module (link dies ~16s in, module
        # spews modem garbage) -- the ONLY run where the link stayed calm
        # through the print window was one WITHOUT the datetime push
        # (2026-07-18 14:24). Sync the clock separately (sync_time.py);
        # only pass --with-datetime to replicate the app's exact sequence.
        if with_datetime and remote_file == jobs.FILENO_REMOTE_DAILY:
            # The register's BT module sometimes needs a while to start
            # advertising again after a previous session -- retry instead of
            # failing on the first "device not found".
            for attempt in range(1, 9):
                try:
                    async with CasioBleClient(address) as client:
                        await client.set_datetime()
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 8:
                        raise
                    print(f"  intento {attempt}: caja no visible aun ({e}); reintento en 4s ...")
                    await asyncio.sleep(4.0)
            print("Fecha/hora empujada (prerrequisito del reporte diario). Esperando 2s ...")
            await asyncio.sleep(2.0)
        # Main connection: retry while the register's BT module comes back up
        # (it takes ~10-30s to re-advertise after an OFF->REG cycle).
        # On Windows, request PowerOptimized conn params (longest LinkTimeout
        # preset, ~20s -- not quite enough). On Linux, set the supervision
        # timeout to 32s via debugfs BEFORE running (see app/linux/); bleak has
        # no cross-platform conn-param API.
        client = CasioBleClient(address, prefer_power_optimized=(sys.platform == "win32"))
        for attempt in range(1, 13):
            try:
                await client.connect()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 12:
                    raise
                print(f"  intento {attempt}: caja no visible aun; reintento en 4s ...")
                await asyncio.sleep(4.0)
        try:
            raw = await client.receive_xz_report(report_type=report_type, remote_file=remote_file)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
    except CasioProtocolError as e:
        print(f"  Error de protocolo: {e}")
    except Exception as e:  # noqa: BLE001 - BLE/connection failures
        print(f"  Fallo de conexion/BLE: {e}")
    finally:
        # Always persist whatever we got, even on error/partial capture.
        if raw:
            out_path.write_bytes(raw)
            print(f"\nCapturados {len(raw)} bytes crudos -> {out_path}")
        else:
            print("\nNo se recibieron datos (0 bytes). Nada que guardar.")
            return None

    try:
        parse_file(out_path)
    except Exception:
        pass
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture/parse a Casio X/Z sales report over BLE.")
    ap.add_argument("report_type", nargs="?", default="X", choices=["X", "Z", "x", "z"],
                    help="X = read-only (default); Z = read AND RESET totals")
    ap.add_argument("--address", default=DEFAULT_ADDRESS, help="register BLE address")
    ap.add_argument("--file", default=jobs.FILENO_REMOTE_DAILY,
                    help=f"remote report file digit (default {jobs.FILENO_REMOTE_DAILY}=daily, 6=time-card)")
    ap.add_argument("--z", action="store_true", help="confirm you really mean a Z (reset) report")
    ap.add_argument("--parse", metavar="PATH", help="offline: parse an already-captured .bin, no BLE")
    ap.add_argument("--with-datetime", action="store_true",
                    help="push date/time before the trigger (destabilizes the register's BT module; sync the clock with sync_time.py instead)")
    args = ap.parse_args()

    if args.parse:
        parse_file(Path(args.parse))
        return

    report_type = args.report_type.upper()
    if report_type == "Z" and not args.z:
        print("Un reporte Z RESETEA los totales de la caja (cierre de dia).")
        print("Si de verdad quieres cerrar el dia, repite con:  python pull_report.py Z --z")
        sys.exit(2)

    asyncio.run(capture(args.address, report_type, args.file, with_datetime=args.with_datetime))


if __name__ == "__main__":
    main()
