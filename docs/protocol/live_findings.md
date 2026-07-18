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
script polls a small `.pairing_pin.txt` file (wait bumped to **180s**) so the
PIN can be dropped in the instant it's read. It also **auto-unpairs a stale
Windows bond** (`pairing.unpair_async()`) before re-pairing.

**If the register's memory-backup batteries are ever pulled (see incident
below), it forgets its bonding — Windows still thinks it's paired
(`is_paired=True`) but the register silently rejects/closes new
connections.** Fix: `pairing.unpair_async()` on the Windows side, then redo
the full pairing dance with a fresh PIN from the register's screen.

### Confirmed live re-pairing procedure (2026-07-18, SUCCESS — see `docs/pairing_log.md`)

Register side (manual pages E-105/E-106): Mode switch **PGM** → navigate to
**[Bluetooth]** → set **System Setting [ON]** → select **[Pairing with mobile]**
→ the register shows its 14-digit device code and waits. PC side then:

```
python -u pair_device.py 08:00:74:57:30:EF   # -u = unbuffered, so you SEE progress
# ... "PairingRequested event: kind=4" + "Waiting up to 180s for PIN ..."
# register now shows a fresh 6-digit PIN -> write it FAST:
printf '<pin>' > app/.pairing_pin.txt
# -> "accepted with pin=..." then "Pairing result status: 0  SUCCESS"
```

**Three gotchas that cost several failed attempts, all now understood:**

1. **Run `pair_device.py` with `python -u`.** Buffered stdout hid that a
   killed-but-still-alive first attempt was holding the pairing ceremony open.
2. **`DevicePairingResultStatus 15` (OperationAlreadyInProgress)** = another
   `pair_device.py` is still running and holding a ceremony. Kill ALL of them
   first (`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` → filter
   CommandLine `*pair_device*` → `Stop-Process -Force`), then retry. The PIN
   changes every ceremony — a PIN from a prior/timed-out attempt is stale.
3. **`DevicePairingResultStatus 19` (generic Failed)** = the PIN was accepted
   by our handler but arrived **too late** for the register's short display
   window. Fix that worked: prime the operator to send the 6 digits the
   instant they appear (don't wait to be asked), and write the file
   immediately — keep "register shows PIN → file written" to a few seconds.
4. **BUSY (`0xF2`) right after a successful pair** = the register is still
   sitting in the Bluetooth pairing menu, which holds the BLE link. Back out
   (SUBTOTAL / Cancel) and return the Mode switch to **REG** before any GATT
   op (time sync, sales-confirm, etc.), else every connect returns busy.
5. **BUSY (`0xF2`) even in REG mode** = another BLE client holds the register's
   single connection slot — most often a nearby **phone/tablet** that was
   paired before and auto-reconnected (the register allows only ONE client).
   Confirmed live 2026-07-18: turning the phone's Bluetooth OFF freed the slot
   and the PC synced the clock immediately. The GATT connect succeeds and only
   the app-level exchange returns `0xF2`, so "Connected, notifications enabled"
   followed by a busy error is the signature. Rule out other devices first.

Log every new pairing in `docs/pairing_log.md`.

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

## Confirmed working: sales-confirm (job `0014`) — dashboard data source

Tested live, works end-to-end, returns real current sales totals with the
register's own Spanish labels. Example (one unit sold, register showing a
$50,000 demo total, amounts in minor units = value×100):
```
gross  BRUTO   qty=10000 amount=5000000   (i.e. 50,000.00, qty 100.00)
net    NETO    qty=10000 amount=5000000
caid   EFEC    amount=5000000   (cash in drawer)
chid   CARGEC  amount=0         (charge)
ckid   CQEC    amount=0         (check)
crid1-4 CRID(1..4) amount=0
```
Confirms `responses.parse_sales_confirm()` layout (9 records, 22 bytes for
gross/net incl. 5-byte BCD qty, 17 bytes for the rest). This is a simple
job (STX → job → single-chunk 0xF5 reply), same proven pattern as
echo/reg-info/set-datetime. **This is the data source the PC dashboard is
built on** — it's cumulative-since-last-Z, so the PC snapshots it over time
and does daily/monthly aggregation locally. No risky bulk-transfer path
needed for basic totals.

## Dashboard built (v1)

`app/casio_ecr/web/server.py` (FastAPI) + `web/templates/dashboard.html` +
`storage/db.py` (SQLite at `app/data/ecr.db`). Run with
`python -m casio_ecr.web.server`, open http://127.0.0.1:8770. "Leer caja"
button pulls a sales-confirm snapshot over BLE and persists it; dashboard
shows current totals cards, per-day and per-month gross bar charts, a
reading-history table, and display settings (currency symbol, decimals
0/2 for COP-friendly formatting, BLE address). Verified end-to-end against
the real register. Amounts stored raw (minor units) in DB, formatted in UI.

