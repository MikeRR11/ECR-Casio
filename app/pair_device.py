"""Custom WinRT BLE pairing helper.

bleak's default BleakClient.pair() doesn't handle interactive pairing
kinds (PIN confirmation / display / entry) that many BLE peripherals
require. This script drives Windows.Devices.Enumeration's
DeviceInformationCustomPairing directly so we can see and respond to
whatever the register's pairing flow asks for.

Usage: python pair_device.py <BLE_ADDRESS_COLON_FORM>
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from winrt.windows.devices.bluetooth import BluetoothLEDevice
from winrt.windows.devices.enumeration import (
    DevicePairingKinds,
    DevicePairingProtectionLevel,
    DevicePairingResultStatus,
)


def addr_to_int(addr: str) -> int:
    return int(addr.replace(":", "").replace("-", ""), 16)


PIN_FILE = os.path.join(os.path.dirname(__file__), ".pairing_pin.txt")


def wait_for_pin_file(timeout: float = 180.0) -> str | None:
    """Poll PIN_FILE for a code written by a separate process/chat turn."""
    if os.path.exists(PIN_FILE):
        os.remove(PIN_FILE)
    deadline = time.monotonic() + timeout
    print(f"    Waiting up to {timeout:.0f}s for PIN to be written to {PIN_FILE} ...")
    while time.monotonic() < deadline:
        if os.path.exists(PIN_FILE):
            with open(PIN_FILE, "r") as f:
                pin = f.read().strip()
            os.remove(PIN_FILE)
            if pin:
                return pin
        time.sleep(0.2)
    return None


async def main(address: str) -> None:
    addr_int = addr_to_int(address)
    print(f"Resolving BluetoothLEDevice for {address} (0x{addr_int:012X})...")
    device = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
    if device is None:
        print("Could not resolve device. Is it advertising right now?")
        return

    info = device.device_information
    pairing = info.pairing
    print(f"Device name: {device.name!r}")
    print(f"Currently paired: {pairing.is_paired}")
    print(f"Can pair: {pairing.can_pair}")

    # If Windows still holds a stale bond (e.g. after the register's backup
    # batteries were pulled, it forgets bonding but Windows doesn't), a fresh
    # pair is refused with AlreadyPaired. Clear it first, then re-resolve.
    if pairing.is_paired:
        print("Removing stale Windows bond before re-pairing...")
        unpair_result = await pairing.unpair_async()
        print(f"  unpair status: {unpair_result.status}")
        device = await BluetoothLEDevice.from_bluetooth_address_async(addr_int)
        if device is None:
            print("Device stopped advertising after unpair. Put it back into pairing")
            print("mode and re-run.")
            return
        info = device.device_information
        pairing = info.pairing
        print(f"  now paired: {pairing.is_paired} / can pair: {pairing.can_pair}")

    custom = pairing.custom

    def on_pairing_requested(sender, args):
        kind = args.pairing_kind
        print(f"\n*** PairingRequested event: kind={kind} ***")
        if kind == DevicePairingKinds.PROVIDE_PIN:
            print("    Register wants US to supply the PIN it's showing on its screen.")
            pin = wait_for_pin_file()
            if pin is None:
                print("    -> no PIN received in time, cannot accept")
                return
            try:
                args.accept_with_pin(pin)
                print(f"    -> accepted with pin={pin!r}")
            except Exception as e:
                print(f"    -> accept_with_pin(pin) failed: {e!r}")
            return
        try:
            print(f"    Pin shown by args: {args.pin!r}")
        except Exception:
            pass
        try:
            args.accept()
            print("    -> accepted")
        except Exception as e:
            print(f"    -> accept() failed: {e!r}")

    token = custom.add_pairing_requested(on_pairing_requested)
    try:
        print("\nRequesting pairing (protection level: Encryption)...")
        kinds = (
            DevicePairingKinds.CONFIRM_ONLY
            | DevicePairingKinds.DISPLAY_PIN
            | DevicePairingKinds.PROVIDE_PIN
            | DevicePairingKinds.CONFIRM_PIN_MATCH
        )
        try:
            result = await custom.pair_async(kinds, DevicePairingProtectionLevel.DEFAULT)
        except TypeError:
            result = await custom.pair_async(kinds)
        status = result.status
        print(f"\nPairing result status: {status}")
        if status == DevicePairingResultStatus.PAIRED:
            print("SUCCESS: device is now paired.")
        else:
            print("FAILED. See status code above (winrt DevicePairingResultStatus enum).")
    finally:
        custom.remove_pairing_requested(token)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
