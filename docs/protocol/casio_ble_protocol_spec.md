# Casio ECR+ (SR-S820) BLE Protocol Specification

Reverse-engineered from jadx-decompiled sources of the discontinued "CASIO ECR+" Android app.
Source root: `sources/jp/co/casio/exconnect/`

All file:line references below point to files under that root unless stated otherwise.

---

## 1. BLE Connection Setup

Source: `connectlibrary/BleRegConnecter.java`

- **Service UUID (VSSPP-style "virtual serial port"):** `0179bbd0-5351-48b5-bf6d-2167639bc867`
  (`BleRegConnecter.java:26`)
- **Characteristic UUID (used for BOTH write and notify):** `0179bbd1-5351-48b5-bf6d-2167639bc867`
  (`BleRegConnecter.java:16`)
- **CCCD (Client Characteristic Config) UUID:** `00002902-0000-1000-8000-00805f9b34fb` (`BleRegConnecter.java:17`)
- Two other services are referenced but **not used for the register protocol** — they are just checked/ignored during service enumeration: Device Information (`0000180a-...`) and Immediate Alert (`00001802-...`) (`BleRegConnecter.java:24-25`, `137-138`).

### Connection sequence (`BleRegConnecter.connect()` / `doConnect()`, lines 189-235)

1. `bluetoothDevice.connectGatt(context, false, callback)` — up to **3 retries** (`CONNECT_RETRY=3`) if the device fails to connect within a **20s** timeout (`CONNECT_TIMEOUT=20`).
2. On `onConnectionStateChange(STATE_CONNECTED)`: reset `mMtuChanged=false`, then call `requestMtuUntilChanged()`.
3. `requestMtuUntilChanged()` (lines 72-107): in a background thread, up to **2 retries** (`REQUEST_MTU_RETRY=2`), call `gatt.requestMtu(185)` (`REQUEST_MTU_SIZE=185`) and sleep **1000 ms** (`REQUEST_MTU_INTERVAL=1000`) between attempts, waiting for `onMtuChanged` to fire. If MTU negotiation never completes after 2 tries, it force-proceeds to `discoverServices()` anyway.
4. On `onMtuChanged`: if not a stale/duplicate callback, call `gatt.discoverServices()`.
5. On `onServicesDiscovered`: iterate all services; when the VSSPP service (`0179bbd0-...`) is found, get its characteristic `0179bbd1-...`:
   - `gatt.setCharacteristicNotification(characteristic, true)`
   - Get the CCCD descriptor (`00002902-...`), `setValue(ENABLE_NOTIFICATION_VALUE)`, `gatt.writeDescriptor(descriptor)`
   - Cache the characteristic, call `characteristic.setWriteType(WRITE_TYPE_NO_RESPONSE)` (constant `1`, i.e. `BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE`) (`BleRegConnecter.java:148`, also re-set in `BluetoothRegDataDeal.setGatt()` at line 945)
6. A `CountDownLatch` gates the whole thing with an additional **3s wait** after connect success (lines 220-225) before calling back `onServicesDiscovered` again to the outer/actual protocol handler in `BluetoothRegDataDeal`.

**Important:** writes use `WRITE_TYPE_NO_RESPONSE` (no ATT "Write Response" is awaited from the BLE stack level) — reliability is instead handled by the application-layer ACK/NACK/CTS protocol described below (§2).

**Note on `BluetoothRegDataDeal`'s own duplicate constants:** `BluetoothRegDataDeal.java` re-declares the same UUIDs (`CHARACTERISTIC_SERVICE_UUID`, `VSSPP_SERVICE_UUID`, `CLIENT_CHARACTERISTIC_CONFIG` — lines 51-52, 169-170, 172) and matches on `0179bbd1-...` in `onCharacteristicChanged` (line 328) to dispatch to `analyzeData()`.

---

## 2. Low-level packet framing

Source: `connectlibrary/BluetoothRegDataDeal.java` (parsing: `analyzeData()` lines 676-906; building: `makeBasePacket*`/`makeJobPacket*` lines 1102-1240; checksum: `calLRC()` line 1018).

### 2.1 Byte-stream framing (as received/sent over the single characteristic)

Every packet on the wire has the form:

```
[LEN_LO][LEN_HI][TYPE][SEQ][TEXT][...payload...][LRC]
```

- **`LEN_LO`, `LEN_HI`** (2 bytes): little-endian-ish length of everything *after* the length header, **minus 1** (see below). Parser: `mPacketLength = (b0 & 0xFF); mPacketLength += (b1*256) & 0xFF00;` (`analyzeData()` lines 693, 698). So `mPacketLength` = number of bytes from `TYPE` through the last payload byte **inclusive of the LRC being one more byte after that** — total frame length on the wire = `mPacketLength + 2` (2 header bytes) `+ 1`? Actually re-derivation: after reading LEN_LO/LEN_HI, the receiver allocates a buffer of `mPacketLength + 2` bytes (`ByteBuffer.allocate(mPacketLength + 2)`, line 702) and starts writing bytes into it including the 2 length bytes it already consumed (`mTempBuffer`, line 704). It then reads exactly `mPacketLength - 1` more bytes as body (`SEQ_DATA` state, terminates when `mReceivedLength == mPacketLength - 1`, line 735), then one more LRC byte (`SEQ_SUM` state, line 739-757). **So total wire length = `mPacketLength + 2` (header) + but the body reader stops at `mPacketLength - 1` bytes and then reads 1 LRC byte — meaning `mPacketLength` = (TYPE+SEQ+TEXT+payload bytes) + 1, i.e. `mPacketLength` counts body+LRC as `mPacketLength`, and total frame = 2 (len header) + mPacketLength.**
  - Confirmed by the sender side: `makeJobPacket()` computes `i = length - 2` and splits it into `b=(i&0xFF)`, `b2=((i/256)&0xFF)` as the two length bytes (lines 1160-1165), where `length` is the **total frame length including LRC**. So **the 2-byte length field = total_frame_length − 2**, i.e. it is the length of TYPE+SEQ+TEXT+payload+LRC combined (little-endian, low byte first).
