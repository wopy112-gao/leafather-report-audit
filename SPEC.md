# 数字身份证契约 v1（report-ledger/1.0）

> 这份文件是**通用化的地基**。任何 agent、任何语言、任何报告格式，只要按这份契约交出账本，
> 就能被审计。不要求你用我们的脚本，不要求你改报告生产流程。

---

## 一、为什么是"契约"而不是"工具"

技能要通用，就不能限定"你必须用我的 Python 渲染报告"。
它只需要一件事：**每个进了报告的数字，都能说清它是谁、从哪来、怎么算的。**

说清了 → 系统能复算、能比对、能追溯。
说不清 → 判「不可追溯」，拦下。

---

## 二、账本文件

一个审计单元 = 一份 `ledger.json`（UTF-8，UTF-8 BOM 也接受）。

```json
{
  "schema": "report-ledger/1.0",
  "report": {
    "name": "减肥场景 × GLP-1 报告",
    "files": ["report.md", "deck.pptx"],
    "data_as_of": "2026-10-19",
    "generated_at": "2026-10-22T15:09:13+08:00",
    "agent": "某 agent"
  },
  "sources": [
    {
      "id": "src1",
      "type": "duckdb",
      "path": "/path/your.parquet",
      "fingerprint": "33999184f03b2063"
    }
  ],
  "numbers": [
    {
      "id": "n1",
      "label": "减肥场景池",
      "value": 15597,
      "unit": "场景",
      "display": ["15,597", "15597"],
      "numerator": 15597,
      "denominator": null,
      "caliber": "疾病名称含减肥/肥胖，COUNT(DISTINCT 场景ID)，截至 2026-10-19",
      "sql": "SELECT COUNT(DISTINCT \"场景ID\") FROM data WHERE \"疾病名称\" LIKE '%减肥%' AND \"ydate\" <= '2026-10-19'",
      "source": "src1"
    }
  ]
}
```

---

## 三、字段规则

### report（必填）

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 报告名称 |
| `files` | ✅ | 报告文件列表（相对账本所在目录），审计器会逐个扫 |
| `data_as_of` | ✅ | 数据截止日 `YYYY-MM-DD`。**报告里不许出现晚于它的数据**（物理约束） |
| `generated_at` | ✅ | 账本生成时间（ISO 8601） |
| `agent` | ⬜ | 谁出的报告（审计结论里会标注） |

### sources（必填，可为空数组）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一标识，被 numbers.source 引用 |
| `type` | ✅ | `duckdb` / `gateway` / `command` / `module` / `none` |
| `path` / `command` / `module` | ✅ | 按 type 给。`none` 表示"数据是别人给的，没法复算" |
| `fingerprint` | ⬜ | 数据文件指纹；审计时会重算比对，不一致则判「数据已变，账本过期」 |

### numbers（必填，每个进报告的数字一条）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一标识 |
| `label` | ✅ | 指标名（人话，比如"成交率"），用于和报告上下文配对 |
| `value` | ✅ | **数值**（机器比对用）。百分比请给百分数本体，如 73.9 |
| `unit` | ✅ | `场景` / `家` / `元` / `pct` / `pct1` / `个` … |
| `display` | ⬜ | 报告里可能的写法，字符串或数组（`"15,597"` / `"1.56万"` / `"73.9%"`）。不填则自动从 value+unit 推导 |
| `numerator` / `denominator` | ⬜ | 分子分母（比率类**强烈建议**给，这是"30 秒答出来源"的关键） |
| `caliber` | ✅ | 一句话口径（人话） |
| `sql` | ⬜ | 可复算的 SQL。**给了就必须能跑出 value** |
| `source` | ⬜ | 指向 sources.id；`sql` 存在时必填 |
| `verifiable` | ⬜ | 默认 `true`；`false` 表示"来源不可复算"，审计降级为「未验证」 |
| `origin` | ⬜ | `verifiable:false` 时必填，说明来历（如"客户提供的 2025 年审计报表"） |
| `role` | ⬜ | `indicator`（默认，交付指标）/ `support`（支撑数据：对照值、样本数、过程计算）。support 不计入"交付指标可追溯率" |
| `data_as_of` | ⬜ | **本条数字自己的数据截止日**，覆盖 `report.data_as_of`。一份报告混用多个口径时必填 |
| `derived_from` | ⬜ | 派生所依赖的指标 id 列表，如 `["n2","n1"]` |
| `expr` | ⬜ | 派生算式，如 `"n2/n1*100"`。**给了就会真算一遍验证**，不能随口编 |

---

## 四、审计器怎么用这份账本

