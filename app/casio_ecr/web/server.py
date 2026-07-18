"""FastAPI backend + dashboard for the Casio ECR sales reports.

Pulls sales-confirm snapshots from the SR-S820 over BLE (job 0014, the
one BLE read confirmed working on real hardware) and persists them to
SQLite so daily/monthly reports and trends survive across sessions and
register Z-resets.

Run:  python -m casio_ecr.web.server   (or: uvicorn casio_ecr.web.server:app)
Then open http://127.0.0.1:8770
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..ble_client import CasioBleClient, CasioProtocolError, scan_for_register
from ..protocol import responses, salesfile
from ..storage.db import LINE_KEYS, Database

# Default register address discovered during setup; overridable via settings.
DEFAULT_ADDRESS = "08:00:74:57:30:EF"

app = FastAPI(title="Casio ECR Dashboard")
db = Database()
TEMPLATES = Path(__file__).resolve().parent / "templates"


def _register_address() -> str:
    return db.get_setting("register_address", DEFAULT_ADDRESS) or DEFAULT_ADDRESS


def _sales_confirm_to_dict(sc: responses.SalesConfirm) -> tuple[dict, dict]:
    lines: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for key in LINE_KEYS:
        line = getattr(sc, key)
        lines[key] = {"quantity": line.quantity, "amount": line.amount}
        labels[key] = line.label
    return lines, labels


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/config")
def get_config() -> dict:
    return {
        "register_address": _register_address(),
        "decimals": int(db.get_setting("decimals", "2")),
        "currency_symbol": db.get_setting("currency_symbol", "$"),
    }


@app.post("/api/config")
def set_config(payload: dict) -> dict:
    for key in ("register_address", "decimals", "currency_symbol"):
        if key in payload and payload[key] is not None:
            db.set_setting(key, str(payload[key]))
    return get_config()


async def _fetch_serial(address: str) -> str:
    """Serial is cached after the first successful read. The register replays
    its previous answer if two jobs share one connection, so serial and
    sales-confirm must each use their own fresh connection."""
    cached = db.get_setting("register_serial", "")
    if cached:
        return cached
    try:
        async with CasioBleClient(address) as client:
            info = await client.get_reg_info()
        db.set_setting("register_serial", info.serial_no)
        return info.serial_no
    except Exception:
        return ""


@app.post("/api/pull")
async def pull_snapshot() -> dict:
    """Connect to the register, read current sales totals, persist a snapshot.

    One BLE operation per connection — chaining jobs in a single connection
    makes the register replay its previous reply (see live findings).
    """
    address = _register_address()
    serial = await _fetch_serial(address)
    try:
        async with CasioBleClient(address) as client:
            sc = await client.sales_confirm()
    except CasioProtocolError as e:
        raise HTTPException(status_code=502, detail=f"Register protocol error: {e}")
    except Exception as e:  # BLE/connection failures
        raise HTTPException(status_code=502, detail=f"Could not read register: {e}")

    lines, labels = _sales_confirm_to_dict(sc)
    snap_id = db.add_snapshot(serial, lines, json.dumps(labels, ensure_ascii=False))
    return {"id": snap_id, "serial": serial, "lines": lines, "labels": labels}


@app.get("/api/scan")
async def scan() -> dict:
    try:
        devices = await scan_for_register(timeout=8.0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan failed: {e}")
    return {"devices": [{"address": d.address, "name": d.name} for d in devices]}


@app.get("/api/snapshots")
def snapshots(limit: int = 500) -> dict:
    return {"snapshots": db.list_snapshots(limit=limit)}


@app.get("/api/latest")
def latest() -> dict:
    return {"snapshot": db.latest_snapshot()}


@app.get("/api/reports/daily")
def daily() -> dict:
    return {"series": db.daily_series()}


@app.get("/api/reports/monthly")
def monthly() -> dict:
    return {"series": db.monthly_series()}


# --- Detailed X/Z report ("detalle de movimientos") -----------------------

def _salesfile_to_lines(sf: salesfile.SalesFile) -> list[dict]:
    lines: list[dict] = []
    for b in sf.blocks:
        for r in b.records:
            lines.append({
                "fno": b.fno,
                "fno_label": b.label,
                "rec_no": r.rec_no,
                "name": r.label or f"rec{r.rec_no}",
                "qty": r.qty,
                "amount": r.amount,
            })
    return lines


def _store_report(report_type: str, serial: str, raw: bytes) -> dict:
    sf, was_comp = salesfile.parse_sales_file_auto(raw)
    lines = _salesfile_to_lines(sf)
    rid = db.add_report(report_type, serial, sf.datetime_raw, was_comp, raw, lines)
    return {"id": rid, "was_compressed": was_comp, "blocks": len(sf.blocks), "lines": len(lines)}


@app.post("/api/reports/pull")
async def pull_report(report_type: str = "X") -> dict:
    """Live: trigger an X/Z report over BLE and store its movement detail.

    X (default) is read-only. Z resets the register's totals and is refused
    here unless explicitly requested, to avoid an accidental day-close from
    the dashboard.
    """
    report_type = report_type.upper()
    if report_type not in ("X", "Z"):
        raise HTTPException(status_code=400, detail="report_type must be X or Z")
    if report_type == "Z":
        raise HTTPException(
            status_code=400,
            detail="Z resetea los totales (cierre de dia). Usa el arnes CLI pull_report.py Z --z para eso.",
        )
    address = _register_address()
    serial = await _fetch_serial(address)
    try:
        async with CasioBleClient(address) as client:
            raw = await client.receive_xz_report(report_type=report_type)
    except CasioProtocolError as e:
        raise HTTPException(status_code=502, detail=f"Register protocol error: {e}")
    except Exception as e:  # BLE/connection failures
        raise HTTPException(status_code=502, detail=f"Could not read register: {e}")
    if not raw:
        raise HTTPException(status_code=502, detail="El registro no envio datos del reporte (0 bytes).")
    try:
        return _store_report(report_type, serial, raw)
    except Exception as e:  # parse failure -- keep raw so it can be diagnosed offline
        raise HTTPException(status_code=500, detail=f"Reporte recibido pero no se pudo parsear: {e}")


@app.post("/api/reports/import")
def import_report(payload: dict) -> dict:
    """Offline/safe: parse a previously captured raw .bin (from pull_report.py)
    and store it. No BLE. `payload = {"path": "...", "report_type": "X"}`."""
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="missing 'path'")
    p = Path(path)
    if not p.is_file():
        raise HTTPException(status_code=404, detail=f"file not found: {path}")
    raw = p.read_bytes()
    try:
        return _store_report(payload.get("report_type", "X").upper(),
                             db.get_setting("register_serial", "") or "", raw)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"No se pudo parsear el archivo: {e}")


@app.get("/api/reports/list")
def reports_list(limit: int = 100) -> dict:
    return {"reports": db.list_reports(limit=limit)}


@app.get("/api/reports/detail/{report_id}")
def report_detail(report_id: int) -> dict:
    detail = db.report_detail(report_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="report not found")
    return detail


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8770)


if __name__ == "__main__":
    main()
