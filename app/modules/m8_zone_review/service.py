"""M8 编排：三源输入 → 逐票分区裁决 → 挂起隔离 → 复核落账（锁定不可变）。

正式结果只收 status=confirmed 的票；open 票是系统按合同分区自动出账、
等待人工确认的结果，金额已算好但不计入正式应收合计。held 票完全不进账单。
"""

from __future__ import annotations

from decimal import Decimal

from app import config, manifest, store
from app.errors import DomainError
from app.modules.m4_weights import billable
from app.modules.m8_zone_review import inputs, resolution
from app.reasons import is_block, sort_codes

STAGE = "zone_review"


def _sample(name: str) -> bytes:
    path = config.SAMPLE_DIR / name
    if not path.exists():
        raise DomainError("SAMPLE_MISSING", f"样例文件不存在：{name}", 404)
    return path.read_bytes()


def _load_inputs(carrier_raw: bytes, address_raw: bytes, mapping_raw: bytes) -> dict:
    carrier_zones, carrier_problems, carrier_codes = inputs.parse_carrier_zones(carrier_raw)
    address_book, address_problems, address_codes = inputs.parse_address_book(address_raw)
    zone_mapping, mapping_problems = inputs.parse_zone_mapping(mapping_raw)
    problems = carrier_problems + address_problems + mapping_problems
    blocking = [p for p in problems if is_block(p.split(":", 2)[1])]
    if blocking:
        raise DomainError(
            "ZONE_INPUT_INVALID",
            "分区复核输入结构异常，已拒绝整批：" + ";".join(blocking[:5]),
            422,
        )
    return {
        "carrier_zones": carrier_zones,
        "address_book": address_book,
        "zone_mapping": zone_mapping,
        "codes": sort_codes(carrier_codes + address_codes),
        "problems": problems,
    }


def _one_review(waybill: dict, loaded: dict, weights: dict[str, dict],
                locked: dict[str, dict]) -> dict:
    waybill_no = waybill["waybill_no"]
    carrier = loaded["carrier_zones"].get(waybill_no)
    address = loaded["address_book"].get(waybill_no)
    carrier_zone = carrier["carrier_zone"] if carrier else ""

    contract_zone, hold_code = resolution.contract_zone_for(
        waybill, address, loaded["zone_mapping"]
    )
    conflict_codes = resolution.detect_conflict(carrier_zone, contract_zone)

    card = None
    amount = Decimal("0.00")
    version_id = ""
    billing_codes: list[str] = []
    status = resolution.HELD if hold_code else resolution.OPEN
    basis = ""

    if not hold_code:
        card, rate_codes = resolution.select_rate_card(contract_zone, waybill)
        if card is None:
            hold_code = "ZONE_NO_CARD"
            status = resolution.HELD
        else:
            weight = weights.get(waybill_no)
            if weight is None or weight.get("source") == billable.UNBILLED:
                hold_code = "ZONE_NO_CARD"
                status = resolution.HELD
            else:
                billing = resolution.bill_amount(
                    waybill, card,
                    Decimal(weight["billable_kg"]), int(weight["steps"]),
                )
                amount = Decimal(billing["total_amount"])
                version_id = card["version_id"]
                billing_codes = billing["codes"] + list(rate_codes)
                basis = resolution.BASIS_CONTRACT

    evidence = resolution.build_evidence(
        waybill, carrier, address, contract_zone, hold_code, card
    )
    codes = resolution.review_codes(conflict_codes, hold_code, billing_codes, basis)

    review = {
        "waybill_no": waybill_no,
        "carrier_zone": carrier_zone,
        "contract_zone": contract_zone,
        "settled_zone": contract_zone if not hold_code else "",
        "basis": basis,
        "status": status,
        "address_text": (
            f"{address['province']}{address['city']}{address['address']}"
            if address else ""
        ),
        "address_source": (
            f"address_book.line={address['line_no']}" if address else ""
        ),
        "address_received_utc": address.get("received_at", "") if address else "",
        "evidence": evidence,
        "rate_version_id": version_id,
        "total_amount": str(amount) if not hold_code else "0.00",
        "codes": ",".join(codes),
        "locked": 0,
        "decided_at": "",
        "_line_no": waybill["line_no"],
        "_hold_code": hold_code,
        "_raw_evidence": evidence,
    }

    # 已确认票：重跑时保留原分区、金额与锁定状态，不允许改账；
    # 地址即使在新一批输入里迟到，也沿用确认时落库的地址证据，保证可追溯不中断
    previous = locked.get(waybill_no)
    if previous is not None:
        review["settled_zone"] = previous["settled_zone"]
        review["basis"] = previous["basis"]
        review["status"] = resolution.CONFIRMED
        review["rate_version_id"] = previous["rate_version_id"]
        review["total_amount"] = previous["total_amount"]
        review["codes"] = previous["codes"]
        review["locked"] = 1
        review["decided_at"] = previous["decided_at"]
        if previous["address_text"]:
            review["address_text"] = previous["address_text"]
            review["address_source"] = previous["address_source"]
            review["address_received_utc"] = previous["address_received_utc"]
            review["evidence"] = review["evidence"] + " | 已确认沿用:" + previous["address_text"]
    return review


