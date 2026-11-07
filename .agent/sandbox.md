# Sandbox

- Level: process.
- Reason: Python 服务可在独立 venv 与临时 GATEWAY_HOME/GATEWAY_CACHE 下运行，无需容器。
- Isolated: 数据、端口、缓存、外部服务凭据。
- Limits: 上游模型与 MCP 客户端需 mock 或显式 allowlist，否则外联不可控。
- Last run: not executed.
