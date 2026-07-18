"""Sync the register's clock to this PC's local time over BLE (job 0001).

Set-datetime is one of the confirmed-safe, reversible operations (the
register just updates its clock). Usage:

    python sync_time.py                      # scan + connect + set time
    python sync_time.py 08:00:74:57:30:EF    # use a known BLE address
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys

from casio_ecr.ble_client import (
    CasioBleClient,
    CasioProtocolError,
    scan_for_register,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_ADDRESS = "08:00:74:57:30:EF"


async def resolve_address(preferred: str | None) -> str | None:
    if preferred:
        return preferred
    print("Escaneando 10s por la caja (servicio Casio VSSPP)...")
    devices = await scan_for_register(timeout=10.0)
    if not devices:
        print("No se encontro la caja. Verifica que este encendida y con Bluetooth ON.")
        return None
    for d in devices:
        print(f"  encontrada: {d.address}  {d.name!r}")
    return devices[0].address


async def run(address: str) -> int:
    now = dt.datetime.now()
    print(f"Conectando a {address} para poner la hora en {now:%Y-%m-%d %H:%M:%S} ...")
    try:
        async with CasioBleClient(address) as client:
            await client.set_datetime(now)
        print("OK: la caja acepto la nueva fecha/hora (deberia verse actualizada en pantalla).")
        return 0
    except CasioProtocolError as e:
        print(f"Error de protocolo: {e}")
    except Exception as e:  # noqa: BLE001 - BLE/connection failures
        print(f"Fallo de conexion/BLE: {e}")
        print("Si dice que rechaza o cierra la conexion, el vinculo se perdio: hay que re-emparejar")
        print("(python pair_device.py <address>) y volver a correr esto.")
    return 1


async def main() -> None:
    preferred = sys.argv[1] if len(sys.argv) > 1 else None
    address = await resolve_address(preferred)
    if not address:
        sys.exit(2)
    sys.exit(await run(address))


if __name__ == "__main__":
    asyncio.run(main())
