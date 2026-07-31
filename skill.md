---
name: lifevault-version-learning
description: LifeVault v0.1-v0.15 的版本迭代、实现路径与工程学习记录。
---

# LifeVault 版本迭代学习手册

这份文档记录 LifeVault 从 v0.1 到 v0.15 的演进过程。它不是产品使用说明，而是面向学习者的实现索引：每一版解决什么问题、为什么这样拆分、代码写在哪里、可以学到什么。

## 1. 项目的核心目标

LifeVault 是一个本地优先的生活事项记录与提醒 Agent，支持：

- 商品订单、订阅服务和生活账单的自然语言录入。
- 本地 Qwen 或规则提取器生成结构化候选数据。
- Python 确定性工具负责日期计算和业务校验。
- LangGraph 负责编排缺字段、校对、查重、保存和提醒确认。
- MCP Server 作为个人数据和副作用的统一边界。
- SQLite 保存记录、提醒、偏好、检查点和审计日志。
- Reminder Worker 在后台扫描并发送桌面通知。
- 已保存记录可以通过 MCP 预览并原子更新，同时重排受影响提醒。

核心设计原则：

```text
模型负责理解
程序负责计算
LangGraph 负责流程
MCP 负责副作用边界
SQLite 负责权威状态
Worker 负责长期任务
用户负责最终确认
```

大模型不直接写数据库、不直接调用桌面通知，也不能绕过用户确认。模型给出的 `tool_plan` 只是经过白名单过滤的候选计划，实际执行顺序由 Agent 和 LangGraph 决定。

## 2. 当前完整链路

```text
自然语言输入
  -> 隐私与长度清洗
  -> Qwen / fallback 结构化提取
  -> 缺字段补充
  -> 日期冻结与确定性计算
  -> 结构化校对
  -> 最终重复检测
  -> 用户确认
  -> MCP 保存记录
  -> 用户选择提醒
  -> MCP 原子创建提醒
  -> Worker 扫描
  -> 桌面通知 / 控制台回退
  -> 脱敏审计

已保存记录
  -> MCP 读取
  -> 类型化局部补丁
  -> 更新与提醒影响预览
  -> 用户确认
  -> 乐观锁、记录更新、提醒重排、审计和幂等结果原子提交
```

主要代码入口：

- [`lifevault/models/schemas.py`](lifevault/models/schemas.py)：领域模型和 Pydantic 校验。
- [`lifevault/models/llm_factory.py`](lifevault/models/llm_factory.py)：Qwen 与 fallback 提取。
- [`lifevault/tools/date_tools.py`](lifevault/tools/date_tools.py)：确定性日期工具。
- [`lifevault/agent/service.py`](lifevault/agent/service.py)：业务构建和提醒规划。
- [`lifevault/agent/graph_agent.py`](lifevault/agent/graph_agent.py)：LangGraph 工作流。
- [`lifevault/mcp_server/server.py`](lifevault/mcp_server/server.py)：MCP 工具边界。
- [`lifevault/storage/repository.py`](lifevault/storage/repository.py)：SQLite 事务和查询。
- [`lifevault/records/update_planner.py`](lifevault/records/update_planner.py)：已保存记录更新和提醒重排计划。
- [`lifevault/worker/reminder_worker.py`](lifevault/worker/reminder_worker.py)：后台提醒执行。
- [`lifevault/app/main.py`](lifevault/app/main.py)：Streamlit 界面。
- [`lifevault/cli.py`](lifevault/cli.py)：CLI 与调试入口。

## 3. 版本总览

