## 变更

<!-- 一句话说明目的与影响范围 -->

## 验证

- [ ] `ruff format --check src tests scripts`
- [ ] `ruff check src tests scripts`
- [ ] `PYTHONPATH=src python -m unittest discover -s tests -t .`
- [ ] `agent-gateway --check`
- [ ] 涉及跨仓契约时已执行 `agent-gateway --export-contracts`

## 审查清单

- [ ] 正确性与边界条件
- [ ] 安全、隐私与权限
- [ ] 配置与凭据边界
- [ ] 测试覆盖与回归
- [ ] 提交原子性与提交信息
- [ ] 未发现规则违背

## 关联

<!-- 任务、Issue、决策记录或回滚方式 -->
