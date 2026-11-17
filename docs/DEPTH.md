# 深度登记 freightrate 物流计费重与运价版本核验

- 规模：中型（8 个功能模块，84-90 文件区间）
- 核心规则：按提货时刻在运价版本半开区间内择版，计费重取实重与体积重的较大值再按台阶向上进位，分区价与附加费按固定顺序叠算，分项之和必须等于应收；分区冲突时合同按收货地址确定的承运分区是唯一结算依据，回传分区只作证据
- 使用者与工作流：3PL 结算组每天收 3 家承运商的运单与月度运价表，先采信运单，再逐票算计费重与应收；晚班出账前在分区复核页对照承运商回传分区与合同地址分区，挂起依据不足的票，确认其余票后导出对账

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 运单采信 | 导入脏运单，看到逐行诊断与原始行号 | POST /api/waybills/import | `app/modules/m1_waybill/handler.py` `app/modules/m1_waybill/service.py` `app/modules/m1_waybill/reader.py` `app/modules/m1_waybill/decode.py` `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| M2 运价版本 | 看版本窗口、校验运价文件是否重叠或陈旧 | GET /api/rates | `app/modules/m2_rates/handler.py` `app/modules/m2_rates/service.py` `app/modules/m2_rates/ratecard.py` `app/modules/m2_rates/version.py` `app/modules/m2_rates/surcharge.py` | `tests/test_m2_rates.py` |
| M3 质量隔离 | 看原因码分布与隔离行，导出证据 CSV | GET /api/quarantine | `app/modules/m3_quality/handler.py` `app/modules/m3_quality/service.py` `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` `app/modules/m3_quality/metrics.py` | `tests/test_m3_quality.py` |
| M4 计费重 | 逐票看实重、体积重、来源与进位后计费重 | POST /api/weights | `app/modules/m4_weights/handler.py` `app/modules/m4_weights/service.py` `app/modules/m4_weights/volumetric.py` `app/modules/m4_weights/rounding.py` `app/modules/m4_weights/billable.py` | `tests/test_m4_weights.py` |
| M5 分区附加 | 看首重、续重、偏远、燃油、保价与应收 | POST /api/charges | `app/modules/m5_charges/handler.py` `app/modules/m5_charges/service.py` `app/modules/m5_charges/zone.py` `app/modules/m5_charges/addon.py` `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| M6 账单对账 | 看四账是否相等，差在哪一项 | GET /api/reconcile | `app/modules/m6_reconcile/handler.py` `app/modules/m6_reconcile/service.py` `app/modules/m6_reconcile/compare.py` `app/modules/m6_reconcile/totals.py` `app/modules/m6_reconcile/explain.py` | `tests/test_m6_reconcile.py` |
| M7 导出 | 导出账单、证据行与可复算的运行清单 | GET /api/export/invoice.csv | `app/modules/m7_export/handler.py` `app/modules/m7_export/service.py` `app/modules/m7_export/csv_out.py` `app/modules/m7_export/invoice_out.py` | `tests/test_m7_export.py` |
| M8 分区复核 | 对照回传分区与合同地址分区，逐票留痕、挂起依据不足票、确认锁定出账 | POST /api/zone-review/run | `app/modules/m8_zone_review/handler.py` `app/modules/m8_zone_review/service.py` `app/modules/m8_zone_review/inputs.py` `app/modules/m8_zone_review/resolution.py` | `tests/test_m8_zone_review.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 运价与运单导出编码不同，GB18030 与 UTF-8 混用 | 直接按 UTF-8 解码抛异常，整份文件读不进 | 编码回退链 utf-8-sig → utf-8 → gb18030，命中回退给 ENCODING_FALLBACK | `app/modules/m1_waybill/decode.py` | `tests/test_m1_waybill.py` |
| TRAP-05 | 中段一行少一列 | 静默补 None，后续列错位算错分区 | 严格列数校验，行数与表头不等即隔离并给 ARITY_MISMATCH | `app/modules/m1_waybill/reader.py` | `tests/test_m1_waybill.py` |
| TRAP-06 | 重量写成 2,500 与全角 １２.５ | float 崩，或全角被当成 0 | NFKC 归一 + 去千分位 + 单位后缀剥离 | `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| TRAP-07 | 重量写成 - 或 N/A | 当成 0 参与合计，运费凭空少一票 | 哨兵清单集中定义，三态分开，哨兵行进隔离区 | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py` |
| TRAP-10 | 20 票各分项用 float 累加 | 合计差 0.01 到 0.10，对账永远不平 | 全程 Decimal，分项先量化到分再求和 | `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| TRAP-11 | 提货时间一处写 Z 一处写 +08:00 | 同一时刻落进两个版本窗口 | 先归一到 UTC 再择版，写法混用标 TZ_MIXED | `app/modules/m1_waybill/normalize.py` | `tests/test_m2_rates.py` |
| TRAP-14 | 同一运单号出现两次，重量不同 | 后写覆盖先写，结果取决于文件顺序 | 保留首条，后条按载荷是否一致分成 DUP_CONFLICT 与 DUPLICATE_WAYBILL | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py` |
| TRAP-15 | 重量用 g 与 kg，尺寸用 cm 与 m | 直接按数字算，体积重差 1000 倍 | 单位表集中定义，先换算到 kg 与 cm 再算 | `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| TRAP-16 | 文件末尾是半行 | 半行被当成有效运单计费 | 末条列数不足记为 TRUNCATED_TAIL 并隔离 | `app/modules/m1_waybill/reader.py` | `tests/test_m1_waybill.py` |
| TRAP-23 | 提货时刻恰好等于版本生效起点或终点 | 两端都算，同一票落进两个版本 | 半开区间 [生效起, 生效止)，终点归到下一版 | `app/modules/m2_rates/version.py` | `tests/test_m2_rates.py` |
| TRAP-24 | 各分项各自四舍五入 | 分项之和与应收差一分钱 | 分项先量化到分，应收由分项求和得出 | `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| TRAP-25 | 同分区两版生效区间相交 | 取到哪版看运气，同一票时高时低 | 先检测重叠，重叠即整份运价集拒绝装载 | `app/modules/m2_rates/version.py` | `tests/test_m2_rates.py` |
| TRAP-26 | 用旧运价集重新提交 | 旧价覆盖新价，已出账单金额回退 | 运价集版本号比较，低于现有版本直接拒绝 | `app/modules/m2_rates/service.py` | `tests/test_m2_rates.py` |
| TRAP-27 | 承运商回传分区与合同地址分区不一致 | 直接采信回传分区，合同应收按错价失真 | 合同分区为唯一结算依据，回传只记 ZONE_CONFLICT 证据，按合同分区重算 | `app/modules/m8_zone_review/resolution.py` | `tests/test_m8_zone_review.py` |
| TRAP-28 | 晚班截单前部分收货地址迟到 | 等齐地址错过截单，或无地址硬出账 | 地址未到记 ADDR_PENDING 挂起不进正式账，其余票照常确认出账 | `app/modules/m8_zone_review/service.py` | `tests/test_m8_zone_review.py` |
| TRAP-29 | 地址只有城市没有省、或省在合同对照里查不到 | 猜测分区计费，事后无法追溯 | 依据不足记 ADDR_UNUSABLE 挂起，证据链保留地址原文与地址簿行号 | `app/modules/m8_zone_review/resolution.py` | `tests/test_m8_zone_review.py` |
| TRAP-30 | 同批数据重跑覆盖了人工已确认的账单 | 已确认金额被新结果改写，账单不可复现 | confirmed 行 locked=1，重跑只刷新未锁定行，二次确认返回 ZONE_REVIEW_LOCKED | `app/store.py` `app/modules/m8_zone_review/service.py` | `tests/test_m8_zone_review.py` |

## 对账规则

- 运单行账：采信记录 = 计费 + 隔离（有未计费行时单列第三项）
- 金额账：应收合计 = 基础运费 + 续重运费 + 偏远附加 + 燃油附加 + 保价费（Decimal，分）
- 版本账：可计费运单 = 单选命中 + 无命中或重叠
- 计费重账：计费行数 = 实重择用 + 体积重择用
- 重跑：同一份输入得到同一个 run_id，行数与合计不变
- 分区复核账：复核票 = 已确认 + 待确认 + 挂起；正式应收只含已确认票，挂起票（ADDR_PENDING/ADDR_UNUSABLE/ZONE_NO_CARD）不进任何账单
- 锁定账：同批重跑后锁定行数与已确认金额不变，未锁定行允许重新裁决

## 已知不覆盖

- 承运商账单金额的二次导入对账仍不做；M8 只核验回传**分区**与合同分区的冲突，不对承运商报价金额
- 不处理多币种换算，运价表 currency 固定 CNY，只做展示
- 体积重只支持单一除数，不支持按件拆分后逐件取整
- 不支持按票拆分多段运输（一票多分区），分区按整票一个值处理
- 地址迟到的时间窗判定目前只看地址簿是否收录该票，不按截单时刻自动比对 received_at