| 版本 | Commit | 迭代主题 | 主要解决的问题 |
|---|---|---|---|
| v0.1 | `574c025` | 最小可运行闭环 | 先跑通文本录入、保存和提醒 |
| v0.2 | `bdba907` | LangGraph HITL | 流程可中断、恢复和确认 |
| v0.3 | `688a857` | MCP Server | 建立独立个人数据服务 |
| v0.4 | `c388bdf` | MCP Client 接线 | Graph 写操作真正经过 MCP |
| v0.5 | `c9ae7e8` | 订阅续费链路 | 支持会员和续费日期 |
| v0.6 | `c5be8e9` | MCP 边界收紧 | 查询和 UI/CLI 数据访问统一 |
| v0.7 | `73d8a33` | Worker 加固 | 通知失败、免打扰和稍后提醒 |
| v0.8 | `bab3af0` | 抽取评测基线 | 用数据衡量提取质量 |
| v0.9 | `7e070d8` | fallback 优化 | 基于失败样例提升规则提取 |
| v0.10 | `b6c0b5a` | 审计闭环 | 写操作可追踪且不泄露隐私 |
| v0.11 | `374a6af` | Preference Memory | 偏好也统一经过 MCP |
| v0.12 | `bf2584c` | 周期续费提醒 | Worker 自动推进下一周期 |
| v0.13 | `d0cf075` | 商品多提醒 | 退货和保修提醒原子批量创建 |
| v0.14 | `bbb7ec2` | 保存前校对 | 用户可修改候选并重新计算 |
| v0.15 | `8cf4704` | 保存后编辑 | 原子更新记录并重排已有提醒 |

---

## 4. v0.1：最小可运行闭环

**目标**

先验证 LifeVault 的核心想法是否能运行，不急着引入复杂编排和跨进程协议。

**实现内容**

- 建立商品、订阅、账单、提醒和用户偏好的 Pydantic 模型。
- 建立 SQLite 数据表和 `VaultRepository`。
- 接入本地 Qwen OpenAI-compatible 接口。
- Qwen 不可用时使用 `FallbackExtractor`。
- 解析相对日期、计算截止日期和提醒时间。
- 保存前检查必填字段、重复项和用户确认。
- 提供 Streamlit、CLI 和基础 Reminder Worker。
- 添加 purchase、subscription、bill 三个任务 Skill。

**关键实现**

- `QwenClient.extract_record()` 请求本地模型并校验 JSON。
- `FallbackExtractor.extract_record()` 提供可离线运行的最小规则。
- `LifeVaultAgent.create_draft()` 构建候选记录和提醒。
- `LifeVaultAgent.save_draft()` 在确认后写入数据库。
- `stable_key()` 为记录和提醒生成幂等键。
- Worker 扫描 SQLite 中到期的 `pending` 提醒。

**为什么这一版不直接上 LangGraph 和 MCP**

最先需要验证的是业务闭环，而不是框架。把领域模型、日期工具、存储和提醒先做出来，可以确认核心逻辑成立，也为后续框架接入提供稳定内核。

**学习重点**

- 先构建最小纵向切片，而不是先搭完整平台。
- 模型输出必须再经过 Pydantic 和确定性业务校验。
- 长期提醒必须落库，不能只放在内存或聊天上下文。

**建议阅读**

```text
models/schemas.py
-> tools/date_tools.py
-> storage/repository.py
-> agent/service.py
-> worker/reminder_worker.py
```

---

## 5. v0.2：LangGraph Human-in-the-loop

**目标**

把 v0.1 的线性业务流程升级成可以暂停、恢复和持久化的状态机。

**实现内容**

- 新增 `GraphAgent` 和 `LifeVaultGraphState`。
- 使用 LangGraph `interrupt()` 实现四个中断：
  - 缺失字段补充
  - 重复记录处理
  - 保存记录确认
  - 创建提醒确认
- 使用 SQLite Checkpointer 保存图状态。
- CLI 支持 `state` 和 `resume`。
- Streamlit 保存 `thread_id` 并恢复未完成流程。

**关键实现**

```text
input_guard
-> extract_record
-> validate_record
-> prepare_record
-> review_duplicate
-> confirm_record
-> save_record
-> confirm_reminder
-> create_reminder
```

每个节点只负责一种状态转换，条件边负责决定下一步。数据库写入仍然复用 v0.1 的业务能力。

**解决的问题**

v0.1 中用户必须一次完成流程，进程退出后草稿状态丢失。v0.2 将“对话流程状态”和“业务数据状态”分开持久化。

**学习重点**

- Human-in-the-loop 不是普通的 `input()`，而是可持久化的工作流中断。
- `thread_id` 是恢复同一业务流程的身份。
- 图节点应保持职责单一，否则恢复和测试会变复杂。

