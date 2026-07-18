"""Manual smoke test against a real Casio SR-S820.

Usage:
    python smoke_test.py scan
    python smoke_test.py test <BLE_ADDRESS>

Before running `test`, put the register into Bluetooth pairing mode
(PGM -> [Bluetooth] -> System Setting -> ON -> Pairing with mobile),
per the SR-S820 manual.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from casio_ecr.ble_client import CasioBleClient, CasioProtocolError, scan_for_register

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def do_scan() -> None:
    print("Scanning for 10s for devices advertising the Casio VSSPP service...")
    devices = await scan_for_register(timeout=10.0)
    if not devices:
        print("No matching devices found. Make sure the register is in Bluetooth")
        print("pairing mode (PGM -> Bluetooth -> System Setting -> ON -> Pairing).")
        return
    for d in devices:
        print(f"  {d.address}  {d.name!r}")


async def do_test(address: str) -> None:
    async with CasioBleClient(address) as client:
        print("Connected. Running echo-back test (job 0010)...")
        try:
            ok = await client.echo_test()
            print(f"  echo test: {'OK' if ok else 'MISMATCH'}")
        except CasioProtocolError as e:
            print(f"  echo test FAILED: {e}")
            return

        print("Running reg-info test (job 0008)...")
        try:
            info = await client.get_reg_info()
            print(f"  serial_no     = {info.serial_no!r}")
            print(f"  terminal_no   = {info.terminal_no!r}")
            print(f"  app_version   = {info.app_version!r}")
            print(f"  booter_version= {info.booter_version!r}")
            print(f"  charset_code  = {info.charset_code!r}")
            print(f"  target_code   = {info.target_code!r}")
            print(f"  language_code = {info.language_code!r}")
        except CasioProtocolError as e:
            print(f"  reg info FAILED: {e}")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("scan", "test"):
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "scan":
        asyncio.run(do_scan())
    else:
        if len(sys.argv) < 3:
            print("usage: python smoke_test.py test <BLE_ADDRESS>")
            sys.exit(1)
        asyncio.run(do_test(sys.argv[2]))


if __name__ == "__main__":
    main()
