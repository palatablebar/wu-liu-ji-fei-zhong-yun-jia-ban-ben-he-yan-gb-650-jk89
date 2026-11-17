"""M8 分区复核：合同定区优先、冲突留痕、迟到/依据不足挂起、确认锁定与重跑不可变。"""

from __future__ import annotations

import pytest

from app import config, store
from app.errors import DomainError
from app.modules.m8_zone_review import inputs, resolution, service
from app.modules.m8_zone_review.handler import evidence_csv_text, invoice_csv_text


def _sample(name: str) -> bytes:
    return (config.SAMPLE_DIR / name).read_bytes()


@pytest.fixture()
def reviewed(pipeline):
    """主流水线之上跑一次全量分区复核；每用例重置，避免锁定行串扰。"""
    run_id = pipeline["run_id"]
    service.reset(run_id)
    service.review_sample(run_id)
    return run_id


@pytest.fixture()
def late_reviewed(pipeline):
    run_id = pipeline["run_id"]
    service.reset(run_id)
    service.review_sample(run_id, late=True)
    return run_id


# --- 输入解析 ---------------------------------------------------------------

def test_parse_three_inputs():
    carrier, c_problems, c_codes = inputs.parse_carrier_zones(
        _sample(config.SAMPLE_CARRIER_ZONES)
    )
    book, a_problems, _ = inputs.parse_address_book(
        _sample(config.SAMPLE_ADDRESS_BOOK)
    )
    mapping, m_problems = inputs.parse_zone_mapping(
        _sample(config.SAMPLE_ZONE_MAPPING)
    )
    assert not c_problems and not a_problems and not m_problems
    assert carrier["WB-1001"]["carrier_zone"] == "Z2"
    assert book["WB-1001"]["usable"] is True
    assert mapping[("SF", "standard", "上海市")] == "Z1"
    assert c_codes == []


def test_bom_and_gb18030_fallback_marked():
    raw = ("﻿waybill_no,carrier_zone,received_at\n"
           "WB-1,Ｚ１,2026-04-01T20:00:00+08:00\n").encode("gb18030")
    carrier, problems, codes = inputs.parse_carrier_zones(raw)
    assert problems == []
    assert "ENCODING_FALLBACK" in codes
    assert carrier["WB-1"]["carrier_zone"] == "Z1"


def test_arity_and_duplicate_rejected_as_problems():
    raw = b"waybill_no,carrier_zone,received_at\nWB-1,Z1,x\nWB-1,Z2,x\nWB-2,Z1\n"
    _carrier, problems, _codes = inputs.parse_carrier_zones(raw)
    joined = ";".join(problems)
    assert "DUP_CONFLICT" in joined
    assert "ARITY_MISMATCH" in joined


def test_missing_zone_key_recorded():
    raw = b"waybill_no,carrier_zone,received_at\nWB-1,,x\n"
    carrier, problems, _ = inputs.parse_carrier_zones(raw)
    assert "WB-1" not in carrier
    assert any("MISSING_KEY" in p for p in problems)


def test_mapping_duplicate_key_conflict_and_empty_file():
    raw = (
        "carrier,service,province,contract_zone\n"
        "SF,standard,上海市,Z1\nSF,standard,上海市,Z2\n"
    ).encode("utf-8")
    mapping, problems = inputs.parse_zone_mapping(raw)
    assert mapping[("SF", "standard", "上海市")] == "Z1"
    assert any("DUP_CONFLICT" in p for p in problems)

    empty, empty_problems = inputs.parse_zone_mapping(b"carrier,service,province,contract_zone\n")
    assert empty == {}
    assert any("EMPTY_FILE" in p for p in empty_problems)


def test_fullwidth_and_thousands_style_zone_normalized():
    raw = (
        "waybill_no,carrier_zone,received_at\n"
        "WB-9,Ｚ３,2026-04-01T20:00:00+08:00\n"
    ).encode("utf-8")
    carrier, problems, _ = inputs.parse_carrier_zones(raw)
    assert problems == []
    assert carrier["WB-9"]["carrier_zone"] == "Z3"


# --- 裁决规则 ---------------------------------------------------------------

def test_contract_zone_wins_over_carrier_zone(reviewed):
    rows = {r["waybill_no"]: r for r in service.rows(reviewed)}
    wb = rows["WB-1001"]
    assert wb["carrier_zone"] == "Z2"          # 承运商回传
    assert wb["contract_zone"] == "Z1"         # 合同按上海地址定区
    assert wb["settled_zone"] == "Z1"          # 结算按合同，不采信回传
    assert wb["basis"] == resolution.BASIS_CONTRACT
    assert "ZONE_CONFLICT" in wb["codes"]
    assert "ZONE_BASIS_CONTRACT" in wb["codes"]