---

## 6. v0.3：Personal Vault MCP Server

**目标**

把个人数据能力从 Agent 内部抽离，建立明确的数据和副作用边界。

**实现内容**

- 新增独立 FastMCP stdio Server。
- 暴露记录、搜索、查重、状态和提醒工具。
- 工具参数使用 Pydantic 校验。
- 写操作要求 `user_confirmed=true`。
- `user_id` 从本地配置注入，不暴露给模型。
- 提供真实 stdio `mcp-smoke` 集成测试。

**初始 MCP 工具**

```text
save_record
search_records
get_record
find_duplicate
update_record_status
create_reminder
list_reminders
snooze_reminder
cancel_reminder
```

**注意**

这一版只是把 MCP Server 建立起来，Graph 的主写入路径尚未全部迁移到 MCP。这是有意拆分：先验证 Server 可以独立运行，再改主链路。

**学习重点**

- MCP 是能力协议和边界，不是让模型直接拥有数据库权限。
- Server 必须在没有大模型的情况下独立测试。
- 用户确认、用户隔离和参数校验必须在 Server 端再次执行。

---

## 7. v0.4：Graph 写操作经过 MCP Client

**目标**

让 MCP 从“可运行的旁路服务”变成 Agent 的真实数据访问路径。

**实现内容**

- 新增 `PersonalVaultMcpClient` Protocol。
- 新增 `InProcessPersonalVaultMcpClient`。
- Graph 的重复检测、记录保存和提醒创建改为调用 MCP Client。
- stdio Server 继续保留给外部客户端和集成测试。
- 增加 MCP Client 和 Graph 写入边界测试。

**为什么使用 in-process Client**

本地 Streamlit 和 CLI 不需要为了每次调用都启动 stdio 子进程，但业务代码仍应面向 MCP 接口编程。in-process Client 复用同一个 Server 工具定义，同时减少本地运行复杂度。

**解决的问题**

v0.3 虽然有 MCP Server，但 Agent 仍可能直接写 Repository。v0.4 才真正形成：

```text
GraphAgent -> MCP Client -> MCP Tool -> Repository -> SQLite
```

**学习重点**

- “实现了服务”不等于“主链路使用了服务”。
- Protocol 可以让业务代码依赖接口，并方便注入测试替身。
- 边界测试要验证调用路径，而不只是验证最终数据存在。

---

## 8. v0.5：订阅续费记录链路

**目标**

从通用记录扩展到更完整的会员和软件订阅场景。

**实现内容**

- 提取服务名、金额、付费周期、自动续费和续费日期。
- 支持月度、年度和周度周期。
- 解析：
  - `每月 15 号`
  - `下个月 3 号`
  - `每年 7 月 15 日`
  - `明年 7 月 15 日`
  - 具体日期和相对日期
- 订阅的下一续费日期写入记录 `deadline`。
- 订阅详情保存续费锚点和提醒偏移。
- 新增 MCP/CLI upcoming subscriptions 查询。
- 创建 `renewal` 类型提醒。

**重要区分**

v0.5 跑通的是“录入一次续费记录并创建提醒”，还没有在提醒结束后自动生成下一周期。真正的周期推进在 v0.12 完成。

**学习重点**

- 周期规则和具体日期是两种不同的数据。
- 模型只负责提取“每月 15 号”，日期工具负责计算下一实际日期。
- 月末、闰年和周期锚点需要独立测试，不能依赖普通日期加法。

---

## 9. v0.6：收紧 MCP 数据边界

**目标**

消除 Agent、CLI 和 Streamlit 对 Repository 的业务数据直读，避免出现多套权限路径。

**实现内容**

- 自然语言查询通过 MCP `search_records`。
- CLI 的记录列表、提醒列表、状态更新和订阅查询通过 MCP。
- Streamlit 的记录和提醒页面通过 MCP。
- MCP 失败时明确显示错误，不静默回退到 Repository。
- 新增 `test_mcp_boundary.py`，通过源码和替身验证边界。

**当时保留的例外**

用户偏好仍由宿主直接读写 Repository。这个缺口后来在 v0.11 关闭。