- **`TYPE`** (1 byte): the "TYPE" byte (also called `TXT`/text control byte in some naming) — see §2.3 for values.
- **`SEQ`** (1 byte): sequence number.
- **`TEXT`** (1 byte): a second control/flag byte (naming is confusing in decompiled code — `mReceivedDataTEXT`); values seen: `0x81` (normal "more data" continuation-ish), `0x80` (also normal, last-frame marker per code checking `b3 == -128 || b3 == -127` at line 795), and it is echoed back in some job packets.
- **payload**: 0+ bytes, command/job specific (see §3).
- **`LRC`** (1 byte): XOR checksum of every preceding byte in the frame (length bytes included). `calLRC()`:
  ```java
  byte calLRC(byte[] bArr) {
      byte b = 0;
      for (byte b2 : bArr) b = (byte)(b ^ b2);
      return b;
  }
  ```
  (`BluetoothRegDataDeal.java:1018-1024`). On mismatch the receiver sends a NACK (`0xF1`) and resets its parse state (`analyzeData()` lines 746-757).

### 2.2 Chunking over the BLE characteristic (MTU-sized writes)

- **`WRITE_SIZE = 57`** bytes (`BluetoothRegDataDeal.java:195`) — this is the max chunk written per `writeCharacteristic()` call regardless of the negotiated 185-byte MTU. A full logical packet (which can be much larger, e.g. sales-data payloads) is split into ≤57-byte chunks via `sendBuffer()` (lines 950-965): `size = min(remaining, 57)`.
- Chunks of one logical packet are written back-to-back, driven by the `onCharacteristicWrite` callback (line 343-368): after each successful write, if the send buffer isn't empty yet, sleep `mWait` (**50 ms**, `BluetoothRegDataDeal.java:222`) then call `sendBuffer()` again for the next chunk. This repeats until `isEmptySendBuffer()` (offset reached `mPacketLength + 2`), at which point sequence resets and the next logical step (`dealnoNextDeal()`) proceeds.
- **This confirms**: WRITE_SIZE=57 is a link-layer chunk size unrelated to the length-header framing — reassembly on the receive side is driven purely by the length header in the logical packet, not by any BLE-level segmentation marker. The register's firmware presumably does the analogous re-chunking/reassembly on its side.

### 2.3 Control byte (`TYPE`) values

Collected from constants and from `analyzeData()`/`makeBasePacket*` usage:

| Hex (as signed byte in code) | Value | Constant / meaning | Source |
|---|---|---|---|
| `0x06` | 6 | STX — "start transmission" (`writeStartTrans`) | `BluetoothRegDataDeal.java:1038-1043` |
| `0x03` | 3 | ETX — "end transmission" (`writeEndTrans`) | lines 1046-1051 |
| `0x08` | 8 | EOT — end of transaction (`writeEOT`) | lines 1054-1059 |
| `0x07` | 7 | CTS / "send permission" (`writeCTS`/`writeSendPermission`) | lines 1062-1067, 1095-1100 |
| `0xF0` (-16) | 240 | ACK | `writeACK()` lines 1070-1082, and checked as `mReceivedDataTYPE == -16`/`0xF0` throughout `analyzeData()` |
| `0xF1` (-15) | 241 | NACK / checksum error response (`writeNACK`) | line 1084-1086 |
| `0xF2` (-14) | 242 | **BLE_PACKET_TYPE_BUSY** — register busy/in-use; maps to `ERROR_BLE_REG_NO_CONNECT_IN_USE` | `BluetoothRegDataDeal.java:48`, `analyzeData()` line 779-783 |
| `0xF6` (-10) | 246 | **BLE_PACKET_TYPE_COMMUNICATION_END** — register-initiated abort; if payload byte[0]==1 → `ERROR_BLE_REG_LIMITED_BY_LAW`, else → `ERROR_BLE_REG_NO_CONNECT` | `BluetoothRegDataDeal.java:49`, lines 784-793 |
| `0x01` | 1 | Job-packet marker (used as the first byte in `makeJobPacket`/`makeJobPacket2` body — NOT the same field as the framing TYPE; see §3) | lines 1166, 1197 |
| `0x02` | 2 | Alternate job-packet marker used in `makeJobPacket3` (data-continuation packets during multi-chunk transfers) | line 1226 |

`TEXT` byte values seen: `0x81` (`-127`, "normal continuing"), `0x80` (`-128`, likely "last/only frame") — both treated equivalently as "not busy/not comm-end" in the main dispatch (`b3 == -128 || b3 == -127`, line 795). Also `0x00` is used as a placeholder TEXT byte in `makeBasePacket(byte b)` for simple control packets (STX/ETX/EOT/CTS/NACK) (line 1107).

### 2.4 Packet builders

- **`makeBasePacket(byte b)`** (lines 1102-1114): builds the 5-byte control-frame body `[0x04][0x00][b][0x00][0x81]` + LRC → 6-byte total frame. Used for STX/ETX/EOT/CTS/NACK. (Length header `0x04,0x00` = `i=length-2=6-2=4`.)
- **`makeBasePacket2(byte b, byte b2)`** (lines 1116-1128): same shape but with `TEXT=b2` instead of fixed `0x81` — used for `writeACK(byte)` with an explicit echoed TEXT byte.
- **`makeBasePacket(byte b, byte b2)`** (lines 1130-1151): concatenates **two** base packets back-to-back (12 bytes total) — used by `writeACKCTS()` = ACK (`0xF0`) immediately followed by CTS (`0x07`) in one BLE payload, matching the "ACK→send-permission" ordering comment at line 855/872 ("ACK:EOT送信の場合、ACK(0xf0)->送信許可(0x07)の順番" = "For EOT send: order is ACK(0xF0)->send-permission(0x07)").
- **`makeJobPacket(byte[] jobCode, byte[] data)`** (lines 1153-1185): builds a job-command packet:
  ```
  [LEN_LO][LEN_HI][0x01][0x00][0x81][COMP][0x3A ':'][0x4A 'J'][jobCode 4 ASCII digits][0x3A ':'][data...][LRC]
  ```
  where `COMP` is `0x6E ('n')`=`JOB_DATA_COMP_NON` (110) or `0x63 ('c')`=`JOB_DATA_COMP_ON` (99) (lines 141-142). Literal bytes `0x3A`=':' and `0x4A`='J' are field delimiters — i.e. the payload is essentially colon-delimited ASCII: `...:J<jobcode>:<data>` with data further colon-terminated.
