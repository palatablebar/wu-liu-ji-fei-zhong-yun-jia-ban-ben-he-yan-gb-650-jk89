"""分区裁决纯规则：合同定区、冲突识别、依据分级。

裁决优先级（结算依据，唯一来源）：
1. 收货地址 + 合同分区对照命中 → 合同分区（依据 contract）
2. 地址迟到/为空 → 挂起 ADDR_PENDING，不进正式账单
3. 地址有值但省无法对照/依据不足 → 挂起 ADDR_UNUSABLE
4. 合同分区在运价表无版本 → 挂起 ZONE_NO_CARD

承运商回传分区只作对照证据，永不参与择区；与合同分区不一致记 ZONE_CONFLICT。
"""

from __future__ import annotations

from decimal import Decimal

from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m5_charges import addon, money, zone as zone_price
from app.reasons import sort_codes

# 复核行状态
OPEN = "open"            # 已按合同分区自动出账，待人工复核
CONFIRMED = "confirmed"  # 人工锁定，重跑不得改
HELD = "held"            # 依据不足挂起，不进正式账单

BASIS_CONTRACT = "contract"
BASIS_MANUAL = "manual"


def contract_zone_for(waybill: dict, address: dict | None,
                      mapping: dict[tuple[str, str, str], str]) -> tuple[str, str]:
    """返回 (合同分区, 挂起原因码)。命中时原因码为空。"""
    if address is None:
        return "", "ADDR_PENDING"
    if not address.get("usable"):
        return "", "ADDR_UNUSABLE"
    province = address["province"]
    key = (waybill["carrier"], waybill["service"], province)
    zone = mapping.get(key, "")
    if not zone:
        return "", "ADDR_UNUSABLE"
    return zone, ""


def detect_conflict(carrier_zone: str, contract_zone: str) -> list[str]:
    if not carrier_zone:
        return []
    if carrier_zone == contract_zone:
        return ["ZONE_MATCH"]
    return ["ZONE_CONFLICT"]


def select_rate_card(contract_zone: str, waybill: dict) -> tuple[dict | None, list[str]]:
    index_map = rates.index()
    pickup = rateversion.parse_utc(waybill["pickup_utc"])
    return rates.select_card(
        index_map, waybill["carrier"], waybill["service"], contract_zone, pickup
    )


def bill_amount(waybill: dict, card: dict, billable_kg: Decimal, steps: int) -> dict:
    """按 M5 同一套叠算规则给复核票算应收，保证与主流水线口径一致。"""
    first = zone_price.first_amount(card)
    step_part = zone_price.step_amount(steps, card)
    topup = zone_price.min_topup(first + step_part, card)
    base = money.q2(first + topup)
    remote = addon.remote_amount(card)
    fuel = addon.fuel_amount(base, step_part, card)
    declared = Decimal(waybill["declared_value"])
    insurance = addon.insurance_amount(declared, card)
    amount = money.total([base, step_part, remote, fuel, insurance])
    row_codes = []
    if topup > 0:
        row_codes.append("MIN_CHARGE_APPLIED")
    if remote > 0:
        row_codes.append("ZONE_REMOTE")
    return {
        "base_amount": str(base),
        "step_amount": str(step_part),
        "remote_amount": str(remote),
        "fuel_amount": str(fuel),
        "insurance_amount": str(insurance),
        "total_amount": str(amount),
        "codes": row_codes,
    }


def weight_basis(weight_lines: list[dict]) -> dict[str, dict]:
    return {row["waybill_no"]: row for row in weight_lines}


def build_evidence(waybill: dict, carrier: dict | None, address: dict | None,
                   contract_zone: str, hold_code: str, card: dict | None) -> str:
    """每条证据都带来源与行号，复核页与 CSV 直接可读。"""
    parts = [
        f"采信行 zone={waybill['zone']}@line={waybill['line_no']}",
    ]
    if carrier is None:
        parts.append("回传分区=缺失")
    else:
        parts.append(
            f"回传分区={carrier['carrier_zone']}@carrier_zones.line={carrier['line_no']}"
            f" received={carrier.get('received_at', '')}"
        )
    if address is None:
        parts.append("收货地址=未到(ADDR_PENDING)")
    else:
        parts.append(
            f"地址={address['province']}{address['city']}{address['address']}"
            f"@address_book.line={address['line_no']} received={address.get('received_at', '')}"
        )
    if contract_zone:
        parts.append(f"合同分区={contract_zone}")
    if hold_code:
        parts.append(f"挂起={hold_code}")
    if card is not None:
        parts.append(f"运价版本={card['version_id']}")
    return " | ".join(parts)


def review_codes(conflict_codes: list[str], hold_code: str, billing_codes: list[str],
                 basis: str) -> list[str]:
    codes = list(conflict_codes)
    if hold_code:
        codes.append(hold_code)
    else:
        codes.append("ZONE_BASIS_CONTRACT" if basis == BASIS_CONTRACT else "ZONE_BASIS_MANUAL")
    codes.extend(billing_codes)
    return sort_codes(codes)