**学习重点**

- 安全边界最怕“方便起见”的旁路。
- 失败时静默回退会让架构边界名存实亡。
- 可以用边界测试防止未来代码重新引入直接数据库访问。

---

## 10. v0.7：Reminder Worker 加固

**目标**

把“能发一次提醒”升级成可长期运行、可恢复、可测试的后台任务。

**实现内容**

- 通知 Provider 可注入，便于测试桌面和控制台渠道。
- 桌面通知失败后回退控制台，同时将目标渠道结果记为失败。
- 发送前重新读取记录并检查状态。
- 已完成或取消事项的提醒自动取消。
- 支持免打扰时段。
- 到达免打扰时段时自动 snooze 到结束时间。
- 稍后提醒创建子任务并保留 `parent_id` 链路。
- 防止 Worker 重复运行时重复发送。
- CLI 和 Streamlit 提供 snooze 操作。

**关键状态**

```text
pending -> sending -> sent
                  -> failed
pending -> snoozed
pending -> cancelled
```

**学习重点**

- 通知是否展示和任务是否成功是两个不同概念。
- Worker 必须在执行副作用前重新验证权威状态。
- “稍后提醒”最好创建新任务，不要覆盖原发送历史。

---

## 11. v0.8：抽取评测基线

**目标**

停止凭感觉改 Prompt 和正则，建立可重复的质量测量方式。

**实现内容**

- 新增 `lifevault/eval/runner.py`。
- 建立 60 条手写 JSONL 样例。
- 覆盖商品、订阅、账单、查询和缺字段输入。
- 默认使用 fallback，保证结果可复现。
- 可选 `--use-qwen` 手动评测本地模型。
- 支持输出 JSON 明细报告。
- CLI 评测只生成报告，不设置阻断式阈值。

**初始基线**

```text
总样例：60
意图准确率：100.0%
记录类型准确率：93.2%
字段准确率：85.4%
整例准确率：48.3%
```

**为什么整例准确率明显更低**

一条记录只要一个字段错误，整例就算失败。它比字段准确率更能反映用户是否需要手工修正。

**学习重点**

- 先建立基线，再做优化。
- 评测样例应保存期望字段，而不是只看最终模型文本。
- 可复现的 fallback 基线和有波动的模型评测应分开报告。

运行方式：

```bash
python3 -m lifevault.cli eval
python3 -m lifevault.cli eval --json-out /tmp/lifevault-eval.json
```

---

## 12. v0.9：基于评测优化 fallback

**目标**

针对 v0.8 暴露的高频失败进行小而确定的修复。

**主要失败簇**

- `ChatGPT Plus 每月 20 美元` 被分错记录类型。
- USD、美元、美金和 `$` 金额提取不足。
- Netflix、Spotify、Apple Music 等服务名提取不稳定。
- 账单标题和缴费日期表达覆盖不足。
- Apple Store 等带空格商家名被截断。

**实现内容**

- 增加已知订阅服务和账单名称词表。
- 金额支持 CNY 和多种 USD 表达。
- 扩展日期在动作前/后的表达。
- 改进商品商家和标题清洗。
- 保持规则小型、确定性，不引入 NLP 依赖。

**优化结果**

```text
总样例：60
意图准确率：100.0%
记录类型准确率：100.0%
字段准确率：100.0%
整例准确率：100.0%
```

**学习重点**

- 优先修复影响大量样例的失败簇。
- 小数据集上的 100% 不等于真实世界 100%，它只说明当前回归集已覆盖。
- 规则优化必须配套回归测试，否则后续很容易反复退化。

---

## 13. v0.10：隐私安全的审计闭环

**目标**

让 MCP 和 Worker 的关键操作可追踪，同时避免审计日志变成新的隐私泄露源。

**实现内容**

- 成功写操作和业务数据在同一 SQLite 事务中提交。
- 审计失败会回滚业务写入。
- MCP 校验拒绝和执行失败使用稳定错误码。
- Worker 发送、失败和自动取消写入审计。
- 新增 `list_audit_logs` MCP 工具。
- CLI 和 Streamlit 增加审计查询。
- 支持 actor、action、result 和游标分页过滤。
- 审计记录保持 append-only。