- **`makeJobPacket2(byte b, byte b2, byte[] data)`** (lines 1187-1214): `[LEN][0x01][0x00][0x81][b][0x3A][b2][data][LRC]` — used for `writeDealSetting()` (setting-file transfer marker + transfer-direction byte).
- **`makeJobPacket3(byte[] data)`** (lines 1216-1240): `[LEN][0x02][mReceivedDataSEQ][mReceivedDataTEXT][data][LRC]` — a **data-continuation packet whose SEQ/TEXT echo the last-received values**, used when streaming large payloads (setting-file upload chunks via `sendDealSetting()`).

### 2.5 Checksum

XOR of all bytes preceding the LRC byte in the frame (length header included). See §2.1. Simple to reimplement:
```python
def calc_lrc(data: bytes) -> int:
    x = 0
    for b in data:
        x ^= b
    return x & 0xFF
```

---

## 3. Job commands and transaction flows

Job codes are always sent as 4-ASCII-digit strings inside a `makeJobPacket()` frame (e.g. `"0010"` for echo-back). Source constants: `BluetoothRegDataDeal.java:133-146` (JOB_COMMAND_*), `59-82` (COMMANDHANDLER_* dispatch ints and DEAL_COMMAND_* strings).

| Job code | Constant | Meaning |
|---|---|---|
| `0001` | `JOB_COMMAND_SYSTEM_DATETIME_SET` | Set register date/time |
| `0007` | `JOB_COMMAND_DATETIME_INFO_GET` | (declared but handler dispatch for raw "7" not found wired to a Task in the reviewed dispatch table — likely superseded/unused; only referenced as a constant) |
| `0008` | `JOB_COMMAND_REG_INFO_GET` | Get register info (serial no, app/boot version, char set, target/language code, unsent-data flag) |
| `0009` | `JOB_COMMAND_UNSENT_DATA_GET` (string `"0009"`) | Query/retrieve count of not-yet-uploaded (unsent) sales data files |
| `0010` | `JOB_COMMAND_ECHO_BACK` | Echo-back / connectivity test — sends `ECHOBACK_DATA = "1234567890"` (line 198) |
| `0013` | `JOB_COMMAND_XZ_START` | Start a Remote X/Z report + sales-data transfer ("RemoteTaskDeal") |
| `0014` | `JOB_COMMAND_SALES_CONFIRM` | Sales-confirmation / current totals snapshot (no report printed) |
| `0016` | `JOB_COMMAND_BLE_DISCONNECT` | Graceful BLE disconnect request |
| `9001`-`9007` | `COMMANDHANDLER_RECEIVE_SETTING_START` .. `COMMANDHANDLER_RECEIVE_UNSENT_SALES_MAIN` | **These are internal Android-side state-machine phase IDs, not job codes sent over the air.** They map to `DEAL_COMMAND_*` strings that ARE sent (`"9001"`..`"9007"`) as the `mJobMode` value driving `BluetoothRegDataDeal`'s dispatch/parsing logic (e.g. which dealno branch to take in `analyzeData()`), but the actual byte protocol for the underlying record/data transfer reuses the low-level STX/CTS/ACK/data-chunk primitives from §2, not additional 4-digit job codes. |

### 3.1 High-level orchestration layer

The actual multi-step business logic (which job to send after which, per user action) lives in `connectlibrary/RegReceiveSalesData.java`'s `mDealHandler.handleMessage()` (lines 51-391), which drives `BluetoothRegWorkDeal` → `BluetoothRegDataDeal` for each phase. Key derived flows:

#### (a) Echo-back / connectivity test — job `0010`
- `RegTest.java` (not fully read, but `writeJobEcho()` in `BluetoothRegDataDeal.java:1292-1313` shows the payload): sends job `"0010"` with data `"1234567890"` via `makeJobPacket()`.
- Single round trip: phone→reg `STX`, then job packet, reg→phone `ACK`, then reg echoes data back, phone `ACK`s, `ETX`/`EOT` to close. (Exact ACK/CTS choreography for this specific job could not be fully confirmed because `SendReceiveDataDealResponseTask.doInBackground()` — the method that literally drives the packet-by-packet exchange — **failed to decompile** into readable Java; see §7 open question.)

#### (b) Get register info — job `0008`
- `ReceiveRegInfoTaskDeal()` (`BluetoothRegDataDeal.java:1638-1665`) resets `regserialnostr`, `regaplverstr`, `regbooterverstr`, `unsentdatastr`, `charactorsetcodestr`, `targetcodestr`, `languagecodestr`, then sends job `"0008"`.
- Populated fields are exposed via getters (`getRegSerialNoStr()`, `getRegAplVerStr()`, `getRegBooterVerStr()`, `getUnSentDataStr()`, `getCharactorSetCodeStr()`, `getTargetCodeStr()`, `getLanguageCodeStr()` — lines 2434-2463) but **the code that parses the response payload into these strings is inside the undecompiled `onPostExecute()`/`doInBackground()` of `SendReceiveDataDealResponseTask`** (lines 1909-1945) — field offsets within the `0008` response payload are NOT recoverable from static analysis alone (open question, §7).

#### (c) Set date/time — job `0001`
- `writeJobDateTime(date, time)` (`BluetoothRegDataDeal.java:1243-1290`): payload = `<yyMMdd ASCII bytes>` + `0x3A` + `<HHmmss ASCII bytes>` + `0x3A`, sent as the `data` parameter of `makeJobPacket("0001", data)`. If `date`/`time` args are null, current phone date/time is used, formatted via `Locale.getDefault()` (or `Locale.US` if Arabic locale).
- Triggered automatically as a prerequisite step before sales-data retrieval flows when `remoteFileNo.equals("1")` (`RegReceiveSalesData.java:59-89`), i.e. before pulling **daily** remote sales data the app first pushes the phone's current date/time to the register, waiting 2000 ms (`COMMANDHANDLER_SYSTEM_DATETIME_SET_WAIT_TIME`) beforehand.

#### (d) Trigger X/Z report + transfer sales data — job `0013` ("RemoteTaskDeal")
Full orchestration (`RegReceiveSalesData.java` `mDealHandler`, message `13` at lines 186-224, chained through `9004`→`9005`→`9006`):