## BUILT, NOT YET LIVE-TESTED: detailed X/Z report ("detalle de movimientos")

The sales-confirm job (0014) only returns the 9 cumulative totals. The
per-department / per-PLU / hourly / clerk / electronic-journal / fixed-totalizer
breakdown lives in the **full report-file transfer** triggered by job `0013`
(X/Z start), which the register then streams back as a CASIO physical
"FileAll" blob (phases 9004 before / 9005 main / 9006 after — see
`casio_ble_protocol_addendum.md` §1.3). As of this session the whole path is
**implemented and verified OFFLINE against synthetic fixtures, but has NOT been
run against the real register yet.**

What was added this session (all offline-tested, zero hardware touched):
- `protocol/salesfile.py` — parser for the SectionH / SectionI / SectionF
  physical file into structured movement records. SectionI is self-describing
  (declares each file's `{field-id, byte-length}` schema), so record layouts
  are read from the stream, not hard-coded. Maps FNO→report (5=Dept, 4=PLU,
  9=Hourly, 48=EJ, 1=Fixed totals, 10=Monthly, …) and FieldID→meaning
  (1=qty, 2=amount, 13=name, 15=price, …).
- `protocol/rle.py::rle_uncompress_record()` — streaming per-record RLE
  decompressor (records may arrive compressed off BLE; each expands to the
  SectionF header's fixed record length). `parse_sales_file_auto()` tries
  compressed then uncompressed and keeps whichever parses cleanly, because
  whether the live register compresses is still unconfirmed.
- `ble_client.py::receive_xz_report()` — the bulk-receive state machine:
  0013 trigger (proven simple-job shape) → grant CTS → accumulate the
  register-initiated data stream (9005), granting CTS per chunk → stop on a
  register-sent ETX/EOT or idle gap. **Deliberately never sends an unsolicited
  trailing ETX** (the past hang suspect) and stays on ONE connection for the
  whole logical transaction (that is correct here — it is not the "two jobs on
  one connection" replay bug).
- `app/tests/test_salesfile.py` — synthetic FileAll round-trip (compressed +
  uncompressed), 5 tests, all green. Run: `python app/tests/test_salesfile.py`.
- `app/pull_report.py` — **supervised live-capture harness**. Defaults to an X
  (read-only) report, saves the RAW received bytes to
  `app/data/captures/xz_X_<ts>.bin` **before** parsing, then parses offline so
  a first run yields diagnostics even if the transfer stalls. Z is gated behind
  an explicit `--z` flag (Z resets totals = day close). Re-parse offline with
  `python app/pull_report.py --parse <file.bin>` — no BLE.
- Dashboard: new "Reportes con detalle de movimientos" panel + endpoints
  `POST /api/reports/pull` (live X), `POST /api/reports/import` (offline, parse
  a captured .bin), `GET /api/reports/list`, `GET /api/reports/detail/{id}`;
  new `report_captures`/`report_lines` SQLite tables. All verified offline via
  the import endpoint; the raw blob is kept in the DB for re-parsing.

### Live-test procedure (needs owner supervision, X first)

Because this exercises the never-confirmed bulk path against business-critical
hardware, run it **only while watching the register**, and start read-only:

1. Put the register in BLE range / paired (re-pair if batteries were pulled —
   see Pairing section above).
2. `cd app && python pull_report.py X` (daily X report, read-only).
3. Watch the register: if the screen shows a stuck "Bluetooth" glyph or keys go
   dead, it hung — full cold power cycle incl. AA batteries (see incident
   section). Otherwise, inspect `app/data/captures/xz_X_*.bin` + the printed
   parse.
4. If bytes arrive but the parse is wrong, iterate `salesfile.py` OFFLINE
   against the captured .bin (`--parse`) — do NOT keep re-pulling live to debug.

### Open unknowns to resolve from the FIRST real capture

- Exact CTS/ACK pacing the register expects during 9005 (the settings-file
  path 9001/'P' stalled with a similar adaptive approach — but that path is
  app-initiated, whereas 0013→9005 is *register*-initiated, so it may behave
  differently and actually stream).
- Whether records are RLE-compressed off BLE (`parse_sales_file_auto` handles
  both; the raw capture settles it).
- `SectionH.mStrDateTime` / `SectionFHeader.mZCounter` real nibble convention.
- Record-number→totalizer-line meaning for FNO=1 (rely on the FIELD_CHAR name
  the register sends per line).

## LIVE-TESTED 2026-07-18: detailed X report — runs but does NOT stream (transport blocker)

Extensive live testing of the X-report bulk path (job `0013` → 9004/9005/9006).
**Big progress; one blocker remains that is transport-level, not protocol.**

What now works / is understood:
- **The X trigger works.** `0013` with transfer `X`, remote file `1` returns
  result code `00` reliably (register runs the report). The datetime-first
  prerequisite for the daily report (spec §3.1c) must be done in its OWN
  connection first — doing 0001+0013 in one connection hits the replay bug and
  the trigger comes back `??`. `pull_report.py` now pushes datetime on a
  separate connection, waits 2s, then triggers.
- **`X data → mobile` / `Z data → mobile` must be YES** on the register
  (PGM → [Bluetooth] → **Functions**). Default after a factory reset is NO, in
  which case the register only PRINTS the report and drops BLE (screen
  "E220 Disp. Bluetooth no conectado", ticket "Bluetooth Fin Error 5900").
  Enabling it changed behaviour: the register then stays connected briefly
  instead of disconnecting immediately.
- The register **prints the full paper report FIRST**, then is supposed to open
  its own data transfer (send STX, phase 9004). Confirmed from the decompiled
  app: phase 9004 budgets `72000×50ms` (~60 min) precisely because the register
  may be printing first, and it **sends nothing** during the wait (pure
  passive wait for the register's STX).

The remaining blocker (transport):
- After `0013`→`00`, the register sends a second `0xF5/TEXT=0x81/payload=00`
  packet, then goes silent, and **the BLE link CLOSES ~16s later** (bleak logs
  `GattSessionStatus.CLOSED`, MTU drops 60→23; once we saw raw `2b 2b 2b 41` =
  `+++A`, a Hayes-modem escape from the register's BLE-serial module). So the
  register never gets to stream the file — the connection dies during the
  print window.
- In the decompiled app, a mid-deal disconnect is treated as `ERROR_BLE_DISCONNECTED`,
  i.e. the app does NOT expect the register to drop — its happy path keeps the
  link alive through the print and receives the register-initiated STX. So the
  difference is almost certainly **BLE connection parameters / link-supervision
  timeout**: the Android GATT connection survives the ~15-30s the register is
  busy printing (unresponsive on BLE); our bleak-on-Windows connection does not
  and the idle link is torn down before the register streams.
- Sending a premature CTS after the trigger just makes the register replay its
  `0xF5/00` ack (one per CTS) — not real data. `receive_xz_report` now waits
  passively for the register's STX (correct per phase 9004) and sends an ETX to
  close the trigger transaction first (dealno-3 `writeEndTrans`, addendum §1.2).

**Next steps to actually get the stream (do NOT keep firing live triggers —
each prints paper + opens the drawer, and the register glitched twice needing a
BT restart):**
1. Best lead: control the **BLE connection parameters** (longer supervision
   timeout / specific connection interval) so the link survives the print
   window. bleak on WinRT exposes little of this; may need a different BLE
   backend or a USB BLE dongle with tunable params.
2. Or capture a real **BLE HCI snoop** of an Android phone running the original
   app against THIS register during one X pull, to read the exact connection
   params + any keepalive. (The app itself can't log in — see dead-end section —
   but the BLE connect/params happen pre-login and could be sniffed with an
   external BLE sniffer.)
3. The jadx re-decompile used for this analysis lives outside the repo; re-run
   with `jadx` (winget `Skylot.jadx`) + JDK 21 if deeper tracing is needed.

**Working fallbacks in the meantime:** sales-confirm (`0014`) gives the current
totals over BLE (dashboard "Leer caja"), and the register prints the FULL
detailed X/Z report on paper (dept/PLU/hourly/EJ) — the paper report already
has every movement line; BLE streaming would only save re-keying it.

### Round 2 findings (same day, later): root causes narrowed further

- **The pre-trigger datetime push is what destabilizes the link.** Every run
  that did connect→0001→0016-disconnect→reconnect→0013 lost the link ~16s
  after the trigger with the register's module spewing modem garbage. The ONE
  run without the datetime cycle (14:24) held a calm link ≥25s through the
  print (it only "failed" because our then-25s wait gave up early and
  disconnected mid-print → register printed "Bluetooth Fin Error 5900"). So:
  **do NOT push datetime before an X pull** (sync the clock separately, hours
  apart); `pull_report.py` now defaults to no-datetime (`--with-datetime` to
  override).
- The app subscribes with plain **notifications** (ENABLE_NOTIFICATION_VALUE,
  CCCD 0x0001) — same as us; the indication hypothesis is dead.
- A freshly-woken register module can take **>5s to answer the first STX**
  (5.4s observed); `receive_xz_report` now waits 15s for the post-STX CTS.
- **Reconnect-on-drop is implemented** (`CasioBleClient.reconnect_until` +
  disconnected callback): when the register drops mid-9004, we reconnect in
  <1s and keep waiting for its STX. Untested-to-success so far because…
- **…after ~15 connect/disconnect cycles in one day the register's BT module
  enters a thrash state**: accepts a connection then drops it ~3s later, in an
  endless ACTIVE→CLOSED loop, "device unreachable when getting services",
  "services changed" storms. The soft PGM-Bluetooth-OFF/ON or key-OFF→REG
  cycle stops fixing it at that point. Recovery: **full AC power cycle with
  the memory batteries LEFT IN** (config survives; only the electronics/BT
  module restart). Pace live attempts far more sparsely than we did.
- Next clean experiment (best condition yet): fresh AC-cycled module → ONE
  single-connection attempt: STX → wait ≤15s CTS → 0013 → '00' → ETX → passive
  wait (no writes) through the whole print → register STX → CTS-paced receive.

### Round 3 (same day): WinRT PowerOptimized — the drop IS a supervision timeout

- Windows DOES expose a connection-parameter lever bleak ignores:
  `BluetoothLEDevice.RequestPreferredConnectionParameters(PowerOptimized)`
  (wired into `CasioBleClient(prefer_power_optimized=True)`; the three presets'
  `link_timeout` = 200/400/**600**, interval 12 / 24-48 / **72-144**).
- Live result 2026-07-18 15:34: with PowerOptimized the link survived
  **19.5s** before the drop, vs ~13-16s on the bleak default. **Changing OUR
  connection parameters moved the drop time by ~4-6s** → the drop is (at least
  partly) a **BLE link supervision timeout we can influence**, NOT the register
  unilaterally closing. Big deal: it means a LONGER timeout can get past the
  print window.
- Windows caps at the PowerOptimized preset (~20s), still short of the full
  print+transition. **But Linux/BlueZ can set the supervision timeout to the
  BLE max of 32s** (`hci_le_conn_update` / raw HCI, or a conn-param update
  after connect). Given we already reach ~20s and only need a little more, the
  **live-USB Ubuntu path is now genuinely promising, not a long shot.**
- CONCLUSION: Windows is exhausted (bleak default AND the PowerOptimized WinRT
  lever). Recommended next real step: live-USB Ubuntu, re-pair, set conn
  supervision timeout to ~32s via HCI, then run the same capture. Fallback
  remains SD-card export (our salesfile.py already parses that format).

## Confirmed working: reg-info (job `0008`)

Tested live, works end-to-end, returns real parsed data:
```
RegInfo(serial_no='EY240139106940', terminal_no='0000',
        app_version='AAUXAC5126', booter_version='AAUWAA5121',
        charset_code='00', target_code='02', language_code='03')
```
Confirms `responses.parse_reg_info()`'s offsets from the addendum are correct.

**Bug found: do not reuse one BLE connection for two sequential job
operations.** Issuing a second STX+job cycle within the same GATT
connection after a first one already completed causes the register to
just replay its previous answer (identical payload bytes, new SEQ
numbers) instead of processing the new request. Always open a fresh
connection per logical operation until this is root-caused — every
`CasioBleClient` method should be used as `async with CasioBleClient(...) as
client: await client.some_op()`, one operation per `async with` block.

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
without us granting CTS, with or without a preceding successful `0008`
reg-info call in the same session, and regardless of which file number is
requested (`0032` message, `0092` all-settings). Consistently reproducible
dead end across 3+ separate live attempts. Owner reports the original
CASIO ECR+ app WAS able to push full configuration over BLE without an SD
card ever being involved, which argues against "this fundamentally
requires an SD card in the register" as the explanation (a real
possibility raised mid-session, not confirmed) — more likely there's a
missing prerequisite step or wrong parameter we haven't found yet. This is squarely the part of the protocol the
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

## CONFIRMED DEAD END: capturing traffic from the original app

Plan was to install the original CASIO ECR+ APK on an Android phone, enable
Bluetooth HCI snoop logging, drive the receipt-message-programming flow,
and capture the real settings-write packets to fix our stuck settings
transfer. **This is not viable.** The app's login/account wizard
(`Login.java` -> `CloudServiceAccountSettingsStartFragment.java` ->
`CloudServiceAccountSettingsStartDealAsyncTask.java`) is a hard gate: every
account step (new account `dealmode=4`, login `=6`, forgot-password,
email/password change) requires a successful response from Casio's cloud
server before it lets you proceed (see `onPostExecute` in
`CloudServiceAccountSettingsStartDealAsyncTask.java` — the `exresult` /
`onSuccessCloudServiceAccountSettings` path only fires on a live server
reply; there is no offline/no-account branch). Casio shut that server down
(service ended Dec 2023), so the app cannot get past login on any phone,
and the config-programming screens live *behind* login. The only BLE the
app does pre-login is the same reg-info read we already replicated. Tested
live on a Samsung SM-S721B (Android 16): app installs and pairs but cannot
log in, dead end confirmed. adb + platform-tools were installed on the PC
for this (`Google.PlatformTools` via winget) in case a future capture
opportunity arises through some other app/firmware, but the discontinued
app itself is a closed door.

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