def test_matching_zones_marked_match_not_conflict(reviewed):
    rows = {r["waybill_no"]: r for r in service.rows(reviewed)}
    wb = rows["WB-1002"]
    assert wb["carrier_zone"] == wb["contract_zone"] == "Z1"
    assert "ZONE_MATCH" in wb["codes"]
    assert "ZONE_CONFLICT" not in wb["codes"]


def test_conflict_count_and_evidence_chain(reviewed):
    summary = service.summarize(reviewed)
    assert summary["conflicts"] == 6
    rows = {r["waybill_no"]: r for r in service.rows(reviewed)}
    evidence = rows["WB-1001"]["evidence"]
    assert "采信行 zone=Z1" in evidence
    assert "回传分区=Z2@carrier_zones.line=2" in evidence
    assert "地址=上海市上海市浦东新区" in evidence
    assert "合同分区=Z1" in evidence
    assert "运价版本=SF-standard-Z1-v2" in evidence


def test_carrier_zone_never_used_for_billing(reviewed):
    """即使回传分区与采信行不同，结算分区也只能来自合同对照。"""
    rows = {r["waybill_no"]: r for r in service.rows(reviewed)}
    for row in rows.values():
        if row["status"] == resolution.OPEN:
            assert row["settled_zone"] == row["contract_zone"]
            assert row["basis"] == resolution.BASIS_CONTRACT
    # WB-1016 回传 Z1、采信 Z2、合同 Z2：合同与回传冲突，按 Z2 出账
    wb = rows["WB-1016"]
    assert wb["carrier_zone"] == "Z1" and wb["contract_zone"] == "Z2"
    assert wb["settled_zone"] == "Z2"
    assert "ZONE_CONFLICT" in wb["codes"]


def test_late_address_ticket_is_held_not_billed(late_reviewed):
    summary = service.summarize(late_reviewed)
    assert set(summary["hold_codes"]) == {"ADDR_PENDING", "ADDR_UNUSABLE"}
    holds = {h["waybill_no"]: h["code"] for h in service.holds(late_reviewed)}
    assert holds["WB-1003"] == "ADDR_PENDING"
    assert holds["WB-1005"] == "ADDR_PENDING"
    assert holds["WB-1018"] == "ADDR_PENDING"
    rows = {r["waybill_no"]: r for r in service.rows(late_reviewed)}
    assert rows["WB-1003"]["status"] == resolution.HELD
    assert rows["WB-1003"]["settled_zone"] == ""
    assert rows["WB-1003"]["total_amount"] == "0.00"


def test_insufficient_address_evidence_held(reviewed):
    rows = {r["waybill_no"]: r for r in service.rows(reviewed)}
    wb = rows["WB-1019"]
    assert wb["status"] == resolution.HELD
    assert "ADDR_UNUSABLE" in wb["codes"]
    holds = {h["waybill_no"] for h in service.holds(reviewed)}
    assert "WB-1019" in holds


def test_held_tickets_never_in_official_invoice(reviewed):
    invoice = service.confirmed_invoice_rows(reviewed)
    assert invoice == []  # 未人工确认前正式账单为空
    summary = service.summarize(reviewed)
    assert summary["billed_total"] == "0.00"
    assert summary["open"] == 14
    assert summary["held"] == 1


def test_billing_matches_main_pipeline_when_zone_unchanged(reviewed):
    """同一票同一分区，M8 复核应收必须等于 M5 主流水线口径；
    合同改区的票按合同价另算，正是冲突处理的目的。"""
    main = {r["waybill_no"]: r["total_amount"] for r in store.charge_lines(reviewed)}
    waybills = {r["waybill_no"]: r for r in store.waybills(reviewed)}
    for row in service.rows(reviewed):
        if row["status"] != resolution.OPEN:
            continue
        if row["settled_zone"] == waybills[row["waybill_no"]]["zone"]:
            assert row["total_amount"] == main[row["waybill_no"]], row["waybill_no"]


