# claude-cost-insight

Claude Code 费用分析看板。基于 OpenTelemetry + Prometheus + Loki + Grafana，可视化 Claude Code 的 token 消耗、费用趋势和 session 级下钻分析。

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

## 数据持久化

数据存储在 `data/` 目录下（已 gitignore），`podman compose down` 不会丢失数据。

## 致谢

基础架构参考 [ColeMurray/claude-code-otel](https://github.com/ColeMurray/claude-code-otel)。