**隐私策略**

允许记录：

```text
actor
action
result
record_type
reminder_type
changed_fields
error_code
```

禁止记录：

```text
原始输入
标题
备注
订单号
提醒消息
查询关键词
幂等键
异常详情
```

**学习重点**

- 审计与业务写入必须共享事务，否则可能出现“写成功但无审计”。
- 只对字段名和值分别建立允许列表，比通用脱敏正则更可靠。
- 审计日志本身也是敏感数据。

---

## 14. v0.11：用户偏好 Memory 经过 MCP

**目标**

关闭 v0.6 留下的偏好访问旁路，并把长期偏好建模成受控 Memory。

**实现内容**

- 新增 MCP `get_preferences` 和 `update_preferences`。
- 支持默认提醒时间、默认提前天数和免打扰时段。
- 更新是部分更新，不要求每次提交完整对象。
- 更新需要用户确认。
- 实际变化和审计共用事务。
- 无变化更新不写数据库，也不产生审计噪声。
- 审计只记录 `changed_fields`，不记录具体时间值。
- Agent 提醒规划通过 MCP 读取默认偏好。
- Streamlit 和 CLI 设置页改走 MCP。
- Worker 作为受信后台进程继续直接读取 Repository。
- 兼容旧数据库中的非法时间值，读取时安全回退默认值。

**解决的问题**

此前 fallback 在用户只说“提醒我”时会默认猜两天。v0.11 改为读取用户保存的 `default_advance_days`，让长期偏好成为权威值。

**学习重点**

- Memory 不是聊天历史，而是明确建模、可校验的长期偏好。
- 部分更新需要区分“字段未提交”和“明确清空”。
- no-op 更新不应制造版本号和审计噪声。

---

## 15. v0.12：订阅提醒自动进入下一周期

**目标**

补齐 v0.5 尚未完成的周期闭环。

**实现内容**

- Worker 处理已到期的自动续费订阅后推进记录 `deadline`。
- 自动创建下一周期 `renewal` 提醒。
- 记录更新、提醒创建和审计在同一事务。
- 使用 `expected_version` 做乐观锁。
- 使用稳定幂等键防止重复周期提醒。
- 月度续费保留原始日锚点。
- 年度续费保留月日锚点。
- 正确处理 31 日和 2 月 29 日。
- Worker 长时间停机后快进到仍在未来的周期。
- 手动续费、取消订阅、取消提醒和跳过提醒不自动推进。

**关键算法**

```text
读取当前 renewal anchor
-> 计算下一周期
-> 如果提醒时间仍在过去，继续快进
-> 得到第一个未来提醒
-> 乐观锁更新记录
-> 幂等创建提醒
-> 同事务写审计
```

**学习重点**

- 周期任务不能简单地给当前日期加一个月。
- Worker 恢复策略必须明确：补发全部、跳过，还是快进。
- 乐观锁和幂等键解决的是不同问题，两者都需要。

---

## 16. v0.13：商品退货和保修多提醒

**目标**

让一条商品记录同时拥有退货和保修两个不同期限及提醒。

**实现内容**

- 商品详情增加：
  - `return_deadline`
  - `warranty_deadline`
- 保修支持明确日期或日历月时长。
- 明确日期与时长冲突时，明确日期优先并提示警告。
- 提醒意图和提前天数按退货/保修分别提取。
- 没有提醒语言时不创建候选提醒。
- 泛指“提醒我”时对已有期限生成候选。
- Streamlit 可以逐条勾选提醒。
- CLI 确认完整提醒批次。
- 新增 MCP `create_reminders`。
- 每批限制 1 到 5 条，且必须属于同一记录。
- 批量提醒原子提交。
- `reminder_batches` 保存请求哈希和结果 ID。
- 相同请求重试返回原结果；同一键不同内容被拒绝。
- 批量审计只保存数量和提醒类型。
- 保留旧 `create_reminder` 和单提醒检查点兼容。
- 已完成商品仍可发送保修提醒，已退货或取消商品不发送。

**抽取评测扩充**

