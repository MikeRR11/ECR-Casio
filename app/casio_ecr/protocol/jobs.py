"""Job codes and payload builders for the Casio ECR BLE protocol."""
from __future__ import annotations

import datetime as _dt

from . import framing

# Job codes (sent as 4-ASCII-digit strings inside a job packet)
JOB_SET_DATETIME = "0001"
JOB_DATETIME_INFO_GET = "0007"  # declared in app but no confirmed call site (SR-S820 support unknown)
JOB_REG_INFO_GET = "0008"
JOB_UNSENT_DATA_GET = "0009"
JOB_ECHO_BACK = "0010"
JOB_XZ_START = "0013"
JOB_SALES_CONFIRM = "0014"
JOB_BLE_DISCONNECT = "0016"

ECHOBACK_DATA = b"1234567890"

TRANSFER_GET_X = ord("X")  # 0x58
TRANSFER_GET_Z = ord("Z")  # 0x5A
TRANSFER_GET_PGM = ord("P")  # 0x50
TRANSFER_SEND_PGM = ord("S")  # 0x53

FILENO_REMOTE_DAILY = "1"
FILENO_REMOTE_TIME_CARD = "6"

# Sales file numbers
FILENO_SALES_DAILY = "0000"
FILENO_SALES_FIX = "0001"
FILENO_SALES_FUNC = "0002"
FILENO_SALES_PLU = "0004"
FILENO_SALES_DEPT = "0005"
FILENO_SALES_GROUP = "0006"
FILENO_SALES_HOURLY = "0009"
FILENO_SALES_MONTHLY = "0010"
FILENO_SALES_CLERK = "0011"
FILENO_SALES_CHECKINDEX = "0015"
FILENO_SALES_TIMEATTENDANCE = "0019"
FILENO_SALES_GT = "0020"
FILENO_SALES_EJ = "0048"

FILENO_SETTING_ALL = "0092"


def echo_back_packet() -> bytes:
    return framing.build_job_packet(JOB_ECHO_BACK, ECHOBACK_DATA)


def reg_info_packet() -> bytes:
    return framing.build_job_packet(JOB_REG_INFO_GET, None)


def unsent_data_packet() -> bytes:
    return framing.build_job_packet(JOB_UNSENT_DATA_GET, None)


def sales_confirm_packet() -> bytes:
    return framing.build_job_packet(JOB_SALES_CONFIRM, None)


def ble_disconnect_packet() -> bytes:
    return framing.build_job_packet(JOB_BLE_DISCONNECT, None)


def set_datetime_packet(when: _dt.datetime | None = None) -> bytes:
    when = when or _dt.datetime.now()
    data = when.strftime("%y%m%d").encode("ascii") + b":" + when.strftime("%H%M%S").encode("ascii") + b":"
    return framing.build_job_packet(JOB_SET_DATETIME, data)


def xz_start_packet(report_type: str = "Z", file_no: str = FILENO_REMOTE_DAILY) -> bytes:
    """report_type: 'X' (read totals, no reset) or 'Z' (read + reset totals)."""
    transfer = TRANSFER_GET_Z if report_type.upper() == "Z" else TRANSFER_GET_X
    digit = int(file_no[0]) if file_no else 1
    data = bytes([transfer, ord(":"), digit, ord(":")])
    return framing.build_job_packet(JOB_XZ_START, data)


def deal_setting_packet(comp: int, transfer: int, file_no: str) -> bytes:
    """Request to receive a settings/sales file (jobs 9001/9002/9003 setup step)."""
    payload = file_no.encode("ascii") + b":"
    return framing.build_job_packet2(comp, transfer, payload)
