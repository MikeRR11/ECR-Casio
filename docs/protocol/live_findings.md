# Live hardware findings — Casio SR-S820 BLE protocol

Empirical results from testing the reconstructed protocol (see
`casio_ble_protocol_spec.md` and `casio_ble_protocol_addendum.md`, both
derived from static analysis of the decompiled CASIO ECR+ app) against a
real SR-S820. Read this file FIRST in future sessions before touching the
register again — it corrects several assumptions the static analysis got
wrong, and documents a real incident.

## Device identity

- Model: SR-S820, internal Casio model family **EY240**, regcode suffix
  `13` (full regcode `EY24013`), confirmed from the register's own pairing
  screen (advertised BLE name `EY240139106940`).
- BLE address (this specific unit): `08:00:74:57:30:EF`.
- Device name over GATT: `CASIO REGISTER` (differs from the advertised
  scan name `EY240139106940`).

## Pairing

The register requires **OS-level BLE bonding** before any GATT traffic is
accepted — `bleak`'s default `BleakClient.pair()` is insufficient because
the pairing kind requested is `ProvidePin` (WinRT `DevicePairingKinds.PROVIDE_PIN`,
value 4): **the register displays a 6-digit code on its own screen, and our
side must supply that code back**, not the other way around. The register's
own pairing-code display times out quickly (a handful of seconds), so this
needs to be fast.

Working approach (see `app/pair_device.py` in this repo): drive
`Windows.Devices.Enumeration.DeviceInformationCustomPairing` directly via
the `winrt` Python bindings, register a `PairingRequested` handler, and on
`kind == PROVIDE_PIN` call `args.accept_with_pin(pin)` (not `args.accept(pin)`
— wrong overload, raises `TypeError: Invalid parameter count`). Because the
code must be relayed from a human reading the register's screen, the
script polls a small `.pairing_pin.txt` file for up to 25s so the PIN can
be dropped in from a separate terminal/process the instant it's read.

**If the register's memory-backup batteries are ever pulled (see incident
below), it forgets its bonding — Windows still thinks it's paired
(`is_paired=True`) but the register silently rejects/closes new
connections.** Fix: `pairing.unpair_async()` on the Windows side, then redo
the full pairing dance with a fresh PIN from the register's screen.

## Confirmed working (read + simple write jobs)

Both tested live and working end-to-end:
- **Echo-back (job `0010`)**: round-trip confirmed, register echoes
  `1234567890` correctly.
- **Set date/time (job `0001`)**: register accepted and visibly updated its
  clock.

### Real wire behavior — corrections to the static-analysis spec

The addendum's per-state action table (derived from decompiled
`doInBackground`) does **not** match real hardware exactly:

1. After sending **STX** (`0x06`), the register replies with **CTS**
   (`0x07`), not **ACK** (`0xF0`) as the decompiled helper names implied.
   Proceeding regardless (not hard-failing on the mismatch) works fine.
2. Sending STX and the job packet back-to-back **without waiting** for
   that post-STX response causes the register to silently drop the job
   and eventually close the connection. **You must wait for the post-STX
   packet before writing the job packet.**
3. The actual answer to a simple job arrives as a single packet with
   **`TYPE=0xF5`** (not documented in the original control-byte table at
   all — new finding) and **`TEXT=0x81`** (single-chunk-complete marker,
   as expected). Payload is empty for jobs with no return data (e.g. set
   date/time), or contains the real answer bytes (e.g. echo payload).
4. **Do not send a trailing ETX** after receiving the complete single-chunk
   answer. The register considers the transaction closed once it sends the
   `TEXT=0x81` packet; sending an unsolicited ETX afterward is out of
   sequence with its own state machine and is the **leading suspect** for
   the firmware hang described below.
5. The working generic shape for a simple job, confirmed live, is:
   ```
   TX STX
   RX <anything, typically CTS 0x07>   -- wait for it, ignore exact type
   TX job packet
   RX TYPE=0xF5 TEXT=0x81 [payload]    -- done, no ETX needed
   ```

This is implemented in `app/casio_ecr/ble_client.py`'s `_run_simple_job()`.

