# Casio ECR+ (SR-S820) BLE Protocol — Addendum

This addendum supplements `casio_ble_protocol_spec.md`. It resolves the two biggest
open items from that spec (§7 items 1 and 5) using a re-decompile of
`BluetoothRegDataDeal.java` with jadx `--show-bad-code`, plus full reads of
`RunLengthEncoding.java` and `RegSetType.java`/`RegConnectInfo.java`.

**Confidence key** used throughout: 🟢 clean/reliable (straight-line code, no
decompiler warning affecting the read), 🟡 medium (surrounded by
warned/duplicated regions but the specific fragment quoted is unambiguous),
🔴 low (the decompiler explicitly flagged this region as incorrect and the
control flow could not be fully trusted).

Source for §1–§2: `decompiled_smali/sources/jp/co/casio/exconnect/connectlibrary/BluetoothRegDataDeal.java`
(line numbers are from THIS re-decompiled tree, not the original spec's line numbers).

---

## 0. Overall verdict on `SendReceiveDataDealResponseTask`

`--show-bad-code` produces syntactically complete Java for both `doInBackground()`
(lines 1889–2298) and `onPostExecute()` (lines 2316–2664), but jadx still emits
`/* JADX WARN: Code decompiled incorrectly, please refer to instructions dump. */`
on both methods, plus several `Removed duplicated region` / `Can't fix incorrect
switch cases order` warnings. In practice:

- The **per-state action table** (which `write*()`/helper method fires for a given
  `(jobMode, mDealno)` pair) reads as consistent, non-contradictory, straight-line
  `if/else`/`switch` code — 🟢 high confidence.
- The **outer loop's exact termination/retry mechanics** (how `mDealno` progresses
  past state 11, exactly how many total round-trips occur, and some deeply nested
  `catch`-block fallthrough in `onPostExecute`) show real decompiler artifacts
  (unreachable-looking `num = null` after the loop, duplicate `if` bodies) — 🔴 low
  confidence on the *exact* iteration/exit bytecode, though the *general* meaning
  (poll every 50 ms, timeout at `i5`, bump `mDealno` on response) is 🟡 medium-to-high
  confidence because it's corroborated by the receive-side `analyzeData()` state
  machine already documented in the main spec (§3.2, §7 item 1 reference).
- **`onPostExecute()`'s response-field parsing** for job `0008` (reg info) and the
  offset math for `0009`/`0013`/`0014` is clean straight-line code on the happy path
  (🟢), even though the surrounding `try/catch` scaffolding is garbled (🔴 for the
  exact exception-recovery paths, which don't matter for a from-scratch Python
  client anyway).

**Bottom line: the single biggest spec gap is now substantially closed.** You can
build a Python state machine directly from the tables below with reasonable
confidence; treat the *exact* number of "idle/poll" iterations and any
never-taken exception branches as implementation-detail noise, not protocol.

---

## 1. `doInBackground()` — the per-job send loop

### 1.1 Mechanics (🟢 clean)

- On task creation, `jobMode` is one of: `"0001"`, `"0009"`, `"0010"`,
  `JOB_COMMAND_REG_INFO_GET`("0008"), `JOB_COMMAND_XZ_START`("0013"),
  `JOB_COMMAND_SALES_CONFIRM`("0014"), `JOB_COMMAND_BLE_DISCONNECT`("0016"),
  or one of the internal `DEAL_COMMAND_*` phase strings `"9001"`–`"9007"`.
- `DEALMAXCOUNT = 11` — this is both the progress-dialog max and (per the
  `mDealno <= i3` check) the nominal highest `mDealno` state number.
