# claude-cost-insight

Claude Code 费用分析看板与诊断工具。基于 OpenTelemetry + Prometheus + Loki + Grafana，可视化 Claude Code 的 token 消耗、费用趋势和 session 级下钻分析；并提供根因诊断脚本，自动定位"为什么贵"。

## 快速开始

```bash
# 启动
make up

# 访问 Grafana
open http://localhost:9847   # admin/admin
```

## Claude Code 配置

在 `~/.zshrc` 中添加：

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_LOG_USER_PROMPTS=1
export OTEL_LOG_TOOL_DETAILS=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_LOG_TOOL_CONTENT=1
```

## 端口

| 服务 | 端口 |
|------|------|
| Grafana | 9847 |
| Prometheus | 9090 |
| Loki | 3100 |
| OTEL Collector (gRPC) | 4317 |
| OTEL Collector (HTTP) | 4318 |

## 根因诊断

`scripts/diagnose_session.py` 通过 `prompt_id` 串联 user_prompt / api_request / tool_result 事件，把 session 切分成"回合"并按 9 条规则做根因分析。

```bash
# 列出最近 30 天最贵的 10 个 session
python3 scripts/diagnose_session.py --list

# 诊断指定 session（支持前缀）
python3 scripts/diagnose_session.py dc338752

# 批量诊断最贵的 N 个
python3 scripts/diagnose_session.py --top 5
```

诊断规则覆盖：

| 类别 | 规则 |
|---|---|
| 回合级 | R1 工具爆炸 / R2 回合雪球 / R3 闲聊贵 / R4 失败重试 / R5 模型错配（带降级建议+节省金额） / R7 大 output 浪费 / R10 Cache 失效 |
| Session 级 | R6 高基线 / R9 启动开销大 / R10 Cache 失效 |

仅依赖 Python 标准库，数据源为本地 Loki。设计文档见 `docs/plans/2026-04-09-session-diagnosis-design.md`。

## 数据持久化

数据存储在 `data/` 目录下（已 gitignore），`podman compose down` 不会丢失数据。

## 致谢

基础架构参考 [ColeMurray/claude-code-otel](https://github.com/ColeMurray/claude-code-otel)。
