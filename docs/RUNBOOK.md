# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id，行数与合计不变。

## 分区冲突复核（晚班出账）

结算依据：合同按收货地址确定的承运分区是唯一依据，承运商回传分区只留痕不采信。

```bash
# 全量情景：地址齐全，14 票待确认、1 票地址依据不足挂起、6 票回传冲突
curl -s -X POST http://127.0.0.1:18209/api/zone-review/run -F scenario=full
# 迟到情景：部分地址/回传缺失，挂起票增加，其余票仍可出账
curl -s -X POST http://127.0.0.1:18209/api/zone-review/run -F scenario=late
# 逐票人工确认（可改定分区，新分区必须有运价版本）
curl -s -X POST http://127.0.0.1:18209/api/zone-review/confirm \
  -H 'Content-Type: application/json' \
  -d '{"waybill_no":"WB-1001"}'
# 正式账单只含已确认票；证据 CSV 全票留痕（含挂起）
curl -s http://127.0.0.1:18209/api/zone-review/invoice.csv
curl -s http://127.0.0.1:18209/api/zone-review/evidence.csv
```

规则：

- `ADDR_PENDING`（地址迟到）、`ADDR_UNUSABLE`（地址依据不足）、`ZONE_NO_CARD`（合同分区无运价）三类票挂起，不混入正式结果
- 已确认票 `locked=1`，同批重跑不新增账单、不改已确认金额；重复确认返回 409 `ZONE_REVIEW_LOCKED`
- 输入 CSV 结构异常（缺列、列数不符、重复运单）整批拒绝，返回 422 `ZONE_INPUT_INVALID`，不落任何复核结果

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
| 分区复核 409 NO_WEIGHTS | 该运行还没算计费重 | 先调 /api/weights 再跑 /api/zone-review/run |
| 分区复核 409 ZONE_REVIEW_LOCKED | 票已确认锁定，重复确认或改分区 | 锁定票不可改；如需改账先由有权限人重置复核批次 |
| 分区复核 422 ZONE_INPUT_INVALID | 三源 CSV 缺列/列数不符/重复运单 | 按表头契约修正后整批重提，旧结果不会被污染 |