评测集从 60 条扩充到 72 条：

```text
整例：72/72
字段：448/448
```

**学习重点**

- 批量写入的“原子性”和“幂等性”需要单独持久化批次元数据。
- 多提醒不能只把单提醒 API 循环调用，否则中途失败会留下部分结果。
- 兼容层可以同时维护 `reminder` 和 `reminders`，逐步迁移旧检查点。

---

## 17. v0.14：保存前结构化校对

**目标**

解决模型只要提错一个字段，用户就必须取消并重新输入的问题。

**实现内容**

- 新增 `CandidateCorrections` 严格校对 Schema。
- 按记录类型限制可编辑字段，禁止修改 `record_type`。
- 商品、订阅和账单使用不同 Streamlit 表单。
- 标题和业务名称可以分别校对。
- 金额、币种、日期、时长和提醒配置使用类型化控件。
- 文本长度、数值范围、控制字符和日期顺序严格校验。
- 修改批次原子应用；一个字段非法时全部不落地。
- 使用 `field_errors` 返回可修正错误。
- “应用修改”和“确认保存”分离。
- 存在未应用修改时禁用保存。
- 修改后重新执行：
  - 必填字段校验
  - 日期计算
  - 提醒规划
  - 最终重复检测
- 相对日期在首次预览时冻结成类型化日期。
- 明确退货/保修日期优先于时长结果并显示冲突警告。
- 保存幂等键基于规范化后的完整最终记录。
- CLI 支持 `--corrections-json`。
- 兼容 v0.13 的记录确认、重复确认和提醒检查点。

**新流程**

```text
提取
-> 缺字段补充
-> 日期冻结
-> 生成记录和提醒预览
-> 结构化校对
   -> 修改：重新校验和计算
   -> 确认：执行最终重复检测
-> MCP 保存
-> 独立提醒确认
```

**为什么查重移到校对之后**

如果先根据错误的模型字段查重，用户修改后原查重结果就失效。v0.14 只对最终确认值执行重复检测，避免无效确认。

**为什么校对不直接调用 MCP**

校对发生在草稿阶段，没有业务数据副作用。只有最终保存和提醒创建才进入 MCP，并产生审计记录。

**学习重点**

- 草稿编辑和已保存记录更新是两个不同能力。
- Patch 必须区分字段未提交、明确清空和非法值。
- 原子校对可以避免候选状态被部分更新污染。
- 工作流拓扑变化时，要用旧检查点测试验证恢复兼容性。

---

## 18. v0.15：已保存记录编辑与提醒重排

**目标**

解决记录保存后只能改状态、不能修正金额、日期和业务字段的问题，同时避免记录日期已经修改、提醒仍停留在旧时间。

**实现内容**

- 新增严格的 `RecordUpdatePatch`：
  - 未提交字段保持不变。
  - 显式 `null` 清空可选字段。
  - 标题、金额和币种不能清空。
  - 按 purchase、subscription、bill 限制字段白名单。
  - `record_type`、ID、用户、来源和时间元数据不可修改。
- 新增纯确定性 `update_planner.py`：
  - 合并当前记录和补丁。
  - 计算实际变化字段。
  - 校验日期关系。
  - 判断受影响提醒。
  - 生成取消和替代提醒计划。
- MCP 新增：
  - `preview_record_update`
  - `update_record`
- 预览只读，返回：
  - 更新后的记录。
  - 变化字段。
  - 将取消和创建的提醒。
  - 警告。
  - 疑似重复记录。
- 提交强制要求：
  - `user_confirmed=true`
  - `expected_version`
  - `idempotency_key`
  - 必要时 `duplicate_confirmed=true`