| 检查 | 依据 | 不过的后果 |
|---|---|---|
| **账本自洽** | 每条 number 字段齐全；`verifiable:true` 必须有 sql | 退出码 2（未就绪） |
| **复算一致** | 跑 `sql`，结果 == `value` | 退出码 1（算错了 / 编的） |
| **快照未变** | 重算 `fingerprint` | 退出码 1（数据变了，账本过期） |
| **报告可追溯** | 报告里抽出的每个数字，都能匹配到某条 number | 退出码 1（有来路不明的数字） |
| **报告未失真** | 匹配上的数字，报告写法与账本值一致 | 退出码 1（报告写错了） |

**退出码**：`0` 通过｜`1` 不通过｜`2` 未就绪（**不算通过**）

---

## 四之二、一份报告混用多个口径怎么办

现实里很常见：正文用「截至 10-19」，某章对比用「截至 10-10」。
**不要放宽判定，要让账本表达清楚** —— 每条数字带自己的 `data_as_of`：

```json
{"id": "n9", "label": "场景数（10-10 口径）", "value": 281225,
 "data_as_of": "2026-10-10", "caliber": "感冒场景，截至 10-10", "sql": "..."}
```

审计器会按各自的 `data_as_of` 校验，不会因为"和全局截止日不同"就判错。
**但如果报告里有账本没有的数字，它照样拦** —— 只是会多给一句线索，指出"可能是口径不同"。

## 四之三、报告里的数字有三类角色

| 类 | 例子 | 怎么处理 |
|---|---|---|
| **A 交付指标** | 场景池、成交率、门店数 | 必须写进账本（`role: indicator`），必须可追溯 |
| **B 支撑数据** | 大盘对照值、样本数、过程计算 | 写进账本（`role: support`），有出处即可，不计入指标可追溯率 |
| **C 技术参数** | 置信阈值、字段完整度、页码 | 用 `<!-- audit-skip:start/end -->` 圈起来，**跳过数量会在结论里披露** |

**为什么不干脆放宽**：C 类圈起来是"透明跳过"，结论会写"跳过 N 个数字"；
而 A/B 类照样要账本 —— 举证责任没变，只是不再把"技术说明"当成造假。

## 五、最小可用账本（只填必填）

```json
{
  "schema": "report-ledger/1.0",
  "report": {"name": "演示", "files": ["r.md"], "data_as_of": "2026-10-19", "generated_at": "2026-10-23T10:00:00+08:00"},
  "sources": [],
  "numbers": [
    {"id": "n1", "label": "场景数", "value": 15597, "unit": "场景",
     "caliber": "去重场景ID", "verifiable": false, "origin": "客户口头提供，未复算"}
  ]
}
```

这份账本能过「账本自洽」和「报告可追溯」，但每条都会标「未验证」——**这就是诚实的样子**。

---

## 六、怎么生成账本（三种，任选）

| 方式 | 命令 | 适用 |
|---|---|---|
| 录像 | `python3 -m rla_track --ledger l.json -- python3 build.py` | Python 出报告，零改造 |
| 录像（Node） | `python3 rla_track.py --ledger l.json -- node build.mjs` | Node 出报告，零改造（拦 `@duckdb/node-api`） |
| 回收 | `python3 rla_collect.py --trace run.trace.jsonl --ledger l.json` | 已有 SQL 执行记录 |
| 通用取数 | `--command "python3 my_query.py"` / `--gateway /path/gw.py` | **任何数据库**（MySQL/PG/Oracle/HTTP），只要能把 SQL 变成 CSV |
| 手写/程序生成 | 任何语言按上面格式输出 JSON | 外部 agent、老报告 |

### 附：trace 录像契约（跨语言的最小接口）

如果你不想用我们的钩子，但愿意交出「跑过哪些 SQL」，那比手写账本可靠得多——
手写账本是"选摘记账"，实测漏 40%；录像把跑过的全记，覆盖面从方法上保证。

`.trace.jsonl`，一行一条：

```json
{"sql": "SELECT COUNT(*) AS v FROM data WHERE \"省份\" = '山东省'", "kind": "duckdb.node.run", "file": "/abs/build.mjs", "line": 42, "caller": "build.mjs:38", "bound": true}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `sql` | ✅ | **必须已把绑定参数内联成字面量**。带 `?`/`$p` 的语句无法复算，会被跳过（等于漏记） |
| `kind` | ⬜ | 来源标记，只用于排障 |
| `file` / `line` | ⬜ | 出处，写进账本的 `caliber` 供人核对 |
| `caller` | ⬜ | 调用者位置（业务把查询封成函数时，靠它区分是哪一句） |
| `bound_unresolved` | ⬜ | `true` = 有参数没能内联。**审计会因此报未就绪，不会静默放过** |

**我们的义务**：任何语言只要按这个格式交 trace + 能提供可复算的数据源，就能进 `rla_collect.py` → 账本 → 审计全链路。

**契约只约束格式，不约束你怎么产生它。**
