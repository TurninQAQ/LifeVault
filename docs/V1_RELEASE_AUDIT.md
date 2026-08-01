# LifeVault v1.0 发布审计

本审计以 `LifeVault架构方案.md`、当前代码、自动化测试、评测、构建产物和真实运行结果为证据。单个冒烟测试不能替代对应范围的验收。

## 功能验收

| 要求 | 状态 | 权威证据 |
|---|---|---|
| 文本提取 purchase/subscription/bill | 已验证 | `lifevault eval` 与 `--use-qwen` 均为 72/72、448/448 |
| 相对日期与截止日期由工具计算 | 已验证 | `test_date_tools.py`、`test_agent.py`、`test_corrections.py` |
| 缺少关键字段不保存 | 已验证 | `test_graph_agent.py` missing-fields 中断与恢复 |
| 重复记录等待用户决定 | 已验证 | Graph duplicate-review 与 MCP duplicate 测试 |
| 保存和提醒均需确认 | 已验证 | Graph、MCP Client/Server、boundary 测试 |
| 重启恢复未完成流程 | 已验证 | SQLite Checkpointer 恢复和备份后 generation 重载测试 |
| 停机期间提醒不丢失 | 已验证 | Worker 重启、重复执行、失败、免打扰和周期推进测试 |
| 自然语言查询基于 MCP | 已验证 | `test_agent.py` 与 `test_mcp_boundary.py` |
| 已保存记录可安全维护 | 已验证 | typed patch、自然语言更新、状态、归档/恢复和提醒重排测试 |
| 整库可移机与崩溃恢复 | 已验证 | `test_backup.py` 的认证、恶意 ZIP、双库回滚、日志恢复和 Worker 暂停测试 |

## 安全验收

| 要求 | 状态 | 权威证据 |
|---|---|---|
| 模型不能直接删除或弹通知 | 已验证 | MCP Tool 清单、tool-plan allowlist、boundary 测试 |
| 重复写请求不重复产生副作用 | 已验证 | 记录、更新、状态、生命周期、提醒批次幂等测试 |
| 敏感信息不进入普通审计 | 已验证 | `test_audit.py` 字段和值 allowlist 测试 |
| 写操作具有审计 | 已验证 | Repository 事务回滚与 MCP/Worker/backup 审计测试 |
| 提醒发送前复核记录状态 | 已验证 | Worker inactive/archived/concurrent 状态测试 |
| 远程 UI 不被误开放 | 已验证 | supervisor 拒绝非 loopback 地址的测试 |
| 备份保密性和完整性 | 已验证 | scrypt/AES-GCM、统一认证错误、AAD/篡改和密码边界测试 |

## 工程与交付验收

| 要求 | 状态 | 权威证据 |
|---|---|---|
| 日期、去重、MCP、Graph、Worker 有分层测试 | 已验证 | 完整 unittest suite，当前 169 个测试 |
| 至少 50 条评测数据并报告结果 | 已验证 | 72 条创建、36 条更新，fallback/Qwen 全通过 |
| 可独立运行的 MCP Server | 已验证 | 安装版 `lifevault mcp-smoke`，21 个 Tool |
| 可运行的 Streamlit 应用 | 已验证 | 安装版 `lifevault serve` 健康端点与 Selenium 桌面/移动截图 |
| Docker 或一键启动 | 已验证 | `lifevault serve` 同时监督 UI 与 Worker，处理端口、信号和异常退出 |
| 全新环境按文档安装 | 已验证 | wheel 连同完整依赖安装到空目录，包、评测、MCP 与 Streamlit 使用该目录运行 |
| README 架构、启动、演示和取舍 | 已验证 | `README.md`、`docs/DEMO.md`、`skill.md` |
| 安全和发布材料 | 已验证 | `SECURITY.md`、`CHANGELOG.md`、本审计 |
| 三分钟演示材料 | 已验证 | `docs/lifevault-v1-demo.mp4` 与逐段讲稿 |

## 发布命令

```bash
export LIFEVAULT_HOME="$(mktemp -d /tmp/lifevault-release-XXXXXX)"
export LIFEVAULT_USE_QWEN=0
python3 -m lifevault.cli init-db
python3 -m unittest discover -s tests
python3 -m lifevault.cli eval
python3 -m lifevault.cli eval --use-qwen
python3 -m lifevault.cli eval-updates
python3 -m lifevault.cli eval-updates --use-qwen
python3 -m lifevault.cli mcp-smoke
python3 -m lifevault.cli doctor --no-qwen --strict
python3 -m pip check
python3 -m pip wheel . --no-deps --wheel-dir dist
git diff --check
```

最终发布还必须从生成的 v1.0 wheel 在仓库外重复运行 `doctor --strict`、评测、MCP smoke 和 `serve` 健康检查，并确认 Git 工作区与 `origin/main` 同步。

## v1.0 明确边界

OCR、截图/PDF 导入、邮件/微信/云推送、远程访问、多用户权限、PostgreSQL、自动付款退款或取消外部订阅、物理删除、云端/计划/增量备份均不是本地单用户 MVP 的完成条件。它们需要新的数据来源、权限或部署架构，不能在 v1.0 发布审计中假装成小功能。
