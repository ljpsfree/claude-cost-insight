# Session 根因诊断脚本 — 设计文档

日期: 2026-04-09
状态: 已批准，准备实施

## 目标

给定一个 session_id，自动分析该 session 的成本构成，定位"为什么贵"的根因，输出可操作的建议。

产出形态：命令行脚本 `scripts/diagnose_session.py`，输出 markdown 报告到 stdout。

## 非目标

- 不做 Grafana 看板
- 不做批量扫描 / 定期报告（验证单 session 有效后再扩）
- 不做 LLM 二次分析（纯规则）
- 不做告警

## 数据来源

Loki `http://localhost:3100`，`service_name="claude-code"`，三类事件由 `prompt_id` 串联：

| event | 关键字段 |
|---|---|
| `user_prompt` | prompt (全文), prompt_id, session_id, event_timestamp |
| `api_request` | prompt_id, model, cost_usd, input/output/cache_read/cache_creation_tokens, duration_ms |
| `tool_result` | prompt_id, tool_name, tool_input (全文), tool_result_size_bytes, duration_ms, success |

## 核心数据结构

以 `prompt_id` 为主键聚合成"回合 (Turn)"：

```
Turn:
  prompt_id, seq, timestamp
  user_prompt: str
  prompt_length: int
  api_requests: [ {model, cost, input, output, cache_read, cache_creation, duration} ]
  tool_calls:   [ {tool_name, tool_input, result_size, duration, success} ]
  # 派生
  total_cost: float
  total_output_tokens: int
  max_tool_result_size: int
  cumulative_cache_read_entering: int   # 进入此回合时的累计 cache_read（衡量 context 膨胀）
```

## 诊断规则

参考业界最佳实践（Anthropic 官方、社区博客）总结，规则分回合级和 session 级两类。

### 回合级规则

| ID | 名称 | 触发条件 | 建议 |
|---|---|---|---|
| R1 | 工具爆炸 | 单次 `tool_result_size_bytes > 100KB` | 先 Grep 定位再局部 Read |
| R2 | 回合雪球 | 进入时 cum_cache_read > 均值 2x 且 cost > 均值 2x | 此处应 /clear 携带结论重开 |
| R3 | 闲聊贵 | `prompt_length < 20` 且 cost > 均值 2x | 考虑开新 session |
| R4 | 失败重试 | 相邻 5 回合 prompt Jaccard > 0.6 | 给出更明确的方向或换思路 |
| R5 | 模型错配 | 任务复杂度分级判定，实际模型高于推荐 tier | 降级到 sonnet/haiku，附带节省金额估算 |
| R7 | 大 output 浪费 | 单回合 `output_tokens > 5000` | 要求只给结论/diff，避免冗长解释 |
| R10 (回合) | Cache 失效 | `cache_creation > 均值 3x` 且 > 5000 | prefix 变动，把变动部分放后面 |

### Session 级规则

| ID | 名称 | 触发条件 | 建议 |
|---|---|---|---|
| R6 | 高基线 | 平均每回合成本 > $1.0 | 检查 CLAUDE.md / 启用的 skill，整体 context 初始就重 |
| R9 | 启动开销大 | 首次 cache_creation > 50K | 加载了过多 skill/MCP/CLAUDE.md |
| R10 (session) | Cache 失效 | `cache_read / (cache_read + cache_creation) < 0.7` | 整体命中率低 |

### R5 复杂度分级逻辑

每个回合根据 `tool_calls 数量 / 工具种类 / output_tokens / prompt_length` 推荐一个 tier：

- **haiku**：纯问答 / 仅 Read+Grep+Glob 各 ≤1 次 / output<300 / prompt<100
- **sonnet**：tool_calls ≤5 / 无 Edit/Write / output<2000
- **opus**：以上都不满足（多文件改、大输出、复杂任务）

实际模型 tier 高于推荐 → 命中 R5，按对应 tier 价格表重算成本，输出"本回合可省 $X"。

### 价格表（USD per million tokens）

| 模型 | input | output | cache_read | cache_creation |
|---|---|---|---|---|
| opus | 15.0 | 75.0 | 1.50 | 18.75 |
| sonnet | 3.0 | 15.0 | 0.30 | 3.75 |
| haiku | 1.0 | 5.0 | 0.10 | 1.25 |

阈值在第一版写死为常量，后续根据实际报告效果调整。

## 报告结构

```
# Session <id> 诊断报告

**时间**: <start> ~ <end>  **时长**: <duration>
**总成本**: $X.XX  **回合**: N  **API 请求**: M  **工具调用**: K
**模型**: opus X% / sonnet Y% / ...

## 成本构成
- cache_read: $X (XX%)
- output:     $X (XX%)
- input:      $X (XX%)
- cache_creation: $X (XX%)

## Top 5 最贵回合
| # | prompt (截断) | cost | 命中规则 |
|---|---|---|---|

## 🔴 异常回合详情

### Turn #N "<prompt 前 60 字>" — $X.XX
**命中规则**: R1, R2
**证据**:
- Read("xxx.py") 返回 487KB
- 该回合后 cache_read 基线从 40k 跃升到 210k
**建议**: <对应规则的建议>

## 📈 回合成本趋势
文本柱状图（简单 ASCII）展示每回合 cost，高亮异常回合

## 💡 总结
- 主导成本: cache_read (72%)
- 关键转折: Turn #23 之后 context 膨胀了 5x
- Top 建议: 1) ... 2) ... 3) ...
```

## 实现结构

单文件 `scripts/diagnose_session.py`，~300 行：

```
main(session_id)
  turns = fetch_and_build_turns(session_id)
  findings = run_rules(turns)
  report = render_report(turns, findings)
  print(report)

fetch_and_build_turns(session_id) -> list[Turn]
  拉三类事件 → 按 prompt_id 聚合 → 按 event_sequence 排序 → 计算 cumulative 字段

run_rules(turns) -> dict[turn_idx, list[FindingID]]
  每条规则一个函数，返回命中的 turn 索引集合

render_report(turns, findings) -> str
  模板化 markdown
```

依赖：只用标准库（`urllib`, `json`, `datetime`, `argparse`）。无需 uv/pyproject 调整。

## 数据获取策略

Loki `/loki/api/v1/query_range`，时间范围：按 session_id 先查 user_prompt 的 min/max timestamp 作为边界，再拉该范围内的 api_request 和 tool_result。避免全量扫描。

limit 设高（5000），单 session 通常不超过 1000 条事件。

## 验收标准

1. `python3 scripts/diagnose_session.py <id>` 能对 top 3 贵 session 跑出报告
2. 报告里至少有一条 finding 是我看了觉得"确实是这样"的
3. 如果一条都没命中，说明规则阈值需要调整——这是预期中的迭代起点

## 已知限制

- 阈值是硬编码，后续调
- R4 失败重试用简单 token Jaccard，中文分词粗糙
- 不支持时间范围筛选（固定按整个 session）
- 单 session，不做跨 session 对比