def review_batch(run_id: str, carrier_raw: bytes, address_raw: bytes,
                 mapping_raw: bytes) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    waybills = store.waybills(run_id)
    if not waybills:
        raise DomainError("NO_ROWS", "该运行没有可计费的运单", 409)
    weight_rows = store.weight_lines(run_id)
    if not weight_rows:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重，无法给分区复核计价", 409)

    loaded = _load_inputs(carrier_raw, address_raw, mapping_raw)
    weights = resolution.weight_basis(weight_rows)
    locked = {row["waybill_no"]: row for row in store.locked_zone_reviews(run_id)}

    reviews = [_one_review(row, loaded, weights, locked) for row in waybills]
    holds = [
        {
            "line_no": item["_line_no"],
            "waybill_no": item["waybill_no"],
            "code": item["_hold_code"],
            "detail": item["evidence"],
            "raw": item["_raw_evidence"],
        }
        for item in reviews
        if item["_hold_code"]
    ]

    persisted = [{k: v for k, v in item.items() if not k.startswith("_")} for item in reviews]
    store.replace_zone_reviews(run_id, persisted)
    store.replace_zone_holds(run_id, holds)
    now = manifest.utc_now_iso()
    store.add_stage(run_id, STAGE, now)
    return summarize(run_id)


def review_sample(run_id: str, *, late: bool = False) -> dict:
    carrier_name = config.SAMPLE_CARRIER_ZONES_LATE if late else config.SAMPLE_CARRIER_ZONES
    address_name = (
        config.SAMPLE_ADDRESS_BOOK_PARTIAL if late else config.SAMPLE_ADDRESS_BOOK
    )
    return review_batch(
        run_id,
        _sample(carrier_name),
        _sample(address_name),
        _sample(config.SAMPLE_ZONE_MAPPING),
    )


