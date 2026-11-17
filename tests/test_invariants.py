"""不变量：幂等、守恒、单调、进位幂等、区间不重不漏、顺序无关。"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m1_waybill import service as waybills
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import ratecard
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import rounding, service as weights
from app.modules.m5_charges import money, service as charges
from app.modules.m6_reconcile import service as reconcile


def test_rerun_keeps_rows_and_total(pipeline):
    run_id = pipeline["run_id"]
    before_rows = len(store.charge_lines(run_id))
    before_total = (store.get_run(run_id) or {})["amount_total"]

    again = waybills.import_sample()
    weights.compute_weights(run_id)
    charges.compute_charges(run_id)
    after_total = (store.get_run(run_id) or {})["amount_total"]

    assert again["run_id"] == run_id
    assert len(store.charge_lines(run_id)) == before_rows
    assert after_total == before_total


def test_conservation_of_parts(pipeline):
    amounts = charges.amounts(pipeline["run_id"])
    assert money.conserved(amounts["total"], amounts["parts"].values()) is True


def test_billable_is_monotonic_in_weight():
    step = Decimal("0.5")
    previous = Decimal("0")
    for raw in ("0.4", "0.5", "0.9", "1.6", "3.2", "7.7", "12.0"):
        current = rounding.ceil_to_step(Decimal(raw), step)
        assert current >= previous
        previous = current


def test_ceil_is_idempotent():
    step = Decimal("0.5")
    once = rounding.ceil_to_step(Decimal("20.833"), step)
    twice = rounding.ceil_to_step(once, step)
    assert once == twice == Decimal("21.0")


def test_adjacent_windows_do_not_overlap():
    bucket = rateversion.window_summary(rates.cards())
    for group in bucket:
        covers = [(rateversion.parse_utc(card["effective_from"]),
                   rateversion.parse_utc(card["effective_to"]))
                  for card in group["versions"]]
        for index in range(len(covers) - 1):
            assert covers[index][1] <= covers[index + 1][0]
        assert group["codes"] == []


def test_card_order_does_not_change_pick():
    bucket = ratecard.index_by_key(rates.cards())[("SF", "standard", "Z1")]
    pickup = rateversion.parse_utc("2026-04-05T00:00:00Z")
    forward = rateversion.select(bucket, pickup)[0]["version_id"]
    backward = rateversion.select(list(reversed(bucket)), pickup)[0]["version_id"]
    assert forward == backward == "SF-standard-Z1-v2"


def test_row_account_balances(pipeline):
    accounts = {account["key"]: account for account in
                reconcile.reconcile(pipeline["run_id"])["accounts"]}
    parts = sum(part["value"] for part in accounts["rows"]["parts"])
    assert parts == accounts["rows"]["left_value"]
    assert accounts["rows"]["delta"] == 0


def test_zone_review_rerun_never_changes_confirmed_bills(pipeline):
    """同批重跑不新增账单、不改已确认金额（TRAP-30）。"""
    from app.modules.m8_zone_review import service as zone_service

    run_id = pipeline["run_id"]
    zone_service.reset(run_id)
    zone_service.review_sample(run_id)
    for waybill_no in ("WB-1001", "WB-1005", "WB-1017"):
        zone_service.confirm(run_id, waybill_no)
    snapshot = {
        row["waybill_no"]: (row["settled_zone"], row["total_amount"], row["rate_version_id"])
        for row in store.locked_zone_reviews(run_id)
    }
    for _ in range(3):
        zone_service.review_sample(run_id)
        zone_service.review_sample(run_id, late=True)
    zone_service.review_sample(run_id)
    rerun = {
        row["waybill_no"]: (row["settled_zone"], row["total_amount"], row["rate_version_id"])
        for row in store.locked_zone_reviews(run_id)
    }
    assert rerun == snapshot
    assert len(rerun) == 3
    zone_service.reset(run_id)


def test_zone_review_does_not_mutate_main_pipeline(pipeline):
    """M8 复核独立落表，M5 账单行数与应收合计保持原值。"""
    from app.modules.m8_zone_review import service as zone_service

    run_id = pipeline["run_id"]
    charge_count_before = len(store.charge_lines(run_id))
    total_before = store.get_run(run_id)["amount_total"]
    zone_service.reset(run_id)
    zone_service.review_sample(run_id)
    zone_service.confirm(run_id, "WB-1002")
    zone_service.review_sample(run_id, late=True)
    assert len(store.charge_lines(run_id)) == charge_count_before
    assert store.get_run(run_id)["amount_total"] == total_before
    zone_service.reset(run_id)