def test_contract_rezone_rebills_on_contract_rate(reviewed):
    """WB-1006 采信分区 Z1，合同按山东地址定 Z2，应收必须按 Z2 价重算而非沿用旧账。"""
    row = next(r for r in service.rows(reviewed) if r["waybill_no"] == "WB-1006")
    assert row["carrier_zone"] == "Z2" and row["contract_zone"] == "Z2"
    assert row["settled_zone"] == "Z2"
    main = next(c["total_amount"] for c in store.charge_lines(reviewed)
                if c["waybill_no"] == "WB-1006")
    assert main == "23.05"                      # 旧口径（采信 Z1）
    assert row["total_amount"] == "26.96"       # 合同 Z2 重算
    assert "ZONE_CONFLICT" not in row["codes"]  # 回传与合同同为 Z2，但与采信行不同


# --- 人工确认与锁定 ----------------------------------------------------------

def test_confirm_locks_and_enters_official_invoice(reviewed):
    result = service.confirm(reviewed, "WB-1001")
    assert result["locked"] == 1
    assert result["settled_zone"] == "Z1"
    assert result["total_amount"] == "42.67"
    row = store.zone_review(reviewed, "WB-1001")
    assert row["status"] == resolution.CONFIRMED
    # 确认时未改分区，保留合同依据；人工锁定只体现在 locked/decided_at
    assert row["basis"] == resolution.BASIS_CONTRACT
    assert "ZONE_BASIS_CONTRACT" in row["codes"]
    invoice = {i["waybill_no"]: i for i in service.confirmed_invoice_rows(reviewed)}
    assert set(invoice) == {"WB-1001"}
    assert invoice["WB-1001"]["total_amount"] == "42.67"
    assert service.summarize(reviewed)["confirmed_total"] == "42.67"


def test_manual_zone_override_rebills_and_locks(reviewed):
    result = service.confirm(reviewed, "WB-1002", "Z2")
    assert result["settled_zone"] == "Z2"
    assert result["rate_version_id"] == "SF-standard-Z2-v2"
    row = store.zone_review(reviewed, "WB-1002")
    assert row["basis"] == resolution.BASIS_MANUAL
    assert "ZONE_BASIS_MANUAL" in row["codes"]


def test_confirm_without_override_keeps_contract_basis(reviewed):
    service.confirm(reviewed, "WB-1002")
    row = store.zone_review(reviewed, "WB-1002")
    assert row["settled_zone"] == "Z1"
    assert row["basis"] == resolution.BASIS_CONTRACT
    assert "ZONE_BASIS_CONTRACT" in row["codes"]
    assert "ZONE_BASIS_MANUAL" not in row["codes"]


def test_override_to_zone_without_card_rejected(reviewed):
    # WB-1003 是 JD/standard，Z4 虽存在但本票合同/择版路径之外，构造无版本场景：
    # JD/standard 在提货时刻没有 Z9 分区卡
    with pytest.raises(DomainError) as exc:
        service.confirm(reviewed, "WB-1003", "Z9")
    assert exc.value.code == "ZONE_NO_CARD"
    assert store.zone_review(reviewed, "WB-1003")["locked"] == 0


def test_held_ticket_cannot_be_confirmed(reviewed):
    with pytest.raises(DomainError) as exc:
        service.confirm(reviewed, "WB-1019")
    assert exc.value.code == "ZONE_BASIS_MISSING"


def test_locked_ticket_cannot_change(reviewed):
    service.confirm(reviewed, "WB-1005")
    with pytest.raises(DomainError) as exc:
        service.confirm(reviewed, "WB-1005", "Z4")
    assert exc.value.code == "ZONE_REVIEW_LOCKED"
    row = store.zone_review(reviewed, "WB-1005")
    assert row["settled_zone"] == "Z3"
    assert row["total_amount"] == "697.05"


# --- 重跑幂等：不新增账单、不改已确认金额 -----------------------------------

def test_rerun_same_batch_keeps_locked_amounts_and_no_new_bills(reviewed):
    service.confirm(reviewed, "WB-1001")
    service.confirm(reviewed, "WB-1005")
    before = {
        r["waybill_no"]: (r["settled_zone"], r["total_amount"], r["decided_at"])
        for r in store.locked_zone_reviews(reviewed)
    }
    service.review_sample(reviewed)
    after = {
        r["waybill_no"]: (r["settled_zone"], r["total_amount"], r["decided_at"])
        for r in store.locked_zone_reviews(reviewed)
    }
    assert before == after
    summary = service.summarize(reviewed)
    assert summary["confirmed"] == 2
    assert summary["confirmed_total"] == "739.72"