## NOT working yet: settings-file transfer (jobs 9001-9007 family)

Attempted reading the message-settings file (`FILENO_SETTING_MESSAGE =
"0032"`) via the `writeDealSetting`-equivalent request
(`jobs.deal_setting_packet`, job marker `0x01` + comp + `:` + transfer +
payload, per addendum §3). Result both with and without a proactive CTS
grant after the register's ACK:

```
TX STX
RX CTS (0x07)
TX deal-setting request packet (comp='n', transfer='P', fileNo="0032:")
RX ACK (0xF0)
TX CTS (0x07)          -- tried granting permission to send
RX ACK (0xF0)           -- register just ACKs our CTS, then goes silent
(idle timeout, 0 bytes received)
```

The register acknowledges the request but never streams any data, with or
without us granting CTS. This is squarely the part of the protocol the
static analysis flagged as lowest-confidence (`SendReceiveDataDealResponseTask`
partially undecompiled, `writeDealSetting`'s record-range handling looked
buggy). **Do not keep guessing live against production hardware** — next
time, either:
- capture a real BLE HCI snoop log from an Android phone still running the
  original CASIO ECR+ app (if one can be found) against this exact
  register, or
- do another round of deeper static analysis (smali-level tracing of
  `analyzeData()`'s dealno transitions specifically for the 9001-9007
  family, which the addendum flagged as medium-confidence at best), or
- accept that settings **read/write over BLE stays out of scope** and rely
  on the physical keypad + an SD card with the official Casio "ECR Setting
  Tool" (a legitimate PC app — see below) instead.

## Incident: firmware hang requiring full reset

During early testing (before the fixes above were in place — trailing ETX
bug + no post-STX wait + rapid reconnect cycling), the register's screen
got stuck showing a large "Bluetooth" glyph across the whole UI and became
unresponsive to physical keys. **This was a firmware-level hang, not
physical damage.**

Recovery required a **full cold power cycle**: unplug AC power **and**
remove the 2x AA memory-backup batteries (the SR-S820 keeps RAM alive
across power loss via these batteries per the printed manual — just
unplugging AC is not enough to force a real reset), wait ~1 minute, restock
batteries, power back on.

**This wiped all configuration** (no SD card was in use, so nothing had
been backed up) — the register came back to a factory-fresh state asking
for language/date/tax setup again. The owner does not currently have an SD
card for this register; getting one (any SD ≤2GB or SDHC 2-32GB) would let
`BackupSD`/`RestoreSD` (native register feature, no reverse engineering
needed) provide a safety net before any further BLE experimentation.

## Safe alternative for configuration: official Casio "ECR Setting Tool"

The printed manual (`docs/` local copy, or the online manual at
`https://www.manualslib.com/manual/3138781/Casio-Sr-S820.html`, "Complete
Manual" variant) states POP/logo images must be prepared on an SD card
"using 'ECR Setting Tool' of a PC" — this is a **separate, legitimate Casio
Windows desktop app** (distinct from the discontinued CASIO ECR+ mobile
app), reportedly supporting department/PLU/tax/clerk setup with CSV
import/export, that exchanges data with the register via SD card rather
than Bluetooth. Found references to versions 1.00/1.20 via web search
(`software.informer.com` mirrors — **do not download from third-party
aggregators**; only use it if/when a download link direct from
`casio.com`/`casio4business.com`/`support.casio.com` is confirmed). This is
the recommended path for bulk configuration and logo/image loading once an
SD card is available, in preference to further BLE reverse engineering.

## Practical safety rules learned for any future live BLE session

1. Never send an unsolicited ETX after a complete single-chunk response.
2. Always wait for the register's post-STX response before sending the
   job packet — never fire both back-to-back.
3. Don't rapid-cycle connect/disconnect — pace retries by several seconds.
4. Treat the settings-file (9001-9007) family as **not implemented /
   unsafe** until re-validated through better means than live guessing.
5. Before any further write-path experiments, get an SD card and take a
   native `BackupSD` first, so a bad experiment is recoverable without a
   full factory reset.
