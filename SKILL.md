---
name: leafather-report-audit
slug: leafather-report-audit
displayName: 数鉴-报告审计
display_name: "数鉴-报告审计"
display_name_en: "Report Number Audit"
version: 3.4.5
license: MIT
author: leafather
summary: "报告里的每个数字，要么能追到出处，要么不许发。不改出报告的方式：跑报告时自动录像记账，事后审计；结论强制披露审计覆盖率，查得太少不许说通过。"
tags: ["数字审计", "数据报告", "可追溯", "交付质检", "报告自动化", "数据治理", "数据质量", "审计", "report-audit", "data-audit", "traceability"]
agent_created: true
description: "数鉴-报告审计技能。将数据报告中每个指标数字追溯到其 SQL、口径和数据截止日，追溯不到即拦截交付。不改变既有报告生产方式——出报告时自动记账，事后审计。同时支持 Python 与 Node 出的报告（Node 拦 duckdb 客户端，参数自动内联）。结论永远报告「审计覆盖率」，覆盖率不足则不许判定通过。支持 md/docx/pptx/xlsx/html，多交付件可查跨件一致性。适用场景：交付带数字的数据报告、交付前检查、排查数字对不上、数字可追溯性审计。触发词：出数据报告、交付前检查、数字对不上、可追溯、审计报告。"
description_zh: "数鉴-报告审计技能。将数据报告中每个指标数字追溯到其 SQL、口径和数据截止日，追溯不到即拦截交付。不改变既有报告生产方式——出报告时自动记账，事后审计。结论永远报告「审计覆盖率」，覆盖率不足则不许判定通过。适用场景：交付带数字的数据报告、交付前检查、排查数字对不上、数字可追溯性审计。触发词：出数据报告、交付前检查、数字对不上、可追溯、审计报告。"
description_en: "Report number audit skill. Traces every metric number in a data report back to its SQL, caliber, and data cutoff date, and blocks delivery when a number cannot be traced. Zero changes to existing report pipelines: the ledger is recorded automatically at generation time and audited afterwards. Supports reports produced by both Python and Node (the Node hook intercepts the duckdb client and inlines bound parameters). The verdict always reports the audit coverage rate, and must not pass when coverage is insufficient. Supports md/docx/pptx/xlsx/html and cross-file consistency across multiple deliverables. Use when delivering data reports with numbers, running pre-delivery checks, investigating mismatched numbers, or auditing number provenance. Triggers: data report delivery, pre-delivery check, number mismatch, traceability, audit report."
---

# leafather-report-audit｜数鉴-报告审计

> 一句话：**报告里的每个指标数字，要么能追溯到底，要么不许发。**

---

## 平台说明

- **Windows**：把示例里的 `python3` 换成 `python`（或 `py`）。
- **技能目录不要带空格**：Node 侧靠 `NODE_OPTIONS` 注入，路径带空格会失效（Python 侧不受影响）。
- **duckdb 报 `DLL load failed ... 应用程序控制策略已阻止此文件`**：被安全策略拦了，不是没装。
  把 site-packages 下的 `duckdb` 加白；或不用 duckdb —— 走 `--command`（自带 `cmd_sqlite.py`），自检自动降级。
缺什么会直接告诉你：`python3 scripts/deps_check.py`

---

## 30 秒看它能干什么

```bash
python3 scripts/rla_selftest.py
```

自造小库跑 5 个用例：正确报告通过、改数被拦、跨件被抓、泄漏被拦、编数被拦。
**5/5 通过，说明它真的在检查，不是一个绿灯装饰。** 没有 duckdb 会自动降级到 sqlite3，同样 5/5。

---

## 它解决什么问题

真实事故：一份报告 6 个交付件，数据补录 1 条后池子从 15,596 变成 15,597，只有 1 个件更新，
另 5 个还是旧数字；而"自检报告"写着 24/24 PASS —— **它比的是写死的常数，不是对账**。

这类错误人眼看不出来，"自检"更看不出来。唯一可靠的办法：**把每个数字的出处记下来再对账**。

---

## 三步上手

### 1. 用一条命令跑报告（推荐入口）

```bash
python3 scripts/rla_report.py --out report.md --ledger ledger.json \
    --mount "data=/path/your.parquet" --report-name "某报告" --data-as-of 2026-09-30 \
    -- python3 build_report.py
```

它做三件事：**跑你的报告脚本（自动录像）→ 生成账本 → 审计报告**。
你的 `build_report.py` **一行都不用改**——钩子藏在子进程里，记录它跑过的每条 SQL。

### 2. 看结论

```
结论：✅ 通过
| **审计覆盖率** | **96%**（追溯 72 / 应审 75） |
| 来源不明 | 0 |
检查执行情况：复算 34/34｜跨件一致 2 份｜必现 跳过（未配置 must）｜泄漏 1 份客户版
```

**这两行必须看**：「审计覆盖率」= 数字查了多少；「检查执行情况」= 哪些检查真跑了、哪些跳过。
不看，你手里可能只是一份"空转的通过"。

### 3. 想更准？给数字配个稳定身份

可选。项目根放一个 `metrics.yaml`：

```yaml
metrics:
  - id: ship.amt_net
    label: 净发货额
    unit: 元
    caliber: 含税金额，扣退货
    match_sql: "sum(amt_tax)"      # 子串匹配录像里的 SQL
```

