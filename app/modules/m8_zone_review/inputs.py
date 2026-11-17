"""M8 输入解析：承运商回传分区、合同地址簿、地址→分区对照表。

三份输入都是独立 CSV，自带列契约与小解码，不依赖 M1 采信模块。
解析只做结构诊断与归一，不做业务裁决，裁决集中在 service.py。
"""

from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field

from app import config
from app.modules.m1_waybill import decode

CARRIER_HEADER = ("waybill_no", "carrier_zone", "received_at")
ADDRESS_HEADER = ("waybill_no", "consignee", "province", "city", "address", "received_at")
MAPPING_HEADER = ("carrier", "service", "province", "contract_zone")

_SENTINELS = config.SENTINELS


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def _is_blank(text: str) -> bool:
    return _nfkc(text).lower() in _SENTINELS


@dataclass
class Sheet:
    header: list[str]
    rows: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    line_of: dict[str, int] = field(default_factory=dict)


def _read_text(raw: bytes) -> tuple[str, list[str]]:
    codes: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        codes.append("BOM_STRIPPED")
    text, file_codes = decode.decode_bytes(raw)
    codes.extend(file_codes)
    return decode.normalize_newlines(text), codes


def parse_sheet(raw: bytes, expected: tuple[str, ...]) -> Sheet:
    """通用 CSV 解析：严格表头、缺列记问题、全角归一、重复运单记行号。"""
    text, codes = _read_text(raw)
    sheet = Sheet(header=[], codes=codes)
    reader = csv.reader(io.StringIO(text, newline=""))
    for cells in reader:
        if not cells or not any((c or "").strip() for c in cells):
            continue
        if not sheet.header:
            sheet.header = [_nfkc(c).lower() for c in cells]
            missing = [c for c in expected if c not in sheet.header]
            if missing:
                # 统一成 行号:原因码 形状，原因码用 block 级，保证整批拒绝
                sheet.problems.append("0:ARITY_MISMATCH:表头缺列:" + ",".join(missing))
                return sheet
            continue
        line_no = reader.line_num
        if len(cells) != len(sheet.header):
            sheet.problems.append(f"{line_no}:ARITY_MISMATCH")
            continue
        row = {key: _nfkc(cells[index]) for index, key in enumerate(sheet.header)}
        row["_line_no"] = line_no
        key = row.get("waybill_no", "")
        if key and key in sheet.line_of:
            sheet.problems.append(f"{line_no}:DUP_CONFLICT")
            continue
        if key:
            sheet.line_of[key] = line_no
        sheet.rows.append(row)
    if not sheet.rows and not sheet.problems:
        sheet.problems.append("0:EMPTY_FILE")
    return sheet


def parse_carrier_zones(raw: bytes) -> tuple[dict[str, dict], list[str], list[str]]:
    """承运商回传：返回 运单号→回传分区、结构问题、文件级原因码。"""
    sheet = parse_sheet(raw, CARRIER_HEADER)
    zones: dict[str, dict] = {}
    for row in sheet.rows:
        waybill_no = row.get("waybill_no", "")
        zone = (row.get("carrier_zone") or "").upper()
        if not waybill_no or _is_blank(zone):
            sheet.problems.append(f"{row.get('_line_no', 0)}:MISSING_KEY")
            continue
        zones[waybill_no] = {
            "carrier_zone": zone,
            "received_at": row.get("received_at", ""),
            "line_no": int(row.get("_line_no", 0)),
        }
    return zones, sheet.problems, sheet.codes


def parse_address_book(raw: bytes) -> tuple[dict[str, dict], list[str], list[str]]:
    """合同地址簿：返回 运单号→收货地址证据、结构问题、文件级原因码。"""
    sheet = parse_sheet(raw, ADDRESS_HEADER)
    book: dict[str, dict] = {}
    for row in sheet.rows:
        waybill_no = row.get("waybill_no", "")
        province = row.get("province", "")
        city = row.get("city", "")
        address = row.get("address", "")
        if not waybill_no:
            sheet.problems.append(f"{row.get('_line_no', 0)}:MISSING_KEY")
            continue
        book[waybill_no] = {
            "consignee": row.get("consignee", ""),
            "province": province,
            "city": city,
            "address": address,
            "received_at": row.get("received_at", ""),
            "line_no": int(row.get("_line_no", 0)),
            "usable": bool(province and not _is_blank(province)),
        }
    return book, sheet.problems, sheet.codes


def parse_zone_mapping(raw: bytes) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """合同分区对照：(承运商, 服务, 省) → 合同分区。同键不同分区记 DUP_CONFLICT。"""
    sheet = parse_sheet(raw, MAPPING_HEADER)
    mapping: dict[tuple[str, str, str], str] = {}
    for row in sheet.rows:
        carrier = (row.get("carrier") or "").upper()
        service = (row.get("service") or "").lower()
        province = row.get("province", "")
        zone = (row.get("contract_zone") or "").upper()
        if not carrier or not service or not province or _is_blank(zone):
            sheet.problems.append(f"{row.get('_line_no', 0)}:MISSING_KEY")
            continue
        key = (carrier, service, province)
        previous = mapping.get(key)
        if previous is not None and previous != zone:
            sheet.problems.append(f"{row.get('_line_no', 0)}:DUP_CONFLICT")
            continue
        mapping[key] = zone
    return mapping, sheet.problems