- 相同幂等键和相同请求返回第一次结果；同一键不同请求返回 `idempotency_conflict`。
- 版本冲突返回 `version_conflict` 和最新记录。
- 受影响提醒处于 `sending` 时返回 `reminder_in_flight`，避免旧提醒发送过程中改计划。
- 记录写入、旧提醒取消、替代提醒创建、脱敏审计和幂等结果写入同一 SQLite 事务。
- 旧提醒通过 `parent_id` 连接替代提醒，保留历史链。
- 已发送、失败和已取消提醒不修改。
- 历史记录仍可修正，但是否创建替代提醒遵守原有状态规则。
- 旧提醒表自动迁移为“仅活动提醒时段唯一”，允许只改标题时在同一时刻创建替代提醒。
- Streamlit 的“我的记录”提供类型化编辑、影响预览、重复确认和最终提交。
- CLI 新增 `update`，支持 `--changes-json`、`--dry-run`、`--yes` 和 `--confirm-duplicate`。
- stdio MCP 冒烟测试真实覆盖预览、拒绝未确认更新、成功提交和幂等重放。

**更新数据流**

```text
record_id + expected_version + changes
-> preview_record_update
   -> 读取当前记录和提醒
   -> 严格 Patch 校验
   -> 计算 proposed record
   -> 计算提醒取消/替代计划
   -> 排除自身后重新查重
-> 用户查看预览并确认
-> update_record
   -> BEGIN IMMEDIATE
   -> 检查幂等结果
   -> 重新检查版本
   -> 重新计算完整计划
   -> 更新记录
   -> 取消旧提醒并创建替代提醒
   -> 写脱敏审计
   -> 保存幂等结果
   -> COMMIT
```

**为什么提交时必须重新计算**

预览和确认之间，Worker 或另一个页面可能修改记录或提醒。`update_record` 不能直接执行客户端传回的预览，而要在拿到写锁后重新读取权威状态并计算计划。`expected_version` 防止覆盖记录变化，`reminder_in_flight` 防止覆盖正在发送的提醒。

**为什么更新需要独立幂等表**

仅靠乐观锁无法区分“第一次提交失败”和“第一次已成功但响应丢失”。`record_update_operations` 保存请求哈希和第一次结果，使完全相同的网络重试得到原结果，而不是模糊的版本冲突。

**为什么修改提醒唯一约束**

旧表对 `(record_id, reminder_type, scheduled_at)` 做全历史唯一。只修改标题时，旧提醒要取消，替代提醒仍使用同一时刻，全历史唯一会阻止新提醒插入。v0.15 保留全部历史行，但只约束 `pending` 和 `sending` 活动提醒不能占用相同时段。

**为什么不让大模型直接编辑已保存记录**

持久化修改比草稿提取风险更高。v0.15 先建立结构化 Patch、预览、确认、乐观锁、幂等和审计链路。自然语言修改后续只能作为 Patch 候选生成器，不能绕过这条确定性写入边界。

**学习重点**

- 预览是用户体验，事务内重新计算才是正确性保证。
- Patch 的“未提交”和“显式清空”必须依赖 `model_fields_set` 区分。
- 幂等、乐观锁和用户确认解决的是三个不同问题，不能互相替代。
- 修改主记录时必须把关联提醒视为同一个一致性边界。
- 数据库唯一约束也要表达生命周期语义，不能只表达字段组合。

---

## 19. 当前测试体系

当前共有 109 个测试，主要分层如下：

| 测试文件 | 关注点 |
|---|---|
| `test_date_tools.py` | 相对日期、周期锚点、月末和闰年 |
| `test_fallback_extractor.py` | 规则提取字段 |
| `test_eval_runner.py` | JSONL 评测加载和报告 |
| `test_agent.py` | 记录构建和提醒规划 |
| `test_corrections.py` | 校对白名单、原子性和跨字段校验 |
| `test_graph_agent.py` | 中断、恢复、路由和旧检查点 |
| `test_mcp_client.py` | MCP 工具调用、确认和幂等 |
| `test_mcp_server.py` | 真实 stdio MCP 冒烟 |
| `test_mcp_boundary.py` | Agent/UI/CLI 不绕过 MCP |
| `test_repository.py` | SQLite 事务、查询和乐观锁 |
| `test_record_updates.py` | Patch、提醒重排、幂等、冲突和旧表迁移 |
| `test_audit.py` | 审计事务和隐私允许列表 |
| `test_worker.py` | 通知、免打扰、失败和周期推进 |
| `test_app.py` | Streamlit 校对、提醒选择和已保存记录编辑 |
| `test_cli.py` | CLI 结构化校对与局部记录更新 |

