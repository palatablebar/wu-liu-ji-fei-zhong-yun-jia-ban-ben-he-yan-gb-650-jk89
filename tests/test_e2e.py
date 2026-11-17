"""端到端：样例脏运单 → 采信 → 计费重 → 账单 → 对账 → 导出，以及页面可用。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from fixtures.expected import AMOUNT_TOTAL, ROWS_IN, ROWS_OK, ROWS_QUARANTINED

from app.main import app
from app.modules.m6_reconcile import service as reconcile


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_sample_flow_end_to_end(pipeline):
    report = reconcile.reconcile(pipeline["run_id"])
    assert report["rows_in"] == ROWS_IN
    assert report["rows_ok"] == ROWS_OK
    assert report["amount_total"] == AMOUNT_TOTAL
    assert report["verdict"]["balanced"] is True


def test_web_pages_return_200(client):
    pages = ["/", "/waybills", "/weights", "/charges", "/zone-review",
             "/reconcile", "/export"]
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        assert "freightrate" in response.text
    assert len(pages) == 7


def test_api_health(client):
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["display_tz"] == "Asia/Shanghai"
    assert payload["rate_set_version"] == 3


def test_export_endpoints_are_not_empty(client):
    invoice = client.get("/api/export/invoice.csv")
    assert invoice.status_code == 200
    assert invoice.text.strip().splitlines()[0].startswith("waybill_no")

    evidence = client.get("/api/export/evidence.csv")
    assert evidence.status_code == 200
    assert "line_no" in evidence.text.splitlines()[0]

    manifest = client.get("/api/export/manifest.json")
    assert manifest.status_code == 200
    doc = json.loads(manifest.text)
    assert doc["rows"]["input"] == ROWS_IN
    assert len(doc["input_sha256"]) == 64


def test_reconcile_endpoint_balanced(client):
    report = client.get("/api/reconcile").json()["report"]
    assert report["verdict"]["balanced"] is True
    assert report["total"] == AMOUNT_TOTAL


def test_quarantine_endpoints(client):
    payload = client.get("/api/quarantine").json()
    assert payload["metrics"]["quarantined_lines"] == ROWS_QUARANTINED
    body = client.get("/api/quarantine.csv")
    assert body.status_code == 200
    assert body.text.strip().splitlines()[0].startswith("line_no")


def test_zone_review_flow_end_to_end(client):
    from app.modules.m8_zone_review import service as zone_service
    run = client.get("/api/health").json()["run_id"]
    zone_service.reset(run)
    # 全量复核：14 票待确认、1 票地址依据不足挂起、6 票冲突
    started = client.post("/api/zone-review/run", data={"scenario": "full"}).json()
    assert started["ok"] is True
    summary = started["result"]
    assert summary["held"] == 1
    assert summary["conflicts"] == 6
    assert summary["confirmed_total"] == "0.00"

    reviews = client.get("/api/zone-review").json()
    conflict = next(r for r in reviews["rows"] if r["waybill_no"] == "WB-1001")
    assert conflict["carrier_zone"] == "Z2"
    assert conflict["settled_zone"] == "Z1"
    assert "ZONE_CONFLICT" in conflict["codes"]

    # 人工确认后才进正式账单
    confirmed = client.post(
        "/api/zone-review/confirm",
        json={"run_id": run, "waybill_no": "WB-1001"},
    ).json()["result"]
    assert confirmed["total_amount"] == "42.67"

    invoice = client.get("/api/zone-review/invoice.csv").text
    assert "WB-1001" in invoice and "WB-1019" not in invoice
    evidence = client.get("/api/zone-review/evidence.csv").text
    assert len(evidence.strip().splitlines()) == 16  # 表头 + 15 票全部留痕

    # 同批重跑：已确认票金额不变、不新增账单
    client.post("/api/zone-review/run", data={"scenario": "full"})
    again = client.get("/api/zone-review").json()["summary"]
    assert again["confirmed"] == 1
    assert again["confirmed_total"] == "42.67"

    # 迟到情景：挂起增多，已确认金额仍然不动
    late = client.post("/api/zone-review/run", data={"scenario": "late"}).json()["result"]
    assert late["held"] == 4
    assert late["confirmed_total"] == "42.67"
    assert "ADDR_PENDING" in late["hold_codes"]
    zone_service.reset(run)


def test_zone_review_locked_ticket_rejects_change(client):
    from app.modules.m8_zone_review import service as zone_service
    run = client.get("/api/health").json()["run_id"]
    zone_service.reset(run)
    client.post("/api/zone-review/run", data={"scenario": "full"})
    client.post("/api/zone-review/confirm", json={"waybill_no": "WB-1005"})
    response = client.post(
        "/api/zone-review/confirm", json={"waybill_no": "WB-1005", "settled_zone": "Z4"}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ZONE_REVIEW_LOCKED"
    zone_service.reset(run)
