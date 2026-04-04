# Claude Code 费用分析看板设计

## 背景

Claude Code 订阅用户无法直接看到细粒度的 token 消耗数据。需要一个本地可视化方案，帮助理解费用构成、定位高消耗 session、优化使用习惯。

## 方案选型

经过评估，放弃自建 TypeScript Web 应用，采用 **Claude Code 原生 OTEL + claude-code-otel + Grafana** 方案：

- Claude Code 原生支持 OpenTelemetry，数据结构化、官方维护
- Grafana 是成熟的可观测性平台，支持下钻、过滤、告警、周期对比
- claude-code-otel 提供 Docker Compose 一键部署（Collector + Prometheus + Loki + Grafana）

## 架构

```
Claude Code
  │ (OTEL gRPC)
  ▼
OTEL Collector (localhost:4317)
  ├──▶ Prometheus (metrics: token/cost/session)
  ├──▶ Loki (events: api_request, tool_result, user_prompt)
  └──▶ Grafana Tempo (traces: 可选，调用链瀑布图)
         │
         ▼
      Grafana (localhost:3000)
```

## Claude Code 环境变量配置

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317

# 开启 prompt 和工具详情记录（用于深度分析）
export OTEL_LOG_USER_PROMPTS=1
export OTEL_LOG_TOOL_DETAILS=1

# 开启 Traces（可选，用于调用链下钻）
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_LOG_TOOL_CONTENT=1
```

## OTEL 数据源

### Metrics（Prometheus）

| Metric | 用途 |
|--------|------|
| `claude_code.cost.usage` | 费用趋势，按 model/session.id 分组 |
| `claude_code.token.usage` | Token 消耗，按 type(input/output/cacheRead/cacheCreation) 分组 |
| `claude_code.session.count` | 活跃 session 数 |
| `claude_code.lines_of_code.count` | 代码变更量 |
| `claude_code.active_time.total` | 活跃时长 |

### Events（Loki）

| Event | 关键字段 | 用途 |
|-------|---------|------|
| `claude_code.api_request` | input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, model, duration_ms | 逐轮费用曲线 |
| `claude_code.tool_result` | tool_name, duration_ms, success, tool_result_size_bytes | 工具使用分析 |
| `claude_code.user_prompt` | prompt_length, prompt(可选) | 用户交互分析 |

### 关联机制

- `session.id`：同一 session 内所有 events 共享
- `prompt.id`：同一用户提问触发的所有 API 请求和工具调用共享
- `event.sequence`：session 内的事件顺序号

## Grafana 看板设计

### 第一层：总览看板（claude-code-otel 自带 + 补充）

自带面板：
- Overview（活跃 session、费用、token、代码变更）
- Cost & Usage Analysis（按模型的费用趋势、token 分类）
- Tool Usage & Performance（工具频率、成功率）
- Performance & Errors（API 延迟、错误率）
- Event Logs（实时事件流）

需补充的面板：
- **Session 费用排行表**：Loki 查询 api_request events，按 session.id 聚合 cost_usd，排序展示
- **按小时费用分布**：Prometheus cost.usage 按小时聚合
- **周热力图**：按 weekday × hour 聚合费用
- **周期对比**：本周 vs 上周费用、session 数、日均消耗

### 第二层：Session 详情看板（新建）

通过 Grafana 变量 `$session_id` 实现下钻：

- **Session 摘要**：总费用、总轮次、时间跨度、模型、工具调用分布
- **逐轮费用曲线**：X=event.sequence，Y=cost_usd（柱状）+ 累计费用（折线）
- **Context 构成堆叠面积图**：X=event.sequence，Y 分层为 cache_read / cache_creation / input / output tokens
- **工具返回大小趋势**：tool_result_size_bytes 随轮次变化
- **Prompt 内容时间线**（需开启 OTEL_LOG_USER_PROMPTS）：Loki Logs 面板按 session.id 过滤

### 分析能力

通过 Grafana 的原生能力实现：

- **费用异常检测**：Grafana Alerting，当 session 费用或单轮费用超过动态阈值时告警
- **context 膨胀分析**：堆叠面积图直接可视化，cache_read 增长斜率一目了然
- **工具滥用检测**：tool_result events 按 tool_name 聚合，结合 tool_result_size_bytes 找出大返回
- **周期对比**：Grafana time shift 功能，对比不同时间段的使用模式

## 实施步骤

1. 克隆 claude-code-otel 并 docker compose up
2. 配置 Claude Code 环境变量（写入 shell profile）
3. 验证数据流通（使用 Claude Code 产生一些数据，在 Grafana 中确认）
4. 补充 Session 费用排行面板
5. 新建 Session 详情看板
6. 配置告警规则

## 局限与未来扩展

- **历史数据**：OTEL 只采集开启后的数据。未来可考虑写 JSONL 导入脚本补历史
- **对话内容浏览**：Loki Logs 面板能显示 prompt 文本，但阅读体验有限。如需更好的 UI，可后续补一个轻量前端
- **system prompt 内容**：OTEL 不直接记录 system prompt 原文，只能通过首轮 cache_creation_tokens 大小间接判断