常用验证命令：

```bash
python3 -m unittest discover -s tests -v
python3 -m lifevault.cli eval
python3 -m lifevault.cli mcp-smoke
python3 -m py_compile \
  lifevault/models/schemas.py \
  lifevault/agent/service.py \
  lifevault/agent/graph_agent.py \
  lifevault/records/update_planner.py \
  lifevault/storage/repository.py
git diff --check
```

## 20. 推荐学习顺序

### 第一阶段：理解确定性内核

1. 阅读 `models/schemas.py`。
2. 阅读 `tools/date_tools.py`。
3. 运行 `tests/test_date_tools.py`。
4. 阅读 `storage/database.py` 和 `storage/repository.py`。

目标：理解模型输出最终为什么不能直接保存。

### 第二阶段：理解模型与业务的边界

1. 阅读 `models/llm_factory.py`。
2. 对比 Qwen Prompt 和 fallback 规则。
3. 阅读 `eval/runner.py` 和 `sample_data/examples.jsonl`。
4. 修改一条样例并观察 mismatch 报告。

目标：理解大模型负责模糊理解，评测负责暴露不稳定部分。

### 第三阶段：理解工作流

1. 阅读 `agent/service.py`。
2. 阅读 `agent/graph_agent.py` 的 `_build_graph()`。
3. 跟踪一个 `missing_fields` 流程。
4. 跟踪一个校对后查重流程。
5. 阅读旧检查点兼容测试。

目标：理解业务逻辑、状态和副作用为什么分层。

### 第四阶段：理解 MCP

1. 阅读 `mcp_server/server.py`。
2. 阅读 `mcp_server/client.py`。
3. 运行 `mcp-smoke`。
4. 阅读 `test_mcp_boundary.py`。
5. 对比 Repository 方法和 MCP Tool 的职责。
6. 跟踪一次 `preview_record_update` 到 `update_record` 的提交过程。

目标：理解 MCP 不是数据库 ORM，而是受控能力边界。

### 第五阶段：理解长期任务

1. 阅读 `worker/reminder_worker.py`。
2. 阅读提醒状态转换。
3. 阅读免打扰和 snooze 测试。
4. 阅读订阅周期推进事务。

目标：理解 Agent 对话结束后，长期任务如何继续可靠运行。

## 21. 用 Git 学习每次迭代

查看某一版完整提交：

```bash
git show 8cf4704
```

查看两个版本之间的变化：

```bash
git diff bbb7ec2..8cf4704
```

只看某个文件的演进：

```bash
git log --follow -p -- lifevault/agent/graph_agent.py
```

在独立目录打开旧版本，不影响当前 `main`：

```bash
git worktree add ../LifeVault-v0.8 bab3af0
```

学习完后可以移除该 worktree：

```bash
git worktree remove ../LifeVault-v0.8
```

建议按以下顺序阅读提交：

```text
574c025  v0.1  领域基线
bdba907  v0.2  LangGraph
688a857  v0.3  MCP Server
c388bdf  v0.4  MCP Client 接线
c9ae7e8  v0.5  订阅
c5be8e9  v0.6  数据边界
73d8a33  v0.7  Worker
bab3af0  v0.8  Eval
7e070d8  v0.9  Eval 驱动优化
b6c0b5a  v0.10 Audit
374a6af  v0.11 Memory
bf2584c  v0.12 周期提醒
d0cf075  v0.13 多提醒
bbb7ec2  v0.14 校对闭环
8cf4704  v0.15 保存后编辑
```

## 22. 尚未实现的能力

当前明确未实现：

- 已保存记录的类型转换和删除。
- 通过自然语言直接修改已保存记录。
- OCR、截图和 PDF 导入。
- 邮件、微信和云端推送。
- 云同步和多用户权限。
- 自动付款、退款或取消订阅。
- 草稿版本历史与撤销。

后续版本仍应保持当前迭代方式：

```text
先明确问题
-> 划定版本边界
-> 建立验收样例
-> 实现确定性内核
-> 接入 Graph / MCP / UI
-> 补回归测试
-> 更新本学习记录
```