1. **App → phone-local:** `sendRequestXZ(regcode, transferByte, fileNoStr, ...)` (`RegReceiveSalesData.java:425-452`) is called from `RegTransXZ.sendRequest()` (`RegTransXZ.java:18-31`) with `transferByte = 'Z'(0x5A)` if report type `i==1`, else `'X'(0x58)` (`JOB_DATA_TRANSFER_GET_Z`/`JOB_DATA_TRANSFER_GET_X`, `BluetoothRegDataDeal.java:145-146`), and `fileNoStr = String.valueOf(reportTypeInt)` — actually a **remote-report file number**, distinct from the sales-file numbers in §4 (`FILENO_REMOTE_DAILY="1"`, `FILENO_REMOTE_TIME_CARD="6"`, `BluetoothRegDataDeal.java:99-100`).
2. If `fileNoStr == FILENO_SALES_CONFIRM ("14")`, it instead dispatches job `0014` (Sales Confirm) — so job `14`/`0014` and the XZ path share the same entry point but diverge based on requested "file" number.
3. Otherwise: sets `jobmode = JOB_COMMAND_XZ_START ("0013")`, sends over BLE via `sendDealRemote(transferByte, fileNoStr)` (`BluetoothRegDataDeal.java:1466-1491`) — payload = `[transferByte]['0x3A'][ (fileNoStr.charAt(0) - '0') as single byte ][0x3A]`, i.e. the remote file number is sent as a **single BCD/raw digit byte**, not ASCII.
4. On success, response's `xzstartresultstr` is checked: **must equal `"00"`** (`RegReceiveSalesData.java:207-211`) or the whole deal aborts (`DEAL_COMMAND_BLE_END`). This confirms the register replies with a 2-character ASCII result/status code embedded somewhere in the `0013` response payload (exact offset unrecovered — see §7). A non-`"00"` result presumably signals the register could not run the requested report (e.g. not in a state to print X/Z).
5. If `"00"`: proceed to `jobmode = DEAL_COMMAND_RECEIVE_SALES_BEFORE ("9004")` → internally re-enters `ReceiveSalesTaskDeal()` with `receivesalestranstart=0`.
6. `9004` succeeds → `jobmode = DEAL_COMMAND_RECEIVE_SALES_MAIN ("9005")` — this is the actual bulk sales-data transfer step (STX, then multiple 57-byte chunked data packets covering the requested sales files, framed per §2, terminated by ETX/EOT per the LRC-checked packet loop in `analyzeData()`).
7. `9005` succeeds → reads `receivedsalestransfer = getTransfer()` (echoes back the transfer-type byte, 'X' 0x58 or 'Z' 0x5A) → `jobmode = DEAL_COMMAND_RECEIVE_SALES_AFTER ("9006")` — a finalization/cleanup phase.
8. `9006` succeeds → if `receivedsalestransfer == 'Z' (0x5A/90)`, chains to `jobmode="0001"` (push date/time — presumably to update the register's internal "last Z" timestamp / re-sync clock after a Z-reset), otherwise goes straight to `DEAL_COMMAND_BLE_END ("9999")` which triggers `bledisconnectTaskDeal()`.
9. Received sales bytes are written by `BluetoothRegDataDeal.writeFile()` (line 2161-2173) — **raw passthrough**, no interpretation; the accumulated file is a **CASIO physical sales-data file** in the FileAll/SectionH/SectionI/SectionF format documented in §5, written to `<app files dir>/<regcode>/XZ/FILE<xxx><transferchar>_<datetime>.<xxx>` (`openFile()`, lines 2111-2159) and RLE-decompressed afterward via `uncompresseddeal()` (lines 2234-2335) into a companion "used" file.

#### (e) Receive sales data for specific file numbers — jobs `9001`/`9003`/`9004`/`9005`/`9006`/`9007`
- These are **not separate over-the-air job codes**; they are internal `mJobMode`/`COMMANDHANDLER_*` values that select which branch of `analyzeData()`'s dealno state machine and which `mFileNo` filter is active. The `mFileNo` field (one of `FILENO_SALES_*`, §4) filters which physical sub-file(s) the register streams back within the bulk transfer.
- `9001` = `ReceiveSettingTaskDeal` (settings/program file retrieval, not sales) — sets `mComp=JOB_DATA_COMP_ON`, `mTransfer=JOB_DATA_TRANSFER_GET_PGM ('P', 0x50)`, `mFileNo="0000"` initially (`BluetoothRegDataDeal.java:1667-1697`).
- `9002` = `SendSettingTaskDeal` (upload a settings file to the register) — `mTransfer=JOB_DATA_TRANSFER_SEND_PGM ('S', 0x53)`.
- `9003` = `ReceiveSalesTaskDeal` entry with `mTransfer=JOB_DATA_TRANSFER_GET_X` fixed (used for the standalone/manual "receive sales" flow outside the XZ-report flow).
- `9004`/`9005`/`9006` = the three phases (before/main/after) of one bulk sales transfer, as in (d) above.
- `9007` = `SendUnSentDataDeal` reused for **iterating multiple unsent files** (`unsentfiledealno` loop, `RegReceiveSalesData.java:329-366`) — after job `0009` reports `unsentfilecount > 0`, the app loops sending `jobmode="9007"` (`DEAL_COMMAND_RECEIVE_UNSENT_SALES_MAIN`) once per unsent file until `unsentfiledealno == unsentfilecount`, then goes to `9999` (disconnect).

#### (f) Disconnect — job `0016`
- `writeJobBleDisconnect()` (`BluetoothRegDataDeal.java:1524-1536`): sends job `"0016"` with no data via `makeJobPacket(bytes, null)`.
- `bledisconnectTaskDeal()` (lines 1845-1866) dispatches this, then the underlying `BleRegConnecter.disconnect()` tears down the GATT connection (`endDeal()`, lines 616-649). Note: the app-level "`9999`"/`DEAL_COMMAND_BLE_END` phase (`RegReceiveSalesData.java:99-123`) is what actually triggers `jobmode = "9999"` → `bluetoothregworkdeal.setStartmode(9)` → eventually calls `endDeal()`/GATT disconnect; job `0016` (`JOB_COMMAND_BLE_DISCONNECT`) is the explicit "tell the register we're closing" wire command, sent before the GATT-level disconnect in the ordinary graceful-close path.

### 3.2 The core packet-driving loop (STX/CTS/ACK choreography)

The generic pattern inferable from `analyzeData()` and the `write*()` helpers, valid for **every** job above:

1. Phone sends **STX** (`0x06`) to start a transaction.
2. Register replies **ACK** (`0xF0`).
3. Phone sends the **job packet** (`makeJobPacket`), possibly chunked into ≤57-byte writes if long.
4. Register replies **ACK** (`0xF0`), and/or **CTS** (`0x07`) meaning "go ahead, I'm ready to send/receive more" — comments in the code describe an "ACK(0xF0)->CTS(0x07)" ordering specifically for the case where an EOT is being sent (lines 855-857, 866-872, 1088-1093 `writeACKCTS()`).
5. For multi-chunk data transfers (sales/setting files), the sender uses `mCTSCount` (line 213) to track "send permission" grants — the receiver paces the sender via repeated CTS packets, classic for a flow-controlled serial-over-BLE link.
6. Phone (or register) sends **ETX** (`0x03`) to mark end of a data block, and **EOT** (`0x08`) to mark end of the whole transaction.
7. Any frame failing LRC triggers **NACK** (`0xF1`) and a state reset — the sender is expected to retransmit (retry logic is **not visible** in the decompiled `doInBackground`/`onPostExecute`, which failed to decompile — see §7).
8. A **3-second inactivity timeout** (`TimeOut` AsyncTask, lines 1538-1589) aborts the transaction with `ERROR_BLE_TIMEOUT` if no further byte arrives within 3000 ms while mid-packet.

**The exact byte-for-byte example sequences for each job (a)-(f) could not be fully reconstructed** because the method that actually issues these writes in order — `SendReceiveDataDealResponseTask.doInBackground()` (`BluetoothRegDataDeal.java:1914-1920`) and its companion `onPostExecute()` (lines 1934-1945) — are both **stub methods jadx could not decompile** ("Method not decompiled... Code decompiled incorrectly"). Only the low-level primitives they presumably call (`writeStartTrans`, `writeCTS`, `writeACK`, `writeEOT`, `writeJobEcho`, etc.) and the receive-side parser (`analyzeData`) were recoverable. This is the single biggest gap in the static analysis — **live packet capture (e.g. Android BLE HCI snoop log while using the original app) is strongly recommended to nail down exact ordering/timing**, though the state-values and packet-builder code above give a very strong basis for a working implementation attempt.

---

## 4. File number codes (`FILENO_SALES_*` / `FILENO_SETTING_*`)

Source: `BluetoothRegDataDeal.java:99-132`.

### Sales file numbers (`FILENO_SALES_*`)
| Constant | Value | Report |
|---|---|---|
| `FILENO_SALES_DAILY` | `"0000"` | Daily sales totals |
| `FILENO_SALES_FIX` | `"0001"` | Fixed totalizers (gross/net/tax/tender totals — same FNO=1 as `File001FIXTTL`, §5) |
| `FILENO_SALES_FUNC` | `"0002"` | Function-key sales |
| `FILENO_SALES_PLU` | `"0004"` | PLU (item) sales |
| `FILENO_SALES_DEPT` | `"0005"` | Department sales |
| `FILENO_SALES_GROUP` | `"0006"` | Group sales |
| `FILENO_SALES_HORLY` | `"0009"` | Hourly sales (note: matches `FieldID.FIELD_HOURLY_*` = 122-124) |
| `FILENO_SALES_MONTHLY` | `"0010"` | Monthly sales |
| `FILENO_SALES_CLERK` | `"0011"` | Clerk sales |
| `FILENO_SALES_CHECKINDEX` | `"0015"` | Check index |
| `FILENO_SALES_TIMEATTENDANCE` | `"0019"` | Time & attendance |
| `FILENO_SALES_GT` | `"0020"` | Grand total |
| `FILENO_SALES_EJ` | `"0048"` | Electronic journal |
| `FILENO_SALES_CONFIRM` | `"14"` | Sales confirm (shares job `0014`, not a file-transfer FNO in the same sense) |

### Setting/program file numbers (`FILENO_SETTING_*`)
| Constant | Value | Setting |
|---|---|---|
| `FILENO_SETTING_FUNC` | `"0002"` | Function keys |
| `FILENO_SETTING_PLU` | `"0004"` | PLU master |
| `FILENO_SETTING_DEPT` | `"0005"` | Department master |
| `FILENO_SETTING_GROUP` | `"0006"` | Group master |
| `FILENO_SETTING_CLERK` | `"0007"` | Clerk master |
| `FILENO_SETTING_GENERAL` | `"0022"` | General settings |
| `FILENO_SETTING_REPORTHEADER` | `"0024"` | Report header |
| `FILENO_SETTING_TAX` | `"0025"` | Tax table |
| `FILENO_SETTING_DAILYXZ` | `"0029"` | Batch X/Z settings |
| `FILENO_SETTING_CLERKITEM` | `"0030"` | Clerk-item link |
| `FILENO_SETTING_MESSAGE` | `"0032"` | Messages |
| `FILENO_SETTING_GRAFICLOGO` | `"0047"` | Graphic logo |
| `FILENO_SETTING_POP1`..`POP5` | `"0078"`-`"0082"` | POP images 1-5 |
| `FILENO_SETTING_ALL` | `"0092"` | "All settings" bundle — used as the default `mFileNo` for job `9001`/`9002` bulk setting transfers |

### Remote-report file numbers (separate namespace, used only with job `0013`)
| Constant | Value | Meaning |
|---|---|---|
| `FILENO_REMOTE_DAILY` | `"1"` | Daily remote report (triggers date/time push first, see §3.1c) |
| `FILENO_REMOTE_TIME_CARD` | `"6"` | Time-card remote report |

The `logical/RegFileEnum`-adjacent numeric FNOs used inside the physical file format (`SectionI.mFNO`, read as a 2-byte BCD short, see §5) appear to be **plain integers** matching the numeric part of the above strings (e.g. FNO=1 for `FILE001X.001`, FNO=4 for PLU, FNO=5 for Dept, FNO=48 for EJ) — confirmed directly by `REG_SALESCHECK_FILE = "FILE001X.001"` and `REG_SALESDATAMAINTENANCE_FILE = "FILE048Z.048"` (`BluetoothRegDataDeal.java:150-155`) and by `File001FIXTTL`/`File004PLU`/`File005DEPT`/`File048EJ` each hard-coding `mFNO = 1/4/5/48` respectively.

---

## 5. Physical sales/setting file record format

Source: `regfile/physical/{FileAll,SectionH,SectionI,SectionF,SectionFHeader,SectionFData,FileModel}.java`, `regfile/common/{BinaryCodedDecimalTool,FieldID}.java`, `regfile/logical/{File000Strategy,File001FIXTTL,File004PLU}.java`.

This is the format of the **raw bytes streamed back over BLE for sales-data jobs** (`0013`/XZ, `9003`-`9007`) and written verbatim by `BluetoothRegDataDeal.writeFile()` — i.e. what you'll receive and need to parse in a Python client.

### 5.1 Overall file layout

```
[SectionH: 32 bytes]
[FileModel 1: SectionI header][SectionF blocks...]
[FileModel 2: SectionI header][SectionF blocks...]
...
(EOF)
```

### 5.2 `SectionH` — file header (32 bytes total, `SectionH.java`)
| Offset | Len | Field | Encoding |
|---|---|---|---|
| 0 | 1 | Magic byte, must be `0x68` ('h') | fixed |
| 1 | 1 | `mMachine` | raw byte |
| 2 | 1 | `mID` | raw byte |
| 3 | 2 | (skipped/reserved) | — |
| 5 | 5 | `mStrDateTime` | BCD→hex-string via `convertBCDtoString` (each byte → 2 hex chars; **not straight decimal BCD** — see caveat below) |
| 10 | 22 | (skipped/reserved) | — |
| **total** | **32** | | |

**Caveat:** `SectionH.mStrDateTime` is produced by `BinaryCodedDecimalTool.convertBCDtoString()` which just hex-formats each raw byte (`String.format("%02x", b)`), NOT the digit-oriented `subBCD()` used elsewhere — so despite the name, treat this datetime field as 5 raw bytes and decode per whatever the actual on-wire nibble convention turns out to be (likely still BCD digits YYMMDDHHmm or similar, but confirm against a live sample — flagged in §7).

### 5.3 `SectionI` — per-file-type descriptor (`SectionI.java`)
Follows immediately after `SectionH`, and again after each `FileModel`'s data:
| Field | Encoding | Notes |
|---|---|---|
| Magic byte, must be `0x69` ('i') | fixed | |
| `mLEN` | 2-byte value via `BinaryCodedDecimalTool.readShort` (big-endian raw short, **not decimal BCD** despite the tool's name — it's `ByteBuffer.getShort()`) | length of the SectionI header remainder |
| `mFNO` | 2-byte value, same `readShort` | **file number** (e.g. 1, 4, 5, 48 — matches §4) |
| 1 byte skipped | | |
| `mALL` | 1 byte | number of fields when file type = SALES |
| `mPGM` | 1 byte | number of fields when file type = SETTING |
| 1 byte skipped | | |
| `mCAL` | 1 byte | |
| Then `getNumOfFields()` repetitions of: `{mFID: 2-byte readShort, mFLength: 1 byte}` | | this is the **per-record field schema**: each entry says "field ID `mFID` occupies `mFLength` bytes at this position in every data record of this file". `getNumOfFields()` = `mALL` if `FILE_TYPE.SALES`, else `mPGM`. |
| `mHeaderLength = mLEN + 3` | computed | |

So **the record layout for a given file/FNO is NOT fixed in code** — it's declared dynamically by the SectionI field-schema table transmitted at the start of that file/FNO's section, using the `FieldID.FIELD_*` constants (§5.5) as the `mFID` values. This is the standard CASIO ECR physical-file convention: self-describing records.

### 5.4 `SectionF` / `SectionFHeader` / `SectionFData` — the actual records (per FNO)

`SectionFHeader` (18 bytes, `SectionFHeader.java`):
| Field | Encoding |
|---|---|
| Magic byte `0x66` ('f') | fixed |
| `mFNO` | 2-byte `readShort` |
| 1 byte skipped | |
| `mZCounter` | 2 bytes, `convertBCDtoString` (hex-formatted, see same caveat as §5.2) |
| `mLEN` | 4-byte `readInt` (record length in bytes for this block) |
| `mStartRec` | 4-byte `readInt` |
| `mEndRec` | 4-byte `readInt` |

Then `(mEndRec - mStartRec + 1)` records follow, each of length `mSectionFHeader.mLEN` bytes (uncompressed) — or, if the file is RLE-compressed (`mIsCompressed=true`, i.e. as received fresh off BLE per `RECEIVESALESDATADEAL`/`uncompresseddeal()` call sites), each record is run-length-encoded via `jp.co.casio.exconnect.common.RunLengthEncoding` (not read in this pass — flagged §7) and must be decompressed with `FileAll.unCompressAll()`/`RunLengthEncoding.unCompress(true, true, data, len)` before the fixed-width field offsets in §5.3's schema apply.

**Record number ("Rec") semantics:** for totals-style files (FNO=1 `FIXTTL`, FNO=4 `PLU`, FNO=5 `DEPT`, etc.), each "record" (`mStartRec..mEndRec`) corresponds to one logical row — e.g. one PLU item, one department, or (for FNO=1) one totalizer line (gross, net, each tax, each tender type, etc. — the mapping from record NUMBER to totalizer MEANING is **not present in the reviewed code** — it's presumably fixed per firmware/model and would need either a data dictionary from Casio or live-sample correlation; flagged §7).

### 5.5 Field ID → meaning (from `regfile/common/FieldID.java`, confirmed against `File000Strategy.java` read/write methods)

Key fields for **sales data parsing**:
| FieldID | Constant | Encoding (from `File000Strategy`) | Meaning |
|---|---|---|---|
| 1 | `FIELD_QTY` | BCD, digit 1, len 10 → `convertBCDtoLong` | Quantity (`readQTY`/`writeQTY`, e.g. PLU/dept/fixed-totalizer quantity) |
| 2 | `FIELD_AMT` | BCD, digit 1, len 10 → `convertBCDtoLong` | Amount (`readAMT`/`writeAMT`) — **this is the money value field for essentially every sales total** |
| 8 | `FIELD_MONTH_GROSS_AMT` | BCD, digit 1, len 10 | Gross amount (`readGrossAMT`) — used for hourly-file gross |
| 9 | `FIELD_HOURLY_MONTH_NET_QTY` | BCD, digit 1, len 10 | Net quantity (`readNetQTY`) |
| 10 | `FIELD_HOURLY_MONTH_NET_AMT` | BCD, digit 1, len 10 | Net amount (`readNetAMT`) |
| 13 | `FIELD_CHAR` | raw bytes → string (charset-table dependent; Shift-JIS for Japan target, IBM864 for Arabic, else default) | Item/dept/PLU **name** text field |
| 14 | `FIELD_BASE` | model-dependent sub-fields (see `RegFileEnum` — taxable status, ADD mode, etc. packed as BCD digit ranges within this field) | Misc config/flags block |
| 15 | `FIELD_PRICE` | BCD, digit=1, len = model-dependent (6 or 8, see `RegFileEnum.RegPrice`) | Unit price |
| 16 | `FIELD_LINK` | BCD, model-dependent digit/len (see `RegDeptLink`/`RegGroupLink`) | Dept/Group link for a PLU |
| 23 | `FIELD_FUNCTION_CODE` | BCD, digit 1, len 4 | Function-key code |
| 31 | `FIELD_CLERK_NO` | BCD, digit 1, len 4 | Clerk password/number |
| 34 | `FIELD_OBR_CODE` | BCD via `convertStringOBRToBCD`/trim `0xEE` sentinel | Barcode/OBR code |
| 37 | `FIELD_UPDATE_DATE` | custom `convertUpdateDateToYYYYMMDD` (not traced further) | Last-update date |
| 122/123/124 | `FIELD_HOURLY_START/END/CUST` | BCD time-string / BCD long | Hourly-file start time, end time, customer count |
| 148/149 | `FIELD_ATTEND_DATE/TIME` | (not read in this pass) | Time & attendance date/time |
| 152 | `FIELD_ATTEND_CLK_REC` | BCD, digit 1, len 4 | Clerk number in attendance record (`readClerkNumber`) |
| 191/192/193 | `FIELD_EJ_TYPE`/`FIELD_EJ_DATA`/`FIELD_EJ_DUMMY` | `mFID=191`: BCD int (record-type code: 0=text/2=BCD-string/10/50 variants); `192`: content per type; `193`: BCD int, secondary flag | **Electronic Journal record** — type-tagged variable content, this is how the EJ/FILE048 log entries are structured |

**Field 2 (`FIELD_AMT`) and field 1 (`FIELD_QTY`) are the money/count columns used throughout — for daily totals (FNO=1), PLU (FNO=4), Dept (FNO=5), and Group (FNO=6) sales files, expect each logical record (one per `Rec` number) to carry at minimum a `FIELD_CHAR`(13) name, `FIELD_QTY`(1), and `FIELD_AMT`(2) triplet, per the SectionI-declared schema for that FNO** — confirmed directly by `File001FIXTTL.java`, `File004PLU.java` both exposing `readCharacter()`/`readQTY()`/`readAMT()` as their primary accessors.

### 5.6 BCD/number encoding helpers (`BinaryCodedDecimalTool.java`)
- `subBCD(bytes, startDigit, numDigits)`: extracts a **decimal-digit** substring from packed BCD (nibble = digit; nibble value `0xF` renders as `'-'`, i.e. sign). This is the "real" BCD decode used for `convertBCDtoInt`/`convertBCDtoLong`.
- `convertBCDtoString(bytes)`: **NOT decimal BCD** — just hex-formats each byte (`%02x`). Used for `SectionH.mStrDateTime`, `SectionFHeader.mZCounter`, tax-rate/report-code raw dumps — treat these as raw hex/BCD nibble pairs, decode with domain knowledge (e.g. YYMMDDHHmm digit pairs) rather than assuming pure hex value.
- `convertStringOBRToBCD`/OBR barcode fields use a sentinel nibble value `0xE` (`'e'`) for padding instead of `0xF`.
- Negative values represented with a leading BCD nibble `0xF` (`'-'` sign) per `convertLongToBCD`.

---

## 6. Authentication / pairing

**No app-layer authentication, passkey, or challenge-response was found anywhere in the reviewed BLE code path.** Specifically:
- `RegConnectInfo.java` (device/connection bookkeeping) stores only a `bdcode` (Bluetooth device address string) and `regcode` (an internal app-assigned register identifier, format `EYxxx` + 2-digit variant, e.g. parsed via `checkRegSetType()` at `RegConnectInfo.java:271-281`) — no PIN, key, or certificate material.
- `BleRegScanner.bindBdcode()` (referenced but not read in this pass) is presumably just a BLE scan-and-match-by-address helper.
- Standard Android BLE bonding (OS-level pairing dialog, if the peripheral requires it) is not explicitly managed in this code — `connectGatt(context, false, callback)` uses `autoConnect=false` and no explicit `createBond()`/bonding-state handling was observed in `BleRegConnecter.java`. If the SR-S820 requires OS-level BLE bonding/pairing (e.g. Just Works or a fixed PIN), that would happen transparently via the Android BLE stack outside this app's code — **untested/unconfirmed, flag for live testing** (§7).
- The only "access control" concept found is the **`0xF2` BUSY** response (`ERROR_BLE_REG_NO_CONNECT_IN_USE`) — i.e. the register refuses a second concurrent BLE client, and **`0xF6` COMMUNICATION_END** with payload byte `1` → `ERROR_BLE_REG_LIMITED_BY_LAW`, suggesting some regulatory-driven connection limiting (e.g. certain countries requiring fiscal-lock behavior) rather than any cryptographic auth.

---

## 7. Open questions / static-analysis limitations

Flagged explicitly so a live-device capture/test session can resolve them:

1. **`SendReceiveDataDealResponseTask.doInBackground()` and `onPostExecute()` in `BluetoothRegDataDeal.java` (lines ~1909-1945) failed to decompile** ("Method not decompiled... Code decompiled incorrectly, please refer to instructions dump"). This is the method that actually issues the STX/job-packet/CTS/ACK/ETX/EOT sequence in order for every job type and parses response fields (reg serial no, app/boot version, char/target/language codes, `xzstartresultstr`, `SalesConfirmDef` fields, `unsentdatastr`). All of §3's flow reconstructions are inferred from (a) the low-level primitives it must be calling, (b) the receive-side `analyzeData()` dealno state transitions, and (c) the caller (`RegReceiveSalesData`)'s orchestration — but the **exact packet order, response-field byte offsets, and retry/backoff behavior inside a single job's exchange are not confirmed from source**. Recommend: capture a live BLE HCI snoop log (`btsnoop_hcp.log` via Android developer options) while running the original ECR+ app against the SR-S820, or sniff with a BLE sniffer, for at least: echo-back (0010), reg-info-get (0008), and one full XZ+sales-receive (0013→9004→9005→9006) cycle.
2. **`writeDealSetting()` (`BluetoothRegDataDeal.java:1339-1420`) also failed to decompile** into clean Java (raw smali/bytecode dump only) — this handles multi-file setting transfers; not critical for a read-only sales-report client but relevant if send/restore functionality is ever wanted.
3. **Casio model-code mapping for the SR-S820 is not confirmed.** The app internally keys behavior off `RegSetType` values `EY240`, `EY240JP`, `EY240JPEX`, `EY220JP`, `EY220JPEX`, `EY200JP`, `EY200JPEX`, `EY201` (`connectlibrary/RegSetType.java`), derived from the first 5 chars + a 2-digit suffix of the app's internal `regcode` string (`RegConnectInfo.checkRegSetType()`, lines 271-281) — **no string "S820" or "SR-S820" appears anywhere in the decompiled tree**. Field widths/positions for price, tax rate, dept/group links, etc. (§5.5, via `RegFileEnum`) differ per `RegSetType`, so **you must determine which `EY*` code family the SR-S820 actually reports as** (likely obtainable directly from job `0008`'s register-info response, or from the `regcode` string the original app stored/displayed during pairing) before trusting the exact digit-widths in §5.5's model-dependent rows.
4. **Record-number → totalizer-line meaning mapping for FNO=1 (`FILE001X`, fixed totals) is not in the code.** `File001FIXTTL` just iterates whatever `Rec` numbers are present and exposes generic `readCharacter()/readQTY()/readAMT()` per record — the mapping of "record 3 = Net Sales", "record 7 = Tax 1", etc. is presumably a fixed firmware convention not present in this app's logic (it likely relies on the `FIELD_CHAR`(13) name string printed alongside each record, e.g. the register itself sends localized totalizer labels). **Recommend cross-checking against real captured FILE001X data and/or a printed X/Z report from the same register for line-label correlation.**
5. **`RunLengthEncoding` compression scheme (`jp.co.casio.exconnect.common.RunLengthEncoding`) was not read in this pass** — needed to decompress `SectionFData` records when the file arrives compressed (`mIsCompressed=true`) straight off BLE, before the FieldID-schema offsets in §5.3 can be applied. This should be read next; it's referenced from `FileAll.java:265-271` (`unCompressLine`/`compressLine`, both delegate to `RunLengthEncoding.unCompress/compress(true, true, data, len)`).
6. **BLE bonding/pairing requirements are unconfirmed** — no explicit PIN/passkey/bonding code found in the app (§6), but whether the SR-S820 peripheral itself requires OS-level BLE bonding (vs. open/Just-Works) was not determined from this code path and needs live testing.
7. **The exact total-frame-length arithmetic in §2.1 was derived by cross-referencing sender and receiver code and is internally consistent, but was not validated against a captured byte sequence** — recommend a byte-level sanity check against a live capture before shipping a parser.
8. **`0007` (`JOB_COMMAND_DATETIME_INFO_GET`) is declared as a constant but no code path was found that actually sends it** in the reviewed files (`ReceiveRegInfoTaskDeal`/etc. all key off `0008`, not `0007`) — possibly dead/legacy from an older register generation; do not assume it's implemented on the SR-S820.
9. **`SectionH.mStrDateTime` and `SectionFHeader.mZCounter` decode via `convertBCDtoString` (raw hex-format, not decimal-digit BCD)** — flagged in §5.2/5.6 as needing confirmation against real data before trusting a particular date parsing scheme.
10. Two files referenced by the task (`RegReceiveEJ.java`, `RegSetType.java` in full, `RegSendSettingFile.java`, `RegReceiveSettingFile.java`, `RegTest.java`, `RegBleDisconnect.java`, `RegCharacterSet.java`, `RegSetDate.java`, `RegLocaleInfo.java`, and the full 1967-line `File000Strategy.java`, plus `File005DEPT.java`/`File006GRP.java`/`File048EJ.java` logical wrappers) were **only partially reviewed or not opened in this pass** due to time constraints; they are mostly thin wrappers following the same pattern as `File004PLU`/`File001FIXTTL` (confirmed by the parts that were read) and are unlikely to change the core protocol picture, but should be skimmed before finalizing field-offset tables for DEPT/GROUP/EJ specifically.

---

## 8. Quick-reference: minimal Python client sketch (not implemented, just the recipe)

1. Connect via `bleak`, discover service `0179bbd0-5351-48b5-bf6d-2167639bc867`, characteristic `0179bbd1-5351-48b5-bf6d-2167639bc867`.
2. Request MTU 185 if the platform allows it (bleak/BlueZ often negotiates automatically; not critical since writes are chunked to 57 bytes regardless).
3. Enable notifications on the characteristic (writes `0x0100` to CCCD `00002902-...`).
4. Implement `calc_lrc()` per §2.5.
5. Implement `make_job_packet(job_code: str, data: bytes|None) -> bytes` per §2.4's `makeJobPacket` layout.
6. Implement a receive-side reassembler mirroring `analyzeData()`'s state machine (SEQ_LEN1→SEQ_LEN2→SEQ_DATA→SEQ_SUM) to reconstruct full logical packets from notification callbacks (which themselves may already be ≤MTU chunks from the register side).
7. Write each outgoing logical packet in ≤57-byte slices via `write_gatt_char(char, chunk, response=False)`, pacing ~50 ms between chunks (mirroring `mWait`).
8. Start with job `0010` (echo-back) as a smoke test — simplest possible round trip, no file parsing required.
9. Then attempt job `0008` (reg info) to confirm the `RegSetType`/model family (§7 item 3) before trusting any file-format digit-widths.
10. Only then attempt `0013` (XZ start) + `9004`/`9005`/`9006` bulk transfer, and pass the resulting raw bytes through a from-scratch reimplementation of §5's `SectionH`/`SectionI`/`SectionF`/`SectionFData` parser (plus RLE decompression per §7 item 5) to extract PLU/Dept/Daily totals.
