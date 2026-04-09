#!/usr/bin/env python3
"""
Session 根因诊断脚本

用法:
  python3 scripts/diagnose_session.py <session_id>
  python3 scripts/diagnose_session.py --top 5          # 诊断最近 30 天最贵的 top 5
  python3 scripts/diagnose_session.py --list           # 列出最近 30 天的 session

只依赖标准库。数据源: Loki http://localhost:3100
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LOKI = "http://localhost:3100"
SERVICE = "claude-code"
DAYS_DEFAULT = 30

# ===== 规则阈值 =====
R1_TOOL_SIZE_BYTES = 100_000        # 工具爆炸
R2_CACHE_MULT = 2.0                 # 回合雪球
R2_COST_MULT = 2.0
R3_PROMPT_LEN = 20                  # 闲聊贵
R3_COST_MULT = 2.0
R4_JACCARD = 0.6                    # 失败重试
R4_WINDOW = 5
R6_AVG_COST_PER_TURN = 1.0          # 高基线（session 级，绝对阈值）
R7_OUTPUT_TOKENS = 5000             # 大 output 浪费
R9_STARTUP_CACHE_CREATION = 50_000  # session 启动开销（首请求 cache_creation）
R10_SESSION_CACHE_RATIO = 0.7       # cache 失效（session 级）
R10_TURN_CC_MULT = 3.0              # cache 失效（回合级）：cache_creation > 均值 N 倍
R10_TURN_CC_MIN = 5000              # cache 失效（回合级）绝对下限

# ===== 模型价格（USD per million tokens） =====
# 数据来源：Anthropic 公开定价
PRICES = {
    "opus": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_creation": 18.75},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_creation": 3.75},
    "haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_creation": 1.25},
}


def model_tier(model: str) -> str:
    m = model.lower()
    if "opus" in m: return "opus"
    if "sonnet" in m: return "sonnet"
    if "haiku" in m: return "haiku"
    return "opus"


def estimate_cost(tier: str, input_t: int, output_t: int, cache_read: int, cache_creation: int) -> float:
    p = PRICES[tier]
    return (input_t * p["input"] + output_t * p["output"]
            + cache_read * p["cache_read"] + cache_creation * p["cache_creation"]) / 1e6


# ============================================================
# Loki 查询
# ============================================================

def loki_query_range(query: str, start_ns: int, end_ns: int, limit: int = 5000) -> list[dict]:
    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(limit),
        "direction": "forward",
    }
    url = f"{LOKI}/loki/api/v1/query_range?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["data"]["result"]


def loki_query_instant(query: str, time_ns: int) -> list[dict]:
    params = {"query": query, "time": str(time_ns)}
    url = f"{LOKI}/loki/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["data"]["result"]


def now_ns() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1e9)


def days_ago_ns(days: int) -> int:
    return now_ns() - int(days * 86400 * 1e9)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ApiReq:
    model: str
    cost: float
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    duration_ms: int


@dataclass
class ToolCall:
    tool_name: str
    tool_input: str
    result_size: int
    duration_ms: int
    success: bool


@dataclass
class Turn:
    prompt_id: str
    seq: int
    timestamp: str
    user_prompt: str
    prompt_length: int
    api_requests: list[ApiReq] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)

    # 派生
    total_cost: float = 0.0
    total_output: int = 0
    total_cache_read: int = 0
    max_tool_size: int = 0
    cum_cache_read_entering: int = 0  # 进入此回合时的累计 cache_read
    models: set[str] = field(default_factory=set)

    def finalize(self) -> None:
        self.total_cost = sum(r.cost for r in self.api_requests)
        self.total_output = sum(r.output_tokens for r in self.api_requests)
        self.total_cache_read = sum(r.cache_read for r in self.api_requests)
        self.max_tool_size = max((t.result_size for t in self.tool_calls), default=0)
        self.models = {r.model for r in self.api_requests}


# ============================================================
# 数据获取
# ============================================================

def fetch_session_events(session_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """拉取 session 的 user_prompt / api_request / tool_result 事件"""
    # 先确定时间窗
    start = days_ago_ns(DAYS_DEFAULT)
    end = now_ns()

    def fetch(event_name: str) -> list[dict]:
        q = f'{{service_name="{SERVICE}"}} | event_name="{event_name}" | session_id="{session_id}"'
        streams = loki_query_range(q, start, end, limit=5000)
        rows = []
        for s in streams:
            meta = s["stream"]
            for ts, _ in s["values"]:
                rows.append({**meta, "_ts": int(ts)})
        return rows

    return fetch("user_prompt"), fetch("api_request"), fetch("tool_result")


def build_turns(session_id: str) -> list[Turn]:
    prompts, api_reqs, tools = fetch_session_events(session_id)
    if not prompts:
        return []

    turns: dict[str, Turn] = {}
    for p in prompts:
        pid = p.get("prompt_id", "")
        if not pid or pid in turns:
            continue
        turns[pid] = Turn(
            prompt_id=pid,
            seq=int(p.get("event_sequence", 0)),
            timestamp=p.get("event_timestamp", ""),
            user_prompt=p.get("prompt", ""),
            prompt_length=int(p.get("prompt_length", 0)),
        )

    for r in api_reqs:
        pid = r.get("prompt_id", "")
        if pid not in turns:
            continue
        turns[pid].api_requests.append(ApiReq(
            model=r.get("model", "?"),
            cost=float(r.get("cost_usd", 0) or 0),
            input_tokens=int(r.get("input_tokens", 0) or 0),
            output_tokens=int(r.get("output_tokens", 0) or 0),
            cache_read=int(r.get("cache_read_tokens", 0) or 0),
            cache_creation=int(r.get("cache_creation_tokens", 0) or 0),
            duration_ms=int(r.get("duration_ms", 0) or 0),
        ))

    for t in tools:
        pid = t.get("prompt_id", "")
        if pid not in turns:
            continue
        turns[pid].tool_calls.append(ToolCall(
            tool_name=t.get("tool_name", "?"),
            tool_input=t.get("tool_input", ""),
            result_size=int(t.get("tool_result_size_bytes", 0) or 0),
            duration_ms=int(t.get("duration_ms", 0) or 0),
            success=t.get("success", "true") == "true",
        ))

    ordered = sorted(turns.values(), key=lambda x: x.seq)
    # finalize + 计算进入时的累计 cache_read
    cum = 0
    for t in ordered:
        t.cum_cache_read_entering = cum
        t.finalize()
        cum += t.total_cache_read
    return ordered


# ============================================================
# 规则引擎
# ============================================================

FINDING_DESC = {
    "R1": ("工具爆炸", "先 Grep 定位再局部 Read，避免一次性把大文件灌入 context"),
    "R2": ("回合雪球", "context 已远超均值，此处应 /clear 携带结论重开"),
    "R3": ("闲聊贵", "小问题落在大 context 里不便宜，考虑开新 session"),
    "R4": ("失败重试", "与相邻 prompt 高度重复，给出更明确的方向或换思路"),
    "R5": ("模型错配", "任务复杂度低，可降级到更便宜的模型"),
    "R6": ("高基线", "session 平均每回合成本过高，整体 context 初始就重；检查 CLAUDE.md / 启用的 skill"),
    "R7": ("大 output 浪费", "单回合 output 过大，要求模型只给结论/diff，避免冗长解释"),
    "R9": ("启动开销大", "session 首次 cache_creation 过大，可能加载了过多 skill/MCP/CLAUDE.md"),
    "R10": ("Cache 失效", "prefix 变动导致 cache 命中率低，把变动部分放后面"),
}


def classify_turn_complexity(turn: "Turn") -> str:
    """根据回合特征推荐合适的模型 tier"""
    n_tools = len(turn.tool_calls)
    tool_names = {tc.tool_name for tc in turn.tool_calls}
    has_write = bool(tool_names & {"Edit", "Write", "NotebookEdit"})
    cheap_tools_only = tool_names <= {"Read", "Grep", "Glob"}

    # haiku: 纯问答 / 极轻量
    if (n_tools == 0 and turn.total_output < 300 and turn.prompt_length < 100):
        return "haiku"
    if (n_tools <= 2 and cheap_tools_only and turn.total_output < 500
            and turn.prompt_length < 200):
        return "haiku"

    # sonnet: 常规代码读写
    if (n_tools <= 5 and not has_write and turn.total_output < 2000):
        return "sonnet"
    if (n_tools <= 8 and turn.total_output < 2000 and turn.prompt_length < 500):
        return "sonnet"

    # opus: 复杂任务
    return "opus"


TIER_RANK = {"haiku": 0, "sonnet": 1, "opus": 2}


def tokenize(text: str) -> set[str]:
    # 中英文粗分：按非字母数字分词 + 中文按字
    import re
    text = text.lower()
    out = set()
    for w in re.findall(r"[a-z0-9]+", text):
        if len(w) >= 2:
            out.add(w)
    for ch in re.findall(r"[\u4e00-\u9fff]", text):
        out.add(ch)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def run_rules(turns: list[Turn]) -> tuple[dict[int, list[str]], dict[str, Any]]:
    """返回 (回合级 findings, session 级 findings)"""
    findings: dict[int, list[str]] = defaultdict(list)
    session_findings: dict[str, Any] = {}
    if not turns:
        return findings, session_findings

    n = len(turns)
    total_cost = sum(t.total_cost for t in turns)
    avg_cost = total_cost / n
    avg_cum_cache = sum(t.cum_cache_read_entering for t in turns) / n if n else 0
    sum_cc = sum(r.cache_creation for t in turns for r in t.api_requests)
    avg_cc_per_turn = sum_cc / n if n else 0

    # R1 工具爆炸
    for i, t in enumerate(turns):
        if t.max_tool_size > R1_TOOL_SIZE_BYTES:
            findings[i].append("R1")

    # R2 回合雪球
    for i, t in enumerate(turns):
        if (t.cum_cache_read_entering > avg_cum_cache * R2_CACHE_MULT
                and t.total_cost > avg_cost * R2_COST_MULT):
            findings[i].append("R2")

    # R3 闲聊贵
    for i, t in enumerate(turns):
        if t.prompt_length < R3_PROMPT_LEN and t.total_cost > avg_cost * R3_COST_MULT:
            findings[i].append("R3")

    # R4 失败重试
    tokens = [tokenize(t.user_prompt) for t in turns]
    for i in range(n):
        for j in range(max(0, i - R4_WINDOW), i):
            if jaccard(tokens[i], tokens[j]) > R4_JACCARD:
                findings[i].append("R4")
                break

    # R5 模型错配（升级版：复杂度分级 + 节省估算）
    for i, t in enumerate(turns):
        if not t.api_requests:
            continue
        actual_tier = max((model_tier(r.model) for r in t.api_requests), key=lambda x: TIER_RANK[x])
        recommended = classify_turn_complexity(t)
        if TIER_RANK[recommended] < TIER_RANK[actual_tier]:
            # 估算降级后成本
            new_cost = sum(
                estimate_cost(recommended, r.input_tokens, r.output_tokens,
                              r.cache_read, r.cache_creation)
                for r in t.api_requests
            )
            t.r5_recommended = recommended  # type: ignore
            t.r5_savings = t.total_cost - new_cost  # type: ignore
            if t.r5_savings > 0.01:  # 节省太少不报
                findings[i].append("R5")

    # R7 大 output 浪费
    for i, t in enumerate(turns):
        if t.total_output > R7_OUTPUT_TOKENS:
            findings[i].append("R7")

    # R10 回合级 cache 失效
    for i, t in enumerate(turns):
        cc = sum(r.cache_creation for r in t.api_requests)
        if cc > R10_TURN_CC_MIN and cc > avg_cc_per_turn * R10_TURN_CC_MULT:
            findings[i].append("R10")

    # ===== Session 级 =====
    # R6 高基线
    if avg_cost > R6_AVG_COST_PER_TURN:
        session_findings["R6"] = {"avg_cost": avg_cost}

    # R9 启动开销大
    first_with_cc = next(
        (r for t in turns for r in t.api_requests if r.cache_creation > 0),
        None
    )
    if first_with_cc and first_with_cc.cache_creation > R9_STARTUP_CACHE_CREATION:
        session_findings["R9"] = {"first_cache_creation": first_with_cc.cache_creation}

    # R10 session 级 cache 失效
    sum_cr = sum(r.cache_read for t in turns for r in t.api_requests)
    if sum_cr + sum_cc > 0:
        ratio = sum_cr / (sum_cr + sum_cc)
        if ratio < R10_SESSION_CACHE_RATIO:
            session_findings["R10"] = {"cache_ratio": ratio}

    return findings, session_findings


# ============================================================
# 报告渲染
# ============================================================

def fmt_money(x: float) -> str:
    return f"${x:.2f}"


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024  # type: ignore
    return f"{n:.1f}TB"


def ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    if max_value <= 0:
        return ""
    filled = int(value / max_value * width)
    return "█" * filled + "·" * (width - filled)


def render_report(session_id: str, turns: list[Turn], findings: dict[int, list[str]],
                  session_findings: dict[str, Any] | None = None) -> str:
    session_findings = session_findings or {}
    if not turns:
        return f"# Session {session_id}\n\n没有找到数据。\n"

    total_cost = sum(t.total_cost for t in turns)
    n_turns = len(turns)
    n_api = sum(len(t.api_requests) for t in turns)
    n_tools = sum(len(t.tool_calls) for t in turns)

    # 成本构成（按 token 类型估算 — 用 token 数占比近似，不是 cost 精确分摊）
    # 更准确方式：直接按 cache_read 在总 token 里的占比乘以 total_cost
    sum_input = sum(r.input_tokens for t in turns for r in t.api_requests)
    sum_output = sum(r.output_tokens for t in turns for r in t.api_requests)
    sum_cread = sum(r.cache_read for t in turns for r in t.api_requests)
    sum_ccreate = sum(r.cache_creation for t in turns for r in t.api_requests)

    # 用代表性价格权重近似（opus 4 价格）：
    # input $15/Mtok, output $75/Mtok, cache_read $1.5/Mtok, cache_creation $18.75/Mtok
    w = {
        "input": sum_input * 15 / 1e6,
        "output": sum_output * 75 / 1e6,
        "cache_read": sum_cread * 1.5 / 1e6,
        "cache_creation": sum_ccreate * 18.75 / 1e6,
    }
    w_total = sum(w.values()) or 1

    # 模型分布
    model_count: dict[str, int] = defaultdict(int)
    for t in turns:
        for r in t.api_requests:
            model_count[r.model] += 1
    total_req = sum(model_count.values()) or 1
    model_str = " / ".join(
        f"{m.replace('claude-', '')} {c*100//total_req}%"
        for m, c in sorted(model_count.items(), key=lambda x: -x[1])
    )

    start_ts = turns[0].timestamp
    end_ts = turns[-1].timestamp

    out = []
    out.append(f"# Session `{session_id[:8]}` 诊断报告\n")
    out.append(f"**时间**: {start_ts} ~ {end_ts}")
    out.append(f"**总成本**: {fmt_money(total_cost)}  |  **回合**: {n_turns}  |  **API 请求**: {n_api}  |  **工具调用**: {n_tools}")
    out.append(f"**模型**: {model_str}\n")

    # Session 级 findings
    if session_findings:
        out.append("## ⚠️ Session 级问题\n")
        for rid, info in session_findings.items():
            name, advice = FINDING_DESC[rid]
            if rid == "R6":
                out.append(f"- **{rid} {name}**: 平均每回合成本 {fmt_money(info['avg_cost'])} （阈值 ${R6_AVG_COST_PER_TURN}）")
            elif rid == "R9":
                out.append(f"- **{rid} {name}**: 首次 cache_creation = {info['first_cache_creation']:,} tokens")
            elif rid == "R10":
                out.append(f"- **{rid} {name}**: cache 命中率 = {info['cache_ratio']*100:.0f}% （阈值 {int(R10_SESSION_CACHE_RATIO*100)}%）")
            out.append(f"  → {advice}")
        out.append("")

    out.append("## 成本构成（估算）\n")
    out.append("| 类型 | 估值 | 占比 |")
    out.append("|---|---|---|")
    for k in ("cache_read", "output", "input", "cache_creation"):
        out.append(f"| {k} | {fmt_money(w[k])} | {w[k]/w_total*100:.0f}% |")
    out.append("")

    # Top 5 贵回合
    top = sorted(range(n_turns), key=lambda i: -turns[i].total_cost)[:5]
    out.append("## Top 5 最贵回合\n")
    out.append("| # | prompt | cost | 命中 |")
    out.append("|---|---|---|---|")
    for i in top:
        t = turns[i]
        p = t.user_prompt.replace("\n", " ").replace("|", "\\|")[:50]
        hits = ",".join(findings.get(i, [])) or "-"
        out.append(f"| {i+1} | {p} | {fmt_money(t.total_cost)} | {hits} |")
    out.append("")

    # 异常回合详情
    flagged = sorted(findings.keys(), key=lambda i: -turns[i].total_cost)
    if flagged:
        out.append("## 🔴 异常回合详情\n")
        for i in flagged[:10]:
            t = turns[i]
            rules = findings[i]
            p = t.user_prompt.replace("\n", " ")[:80]
            out.append(f"### Turn #{i+1} \"{p}\" — {fmt_money(t.total_cost)}")
            out.append(f"**命中规则**: {', '.join(f'{r}({FINDING_DESC[r][0]})' for r in rules)}")
            out.append("")
            out.append("**证据**:")
            if "R1" in rules:
                big = [tc for tc in t.tool_calls if tc.result_size > R1_TOOL_SIZE_BYTES]
                for tc in big[:3]:
                    inp = tc.tool_input[:80].replace("\n", " ")
                    out.append(f"- `{tc.tool_name}` 返回 **{fmt_bytes(tc.result_size)}**: `{inp}`")
            if "R2" in rules:
                out.append(f"- 进入此回合时累计 cache_read = {t.cum_cache_read_entering:,}（远超均值）")
                out.append(f"- 该回合成本 {fmt_money(t.total_cost)}")
            if "R3" in rules:
                out.append(f"- prompt 长度 {t.prompt_length} 字，但回合成本 {fmt_money(t.total_cost)}")
            if "R4" in rules:
                out.append("- 与前 5 回合内某条 prompt 高度相似")
            if "R5" in rules:
                rec = getattr(t, "r5_recommended", "?")
                save = getattr(t, "r5_savings", 0.0)
                actual = ",".join(sorted({model_tier(m) for m in t.models}))
                out.append(f"- 任务复杂度低（{len(t.tool_calls)} tools, output {t.total_output}），实际用 **{actual}**，建议用 **{rec}**")
                out.append(f"- 本回合可省 **{fmt_money(save)}**")
            if "R7" in rules:
                out.append(f"- output_tokens = {t.total_output:,}（阈值 {R7_OUTPUT_TOKENS}）")
            if "R10" in rules:
                cc = sum(r.cache_creation for r in t.api_requests)
                out.append(f"- 此回合 cache_creation = {cc:,}（远超均值），prefix 被打破")
            out.append("")
            out.append(f"**建议**: " + "；".join(FINDING_DESC[r][1] for r in rules))
            out.append("")
    else:
        out.append("## 🟢 未命中任何规则\n")
        out.append("该 session 看起来没有明显异常回合。如果它仍然很贵，说明成本均匀分布——可能整体上下文就是大。\n")

    # 成本趋势
    out.append("## 📈 回合成本趋势\n")
    max_cost = max(t.total_cost for t in turns)
    out.append("```")
    for i, t in enumerate(turns):
        mark = " 🔴" if i in findings else ""
        out.append(f"#{i+1:3d} {ascii_bar(t.total_cost, max_cost)} {fmt_money(t.total_cost)}{mark}")
    out.append("```\n")

    # 总结
    out.append("## 💡 总结\n")
    dominant = max(w.items(), key=lambda x: x[1])
    out.append(f"- 主导成本: **{dominant[0]}** ({dominant[1]/w_total*100:.0f}%)")
    if flagged:
        worst = flagged[0]
        out.append(f"- 最值得关注的回合: **#{worst+1}** ({fmt_money(turns[worst].total_cost)}, {','.join(findings[worst])})")
        # 统计各规则命中次数
        rule_count: dict[str, int] = defaultdict(int)
        for rules in findings.values():
            for r in rules:
                rule_count[r] += 1
        top_rule = max(rule_count.items(), key=lambda x: x[1])
        out.append(f"- 最常见问题: **{top_rule[0]} ({FINDING_DESC[top_rule[0]][0]})** 命中 {top_rule[1]} 次")
    else:
        out.append("- 没有异常回合")

    # 总可省金额（R5）
    total_savings = sum(getattr(t, "r5_savings", 0.0) for i, t in enumerate(turns) if "R5" in findings.get(i, []))
    if total_savings > 0.01:
        out.append(f"- 💰 **若按建议降级模型，本 session 可省 {fmt_money(total_savings)}** ({total_savings/total_cost*100:.0f}% of 总成本)")

    return "\n".join(out)


# ============================================================
# 辅助命令：列出/挑选 session
# ============================================================

def list_top_sessions(days: int = 30, limit: int = 10) -> list[tuple[str, float]]:
    q = (f'sum by (session_id) (sum_over_time('
         f'{{service_name="{SERVICE}"}} | event_name="api_request" '
         f'| unwrap cost_usd [{days}d]))')
    r = loki_query_instant(q, now_ns())
    rows = [(x["metric"].get("session_id", ""), float(x["value"][1])) for x in r]
    rows.sort(key=lambda x: -x[1])
    return rows[:limit]


# ============================================================
# main
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id", nargs="?", help="session id（或前缀）")
    ap.add_argument("--top", type=int, help="诊断最近 30 天最贵的 top N")
    ap.add_argument("--list", action="store_true", help="仅列出最近 30 天的 session")
    args = ap.parse_args()

    if args.list:
        for sid, cost in list_top_sessions():
            print(f"{fmt_money(cost):>8}  {sid}")
        return 0

    if args.top:
        for sid, cost in list_top_sessions(limit=args.top):
            t, f, sf = diagnose(sid)
            print(render_report(sid, t, f, sf))
            print("\n---\n")
        return 0

    if not args.session_id:
        ap.print_help()
        return 1

    # 支持前缀匹配
    sid = args.session_id
    if len(sid) < 36:
        candidates = [s for s, _ in list_top_sessions(limit=50) if s.startswith(sid)]
        if not candidates:
            print(f"未找到以 {sid} 开头的 session", file=sys.stderr)
            return 2
        sid = candidates[0]

    t, f, sf = diagnose(sid)
    print(render_report(sid, t, f, sf))
    return 0


def diagnose(session_id: str):
    turns = build_turns(session_id)
    findings, session_findings = run_rules(turns)
    return turns, findings, session_findings


if __name__ == "__main__":
    sys.exit(main())