def confirm(run_id: str, waybill_no: str, settled_zone: str | None = None) -> dict:
    """人工确认：可改定分区（必须在运价表有版本），锁定后重跑不可变。"""
    review = store.zone_review(run_id, waybill_no)
    if review is None:
        raise DomainError("REVIEW_NOT_FOUND", f"复核票不存在：{waybill_no}", 404)
    if review["locked"]:
        raise DomainError(
            "ZONE_REVIEW_LOCKED",
            f"{waybill_no} 已确认锁定，金额 {review['total_amount']} 不得修改",
            409,
        )
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    waybill = waybills.get(waybill_no)
    if waybill is None:
        raise DomainError("REVIEW_NOT_FOUND", f"运单不存在：{waybill_no}", 404)

    target_zone = (settled_zone or review["contract_zone"] or "").strip().upper()
    if not target_zone:
        raise DomainError("ZONE_BASIS_MISSING", "没有可确认的结算分区", 409)
    # 确认时未改合同分区 → 保留合同依据；人工改定分区 → 记人工裁定
    basis = (
        resolution.BASIS_CONTRACT
        if target_zone == review["contract_zone"]
        else resolution.BASIS_MANUAL
    )
    basis_code = "ZONE_BASIS_CONTRACT" if basis == resolution.BASIS_CONTRACT else "ZONE_BASIS_MANUAL"

    card, _codes = resolution.select_rate_card(target_zone, waybill)
    if card is None:
        raise DomainError(
            "ZONE_NO_CARD",
            f"{waybill_no} 分区 {target_zone} 在运价表无可用版本，不能确认",
            409,
        )
    weights = {row["waybill_no"]: row for row in store.weight_lines(run_id)}
    weight = weights.get(waybill_no)
    if weight is None or weight.get("source") == billable.UNBILLED:
        raise DomainError("NO_WEIGHTS", f"{waybill_no} 缺计费重依据，不能确认", 409)

    billing = resolution.bill_amount(
        waybill, card, Decimal(weight["billable_kg"]), int(weight["steps"])
    )
    manual_codes = sort_codes(
        [c for c in review["codes"].split(",") if c] + [basis_code]
    )
    now = manifest.utc_now_iso()
    store.lock_zone_review(
        run_id, waybill_no, target_zone, basis,
        card["version_id"], billing["total_amount"], ",".join(manual_codes), now,
    )
    return {
        "run_id": run_id,
        "waybill_no": waybill_no,
        "settled_zone": target_zone,
        "rate_version_id": card["version_id"],
        "total_amount": billing["total_amount"],
        "locked": 1,
        "decided_at": now,
    }


def rows(run_id: str) -> list[dict]:
    return store.zone_reviews(run_id) if run_id else []


def holds(run_id: str) -> list[dict]:
    return store.zone_holds(run_id) if run_id else []


def summarize(run_id: str) -> dict:
    items = rows(run_id)
    hold_items = holds(run_id)
    confirmed = [i for i in items if i["status"] == resolution.CONFIRMED]
    open_items = [i for i in items if i["status"] == resolution.OPEN]
    held = [i for i in items if i["status"] == resolution.HELD]
    conflicts = [i for i in items if "ZONE_CONFLICT" in (i["codes"] or "")]
    confirmed_total = Decimal("0.00")
    for item in confirmed:
        confirmed_total += Decimal(item["total_amount"] or "0")
    open_total = sum((Decimal(i["total_amount"] or "0") for i in open_items), Decimal("0.00"))
    return {
        "run_id": run_id,
        "total": len(items),
        "confirmed": len(confirmed),
        "open": len(open_items),
        "held": len(held),
        "conflicts": len(conflicts),
        "hold_codes": sorted({h["code"] for h in hold_items}),
        "confirmed_total": str(confirmed_total),
        "open_total": str(open_total),
        "billed_total": str(confirmed_total),
        "hold_lines": sorted({h["line_no"] for h in hold_items}),
    }


def confirmed_invoice_rows(run_id: str) -> list[dict]:
    """正式账单只放已确认票；open/held 一律不混入。"""
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    out = []
    for item in store.locked_zone_reviews(run_id):
        waybill = waybills.get(item["waybill_no"], {})
        out.append({
            "waybill_no": item["waybill_no"],
            "carrier": waybill.get("carrier", ""),
            "service": waybill.get("service", ""),
            "carrier_zone": item["carrier_zone"],
            "contract_zone": item["contract_zone"],
            "settled_zone": item["settled_zone"],
            "basis": item["basis"],
            "address_text": item["address_text"],
            "address_source": item["address_source"],
            "rate_version_id": item["rate_version_id"],
            "total_amount": item["total_amount"],
            "decided_at": item["decided_at"],
            "codes": item["codes"],
            "evidence": item["evidence"],
        })
    return out


def reset(run_id: str) -> None:
    """清空某运行的复核结果与挂起（含锁定行），仅用于换情景重开复核。"""
    store.clear_zone_reviews(run_id)
    store.clear_zone_holds(run_id)
    run = store.get_run(run_id)
    if run:
        parts = [p for p in (run.get("stages") or "").split(",") if p and p != STAGE]
        store.set_stages(run_id, ",".join(parts))