def test_rerun_with_late_inputs_still_keeps_locked(reviewed):
    service.confirm(reviewed, "WB-1001")
    locked_before = store.zone_review(reviewed, "WB-1001")
    service.review_sample(reviewed, late=True)
    locked_after = store.zone_review(reviewed, "WB-1001")
    assert locked_after["locked"] == 1
    assert locked_after["total_amount"] == locked_before["total_amount"]
    assert locked_after["settled_zone"] == locked_before["settled_zone"]
    # 迟到批次未删该票地址证据，确认时的地址仍可追溯
    assert locked_after["address_text"] == locked_before["address_text"]
    summary = service.summarize(reviewed)
    assert summary["confirmed"] == 1
    assert summary["confirmed_total"] == "42.67"


def test_locked_ticket_survives_when_address_disappears_in_later_batch(pipeline):
    """已确认票即使在后续批次里地址迟到，也不挂起、不改账。"""
    run_id = pipeline["run_id"]
    service.reset(run_id)
    service.review_sample(run_id)
    service.confirm(run_id, "WB-1005")  # 迟到地址簿里没有 WB-1005
    before = store.zone_review(run_id, "WB-1005")
    service.review_sample(run_id, late=True)
    after = store.zone_review(run_id, "WB-1005")
    assert after["status"] == resolution.CONFIRMED
    assert after["locked"] == 1
    assert after["total_amount"] == before["total_amount"]
    assert after["settled_zone"] == "Z3"
    assert "广东省广州市" in after["address_text"]
    service.reset(run_id)


def test_open_tickets_remain_outside_official_total_across_reruns(reviewed):
    service.confirm(reviewed, "WB-1017")
    first = service.summarize(reviewed)
    service.review_sample(reviewed)
    second = service.summarize(reviewed)
    assert first["confirmed_total"] == second["confirmed_total"]
    assert second["open_total"] != "0.00"
    assert second["billed_total"] == second["confirmed_total"]


# --- 导出 -------------------------------------------------------------------

def test_evidence_csv_covers_every_ticket(reviewed):
    body = evidence_csv_text(reviewed)
    lines = body.strip().splitlines()
    assert lines[0].split(",")[0] == "waybill_no"
    assert len(lines) == 16  # 表头 + 15 票
    assert "WB-1019" in body and "ADDR_UNUSABLE" in body


def test_invoice_csv_contains_confirmed_only(reviewed):
    service.confirm(reviewed, "WB-1001")
    service.confirm(reviewed, "WB-1002", "Z2")
    body = invoice_csv_text(reviewed)
    lines = body.strip().splitlines()
    assert len(lines) == 3  # 表头 + 2 票已确认
    assert "WB-1019" not in body  # 挂起票不混入
    assert "WB-1003" not in body  # 待确认票不混入


# --- 输入结构异常整批拒绝 ----------------------------------------------------

def test_malformed_input_rejects_whole_batch(pipeline):
    run_id = pipeline["run_id"]
    service.reset(run_id)
    bad = b"waybill_no,carrier_zone,received_at\nWB-1,Z1\n"
    with pytest.raises(DomainError) as exc:
        service.review_batch(
            run_id,
            bad,
            _sample(config.SAMPLE_ADDRESS_BOOK),
            _sample(config.SAMPLE_ZONE_MAPPING),
        )
    assert exc.value.code == "ZONE_INPUT_INVALID"
    # 整批拒绝后不留复核结果、不留挂起
    assert service.rows(run_id) == []
    assert service.holds(run_id) == []


def test_review_requires_weights(pipeline):
    # 一个没有计费重的新运行
    from app.modules.m1_waybill import service as waybills
    from fixtures import samples
    raw = samples.csv_bytes([samples.row("WB-7001")])
    ingest = waybills.import_bytes(raw, "zone_no_weights.csv")
    with pytest.raises(DomainError) as exc:
        service.review_sample(ingest["run_id"])
    assert exc.value.code == "NO_WEIGHTS"


def test_partial_upload_rejected_at_handler():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.post(
            "/api/zone-review/run",
            files={"carrier_file": ("c.csv", b"waybill_no,carrier_zone,received_at\n", "text/csv")},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "ZONE_INPUT_INVALID"


def test_missing_header_column_rejects_whole_batch(pipeline):
    run_id = pipeline["run_id"]
    service.reset(run_id)
    bad_address = (
        "waybill_no,consignee,city,address,received_at\n"
        "WB-1,张三,上海,路1号,x\n"
    ).encode("utf-8")
    with pytest.raises(DomainError) as exc:
        service.review_batch(
            run_id,
            _sample(config.SAMPLE_CARRIER_ZONES),
            bad_address,
            _sample(config.SAMPLE_ZONE_MAPPING),
        )
    assert exc.value.code == "ZONE_INPUT_INVALID"
    assert service.rows(run_id) == []
    service.reset(run_id)
