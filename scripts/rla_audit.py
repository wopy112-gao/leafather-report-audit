#!/usr/bin/env python3
"""rla_audit.py — 报告数字审计主入口（内核）。

一句话：**报告里的每个指标数字，要么有出生证明，要么不许发。**

用法：
    # 基本：拿账本审报告
    python3 scripts/rla_audit.py --ledger ledger.json

    # 覆盖报告文件（默认用账本 report.files）
    python3 scripts/rla_audit.py --ledger ledger.json --files a.md b.pptx

    # 复算（验证账本里的数字是不是真的）
    python3 scripts/rla_audit.py --ledger ledger.json --mount "data=/path/your.parquet"

    # 只要可读结论，不写文件
    python3 scripts/rla_audit.py --ledger ledger.json --no-write

退出码：0 通过｜1 不通过｜2 未就绪（配置/数据缺失，不算通过）
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import policy as PL     # noqa: E402
import rla_extract  # noqa: E402




class NotReady(Exception):
    pass


REQUIRED_NUM = ("id", "label", "value", "caliber")   # unit 可选：自动采集时拿不到单位


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 账本校验
def validate(ledger: dict) -> tuple[list[str], list[str]]:
    """返回 (错误, 警告)。错误 → 退出码 2。"""
    errs, warns = [], []
    # 单位口径硬规矩：账本 value 必须是基本单位（元/个/单/家），不能用「万元/千」这类换算单位。
    # 否则报告里写「34.0 万元」会被解析成 340000，和账本的 34.0 对不上（2026-10-25 自检撞到）。
    bad_unit = [n.get("id") for n in (ledger.get("numbers") or [])
                if any(u in str(n.get("unit") or "") for u in ("万", "千", "亿"))]
    if bad_unit:
        warns.append(f'{len(bad_unit)} 条账本的 unit 用了换算单位（万元/千/亿）：'
                     f'{", ".join(map(str, bad_unit[:5]))}。'
                     f'请把 value 换回基本单位（元），unit 写「元」')
    if ledger.get("schema") != "report-ledger/1.0":
        warns.append(f'schema 不是 report-ledger/1.0（当前 {ledger.get("schema")!r}），按兼容模式处理')
    rep = ledger.get("report") or {}
    for k in ("name", "files", "data_as_of", "generated_at"):
        if not rep.get(k):
            errs.append(f"report.{k} 缺失")
    nums = ledger.get("numbers")
    if not isinstance(nums, list) or not nums:
        errs.append("numbers 为空 —— 没有账本就谈不上审计（不算通过）")
        return errs, warns
    seen = set()
    for i, n in enumerate(nums, 1):
        tag = n.get("id") or f"#{i}"
        for k in REQUIRED_NUM:
            if n.get(k) in (None, ""):
                errs.append(f"{tag}：缺字段 {k}")
        if tag in seen:
            errs.append(f"{tag}：id 重复")
        seen.add(tag)
        # 派生条目用 expr 验证，不要求 sql（2026-10-25）
        if n.get("verifiable", True) and not n.get("sql") and not n.get("expr"):
            errs.append(f"{tag}：verifiable=true 但既没有 sql 也没有 expr"
                        "（不可复算就不能算已验证）")
        if n.get("expr") and not n.get("derived_from"):
            errs.append(f"{tag}：有 expr 但没写 derived_from（说不出依赖哪几个指标）")
        if n.get("verifiable") is False and not n.get("origin"):
            errs.append(f"{tag}：verifiable=false 但没写 origin（不给来历等于不诚实）")
        if n.get("sql") and not n.get("source"):
            warns.append(f"{tag}：有 sql 但没指 source，复算时按默认源处理")
    return errs, warns


# ---------------------------------------------------------------- 复算
def recompute(ledger: dict, mounts: dict[str, str], use_gateway: str | None, pol: dict,
              use_command: str | None = None) -> tuple[list[str], dict]:
    """跑 SQL 验账本值。返回 (错误列表, 复算结果{id: 实际值})。"""
    errs, got = [], {}
    nums = [n for n in ledger["numbers"] if n.get("verifiable", True) and n.get("sql")]
    if not nums:
        return errs, got

    conn = None
    if mounts:
        import rla_collect
        try:
            conn, _ = rla_collect.build_source(mounts)
        except SystemExit as e:
            raise NotReady(f"数据源挂载失败：{e}") from e
    elif use_command:
        # --command 接法：SQL 从 stdin 进、CSV 从 stdout 出。没有 duckdb 也能复算。
        # 拆分交给 rla_collect.split_command：Windows 上必须 posix=False，否则反斜杠路径被吃掉
        import rla_collect
        conn = rla_collect.CommandConn(rla_collect.split_command(use_command))

    for n in nums:
        sql = n["sql"]
        try:
            if conn is not None:
                cur = conn.execute(sql)
                row = cur.fetchone()
                if row is None:
                    errs.append(f'{n["id"]}：复算无返回行')
                    continue
                actual = float(row[0])
            elif use_gateway:
                actual = _run_gateway(use_gateway, sql)
            else:
                continue
        except Exception as e:  # noqa: BLE001
            errs.append(f'{n["id"]}：复算失败 {type(e).__name__}: {e}')
            continue
        got[n["id"]] = actual
        expect = float(n["value"])
        if not PL.close_ledger(actual, expect, pol):
            errs.append(f'{n["id"]}（{n["label"]}）：账本写 {expect}，复算是 {actual}')
    return errs, got


def _run_gateway(gw: str, sql: str) -> float:
    import csv
    import os
    import subprocess
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    p = subprocess.run([sys.executable, gw, "--sql", sql, "--out", out, "--limit", "10", "--timeout", "120"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-300:])
    with open(out, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise RuntimeError("网关无返回行")
    return float(list(rows[0].values())[0])


# ---------------------------------------------------------------- 配对
_EXPR_MAX_LEN = 2000

# 表达式里只允许这些 AST 节点：数字、四则运算、取模、幂、一元正负、括号
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
    ast.USub, ast.UAdd, ast.Load,
)


def _calc_node(node: ast.AST) -> float:
    """按白名单递归求值（不调用 eval / compile，结构上没有代码执行的可能）。"""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("表达式里只能有数字")
        return float(node.value)
    if isinstance(node, ast.UnaryOp):
        v = _calc_node(node.operand)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        raise ValueError("不支持的一元运算")
    if isinstance(node, ast.BinOp):
        a, b = _calc_node(node.left), _calc_node(node.right)
        op = node.op
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            if b == 0:
                raise ValueError("除数为 0")
            return a / b
        if isinstance(op, ast.Pow):
            # 幂最容易用来拖死进程（9**9**9**9），直接限死规模
            if abs(b) > 64 or abs(a) > 1e9:
                raise ValueError("幂运算超出允许范围")
            return a ** b
        raise ValueError("不支持的二元运算")
    raise ValueError(f"表达式含不允许的语法：{type(node).__name__}")


def _eval_expr(expr: str, by_id: dict) -> float:
    """算派生表达式：把指标 id 替换成数值后，用 AST 白名单求值。

    为什么不用 eval（2026-10-26 改，上架安全审查口径）：
        账本的 expr 字段**是第三方写的**，属于外部输入。eval 哪怕加了字符集白名单，
        也始终是"需要论证才安全"；而通用安全清单（skill-vetter、鼎实验室的
        skills-security-check）看到内置求值函数就标红旗。改成 AST 白名单 + 自己递归求值后，
        结构上就没有代码执行的可能：只认数字、四则运算、括号、一元正负，其余节点一律拒绝。
    """
    s = str(expr)
    if len(s) > _EXPR_MAX_LEN:
        raise ValueError(f"表达式过长（>{_EXPR_MAX_LEN} 字符）")
    for k in sorted(by_id, key=len, reverse=True):
        v = by_id[k].get("value")
        if v is None:
            raise ValueError(f"派生引用的 {k} 没有数值")
        s = s.replace(k, f"({float(v)})")
    if not re.fullmatch(r"[\d\.\s\+\-\*\/\(\)eE]+", s):
        raise ValueError(f"表达式含不允许的字符：{s[:60]}")
    try:
        tree = ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误：{e.msg}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"表达式含不允许的语法：{type(node).__name__}")
    return _calc_node(tree.body)


def verify_derived(ledger: dict, pol: dict) -> list[str]:
    """验证账本里声明的派生：按 expr 算出来，是否等于声明的 value。

    这是"声明 + 验证"模式 —— 审计只核对，不搜索。
    """
    errs = []
    by_id = {n["id"]: n for n in ledger["numbers"]}
    for n in ledger["numbers"]:
        df = n.get("derived_from")
        if not df:
            continue
        expr = n.get("expr")
        if not expr:
            errs.append(f'{n["id"]}（{n.get("label","")}）：声明了 derived_from 却没有 expr')
            continue
        miss = [x for x in (df if isinstance(df, list) else [df]) if x not in by_id]
        if miss:
            errs.append(f'{n["id"]}：derived_from 引用了不存在的指标 {miss}')
            continue
        try:
            actual = _eval_expr(expr, by_id)
        except Exception as e:  # noqa: BLE001
            errs.append(f'{n["id"]}：派生算式无法计算（{e}）')
            continue
        if not PL.close_ledger(actual, float(n["value"]), pol):
            errs.append(f'{n["id"]}（{n.get("label","")}）：声明 {expr} 应为 {actual:.4g}，'
                        f'账本却写 {n["value"]}')
    return errs


def pair(report_nums: list[dict], ledger: dict, pol: dict) -> tuple[list[dict], list[dict]]:
    """报告数字 → 账本条目。返回 (matched, unmatched)。"""
    nums = ledger["numbers"]
    by_value = {}
    for n in nums:
        try:
            v = float(n["value"])
        except (TypeError, ValueError):
            continue
        by_value.setdefault(round(v, 6), []).append(n)

    matched, unmatched, ambiguous, weak = [], [], [], []
    for r in report_nums:
        if r["kind"] != "metric":
            continue
        # A：量纲必须一致（报告写 % 的，不能配到"场景数"；反之亦然）
        r_dim = "pct" if r.get("unit_hint") == "pct1" else "count"
        cand = [n for n in by_value.get(round(r["value"], 6), [])
                if _dim_ok(r_dim, _dim(n.get("unit")))]
        if not cand:  # 宽容差匹配（四舍五入 / 百分比小数位）
            cand = [n for n in nums
                    if isinstance(n.get("value"), (int, float))
                    and _dim_ok(r_dim, _dim(n.get("unit")))
                    and PL.close(r.get("raw", ""), r["value"], float(n["value"]), pol)]
        if cand:
            best = min(cand, key=lambda n: _ctx_distance(r["context"], str(n.get("label", ""))))
            dist = _ctx_distance(r["context"], str(best.get("label", "")))
            # 锚定是**必要条件**（2026-10-25）：值相等但上下文说不清是哪个指标 → 不算命中。
            # 曾经只对 <10 的小值要求锚定，结果"准确率 84%"照样配上"84 场景数"。
            # dist==5 表示"标签里没有可锚定的词"（如 label 就叫「场景数」，词全在通用词表里）。
            # 这是账本 label 写得泛，不该罚报告方 —— 只要值唯一就认，但标注 weak_label。
            if dist == 5:
                if len(cand) == 1:
                    matched.append({"report": r, "ledger": best, "how": "exact",
                                    "role": best.get("role", "indicator"),
                                    "anchor": "weak_label"})
                else:
                    ambiguous.append({"report": r, "cands": cand[:3]})
                continue
            if dist >= 9:
                # 撤掉 unique 兜底（2026-10-25）：实测"值唯一就放行"精度只有 61%
                # （18 条里 7 条假），是第三次"兜底宽容变误配"。
                # 但值唯一的那些**不是判死**，单独列为"弱锚定待补"：
                #   值对得上、账本里唯一，只是报告上下文没说清 → 请报告方补上下文，而不是我们替它猜。
                too_small = r_dim == "count" and abs(float(r["value"])) < SMALL_VALUE
                if len(cand) == 1 and not too_small:
                    weak.append({"report": r, "ledger": best})
                    continue
                ambiguous.append({"report": r, "cands": cand[:3]})
                continue
            matched.append({"report": r, "ledger": best, "how": "exact",
                            "role": best.get("role", "indicator"),
                            "anchor": "strong" if dist == 0 else "weak"})
            continue
        # 不再搜索派生（2026-10-25 改造）：
        # 审计器在这里"找一个能圆上的算式"等于替被审计方辩护，
        # 且实测随账本规模退化 —— 195 条指标时任意编造的数字 100% 能被圆上。
        # 现在派生必须由账本显式声明（derived_from + expr），审计只做验证。
        unmatched.append(r)
    return matched, unmatched, ambiguous, weak


SMALL_VALUE = 10        # 小于它、且上下文无锚点 → 这个数字本身不可区分（只对计数生效）
PCT_UNITS = {"pct", "pct1", "%", "％", "百分比", "percent"}


def _dim(unit) -> str:
    """量纲：pct / count / unknown。

    账本条目没写 unit 时返回 unknown —— 这时不强制量纲（自动采集拿不到单位），
    否则会把"账本没标单位"误判成"量纲不符"而误杀。
    """
    u = str(unit or "").strip().lower()
    if u in PCT_UNITS:
        return "pct"
    if not u:
        return "unknown"
    return "count"


def _dim_ok(report_dim: str, ledger_dim: str) -> bool:
    return ledger_dim == report_dim or ledger_dim == "unknown"


# 通用词：出现在哪都算"锚定"就没有意义了（"场景"两个字报告里到处都是）
GENERIC_WORDS = {"场景", "数据", "数量", "合计", "总计", "占比", "比例", "情况",
                 "其中", "全部", "整体", "个数", "单位", "数值", "级", "层"}
# 这些字单独或组合都太泛，切出来的碎片（如「场景数」→「景数」）不该当锚定词
GENERIC_CHARS = set("".join(GENERIC_WORDS))


def _ctx_distance(ctx: str, label: str) -> int:
    """标签里的"专有词"是否出现在上下文。0=锚定 / 5=标签无专有词 / 9=有词但上下文没有。

    2026-10-25 修：原来"标签里任意中文词出现"就算锚定，
    而「贝那鲁肽 场景」里的「场景」在报告里遍地都是 → 25 条 GLP-1 的 1 被误判命中。
    现在排除通用词，并要求"锚定"是命中的必要条件，不再是同值时的择优条件。
    """
    if not label:
        return 9
    # 中文没有空格，整串匹配会失败（"覆盖店员"≠"覆盖门店 / 店员"）。
    # 改成二元切分：覆盖店员 → 覆盖 / 盖店 / 店员
    grams = set()
    for block in re.findall(r"[\u4e00-\u9fa5]+", label):
        if len(block) == 1:
            continue
        for i in range(len(block) - 1):
            g = block[i:i + 2]
            # 整词在通用表里，或两个字都是通用字（"景数""场景"这种碎片）→ 不算锚定词
            if g in GENERIC_WORDS or all(c in GENERIC_CHARS for c in g):
                continue
            grams.add(g)
    en = re.findall(r"[A-Za-z]{2,}", label)          # GLP / ATC 这类
    cands = list(grams) + en
    if not cands:
        return 5
    return 0 if any(w in ctx for w in cands) else 9


# ---------------------------------------------------------------- 报告
def _norm_form(raw: str) -> str:
    """报告里数字的写法归一（去千分位/空格），用于比较"同一件事写法是否一致"。"""
    return str(raw).replace(",", "").replace("，", "").replace(" ", "").strip()


def check_cross_file(matched: list[dict]) -> list[tuple[str, str, str]]:
    """跨件一致（闸门3）：同一账本条目，在多份交付件里的写法必须一致。

    为什么要按"账本记录 id"分组，而不是按 metric_id：
        实测某客户 143 条账里，110 条都认领成 ship.amt_net（净发货额），
        但它们分别是月度/周度/省份/客户等不同筛选 —— 按 metric_id 分组会把它们混成一个。
        账本记录 id 才是"这一个数"的唯一身份。
    """
    from collections import defaultdict
    groups = defaultdict(list)
    for m in matched:
        groups[m["ledger"]["id"]].append(m)

    issues = []
    for lid, items in groups.items():
        files = {x["report"]["file"] for x in items}
        if len(files) < 2:
            continue                      # 只有一份提到，不涉及跨件
        forms = defaultdict(list)
        for x in items:
            forms[_norm_form(x["report"]["raw"])].append(f'{x["report"]["file"]}:{x["report"]["loc"]}')
        if len(forms) > 1:
            label = items[0]["ledger"].get("label") or lid
            detail = "；".join(f"{k}（{', '.join(v[:2])}）" for k, v in forms.items())
            issues.append((lid, label, detail))
    return issues


def check_must(matched: list[dict], must_ids: list[str], files: list) -> list[str]:
    """闸门4 必现：配置的核心指标必须出现在**每个**交付件里。"""
    if not must_ids:
        return []
    per_file = {}
    for m in matched:
        mid = m["ledger"].get("metric_id")
        if mid:
            per_file.setdefault(m["report"]["file"], set()).add(mid)
    issues = []
    for f in files:
        have = per_file.get(f.name, set())
        missing = [x for x in must_ids if x not in have]
        if missing:
            issues.append(f"{f.name} 缺少必现指标：{', '.join(missing)}")
    return issues


def check_leak(files: list, checks: dict) -> list[str]:
    """闸门5 泄漏：客户版不得出现内部标记。"""
    import fnmatch
    pats = checks.get("leak_patterns") or []
    rules = checks.get("leak_rules") or []
    # 防假绿（2026-10-25 实测撞到）：配了客户版匹配模式、却没配任何泄漏规则时，
    # 检查压根不跑，结论却显示"通过" —— 等于盖了个空章。
    # 现在改成明确报"未配置"，不让它静默通过。
    if pats and not rules:
        return ["⚠️ 泄漏检查未配置规则（policy.checks.leak_rules 为空）—— "
                "客户版实际未检查，不能当作已通过"]
    if not pats:
        return []
    issues = []
    for f in files:
        if not any(fnmatch.fnmatch(f.name, p) for p in pats):
            continue
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for rule in rules:
            rgx = rule.get("regex")
            if not rgx:
                continue
            for m in re.finditer(rgx, txt):
                line = txt[:m.start()].count("\n") + 1
                issues.append(f'{f.name} 客户版泄漏「{rule.get("name", rgx)}」：'
                              f'{m.group(0)[:40]}（L{line}）')
                break          # 每条规则每文件报一次，避免刷屏
    return issues


def _sort_by_policy(categorized: list[tuple[str, str]], pol: dict):
    """按 policy 把 (类别, 说明) 分成 FAIL / REVIEW / 丢弃（未归类）。"""
    f = set(pol["verdict"]["fail"])
    r = set(pol["verdict"]["review"])
    fails = [m for c, m in categorized if c in f]
    revs = [m for c, m in categorized if c in r and c not in f]
    dropped = [f"{c}：{m}" for c, m in categorized if c not in f and c not in r]
    return fails, revs, dropped


DATE_HINT_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def date_mismatch_hint(paths: list, ledger_cap: str) -> str | None:
    """报告里提到别的数据截止日 → 提示"可能是口径不同"，而不是只说"来源不明"。

    这是**帮它定位**，不是放它过：账本没有的数字照样拦，
    但我们告诉它"大概率为啥对不上"，省得它瞎猜。
    """
    found = set()
    for p in paths:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in DATE_HINT_RE.finditer(txt):
            y, mo, d = (int(x) for x in m.groups())
            if 1 <= mo <= 12 and 1 <= d <= 31:
                found.add(f"{y:04d}-{mo:02d}-{d:02d}")
    if not found:
        return None
    others = sorted(x for x in found if x != ledger_cap)
    if not others:
        return None
    return (f"报告提到日期 {', '.join(others[:3])}，而账本数据截止是 {ledger_cap}。"
            f"**若这些数字是另一口径算出的，请把它们也算进账本并各自标注 data_as_of**；"
            f"否则说明数字确实没有出处。")


def render_md(res: dict) -> str:
    L = []
    L.append(f'# 数字审计结论 · {res["report_name"]}')
    L.append("")
    L.append(f'- 审计时间：{res["audited_at"]}')
    L.append(f'- 数据截止：{res["data_as_of"]}')
    L.append(f'- 报告文件：{", ".join(res["files"]) or "（无）"}')
    if res.get("policy"):
        pv = res["policy"]
        L.append(f'- 审计策略：{os.path.basename(str(pv["source"]))} '
                 f'（hash {pv["hash"]}｜舍入 {pv["rounding"]}）')
    L.append(f'- **结论：{res["verdict"]}**')
    L.append("")
    L.append("| 项 | 数 |")
    L.append("|---|---|")
    L.append(f'| 账本数字 | {res["n_ledger"]} |')
    L.append(f'| 账本可复算 | {res["n_verifiable"]} |')
    L.append(f'| 复算通过 | {res["n_recomputed_ok"]} |')
    L.append(f'| 报告抽到数字 | {res["n_extracted"]} |')
    L.append(f'| 其中判定为指标 | {res["n_metric"]} |')
    L.append(f'| 追溯到「交付指标」 | {res.get("n_matched_indicator", res["n_matched"])} |')
    L.append(f'| 追溯到「支撑数据」 | {res.get("n_matched_support", 0)} |')
    L.append(f'| **弱锚定待补（值唯一但上下文没说清）** | **{res.get("n_weak", 0)}** |')
    L.append(f'| **无法判定（上下文锚不住）** | **{res.get("n_ambiguous", 0)}** |')
    L.append(f'| **来源不明** | **{res["n_unmatched"]}** |')
    # 覆盖率（方案 A）：把"我到底查了多少"摆到台面上，别让"通过"盖住"没查"
    cov = res.get("coverage")
    if cov is None:
        L.append('| **审计覆盖率** | **—**（报告里没有像数据的数字） |')
    else:
        L.append(f'| **审计覆盖率** | **{cov:.0%}**（追溯 {res["n_matched"]} / 应审 {res["n_should"]}） |')
    L.append(f'| 需人工复核项 | {len(res.get("reviews") or [])} |')
    L.append("")
    if res.get("exec_status"):
        L.append("**检查执行情况**：" + "｜".join(f"{k} {v}" for k, v in res["exec_status"]))
    L.append("")
    if res.get("matched"):
        L.append("## ✅ 已追溯的数字")
        L.append("")
        L.append("| 报告里的数 | 出处 | 方式 |")
        L.append("|---|---|---|")
        for m in res["matched"][:40]:
            rp, lg, how = m["report"], m["ledger"], m["how"]
            tag = "直接命中" if how == "exact" else f'派生（{lg["caliber"]}）'
            L.append(f'| {rp["raw"]} | {lg["label"]}（{lg["id"]}） | {tag} |')
        if len(res["matched"]) > 40:
            L.append(f'| … | 其余 {len(res["matched"]) - 40} 条 | |')
        L.append("")
    if res["problems"]:
        L.append("## ❌ 拦下的问题")
        L.append("")
        for p in res["problems"]:
            L.append(f"- {p}")
        L.append("")
    if res.get("reviews"):
        L.append("## ⚠️ 需人工复核（不拦，但不能当已验证）")
        L.append("")
        for r in res["reviews"]:
            L.append(f"- {r}")
        L.append("")
    if res.get("hint"):
        L.append("## 🧭 线索：可能是口径不同")
        L.append("")
        L.append(res["hint"])
        L.append("")
    if res["unmatched"]:
        L.append("## ❌ 来源不明的数字（账本里没有）")
        L.append("")
        L.append("| 文件 | 位置 | 数字 | 上下文 |")
        L.append("|---|---|---|---|")
        for u in res["unmatched"][:40]:
            L.append(f'| {u["file"]} | {u["loc"]} | {u["raw"]} | {u["context"][:40]} |')
        if len(res["unmatched"]) > 40:
            L.append(f'| … | | 其余 {len(res["unmatched"]) - 40} 条 | |')
        L.append("")
    if res["exempt_summary"]:
        L.append("## ℹ️ 未纳入审计的数字（日期/页码/年份等）")
        L.append("")
        L.append("　" + "　".join(f"{k}={v}" for k, v in res["exempt_summary"].items()))
        L.append("")
        L.append("> 这些不是指标，默认豁免；若其中有本该审计的关键数字，请把它写进账本。")
        L.append("")
    L.append("---")
    L.append("")
    L.append("> 由 rla_audit 生成。判定规则：只对「被识别为指标」的数字要求可追溯；"
             "账本里 `verifiable:false` 的条目视为未验证（不计入通过）。")
    return "\n".join(L)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    import deps_check as DC
    DC.banner(allow_degrade=True)   # 依赖体检（没有 duckdb 也能用 --command 走完）
    ap = argparse.ArgumentParser(description="报告数字审计")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--files", nargs="*", default=None, help="覆盖账本里的报告文件")
    ap.add_argument("--mount", action="append", default=[], help='"视图名=路径"，可重复')
    ap.add_argument("--gateway", default=None, help="用取数网关脚本复算（--sql/--out 协议）")
    ap.add_argument("--command", default=None,
                    help="通用取数复算：SQL 从 stdin 进、CSV 从 stdout 出"
                         "（没有 duckdb 时用它；cmd_sqlite.py 就是现成例子）")
    ap.add_argument("--out", default=None, help="结论输出路径（默认与账本同目录 audit.md）")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--policy", default=None, help="审计策略 YAML（默认：技能根 policy.yaml，没有就用内置默认）")
    ap.add_argument("--json", action="store_true", help="同时打印机器可读摘要")
    ap.add_argument("--export-missing", default=None,
                    help="把「报告里有、账本没有」的数字导出成待补账本（JSON），交给报告方补全")
    args = ap.parse_args()

    pol_path = args.policy
    if pol_path is None:
        cand = HERE.parent / PL.POLICY_NAME
        pol_path = cand if cand.exists() else None
    try:
        pol = PL.load(pol_path)
    except PL.PolicyError as e:
        print(f"❌ 未就绪：策略有问题 —— {e}", file=sys.stderr)
        return 2

    lp = Path(args.ledger)
    if not lp.exists():
        print(f"❌ 未就绪：账本不存在 {lp}", file=sys.stderr)
        return 2
    try:
        ledger = json.loads(lp.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"❌ 未就绪：账本不是合法 JSON：{e}", file=sys.stderr)
        return 2

    errs, warns = validate(ledger)
    if errs:
        print("❌ 未就绪：账本不合格（不算通过）", file=sys.stderr)
        for e in errs:
            print("   " + e, file=sys.stderr)
        return 2

    rep = ledger["report"]
    files = args.files if args.files is not None else rep.get("files") or []
    base = lp.parent
    paths = [(base / f) if not Path(f).is_absolute() else Path(f) for f in files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        # 区分两种"找不到"：真不存在 vs 按账本目录解析后不存在（后者的报错最误导人）
        msgs = []
        for p in missing:
            if Path(p.name).exists():
                msgs.append(f"{p}（文件在 CWD 存在，但按账本目录 {base} 解析后找不到——"
                            f"请用绝对路径，或改成相对账本目录的写法）")
            else:
                msgs.append(str(p))
        print("❌ 未就绪：报告文件不存在：" + "；".join(msgs), file=sys.stderr)
        return 2
    if not paths:
        print("❌ 未就绪：没有报告文件可审（report.files 为空）", file=sys.stderr)
        return 2

    mounts = {}
    for m in args.mount:
        if "=" not in m:
            print(f'❌ --mount 格式应为 "视图名=路径"：{m}', file=sys.stderr)
            return 2
        k, v = m.split("=", 1)
        mounts[k.strip()] = v.strip()

    problems: list[str] = []
    # 先验证账本里声明的派生（声明 + 验证，不搜索）
    derr = verify_derived(ledger, pol)
    problems.extend(derr)
    try:
        rec_errs, recomputed = recompute(ledger, mounts, args.gateway, pol, args.command)
    except NotReady as e:
        print(f"❌ 未就绪：{e}", file=sys.stderr)
        return 2
    problems.extend(rec_errs)

    # 抽数字（同时收集"扫不到"的提示，例如 PPT 内图片）
    all_nums, extract_warns = [], []
    for p in paths:
        try:
            nums, warns = rla_extract.extract_with_warnings(p)
        except SystemExit as e:
            print(f"❌ 未就绪：{e}", file=sys.stderr)
            return 2
        all_nums.extend(nums)
        extract_warns.extend(warns)
    from collections import Counter
    exempt = Counter(n["kind"] for n in all_nums if n["kind"] != "metric")
    metric_nums = [n for n in all_nums if n["kind"] == "metric"]

    matched, unmatched, ambiguous, weak = pair(metric_nums, ledger, pol)

    # 覆盖率（2026-10-26 方案 A）：分母扣掉"明确不是数据的"（日期/序号/年份），
    # 剩下的都是"看着像数据"的数字——它们才该有出处。
    n_exempt_struct = sum(v for k, v in exempt.items() if k in ("date", "ordinal", "year"))
    n_should = len(all_nums) - n_exempt_struct
    coverage = (len(matched) / n_should) if n_should > 0 else None

    verifiable = [n for n in ledger["numbers"] if n.get("verifiable", True)]
    res = {
        "report_name": rep.get("name"),
        "audited_at": now(),
        "data_as_of": rep.get("data_as_of"),
        "files": [p.name for p in paths],
        "n_ledger": len(ledger["numbers"]),
        "n_verifiable": len(verifiable),
        "n_recomputed_ok": len(recomputed),
        "n_extracted": len(all_nums),
        "n_metric": len(metric_nums),
        "n_matched": len(matched),
        "n_matched_indicator": sum(1 for m in matched if m.get("role", "indicator") == "indicator"),
        "n_matched_support": sum(1 for m in matched if m.get("role") == "support"),
        "n_unmatched": len(unmatched),
        "n_ambiguous": len(ambiguous),
        "n_weak": len(weak),
        "n_exempt_struct": n_exempt_struct,
        "n_should": n_should,
        "coverage": coverage,
        "weak": weak[:20],
        "ambiguous": ambiguous[:20],
        "problems": problems,
        "unmatched": unmatched,
        "matched": matched,
        "exempt_summary": dict(exempt),
    }
    # ---- 打类别，交 policy 决定算 FAIL 还是 REVIEW ----
    categorized: list[tuple[str, str]] = []
    for e in problems:
        categorized.append(("ledger_mismatch", e))
    for u in unmatched:
        categorized.append(("unmatched_number", f'来源不明：{u["raw"]} @ {u["file"]} {u["loc"]}'))
    # 闸门4 必现 / 闸门5 泄漏
    checks = pol.get("checks") or {}
    for msg in check_must(matched, checks.get("must") or [], paths):
        categorized.append(("must_missing", msg))
    for msg in check_leak(paths, checks):
        categorized.append(("leak", msg))

    # 闸门3：跨件一致（多份交付件不能各说一套）
    if len({p.name for p in paths}) > 1:
        for lid, label, detail in check_cross_file(matched):
            categorized.append(("cross_file_mismatch",
                                f'跨件不一致：{label}（{lid}）在各件写法不同 —— {detail}'))

    for wk in weak:
        rp = wk["report"]
        categorized.append(("weak_anchor",
                            f'弱锚定待补：{rp["raw"]} @ {rp["file"]} {rp["loc"]}'
                            f'（账本里值唯一，但上下文没说清是哪个指标）'))
    for a in ambiguous:
        rp = a["report"]
        names = "、".join(str(c.get("label", c.get("id"))) for c in a["cands"])
        categorized.append(("ambiguous_number",
                            f'无法判定：{rp["raw"]} @ {rp["file"]} {rp["loc"]}'
                            f'（上下文锚不住，可能对应 {names}）'))

    verifiable_nums = [n for n in ledger["numbers"] if n.get("verifiable", True) and n.get("sql")]
    if verifiable_nums and not mounts and not args.gateway and not args.command:
        categorized.append(("not_recomputed",
                            f"账本有 {len(verifiable_nums)} 条可复算数字，但本次未复算"
                            "（加 --mount 或 --gateway 才能核验算术）"))
    unver = [n for n in ledger["numbers"] if n.get("verifiable") is False]
    if unver:
        orgs = "；".join(f'{n["id"]}:{n.get("origin", "未说明")}' for n in unver[:5])
        categorized.append(("unverifiable_entry", f"账本有 {len(unver)} 条声明为不可复算（{orgs}）"))
    for w in extract_warns:
        categorized.append(("unscanned_image", f"未覆盖：{w}"))

    # ── 覆盖率闸门（方案 A，2026-10-26）：没有可查的东西 ≠ 全都查过了
    #    背景：判"这个数字要不要追"靠词表。词表外的说法会被当成杂项豁免，
    #    实测同一份报告只把「总行数」改成「样本池规模」，4 个数字全被豁免、结论仍显示 ✅ 通过。
    #    这条闸门不改"查什么"，只保证结论不说谎：审得太少就不许说通过。
    cov_cfg = pol.get("coverage") or {}
    min_ratio = float(cov_cfg.get("min_ratio", 0.9))
    min_metric = int(cov_cfg.get("min_metric", 1))
    if n_should > 0 and len(metric_nums) < min_metric:
        categorized.append(("no_metric",
                            f"这次没有任何数字被认定为「指标」（抽到 {len(all_nums)} 个数字，"
                            f"其中 {n_should} 个看着像数据）。没有可查的东西不等于全都查过了，"
                            "不能算通过——请换用更常见的说法，或把这些数字写进账本"))
    elif coverage is not None and coverage < min_ratio:
        miss = n_should - len(matched)
        big_misc = sum(1 for n in all_nums
                       if n["kind"] == "misc" and rla_extract.looks_like_data(n.get("raw", "")))
        extra = f"（其中 {big_misc} 个是千分位/百分比/上千的大数，最该有出处）" if big_misc else ""
        categorized.append(("low_coverage",
                            f"审计覆盖率 {coverage:.0%}（追溯 {len(matched)} / 应审 {n_should}）"
                            f"低于 {min_ratio:.0%}：还有 {miss} 个数字没查到出处{extra}"))

    hint = date_mismatch_hint(paths, rep.get("data_as_of", ""))
    if hint and unmatched:
        categorized.append(("unmatched_number", f"[线索] {hint}"))

    fails, reviews, dropped = _sort_by_policy(categorized, pol)
    # 抓到了、却没在 policy 的 fail/review 里归类 → 绝不能静默丢弃。
    # 这是"盖空章"的另一种形态：检查跑了、结果没人看、结论照样说通过。
    # 保守处理：不判失败，但也绝不判通过 —— 降为需人工复核，并把内容列出来。
    res["dropped"] = dropped
    res["reviews"] = reviews
    res["hint"] = hint if unmatched else None
    res["policy"] = {"source": pol.get("_source") or "内置默认", "hash": pol["_hash"],
                     "rounding": pol["numeric"]["rounding"]}
    # ── 检查自检：哪些检查真跑了、哪些跳过了（一眼看出"空转"）
    checks_cfg = pol.get("checks") or {}
    leaks_on = bool(checks_cfg.get("leak_rules")) and bool(checks_cfg.get("leak_patterns"))
    client_files = [p for p in paths
                    if any(__import__("fnmatch").fnmatch(p.name, pat)
                           for pat in (checks_cfg.get("leak_patterns") or []))]
    exec_status = [
        ("复算", f"{len(recomputed)}/{len(verifiable_nums)}" if (mounts or args.gateway or args.command)
                 else f"跳过（未给数据源，{len(verifiable_nums)} 条待复算）"),
        ("跨件一致", f"{len(paths)} 份" if len(paths) > 1 else "跳过（仅 1 份文件）"),
        ("必现", f"{len(checks_cfg.get('must') or [])} 条"
                 if checks_cfg.get("must") else "跳过（未配置 must）"),
        ("泄漏", f"{len(client_files)} 份客户版" if leaks_on
                 else ("跳过（识别到客户版但没配规则）" if client_files else "跳过（未识别到客户版）")),
    ]
    res["exec_status"] = exec_status
    res["verdict"], exit_code = PL.decide(fails, reviews, pol)
    if dropped and exit_code == 0:
        # 有检查结果没被归类 → 不能当"通过"（策略配置漏了，不该让问题混过去）
        res["verdict"], exit_code = "⚠️ 需人工复核", 3
        res["reviews"] = list(reviews) + [
            f"策略未归类（policy.verdict 里缺这些类别，本次按「需复核」处理）：{d}" for d in dropped
        ]

    # 导出待补账本：不是放行，是把"该补什么"明确交给报告方
    if args.export_missing:
        out = Path(args.export_missing)
        missing = [{
            "id": f"m{i}", "label": "", "value": u["value"],
            "unit": u.get("unit_hint") or "", "role": "support",
            "caliber": "", "verifiable": False,
            "origin": f"待补：出现在 {u['file']} {u['loc']}，上下文「{u['context'][:40]}」",
        } for i, u in enumerate(unmatched, 1)]
        out.write_text(json.dumps({
            "schema": "report-ledger/1.0",
            "report": {"name": f"{rep.get('name')} · 待补账", "files": [],
                       "data_as_of": rep.get("data_as_of", ""), "generated_at": now()},
            "sources": [], "numbers": missing,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n📋 待补清单已导出：{out}（{len(missing)} 条，role=support，请补 label/caliber/出处后合并进账本）")

    md = render_md(res)
    if not args.no_write:
        out = Path(args.out) if args.out else (lp.parent / "audit.md")
        out.write_text(md, encoding="utf-8")
        print(md)
        print(f"\n（结论已写入 {out}）")
    else:
        print(md)

    if args.json:
        print(json.dumps({k: res[k] for k in
                          ("verdict", "n_ledger", "n_matched", "n_unmatched", "problems")},
                         ensure_ascii=False, default=str))

    for w in warns:
        print(f"⚠️  {w}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
