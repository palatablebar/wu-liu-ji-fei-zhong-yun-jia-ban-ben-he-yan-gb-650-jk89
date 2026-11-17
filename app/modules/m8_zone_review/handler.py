"""M8 入口：分区复核页、三源提交、人工确认、挂起与证据查询。"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app import config, store, web
from app.errors import DomainError
from app.modules.m8_zone_review import service

router = APIRouter(tags=["zone_review"])


@router.get("/zone-review", response_class=HTMLResponse)
def page_zone_review(request: Request):
    context = web.shell(request, active="zone_review")
    run_id = context["run_id"]
    context.update({
        "reviews": service.rows(run_id) if run_id else [],
        "holds": service.holds(run_id) if run_id else [],
        "summary": service.summarize(run_id) if run_id else {},
        "samples": {
            "carrier": config.SAMPLE_CARRIER_ZONES,
            "carrier_late": config.SAMPLE_CARRIER_ZONES_LATE,
            "address": config.SAMPLE_ADDRESS_BOOK,
            "address_partial": config.SAMPLE_ADDRESS_BOOK_PARTIAL,
            "mapping": config.SAMPLE_ZONE_MAPPING,
        },
    })
    return web.page("zone_review.html", context)


@router.get("/api/zone-review")
def api_reviews(run: str = ""):
    run_id = run or store.latest_run_id()
    return {
        "ok": True,
        "run_id": run_id,
        "summary": service.summarize(run_id) if run_id else {},
        "rows": service.rows(run_id),
        "holds": service.holds(run_id),
    }


@router.post("/api/zone-review/run")
async def api_run(
    run: str = "",
    carrier_file: UploadFile | None = File(default=None),
    address_file: UploadFile | None = File(default=None),
    mapping_file: UploadFile | None = File(default=None),
    scenario: str = Form(default="full"),
):
    run_id = run or store.latest_run_id()
    files_present = [
        f is not None and bool(f.filename)
        for f in (carrier_file, address_file, mapping_file)
    ]
    if any(files_present) and not all(files_present):
        raise DomainError(
            "ZONE_INPUT_INVALID",
            "回传分区、地址簿、分区对照三个文件必须同时上传，否则请改用样例情景",
            400,
        )
    if all(files_present):
        carrier_raw = await carrier_file.read()
        address_raw = await address_file.read()
        mapping_raw = await mapping_file.read()
        result = service.review_batch(run_id, carrier_raw, address_raw, mapping_raw)
    else:
        result = service.review_sample(run_id, late=(scenario == "late"))
    return {"ok": True, "result": result}


@router.post("/api/zone-review/confirm")
async def api_confirm(request: Request):
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 空 body/非 JSON 按空载荷处理
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    run_id = payload.get("run_id") or store.latest_run_id()
    waybill_no = (payload.get("waybill_no") or "").strip()
    settled_zone = (payload.get("settled_zone") or "").strip() or None
    if not waybill_no:
        raise DomainError("MISSING_KEY", "waybill_no 不能为空", 400)
    return {"ok": True, "result": service.confirm(run_id, waybill_no, settled_zone)}


@router.get("/api/zone-review/evidence.csv")
def api_evidence_csv(run: str = ""):
    run_id = run or store.latest_run_id()
    body = evidence_csv_text(run_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="zone_review_evidence.csv"'},
    )


@router.get("/api/zone-review/invoice.csv")
def api_invoice_csv(run: str = ""):
    run_id = run or store.latest_run_id()
    body = invoice_csv_text(run_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="zone_review_invoice.csv"'},
    )


EVIDENCE_HEADER = [
    "waybill_no", "status", "carrier_zone", "contract_zone", "settled_zone",
    "basis", "address_text", "address_source", "address_received_utc",
    "rate_version_id", "total_amount", "locked", "decided_at", "codes", "evidence",
]

INVOICE_HEADER = [
    "waybill_no", "carrier", "service", "carrier_zone", "contract_zone",
    "settled_zone", "basis", "rate_version_id", "total_amount",
    "decided_at", "codes", "address_text", "address_source",
]


def evidence_csv_text(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EVIDENCE_HEADER)
    for row in service.rows(run_id):
        writer.writerow([row.get(key, "") for key in EVIDENCE_HEADER])
    return buffer.getvalue()


def invoice_csv_text(run_id: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INVOICE_HEADER)
    for row in service.confirmed_invoice_rows(run_id):
        writer.writerow([row.get(key, "") for key in INVOICE_HEADER])
    return buffer.getvalue()