- Per-job **base timeout** `i5` (in units the code calls `i4`, used later as the
  `waitcount` ceiling, effectively "number of 50ms polls before ERROR_BLE_TIMEOUT"):
  - `DEAL_COMMAND_RECEIVE_SALES_BEFORE` ("9004"): **72000** (huge — this state
    just waits for the register to *start* transmitting sales data after the XZ
    trigger; register may be printing a physical report first)
  - `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` ("9007") **and** `mDealno==12`:
    **4000**
  - `JOB_COMMAND_REG_INFO_GET` ("0008"): **1800**
  - all other jobs: `ScribeConfig.DEFAULT_SEND_INTERVAL_SECONDS` — a Firebase
    Crashlytics constant reused here as a magic number; its value wasn't resolved
    in this pass, but by convention this is commonly `1000`. 🟡 **flag as
    unconfirmed** — if using this as a wait budget, verify empirically (start with
    a generous ~3–5s default per non-XZ job and see if that's ever hit).
- Every iteration of the outer `while(true)` that doesn't act sleeps **50 ms**
  (`Thread.sleep(50L)`) and increments a `waitcount`. If `waitcount > <job's timeout
  budget>`, sets `errorno = ERROR_BLE_TIMEOUT` and aborts (returns -1). This is the
  literal implementation of the "3-second inactivity timeout" concept in the main
  spec §3.2 item 8 — except the real budget is **per-job and per-state**, not a flat
  3000 ms (the flat-3s figure in the main spec came from the separate `TimeOut`
  AsyncTask, which is a different, lower-level watchdog — see §1.4 below).
- State variable `mDealno` starts at 1 and is driven forward either by an explicit
  write (which also sets `mDealnoBak = mDealno` to mark "already acted on this
  state") or, once a response of the expected type is observed on the receive side,
  by direct `mDealno++`/jump-to-`dealnonextcount_noN` assignments.
- `mDealnoBak` mirrors "the last mDealno we took an action for" — the `mDealno !=
  mDealnoBak` check is how the loop distinguishes "just entered a new state, do the
  action" from "still waiting for a response in the current state."

### 1.2 Per-state action table (🟢, from the `switch (mDealno)` in `doInBackground`, lines 1917–2178)

Each row = "when in this `mDealno`, for this `jobMode`, do this." Jobs not listed
for a given state fall into that state's `else` branch, described in the "default
(unlisted jobs)" row.

| `mDealno` | Job condition | Action | Notes |
|---|---|---|---|
| 1 | `DEAL_COMMAND_RECEIVE_SALES_MAIN` ("9005") | `writeCTS()`; `mCTSCount=0` | starts bulk-sales streaming phase with a CTS grant |
| 1 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` ("9007") | if `unsentfiledealno > 1`: `writeACK()`; else: no-op (just advances) | subsequent unsent-file iterations start with ACK instead of STX (session/handshake already open) |
| 1 | *default (all others incl. "0001","0008","0009","0010","0013","0014","0016","9001"-"9003")* | `writeStartTrans()` [STX 0x06]; `mCTSCount=0`; `mSendDealSettingResult=0` | **every simple job starts with STX** |
| 2 | `"0001"` | `writeJobDateTime(inDate, inTime)` | date/time-set job packet |
| 2 | `"0010"` | `writeJobEcho()` | echo job packet, data=`"1234567890"` |
| 2 | `JOB_COMMAND_REG_INFO_GET` ("0008") | `writeJobRegInfo()` | reg-info job packet |
| 2 | `DEAL_COMMAND_RECEIVE_SETTING_START`("9001") / `DEAL_COMMAND_SEND_SETTING_START`("9002") / `DEAL_COMMAND_RECEIVE_SALES_START`("9003") | `writeDealSetting(mComp, mTransfer, mFileNo, mStartRecordNo, mEndRecordNo)` | setting/sales-file-transfer request packet (see §3 below) |
| 2 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`("9005") | `writeACK()`; `mCTSCount=0` | |
| 2 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN`("9007") | `writeCTS()`; `mCTSCount=0` | |
| 2 | `JOB_COMMAND_XZ_START`("0013") | `sendDealRemote(mTransfer, mFileNo)` | the X/Z trigger packet (transfer char 'X'/'Z' + remote file digit) |
| 2 | `"0009"` | `writeJobUnSentData()` | |
| 2 | `JOB_COMMAND_SALES_CONFIRM`("0014") | `writeJobSalesConfirm()` | |
| 2 | `JOB_COMMAND_BLE_DISCONNECT`("0016") | `writeJobBleDisconnect()` | |
| 2 | *default* | no write, `mDealno++` | |
| 3 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`("9005") | `writeACK()`; jump target = `dealnonextcount_no3` (default 3) | |
| 3 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN`("9007") | `writeACK()` | |
| 3 | *default* | `writeEndTrans()` [ETX 0x03] | **simple jobs send ETX at state 3**, i.e. right after the job packet — meaning for jobs like 0010/0008/0009/0013/0014/0001/0016, the sequence is essentially STX → job-packet → ETX, then wait for the response payload |
| 4 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN`("9007") | `writeACK()`; jump target = `dealnonextcount_no4` (default 4) | |
| 4 | *default* | no write, `mDealno++` | |
| 5 | `DEAL_COMMAND_RECEIVE_SETTING_START`/`DEAL_COMMAND_RECEIVE_SALES_START` | `writeCTS()`; if `mCTSCount==0`: `openFile()`; `mCTSCount++` | opens the destination file on the **first** CTS of a multi-chunk receive |
| 5 | `DEAL_COMMAND_SEND_SETTING_START`("9002") | `sendDealSetting(mSendData, mCTSCount)` (result stored in `mSendDealSettingResult`, success = `>=0`); `noFileMake()`; `mCTSCount++` | outbound chunked settings upload |
| 5 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`/`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | `writeCTS()`; jump target=`dealnonextcount_no5` (default 5); `mCTSCount=0`→then re-checked (`openFile()` if 0); `mCTSCount++` | |
| 5 | *default* | no write, `mDealno++`; `noFileMake()` | |
| 6 | `DEAL_COMMAND_RECEIVE_SETTING_START`/`DEAL_COMMAND_RECEIVE_SALES_START` | `writeACK()` | |
| 6 | `DEAL_COMMAND_SEND_SETTING_START` | `writeEOT()` [0x08] | ends the settings-upload chunk loop |
| 6 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`/`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | no write, state held | |
| 6 | *default* | no write, `mDealno++` | |
| 7 | `DEAL_COMMAND_RECEIVE_SETTING_START`/`DEAL_COMMAND_RECEIVE_SALES_START` | `writeACK()` | |
| 7 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`/`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | `writeACK()` | |
| 7 | *default* | no write, `mDealno++` | |
| 8 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`/`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | `writeACK()` | |
| 8 | *default* | `writeEndTrans()` [ETX] | (second ETX opportunity, for jobs that reach this far — mostly the 9001/9002/9003 family) |
| 9 | `DEAL_COMMAND_RECEIVE_SALES_MAIN`/`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | `writeACK()`; jump target=`dealnonextcount_no9` (default 9) | |
| 9 | *default* | no write, `mDealno++` | |
| 10 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | `writeCTS()`; `mCTSCount=0` | starts the *next* unsent file's CTS grant |
| 10 | *default* | no write, `mDealno++` | |
| 11 | `DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN` | if `unsentfiledealno >= unsentfilecount`: `writeACK()`, jump=`dealnonextcount_no11`(11); else: no write, `mDealno++` (loop back for next file) | terminal check for the unsent-files iteration loop |
| 11 | *default* | no write, `mDealno++` | |
| default (>11) | any | no write, "true" (🔴 loop-exit mechanics unclear here — see §0) | |

**`dealnonextcount_no3/4/5/9/11`** default to `3/4/5/9/11` respectively (i.e.
"stay at this state") but are **dynamically overwritten from inside `analyzeData()`**
(the receive-side parser) at lines ~918–931 of the same file, e.g.:
```java
this.mDealno = this.dealnonextcount_no9 + 1;   // when more chunks are still coming
...
this.mDealno = this.dealnonextcount_no5 + 1;   // when a CTS-paced data block finished
```
This is the actual mechanism for **multi-chunk pacing**: the receive parser
recognizes "there's more data in this block" vs. "block done" from the incoming
packet's SEQ/TEXT/length fields and either loops the sender back to re-issue CTS
(more data expected) or advances it forward (block complete, move to next phase).
🟡 medium confidence on the *exact* trigger conditions inside `analyzeData()` (that
method wasn't re-read in this pass — the main spec's summary of it stands), but 🟢
high confidence that this dynamic re-targeting is how CTS-paced flow control works.

### 1.3 Derived job sequences

Putting the table together, the concrete wire sequence per job is:

**`0010` (Echo-back):**
1. `writeStartTrans()` — STX
2. *(wait for ACK)*
3. `writeJobEcho()` — job packet `0010` data=`"1234567890"`
4. *(wait for ACK)*
5. `writeEndTrans()` — ETX
6. *(wait for register's echoed data payload — arrives async via `analyzeData()`/`onCharacteristicChanged`, captured into `mReceivedData`)*
7. states 4–11 fall through with no further writes; task completes, `onPostExecute()` fires and compares `mReceivedData` to `"1234567890"`.

**`0008` (Reg info get):** identical shape to 0010 — STX → `writeJobRegInfo()` → ETX → wait for payload → parse (see §2.1).

**`0001` (Set date/time):** STX → `writeJobDateTime()` → ETX → wait for ACK/response → done. No payload parsing in `onPostExecute` for this job (spec's §3.1c already covers triggering conditions).

**`0009` (Unsent data count):** STX → `writeJobUnSentData()` → ETX → wait for payload → parse 4-byte little-endian count (§2.2).

**`0013` (XZ start):** STX → `sendDealRemote(transferByte, fileNoStr)` → ETX → wait for payload → parse 1-byte BCD result code (§2.3), must be `"00"`.

**`0014` (Sales confirm):** STX → `writeJobSalesConfirm()` → ETX → wait for payload → parse into `SalesConfirmDef` (§2.4). Confirms main spec §3.1(d).2's note that 0014 and 0013 share the `sendRequestXZ` dispatch entry point but are genuinely separate job codes at the wire level.

**`0016` (BLE disconnect):** STX → `writeJobBleDisconnect()` → ETX → (register presumably ACKs, then app tears down GATT).

**`9001`/`9002`/`9003` (setting/sales-file transfer setup — `RECEIVE_SETTING_START`/`SEND_SETTING_START`/`RECEIVE_SALES_START`):**
1. `writeStartTrans()` — STX (dealno1, default branch)
2. `writeDealSetting(comp, transfer, fileNo, startRec, endRec)` (dealno2)
3. `writeEndTrans()` (dealno3, default branch — wait, actually for jobMode="9001"/"9003" the dealno3 explicit branches aren't listed, so they DO fall to default `writeEndTrans()`; "9002" (`SEND_SETTING_START`) also falls to default at dealno3)

   *(Correction/nuance: dealno3's explicit cases only special-case `RECEIVE_SALES_MAIN`/`RECEIVE_UNSENT_SALES_MAIN`; 9001/9002/9003 all take the default `writeEndTrans()` here.)*
4. dealno4: default, no write, advance
5. dealno5: for `9001`/`9003` → `writeCTS()` + `openFile()` on first pass; for `9002` → `sendDealSetting()` chunk loop (chunked upload, see §3)
6. dealno6: for `9001`/`9003` → `writeACK()`; for `9002` → `writeEOT()`
7. dealno7: for `9001`/`9003` → `writeACK()`; for `9002` → default advance
8. dealno8 onward: default `writeEndTrans()`/advance until task completion.

This matches the main spec's inference that 9001–9007 reuse the low-level
STX/CTS/ACK/ETX/EOT primitives rather than sending distinct 4-digit job codes —
now confirmed directly rather than inferred.

**`9004`/`9005`/`9006` (bulk sales transfer before/main/after):** these three
`jobMode` values are handled specially — note line 1916's condition explicitly
**excludes** `DEAL_COMMAND_RECEIVE_SALES_BEFORE` and `DEAL_COMMAND_RECEIVE_SALES_AFTER`
from the "act on state change" branch entirely; those two phases are driven
**purely from the receive side** (the `else` branch at line 2189+, which watches
`mReceivedDataTYPE` directly):
- `"9004"` (BEFORE): waits for `mReceivedDataTYPE == 6` (STX from the register,
  i.e. the register-initiated "I'm about to send sales data" transaction-start
  packet) → sets `receivesalestranstart=1`, advances.
- `"9005"` (MAIN): the one row in the table above that starts with `writeCTS()`
  at dealno1 (not `writeStartTrans()`!) — because the register already opened the
  transaction in phase "9004". This is the actual bulk-chunk receive loop, CTS-paced
  via the `dealnonextcount_no*` jump targets.
- `"9006"` (AFTER): waits for `mReceivedDataTYPE == 3` (ETX from the register) →
  for this jobMode, dealno3's special branch sends `writeACK()` and `writeCTS()`
  wait pairing to close out, then `receivedsalestransfer = getTransfer()` reads back
  the echoed transfer type char.

🟡 medium confidence on this last group's exact byte sequence (it's built from the
`else`/response-driven branch at lines 2189–2292, which is one of the sections with
`Removed duplicated region` warnings) — but the *shape* (9004 waits passively for
register-STX, 9005 does the CTS-paced bulk receive, 9006 waits passively for
register-ETX then ACKs) is corroborated by the main spec's independent
orchestration-layer analysis (`RegReceiveSalesData.java`) and is consistent across
both readings.

### 1.4 `TimeOut` AsyncTask (context, lines ~1536–1589 per main spec numbering)

Not re-examined in this pass beyond what the main spec already states — the 3-second
watchdog is a **separate, lower-level mechanism** from the per-job waitcount timeouts
described in §1.1 above. Treat both as real: the per-job timeout (§1.1, tens of
seconds to over a minute depending on job) governs the *whole exchange*, while the
3s `TimeOut` watchdog appears to guard against a stall *mid-packet* at the byte-stream
reassembly level (`analyzeData()`), independent of which high-level job is running.

---

## 2. `onPostExecute()` — response field offsets (🟢 clean on the happy path)

All offsets are into `mReceivedData.array()[0 .. mReceivedDataLength)`, i.e. the
**payload portion of the logical packet already reassembled** by `analyzeData()`
(framing/LRC/length bytes already stripped).

### 2.1 Job `0008` (`JOB_COMMAND_REG_INFO_GET`) — requires `mReceivedDataLength >= 40`

| Offset | Length | Field | Decode |
|---|---|---|---|
| 0 | 14 | `regserialnostr` | UTF-8 string |
| 14 | 2 | `termilalnostr` | `BinaryCodedDecimalTool.convertBCDtoString()` (hex-format each byte, per main spec §5.6 caveat) |
| 16 | 10 | `regaplverstr` | UTF-8 string |
| 26 | 10 | `regbooterverstr` | UTF-8 string |
| 36 | 1 | *(read into a byte array but never assigned to any field)* | — |
| 37 | 1 | `charactorsetcodestr` | `String.format("%02d", (int) byte[0])` — **note: this formats the raw signed byte value as a decimal integer, not hex** |
| 38 | 1 | `targetcodestr` | same `%02d` format |
| 39 | 1 | `languagecodestr` | same `%02d` format |

Additionally, `unsentdatastr` is **hard-coded to `"00"`** in this code path — it is
**not parsed from the response at all** despite the main spec's §3.1(b) assumption
that it comes from the 0008 payload. (This may be a known-dead/legacy field, or the
"real" unsent-data value is only ever obtained via job `0009`, which is consistent
with job `0009` existing as `JOB_COMMAND_UNSENT_DATA_GET`.) 🟢 high confidence — this
is unambiguous straight-line code.

The offset-36-byte read exists in the code (`Arrays.copyOfRange(...,36,37)`) but its
result is discarded (assigned to nothing) — likely a reserved/padding byte in the
real protocol, or a field the Android app simply doesn't surface. 🟡 flag for
live-capture confirmation if you need that byte for something.

### 2.2 Job `0009` (`JOB_COMMAND_UNSENT_DATA_GET`)

```java
byte[] b = Arrays.copyOfRange(mReceivedData.array(), 0, 4);
long unsentfilecount = Long.parseLong(
    String.format("%02X", b[3]) + String.format("%02X", b[2]) +
    String.format("%02X", b[1]) + String.format("%02X", b[0]), 16);
```
i.e. **`unsentfilecount` = little-endian uint32 at offset 0** (bytes reversed before
hex-parsing == byte 0 is the least-significant byte). Python: `int.from_bytes(data[0:4], "little")`. 🟢 high confidence.

### 2.3 Job `0013` (`JOB_COMMAND_XZ_START`)

```java
xzstartresultstr = BinaryCodedDecimalTool.convertBCDtoString(
    Arrays.copyOfRange(mReceivedData.array(), 0, 1));
```
**1 raw byte at offset 0**, hex-formatted to a 2-character string (e.g. byte `0x00`
→ `"00"`). This confirms the main spec's assumption that the "must equal `00`"
result code is 2 ASCII characters — but clarifies it's derived from a **single
byte** via hex-formatting, not 2 literal ASCII-digit bytes on the wire. 🟢 high
confidence.

### 2.4 Job `0014` (`JOB_COMMAND_SALES_CONFIRM`) — `SalesConfirmDef`

Parsed in a 9-iteration loop over `mReceivedData`, offset accumulating as it goes
(no fixed per-field table — must be walked sequentially):

```
offset = 0
for i in range(9):
    charactor = data[offset : offset+12]           # 12-byte name/label, UTF-8
    offset += 12
    if i < 2:                                       # GROSS (i=0), NET (i=1) only
        qty_bytes = data[offset : offset+5]          # 5-byte BCD quantity
        offset += 5
        qty = 0 if all(b < 0 for b in qty_bytes) else int(bcd_to_string(qty_bytes))
    else:
        qty = 0   # (not read for i>=2 — quantity only exists for gross/net)
    amt_bytes = data[offset : offset+5]              # 5-byte BCD amount
    offset += 5
    amt = 0 if all(b < 0 for b in amt_bytes) else int(bcd_to_string(amt_bytes))
```
Field assignment by `i`:

| `i` | Setter target | Has quantity? |
|---|---|---|
| 0 | Gross (`GrossCharactor`/`GrossQuantityStr`/`GrossAmountStr`) | yes |
| 1 | Net (`NetCharactor`/`NetQuantityStr`/`NetAmountStr`) | yes |
| 2 | Caid (`CaidCharactor`/`CaidAmountStr`) | no |
| 3 | Chid (`ChidCharactor`/`ChidAmountStr`) | no |
| 4 | Ckid (`CkidCharactor`/`CkidAmountStr`) | no |
| 5 | Crid1 (`Crid1Charactor`/`Crid1AmountStr`) | no |
| 6 | Crid2 | no |
| 7 | Crid3 | no |
| 8 | Crid4 | no |

Record byte width: **22 bytes** for `i<2` (12+5+5), **17 bytes** for `i>=2` (12+5).
Total payload consumed: `2*22 + 7*17 = 44 + 119 = 163` bytes.

The "all bytes negative" check (`b < 0`, i.e. high bit set on every byte of the
5-byte field) is used as a **BCD sentinel for "field not populated/blank"** — if
every nibble-pair byte has its sign bit set (which happens for the `0xFF`-family
BCD "blank/dash" convention noted in the main spec §5.6), the numeric value is
treated as `0` rather than attempting a BCD parse (which would otherwise throw/
produce garbage on `0xFF` bytes). This is a useful general pattern to replicate for
any other BCD field parsing.

`salesconfirmdef.setDateTime(...)` is set to the **phone's current local time**
formatted `yyMMddHHmm`, NOT anything read from the response — the register apparently
doesn't send its own timestamp for this job (or if it does, the app ignores it and
substitutes the phone clock). 🟢 high confidence, this is unambiguous.

### 2.5 Job `0010` (Echo-back) — no offset parsing

The entire `mReceivedDataLength`-byte payload is UTF-8-decoded and string-compared
against `ECHOBACK_DATA` ("1234567890"). Success ⟺ exact match. 🟢

---

## 3. `writeDealSetting()` — now readable (lower priority per task, brief summary)

Source: same file, lines 1340–1396 (this decompile) / spec's original line
1339–1420 reference. Signature: `writeDealSetting(byte comp, byte transfer, String
fileNo, String startRec, String endRec)`.

- Builds an ASCII payload of the requested file number(s), each colon-terminated:
  `<fileNo ASCII bytes>:` — or, if `mFileNoSetting` (an array field, used for
  multi-file requests) is non-empty, each entry from that array joined the same way
  (`<fileNo1>:<fileNo2>:...`).
  - 🔴 **Caveat**: the `if (length2 <= 0)` / `else` branching around which case
    builds the single-file vs. multi-file buffer looks **inverted** relative to
    what the variable names suggest (the "single file" `bytes` array is written
    inside the `length2 <= 0` branch label but the code that actually executes
    under a caught-exception path also independently writes the same buffer) —
    this is exactly the kind of jadx `--show-bad-code` artifact flagged in §0.
    The *safe* interpretation, consistent with `writeDealSetting`'s callers (which
    always pass a single non-null `str` fileNo and don't populate
    `mFileNoSetting` in any call site reviewed), is: **normal case = single file
    number, colon-terminated, no multi-file array in play.** Don't trust the
    multi-file branch's exact byte layout without a live capture.
- The resulting payload is sent via `makeJobPacket2(comp, transfer, payload)` (per
  main spec §2.4: `[LEN][0x01][0x00][0x81][comp][0x3A][transfer][payload][LRC]`).
- `str2`/`str3` (start/end record number strings) are converted to bytes but
  **never actually written into the outgoing buffer** in the reviewed code — only
  `bytes` (the file-number string) ends up in the payload. 🔴 This is surprising —
  either start/end record range is not actually part of this particular job
  packet (maybe it's applied via a different mechanism, e.g. embedded in `mFileNo`
  itself, or sent in a follow-up packet), or this is another decompiler-dropped
  code path. **Flag strongly for live-capture verification** if partial-range
  sales/setting retrieval is needed — right now, requesting "file all records" is
  the only behavior confirmed safe to replicate.

### `sendDealSetting()` (chunked upload helper, lines ~1398+, 🟢 clean)

- Chunks `mSendData` into **74-byte pieces**, indexed by call count `i` (i.e.
  `mCTSCount`): `start = i*74`, `size = min(74, remaining)`.
- Sets `mReceivedDataSEQ = i % 256` (chunk sequence number) and a `mReceivedDataTEXT`
  continuation flag depending on position:
  - first chunk (`i==0`) **and** last chunk overall (only chunk) → `TEXT = 0x81` (-127)
  - first chunk (`i==0`), more chunks follow → `TEXT = 0x01`
  - last chunk (not first) → `TEXT = 0x80` (-128, via `UnsignedBytes.MAX_POWER_OF_TWO`)
  - middle chunk → `TEXT = 0x00`
  
  This 4-state continuation-flag scheme (start+end / start-only / end-only / middle)
  is a cleaner and more specific picture than the main spec's §2.3 "0x81 = more
  data, 0x80 = last frame" simplification — worth updating the main spec if this
  file is revised, since it shows `0x81` specifically means "single-chunk message"
  (both start and end) rather than a generic "continuing" flag.

---

## 4. `RunLengthEncoding` — full algorithm (🟢 clean, `jp/co/casio/exconnect/common/RunLengthEncoding.java`, fully read, no decompiler warnings)

Two **independent, orthogonal** escape schemes, gated by booleans `z` (zero-byte
run-length) and `z2` (space-byte 0x20 run-length). `unCompress(true, true, data,
len)` means "both schemes active" — this is the mode used for `SectionFData`
records per the main spec §5.4/§7 item 5.

**Escape bytes:**
- `0xFE` (254, decimal) = zero-run marker (only interpreted if `z==true`)
- `0xFD` (253, decimal) = space-run marker (only interpreted if `z2==true`)
- Any other byte value = literal passthrough

**Marker encoding (2 bytes: `[marker][count]`):**
- `count == 0` → the marker byte itself was a **literal occurrence** of that byte
  value in the original data (i.e. `0xFE`/`0xFD` occurring naturally get
  self-escaped so they're never ambiguous with a real run marker).
- `count in 1..255` → **but note**: a `count` of exactly 1 is never actually
  emitted by the compressor (see below) — the compressor only emits a 2-byte
  marker when the run length is `>1`; a lone `0x00`/`0x20` byte is written as
  itself (no escaping needed) since it can't be confused with the 2-byte marker
  form unless immediately followed by a byte that looks like a count. Decompression,
  however, must still handle `count>=1` generically since a compressed stream could
  in principle contain any count.
- `count in 1..255` (when not 0) → **expand to `count` literal bytes** of value
  `0x00` (for the `0xFE` scheme) or `0x20`/space (for the `0xFD` scheme).

**Python reimplementation:**

```python
def rle_compress(zero_rle: bool, space_rle: bool, data: bytes) -> bytes:
    if not data:
        return b""
    if not zero_rle and not space_rle:
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if zero_rle and b == 0xFE:
            out += bytes([0xFE, 0x00])
            i += 1
        elif space_rle and b == 0xFD:
            out += bytes([0xFD, 0x00])
            i += 1
        elif (not zero_rle or b != 0x00) and (not space_rle or b != 0x20):
            out.append(b)
            i += 1
        elif zero_rle and b == 0x00:
            run = 0
            while i < n and run < 255 and data[i] == 0x00:
                i += 1
                run += 1
            if run > 1:
                out += bytes([0xFE, run])
            else:
                out.append(0x00)
        elif space_rle and b == 0x20:
            run = 0
            while i < n and run < 255 and data[i] == 0x20:
                i += 1
                run += 1
            if run > 1:
                out += bytes([0xFD, run])
            else:
                out.append(0x20)
    return bytes(out)


def rle_uncompress(zero_rle: bool, space_rle: bool, data: bytes, out_len: int) -> bytes | None:
    if data is None:
        return None
    if not zero_rle and not space_rle:
        return data if len(data) == out_len else None
    out = bytearray(out_len)
    i = j = k = 0          # i=input pos, j=output count so far, k=output write pos
    n = len(data)
    while j < out_len and i < n:
        b = data[i]
        if zero_rle and b == 0xFE:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            if nxt == 0x00:
                out[k] = 0xFE          # literal 0xFE byte
                k += 1
                j += 1
            else:
                if nxt > 255:          # (can't happen with a single byte; ported 1:1 from Java bounds check)
                    return None
                k += nxt
                j += nxt
        elif space_rle and b == 0xFD:
            i += 1
            if i >= n:
                break
            nxt = data[i]
            if nxt == 0x00:
                out[k] = 0xFD          # literal 0xFD byte
                # NOTE: Java increments k/j only in the shared tail below for this
                # sub-branch's fallthrough — see caveat below.
            else:
                if nxt > 255:
                    return None
                for _ in range(nxt):
                    out[k] = 0x20
                    k += 1
                j += nxt
                # falls through to shared i+=1/i+=1 below WITHOUT extra k/j bump
                i += 1
                continue
            k += 1
            j += 1
        else:
            out[k] = b
            k += 1
            j += 1
        i += 1
    return bytes(out) if j == out_len else None
```

⚠️ The Python above is a faithful line-by-line port including one quirk worth
flagging explicitly: in the Java `unCompress`, the `0xFD`-with-`count>1` branch
computes the expanded run and **updates `i7`/`j`-equivalents inside its own `while`
sub-loop**, then falls through to the shared `i6++; i5++` at the loop tail — i.e.
**it does NOT do the generic `i7++; i4++` that the plain-literal-byte branch does**.
This asymmetry versus the `0xFE` zero-run branch (which *does* fall through to a
shared `i7++`/`i4++` after its count expansion) is present in the decompiled source
as written. It's very likely intentional/correct (both branches end up
incrementing the output position by exactly the run length either way — the
`0xFE` branch does it via `i7 += iConvertToUnsignedInt` directly, the `0xFD` branch
via the explicit `while` fill loop) — but it's subtle enough, and the method
lacks any decompiler warning, that I'm calling it 🟢 **high confidence** (it's clean
code) while still flagging it because run-length-decode off-by-ones are exactly
the kind of bug that's invisible until you feed it real compressed data. Test
against a real captured/compressed `SectionFData` block before trusting this in
production.

**Practical guidance:** implement `rle_uncompress` straightforwardly as "read a
byte; if it's an active marker, read the count byte and either emit one literal
marker byte (count==0) or emit `count` fill bytes (0x00 or 0x20 respectively);
otherwise emit the byte as-is" — and unit-test round-trip against `rle_compress`
above (which is unambiguous) rather than hand-deriving edge cases from the
Java control flow.

---

## 5. `RegSetType` — model-code detection (🟢 clean, `RegSetType.java`, fully read; no decompiler warnings on the parts that matter — only a harmless "enum reconstruction failed" note, the resulting flat-class code is complete and correct)

### 5.1 How model family is derived

`RegConnectInfo.checkRegSetType(context, regcode)` (from `RegConnectInfo.java:275-281`,
also read in full this pass):
```java
RegSetType.getModel(
    regcode.substring(0, 5),                 // e.g. "EY240", "EY220", "EY200", "EY201"
    checkJapaneseRegEnv(context, regcode),    // isJP boolean
    Integer.parseInt(regcode.substring(5, 7)) // 2-digit variant/suffix number
)
```

`RegSetType.getModel(String prefix, boolean isJp, int suffix)` (lines 1084–1109):
1. Iterates all `RegSetType` enum-ish constants; matches when
   `prefix.matches(regSetType.mTopSerialNo) && regSetType.mJp == isJp &&
   !regSetType.isInvoice()`.
2. `mTopSerialNo` per constant:
   | Constant | `mTopSerialNo` pattern | Match type |
   |---|---|---|
   | `EY240` | `"^EY(240\|241\|540\|541)"` | **regex** — matches `EY240`, `EY241`, `EY540`, `EY541` prefixes |
   | `EY240JP` | same regex | (same prefixes, `isJp=true`) |
   | `EY240JPEX` | same regex | (same prefixes, `isJp=true`, is invoice-capable variant) |
   | `EY220JP` | `"EY220"` | `String.matches()` on a plain literal = **exact 5-char match only** |
   | `EY220JPEX` | `"EY220"` | same |
   | `EY200JP` | `"EY200"` | exact match |
   | `EY200JPEX` | `"EY200"` | exact match |
   | `EY201` | `"EY201"` | exact match, `isJp=false` (only variant without a `*JP` distinction) |
   | `NONE` | `""` | fallback |
3. Once a family matches, there's a **suffix-driven upgrade to the "*EX" (invoice)
   variant**, mirrored exactly in `RegConnectInfo.isInvoice()` (lines 287–290, also
   read this pass):
   - prefix `EY200`: `isInvoice ⟺ suffix >= 8`
   - prefix `EY220`: `isInvoice ⟺ suffix >= 8`
   - prefix `EY241`: `isInvoice ⟺ suffix >= 13`
   - prefix `EY540`: `isInvoice ⟺ suffix >= 9`
   - (no rule listed for bare `EY240`/`EY541` suffix upgrade — only `EY241`/`EY540`
     are called out in `isInvoice()`; `getModel()`'s inline switch additionally
     handles the `EY241`/`EY540` upgrade to `EY240JPEX` specifically, consistent
     with `isInvoice()`.)

So the **`regcode` string format is exactly 7 characters**: 5-char model prefix +
2-digit numeric suffix (e.g. `"EY24013"`, `"EY22008"`), confirming/refining the
main spec §6's "format `EYxxx` + 2-digit variant" note precisely.

### 5.2 SR-S820 → EY-code mapping: **not statically determinable**

A targeted grep across the **entire** decompiled source tree
(`sources/jp/co/casio/exconnect/**`) for the literal strings `S820`, `SR-S`, `S500`,
`T540` returned **zero matches** anywhere in the app. The only "S8xx"/"S5xx"-shaped
strings that exist are the model-family codes above (`EY200`/`EY201`/`EY220`/`EY240`
and their JP/EX variants) — there is no hardcoded table anywhere in this app mapping
a Casio commercial model number (SR-S820, or anything else) to an `EY*` internal
code.

**Conclusion (🔴 confirmed-negative, i.e. this really isn't in the app):** the
mapping must be obtained empirically — either:
1. Query job `0008` (reg info) against a live SR-S820 and read `regserialnostr`
   (§2.1) — Casio serial numbers commonly embed a model code, so this is the most
   promising source, OR
2. Whatever `regcode` string the original ECR+ app assigned/displayed during BLE
   pairing for this specific register (check `RegConnectInfo`'s persisted
   `bdcode`/`regcode` pairing storage on a phone that has previously paired with
   this SR-S820, if such a phone/backup is available), OR
3. Trial-and-error: since `SR-S820` is presumably in the same tier as the `EY200`/
   `EY220`/`EY240` "S-" or entry-level ECR family judging by typical Casio SR-series
   naming (SR-S products tend to be simpler baseline registers), **EY200 or EY220
   are the most plausible starting guesses**, but this is a naming-convention
   guess, not evidence from the codebase — do not hardcode field-width assumptions
   (§5.5 of the main spec) based on this guess without confirming against a live
   `0008` response and/or a real captured `SectionI` field schema (which, per main
   spec §5.3, is self-describing per-file anyway, so getting the model family
   exactly right matters less than the main spec worried — **the record layout
   for sales files is transmitted at runtime in the `SectionI` header, not hardcoded
   per model**, which somewhat de-risks this open question for a read-only sales
   client).

---

## 6. Summary of what's now resolved vs. still open

**Resolved this pass:**
- Full per-job, per-state action table for `doInBackground()` (§1) — closes main
  spec §7 item 1's biggest ask.
- Exact byte offsets for `0008`/`0009`/`0013`/`0014` response parsing (§2) — closes
  the "field offsets being unrecovered" open question referenced in main spec §3.1(b)
  and §3.1(d).4.
- `unsentdatastr` is **not** actually parsed from the `0008` response (hardcoded
  `"00"`) — corrects an assumption in the main spec.
- `writeDealSetting()`/`sendDealSetting()` mostly readable (§3) — closes main spec
  §7 item 2, with one real caveat flagged (start/end record range appears unused
  in the outgoing packet — needs live verification).
- `RunLengthEncoding` fully decoded with a working Python port (§4) — closes main
  spec §7 item 5.
- `RegSetType`/`RegConnectInfo` model-detection logic fully decoded, confirming the
  7-char `regcode` format and the EX/invoice suffix-threshold rules (§5.1).

**Still open / needs live-device verification:**
- Exact loop-termination bytecode past `mDealno==11` in `doInBackground()` (🔴, §0/§1.1) — low practical risk since the per-job sequences derived in §1.3 are self-consistent and terminate naturally via `writeEndTrans()`/`onPostExecute()`.
- The `ScribeConfig.DEFAULT_SEND_INTERVAL_SECONDS` magic timeout value used as the default per-job wait budget (🟡, §1.1) — assumed ~1000ms by convention, unconfirmed.
- Whether `mFileNoSetting` multi-file array path in `writeDealSetting()` is ever really exercised, and its exact byte layout if so (🔴, §3).
- Whether/how `mStartRecordNo`/`mEndRecordNo` (passed into `writeDealSetting()` but apparently unused in the outgoing payload) actually constrain a partial record-range request (🔴, §3) — if you need ranged sales pulls rather than "all records," this needs a live capture.
- **SR-S820 → EY-code mapping remains fully unresolved** (§5.2) — confirmed absent from the app; must be obtained via live job `0008` query or historical pairing data.
- Everything already flagged open in the main spec's §7 that this addendum didn't touch (items 4, 6, 7, 9, 10) remains open.

**Overall assessment for a Python client implementer:** the packet *sequencing*
(§1.3) is now trustworthy enough to implement directly — it's derived from
clean, non-contradictory code paths and corroborated by the independently-read
orchestration layer (`RegReceiveSalesData.java`) already in the main spec. Start
with `0010` (echo) and `0008` (reg info) as smoke tests exactly as the main spec's
§8 recipe suggests; both are now fully specified end-to-end (request sequence +
response parsing) with no remaining unknowns beyond the generic BLE-transport
questions (bonding, MTU behavior) already flagged in the main spec.