不写也能跑（label 靠 SQL 猜）。写了之后：label 确定、跨报告能认出同一指标、客户版台账能直接交付。

---

## Node 出的报告怎么接

```bash
python3 scripts/rla_report.py --out report.md --ledger ledger.json \
    --mount "data=/path/your.parquet" --report-name "某报告" --data-as-of 2026-09-30 \
    -- node build_report.mjs --out report.md
```

**业务代码一行都不用改**——钩子靠 `NODE_OPTIONS=--require` 挂进 Node 进程。

三条前提：业务项目里有 `@duckdb/node-api`（找不到用 `RLA_NODE_MODULES` 指）、目录不带空格、只支持 duckdb。

位置参数、命名参数、prepared+bind、stream 都能录，**参数会被内联成可复算的 SQL**。
追不到内联的类型会打 `bound_unresolved`，审计报未就绪，不静默放过。
**不做常驻服务内的分段录像**（服务常驻时"哪一次是出报告"没有边界）——细节见 `references/usage-notes.md`。

---

## 数据源怎么接（详细见 `references/usage-notes.md`）

| 你的数据 | 怎么写 |
|---|---|
| parquet / csv | `--mount "data=/path/x.parquet"` |
| duckdb 库（多表） | `--mount "=/path/x.duckdb"`（等号左边留空 = 当主库） |
| MySQL / PG / 任何库 | `--command "python3 my_query.py"`（SQL 进 stdin、CSV 出 stdout） |

**非 duckdb 也能用**：复算走 `--command`，技能自带 `scripts/cmd_sqlite.py` 当例子。

---

## 判定三态

| 情况 | 判定 | 退出码 |
|---|---|---|
| 全可追溯 + 复算一致 + 覆盖率够 | ✅ 通过 | 0 |
| 未复算 / 弱锚定 / 跨件写法不同 / 泄漏未配置 / **覆盖率<90%** / **零指标** / **policy 没归类** | ⚠️ 需人工复核 | 3 |
| 账本与复算不符 / 报告有账本外的指标数字 / 跨件不一致 / 客户版泄漏 | ❌ 不通过 | 1 |
| 账本字段缺失 / 文件不存在 / 数据源挂不上 | ⛔ 未就绪（**不算通过**） | 2 |

**优先级：FAIL > REVIEW > PASS。** 严格发布流只认 0；务实流 0 发、3 附结论后发、1 不发。

---

## 六个最常见的坑（详细见 `references/usage-notes.md`）

1. **账本 `value` 用基本单位**：写 `340000 元`，别写 `34.0 万元`
2. **`--files` 相对「账本所在目录」**，不是当前目录（常见错：`deliverables/deliverables/r.md`）
3. **值 < 10 的小数字天然不可区分**，上下文没锚定就判「无法判定」
4. **PPT / 图片里的数字扫不到**，结论会标「未覆盖 N 张图片」——事后审计的天花板
5. **数字分三类**：交付指标（必须可追溯）/ 支撑数据（`role: support`）/ 技术参数（`audit-skip` 圈起）
6. **「通过」不等于「都查了」**：永远先看「审计覆盖率」

---

## 边界（不吹）

没跑过 SQL 的数字**验不了**（只能判"来源不明"）；**口径对不对不管**（防不了"一开始就想错"）；
**图片里的数字管不了**（只标"未覆盖"）；**分类可能误判**（结论里列了豁免明细供抽查）。

---

## 文件

| 文件 | 作用 |
|---|---|
| `SPEC.md` | **数字身份证契约** —— 第三方照这个交账，不用我们的脚本也能被审 |
| `policy.yaml` | 审计策略（舍入、哪些算 FAIL/REVIEW、必现、泄漏规则）。**未知键会报错**，拼错不会静默失效 |
| `scripts/rla_report.py` | 一条命令：录像 → 记账 → 审计 |
| `scripts/rla_selftest.py` | 自带数据自检（30 秒证明它真的在检查） |
| `scripts/rla_audit.py` | 审计内核：复算 + 配对 + 三态判定 |
| `rla_track.py` / `rla_collect.py` / `rla_track_node.cjs` | 录像（Python）/ 记账 / 录像（Node 钩子） |
| `examples/build_report.mjs` | Node 出报告的最小可用示例（照抄即可） |
| `scripts/metrics.py` / `rla_xlsx.py` | `metrics.yaml` 认领 / 内部+客户双版 Excel 台账 |
| `scripts/rla_derive.py` / `rla_replay.py` | 派生候选建议 / 换策略后的判决回放 |
| `scripts/deps_check.py` | 依赖体检（缺包/DLL 被拦时给人话，不给 traceback） |
| `scripts/cmd_sqlite.py` | `--command` 接法的现成例子（SQL→CSV，背后 sqlite3） |
| `references/usage-notes.md` | 细节：数据源接法、六个坑、Node 细节、依赖降级 |

**非 duckdb 用户**：写 10 行把 SQL 变成 CSV 就能接（`--command`），例子见 `references/usage-notes.md`。

**依赖**：Python 3.10+、`duckdb`、`pyyaml`；解析 docx/pptx/xlsx/html 另需 `python-docx`、`python-pptx`、`openpyxl`、`beautifulsoup4`（缺了只是少支持一种格式，不影响主流程）。
**Node 报告**另需业务项目自带 `@duckdb/node-api`（本技能不替你装驱动）。
