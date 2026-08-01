---
name: lifevault-version-learning
description: LifeVault v0.1-v0.18 的版本迭代、实现路径与工程学习记录。
---

# LifeVault 版本迭代学习手册

这份文档记录 LifeVault 从 v0.1 到 v0.18 的演进过程。它不是产品使用说明，而是面向学习者的实现索引：每一版解决什么问题、为什么这样拆分、代码写在哪里、可以学到什么。

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
- 已保存记录可以通过独立 LangGraph 自然语言修改，但必须由用户选择目标并确认补丁。
- 已保存记录可以归档和恢复；归档会取消活动提醒，但不会混用业务状态或物理删除数据。
- 整个本地知识库可以导出为强制密码加密的双数据库快照，并通过安全备份和崩溃日志完成全量恢复。

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

大模型不直接写数据库、不直接调用桌面通知，也不能绕过用户确认。创建流程中的 `tool_plan` 和更新流程中的目标/Patch 都只是受校验的候选意图，实际工具、记录 ID、版本、幂等键和执行顺序由 Agent 与 LangGraph 决定。

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

自然语言修改已保存记录
  -> Qwen / fallback 提取目标搜索条件
  -> MCP 搜索
  -> 用户明确选择目标记录
  -> 按已知记录类型提取绝对 Patch 或状态
  -> 确定性解析并冻结相对日期
  -> MCP 只读预览
  -> 用户结构化纠正和确认
  -> MCP 原子提交

归档或恢复已保存记录
  -> 提取明确生命周期意图
  -> MCP 按 active/archived 范围搜索
  -> 用户明确选择目标记录
  -> MCP 只读生命周期预览
  -> 用户确认
  -> 归档：记录版本、提醒取消、审计、幂等结果原子提交
  -> 恢复：清除 archived_at，但不自动重建提醒
```

主要代码入口：

- [`lifevault/models/schemas.py`](lifevault/models/schemas.py)：领域模型和 Pydantic 校验。
- [`lifevault/models/llm_factory.py`](lifevault/models/llm_factory.py)：Qwen 与 fallback 提取。
- [`lifevault/models/update_extractor.py`](lifevault/models/update_extractor.py)：自然语言目标与更新补丁的两阶段提取。
- [`lifevault/tools/date_tools.py`](lifevault/tools/date_tools.py)：确定性日期工具。
- [`lifevault/agent/service.py`](lifevault/agent/service.py)：业务构建和提醒规划。
- [`lifevault/agent/graph_agent.py`](lifevault/agent/graph_agent.py)：LangGraph 工作流。
- [`lifevault/agent/update_graph_agent.py`](lifevault/agent/update_graph_agent.py)：独立的已保存记录自然语言更新图。
- [`lifevault/agent/update_intent.py`](lifevault/agent/update_intent.py)：模型更新意图到严格 Patch 的确定性转换。
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
| v0.16 | `e9cace7` | 自然语言更新 | 选目标、预览、纠正、确认后原子修改 |
| v0.17 | `af6ef69` | 记录归档与恢复 | 可恢复删除、提醒一致性和归档查询范围 |

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

## 19. v0.16：受控的自然语言已保存记录更新

**目标**

让用户可以说“把 ChatGPT Plus 的月费改成 25 美元”或“把房租标记为已支付”，同时保持 v0.15 已建立的预览、确认、乐观锁、幂等、提醒一致性和审计边界。

**实现内容**

- 新增独立 `RecordUpdateGraphAgent`，不扩展创建记录图，避免创建和更新两种生命周期互相污染。
- 模型分两阶段工作：
  - 第一阶段只提取 `record_type`、搜索关键词和目标日期文本。
  - 用户选中目标并知道真实记录类型后，第二阶段才提取更新字段或目标状态。
- 模型不能输出或决定记录 ID、版本、用户确认、重复确认和幂等键；这些权限字段由 Graph 和 MCP 主机代码生成。
- 未预选记录时，Graph 固定调用 MCP `search_records(limit=10)`，即使只有一个结果也必须由用户选择。
- 从单条记录入口发起时可以预选 ID，但 Graph 仍重新通过 MCP 读取记录和版本。
- `NaturalRecordUpdateIntent` 只表达绝对赋值、显式清空和状态意图。`RecordUpdatePatch` 再执行最终字段白名单和类型校验。
- 相对日期先保留文本，再由 `date_tools` 确定性解析；第一次补丁构建后把绝对日期、原文本和冻结时间写入 checkpoint。
- 只接受“改成 25”“设为明天”“清空备注”这类操作；“加 10”“往后推三天”“追加文字”不会转换成写操作，而是要求补充绝对值。
- 内容更新和状态更新必须拆开。付款、退款、停止扣款和取消真实订阅属于外部动作，LifeVault 明确拒绝，不能伪装成本地状态更新。
- 更新 checkpoint 只保存脱敏输入、搜索条件、候选 ID、选中 ID/版本、冻结补丁、日期来源、阶段和错误；完整记录在恢复和展示时重新通过 MCP 获取。
- 版本冲突后重新读取最新记录并生成新预览，原确认失效，用户必须再次确认。
- 无实际变化时以 `no_changes` 成功只读结束，不增加版本、不修改提醒、不写审计。

**Qwen 与规则怎样协作**

v0.16 没有简单采用“Qwen 成功就完全相信，否则 fallback”。更新提取器会同时运行 Qwen 和确定性规则：

- 规则明确命中的新值、状态、清空和外部动作拥有更高优先级。
- Qwen 只能补充规则没有覆盖的语义字段。
- 显式清空只接受规则确认过的清空语言和允许字段，避免模型幻觉造成数据删除。
- 规则已经识别为纯状态更新时，丢弃模型幻觉出的内容字段。
- 两路结果仍要通过 Pydantic、类型字段白名单和 MCP 预览。

这使小参数本地模型仍能提供语义覆盖，同时把破坏性错误限制在确认前的候选层。

**状态更新加固**

- MCP 新增只读 `preview_record_status_update`。
- `update_record_status` 新增强制 `user_confirmed`、`expected_version` 和 `idempotency_key`。
- 状态按类型限制：
  - purchase：`active/completed/returned/cancelled`
  - subscription：`active/cancelled`
  - bill：`active/paid/cancelled`
- 状态使提醒失效时，相关 `pending/snoozed` 提醒和记录状态、审计、幂等结果在同一事务中提交。
- 相关提醒处于 `sending` 时返回 `reminder_in_flight`，整个事务不写入。
- 恢复 `active` 不会猜测或重建以前取消的提醒。

**工作流**

```text
sanitized input
-> extract_target
-> search_target through MCP
-> select_target interrupt
-> extract_update for selected record type
-> deterministic patch/date conversion
-> preview_record_update or preview_record_status_update
-> update_confirmation interrupt
   -> apply typed corrections: re-preview
   -> confirm: submit
-> update_record or update_record_status
```

**入口和评测**

- Streamlit“我的记录”顶部支持自然语言查找修改，每条记录也有预选目标的自然语言入口。
- 状态按钮改成预览和确认两阶段，状态选项按记录类型收窄。
- CLI 新增 `edit`、`edit-resume`、`edit-state`；原 `status` 命令也先预览再确认。
- `sample_data/update_examples.jsonl` 包含 24 条目标和补丁样例。
- `eval-updates` 只生成报告，不设置失败阈值。当前 fallback 和本地 Qwen 都是 24/24 case、54/54 field。

**学习重点**

- “模型会调用工具”在安全系统中通常应实现为“模型提出结构化意图，Graph 决定并调用工具”。
- 先选目标再提取补丁，可以避免模型猜记录 ID，也让第二阶段得到可靠的记录类型约束。
- 用户确认必须绑定具体版本和具体补丁；版本变化后，旧确认不能继续使用。
- 状态变化不仅是修改一列，还会改变提醒是否仍然有效，因此属于跨实体事务。
- 评测不仅衡量模型，也能指导模型与规则的合并边界。

---

## 20. v0.17：可恢复的记录归档与恢复

**目标**

允许用户整理不再活跃的记录，并把自然语言“删除这条记录”实现为可恢复归档，而不是立即物理删除。整个功能继续遵守 MCP、确认、乐观锁、幂等、审计和提醒一致性边界。

**数据模型与事务**

- `life_records` 新增独立 `archived_at`，不把归档塞入 `status`。归档和恢复都保留标题、金额、详情和原业务状态。
- `search_records` 新增 `archive_scope=active|archived|all`，默认只返回当前记录；按 ID 的 `get_record` 仍可读取归档记录。
- 新增 `record_lifecycle_operations` 保存操作类型、请求哈希和首次结果。完全相同的重试返回原结果，冲突复用幂等键会被拒绝。
- 归档事务重新读取记录和提醒：任何相关提醒处于 `sending` 时返回 `reminder_in_flight`；否则记录版本更新、全部 `pending/snoozed` 提醒取消、审计和幂等结果同事务提交。
- 恢复只清除 `archived_at` 并更新版本、时间、审计和幂等结果，不猜测也不重建曾取消的提醒。
- 重复归档或恢复返回 `no_changes`，不增加版本、不写审计、不新增幂等行。
- 归档记录的内容和状态更新在 Repository 服务端统一返回 `record_archived`，不能只靠 UI 隐藏按钮。

**MCP 与 Graph**

- MCP 新增 `preview_record_archive`、`archive_record`、`preview_record_restore`、`restore_record`。写工具要求 `user_confirmed`、`expected_version` 和 `idempotency_key`。
- `RecordUpdateGraphAgent` 复用现有选目标、预览、确认和版本冲突流程；归档搜索 active，恢复搜索 archived。
- 生命周期意图选定后直接进入预览，不执行第二次 Patch Qwen 调用，因为归档/恢复没有内容补丁。
- “删除订单号”仍是字段清空；“删除 ChatGPT Plus 记录”是归档；“恢复归档记录/取消归档/找回删除记录”才是恢复。
- “恢复 ChatGPT Plus”和“删除它”被确定性规则标记为歧义。即使 Qwen 猜出操作，Graph 也要求用户补充明确指令。
- 用户只说“归档这条记录”时，Graph 会在补充目标名称后保留已经明确的归档意图。

**Worker、入口与评测**

- Worker 不发送归档记录的遗留提醒，也不推进归档订阅；Repository 查询和滚动事务都检查 `archived_at`，覆盖查询后发生归档的竞态。
- 重复检测包含归档记录，并在候选中返回 `archived=true`，但创建流程不会擅自跳转恢复。
- Streamlit“我的记录”分当前/归档视图，当前记录可预览归档，归档记录可预览恢复；归档视图不提供内容编辑和状态修改。
- CLI 新增 `archive RECORD_ID VERSION`、`restore RECORD_ID VERSION`，支持 `--dry-run` 和 `--yes`；`list` 支持 `--archive-scope`。
- `sample_data/update_examples.jsonl` 从 24 条扩到 36 条。fallback 和本地 Qwen 均为 36/36 case、87/87 field。

**学习重点**

- 归档是记录生命周期，业务状态是领域事实，两者必须正交。
- “软删除”不只是增加一列，还要定义查询默认值、提醒行为、重复检测、编辑限制、审计和恢复语义。
- 删除意图属于高风险语义，确定性歧义规则应覆盖模型的自信猜测。
- 恢复不自动重建提醒是保守策略；系统缺少原始用户意图时，不应创造新的长期副作用。
- 预览绑定版本，版本冲突后旧确认失效，这与内容更新遵守同一并发原则。

---

## 21. v0.18：加密整库备份与崩溃安全恢复

**目标**

给本地单用户知识库增加可移机、可验证、可回滚的完整快照。v0.18 只备份业务 SQLite 和 LangGraph checkpoint SQLite，不把配置、代码、日志、已有备份或密码打包进去。

这不是普通“导出 JSON”：JSON 会丢失 SQLite 结构、事务状态、审计、幂等结果和 Graph 中断点。整库快照保留这些权威状态，也因此只能执行全量替换，不能假装成选择性合并。

**模块划分**

- `backup/service.py`：备份、检查、导入、恢复、安全备份、严格验证和稳定错误码。
- `backup/locking.py`：Linux `fcntl.flock` 跨进程锁和进程内可重入控制。
- `backup/runtime.py`：`vault_generation`、Worker 暂停状态和原子 JSON 写入。
- `backup/errors.py`：CLI、Streamlit、Worker 共用的稳定错误对象。
- `storage/database.py`：所有 Repository 连接自动持有共享 vault 锁，并正式维护 `PRAGMA user_version=1`。
- `tests/test_backup.py`：密码学、边界校验、双库回滚、代次重载和 Worker 暂停测试。

**`.lvbackup` v1 容器**

```text
8-byte magic
-> format version + canonical header length
-> canonical JSON header (algorithm, fixed KDF parameters, salt, nonce)
-> AES-GCM ciphertext
-> 16-byte authentication tag
```

明文头部只承担解密所必需的协议协商，不包含用户、时区、创建时间、记录数或备份类型。整个规范头部作为 AES-GCM AAD，修改 salt、nonce 或算法字段都会导致认证失败或固定配置拒绝。

加密参数：

- scrypt：32 字节随机 salt，`N=2^17, r=8, p=1`，输出 256 位密钥。
- AES-256-GCM：12 字节随机 nonce，16 字节 tag。
- 密码：NFC 规范化后的 12–256 个 Unicode 字符，不裁剪首尾空格。
- 依赖：`cryptography>=46.0.3,<50`；源码启动也检查实际加载版本，不能静默使用系统旧库。

加密前载荷是严格 ZIP/DEFLATE，只允许：

```text
manifest.json
vault.sqlite
langgraph.sqlite
```

解密必须先完成 GCM 认证，再解析 ZIP。条目数量、重复名称、绝对路径、`..`、目录、符号链接、特殊文件、声明大小、累计 1 GiB 上限和实际写出大小都要检查。数据库 SHA-256、结构 SHA-256 和逻辑大小保存在加密 manifest 中。

**创建流程**

```text
输入并二次确认密码
-> 获取独立 crypto 锁
-> 获取 vault 独占锁
-> SQLite Connection.backup() 快照两个数据库
-> quick_check + schema/user-scope 校验
-> 手动备份释放 vault 锁
-> 生成 manifest 和 ZIP
-> 流式 AES-GCM 写 UUID.lvbackup.partial
-> 重新解密并比对 ZIP 哈希
-> fsync 文件
-> os.replace 原子发布 UUID.lvbackup
-> fsync 目录
-> 写 create_backup 审计
```

checkpoint 从未使用时，备份中放入合法空 SQLite，并标记 `checkpoint_state=empty`。已存在但损坏的 checkpoint 不能静默替换为空库。

成功审计发生在快照之后，因此一个备份不会包含“创建自身成功”的审计事件；当前活动数据库会包含该事件，manifest 则提供备份本身的来源元数据。这避免了“备份必须先成功，成功记录又必须预先在备份里”的循环依赖。

**导入与检查**

- `list` 不需要密码，只扫描严格 UUID 文件名，显示 ID、密文大小、文件系统时间和 `unverified`。
- `inspect` 每次重新执行 scrypt、GCM、ZIP、manifest、数据库和用户边界验证。
- `import` 只接受普通、非软/硬链接文件；验证成功后才按 manifest UUID 原子写入固定备份目录。
- 同 ID 同哈希返回 `already_present`；同 ID 不同哈希返回 `backup_id_conflict`，绝不覆盖。
- 密码错误和合法结构内的密文篡改都返回 `backup_authentication_failed`，不提供密码猜测 oracle。

**恢复事务**

恢复不能用一次 `os.replace` 原子替换两个文件。v0.18 使用“数据库锁 + 同文件系统候选 + 持久化事务日志”补齐跨文件崩溃一致性：

```text
第一次密码：只读恢复预览
-> 完整输入 backup ID
-> 第二次密码：重新认证并重算密文 SHA-256
-> vault 独占锁
-> 两个活动库 wal_checkpoint(TRUNCATE)
-> 用同一密码创建并验证 pre_restore_safety 备份
-> 候选库复制到各自目标目录并 fsync
-> 写 prepared 恢复日志并 fsync
-> 原库改名为 UUID rollback 文件
-> 候选库依次 os.replace 到正式路径
-> quick_check、结构、用户和 checkpoint 校验
-> 在恢复出的业务库写 restore_backup 审计
-> 更新 vault_generation 并暂停 Worker
-> 写 committed 阶段
-> 删除事务日志和明文 rollback/candidate
```

WAL 处理是必要步骤。只替换 `.db` 主文件会让旧 `-wal/-shm` 把备份时点之后的 checkpoint 重新叠加到恢复库。对应回归测试让旧 Graph Agent 保持连接，恢复后确认它只能看到备份时点内的线程。

任何阶段失败都会把两个原数据库一起恢复。进程在中途退出时，下次 CLI、Streamlit、Worker 或 MCP Server 启动先读取恢复日志：未提交阶段自动回滚；`committed` 阶段只完成残留清理，不能误回滚已成功恢复的数据。无法确定状态时返回 `restore_recovery_required` 并拒绝猜测。

**锁与运行代次**

- 普通 Repository、Graph、MCP 和 Worker 操作使用 3 秒共享锁。
- 手动快照使用 10 秒独占锁，正式恢复使用 30 秒独占锁。
- scrypt 约使用 128 MiB 内存，另有 crypto 独占锁串行化创建、导入、检查和恢复认证。
- 手动备份只在双库快照阶段阻塞业务；安全备份为了精确保存恢复前状态，会一直持锁到加密、验证和审计完成。
- 恢复更新外部运行状态中的 UUID `vault_generation`。Graph 发现变化后关闭旧 checkpoint 连接并重建；MCP/Worker 的数据库操作不能跨代次写回。
- Worker 恢复后默认暂停，不发送提醒也不推进订阅。CLI 输入 `RESUME WORKER` 或 Streamlit 明确勾选后，审计成功才解除暂停。

运行状态文件、锁和恢复日志都位于业务数据库旁，不进入备份：

```text
<LIFEVAULT_DB>.runtime.json
<LIFEVAULT_DB>.lock
<LIFEVAULT_DB>.backup-crypto.lock
<LIFEVAULT_DB>.restore-journal.json
```

运行状态损坏时生成新代次、强制暂停 Worker 并写 `runtime_state_recovered` 审计。失败操作留下的 staging、`.partial`、candidate 和 rollback 只有在确认没有活动锁或恢复日志时才清理。

**接口与能力边界**

CLI：

```text
backup create
backup list
backup inspect BACKUP_ID
backup import FILE
backup restore BACKUP_ID
backup status
backup resume-worker
```

密码只通过 `getpass` 或 Streamlit 密码控件进入。没有 `--password`、`--yes`、`--force`、自定义单次输出目录、删除命令或跳过检查开关。Streamlit 在操作提交后清除密码 widget 状态，恢复预览只保存非秘密汇总、密文 SHA-256 和 15 分钟有效期。

备份不属于 MCP 数据工具。MCP Tool 无法读取任意路径、接收密码、创建备份或替换数据库；测试明确断言工具清单中没有备份能力。原因是 MCP 面向模型可调用的个人记录能力，而整库恢复是本地运维权限。

**为什么没有换 PostgreSQL**

PostgreSQL 可以在同一实例中用事务协调业务表和 checkpoint 表，但这要求重写 Repository、checkpointer、部署和恢复模型，并让本地个人应用依赖常驻数据库服务。v0.18 的目标是补齐 SQLite 本地备份，不是迁移存储后端。

未来出现网络服务、多用户权限和高并发需求时，再把 PostgreSQL 作为独立架构版本讨论；不能用“为了备份原子性”顺带引入整个服务端数据库栈。

**明确不做**

- JSON/CSV、选择性恢复、合并恢复、增量/差异备份。
- 云备份、计划任务、自动保留期、应用内删除备份。
- 密码持久化、系统钥匙串、恢复密钥或绕过认证。
- 跨用户 ID 映射、强制降级、未知结构兼容或跳过校验。
- 配置、代码、附件、日志和已有备份嵌套打包。
- PostgreSQL 迁移和业务记录物理删除。

**学习重点**

- “文件加密”不等于“可恢复系统”：还需要快照一致性、格式边界、资源上限、审计、失败回滚和进程重载。
- 两个文件无法共享一个文件系统原子替换点；持久化阶段日志可以把中断操作变成启动时可判定的状态机。
- 数据库主文件、WAL 和长连接共同构成运行状态，测试只替换 `.db` 的 happy path 会漏掉真实恢复错误。
- 密码学参数必须固定并限制，不能让不可信头部自行指定超大 KDF 资源。
- 全量恢复会让旧提醒重新出现，因此数据一致性之外还需要暂停长期副作用。
- PostgreSQL 是未来部署模型决策，不是当前 SQLite 恢复问题的局部工具。

---

## 22. 当前测试体系

当前共有 146 个测试，主要分层如下：

| 测试文件 | 关注点 |
|---|---|
| `test_date_tools.py` | 相对日期、周期锚点、月末和闰年 |
| `test_fallback_extractor.py` | 规则提取字段 |
| `test_eval_runner.py` | JSONL 评测加载和报告 |
| `test_update_eval_runner.py` | 自然语言目标与补丁的 JSONL 评测 |
| `test_agent.py` | 记录构建和提醒规划 |
| `test_corrections.py` | 校对白名单、原子性和跨字段校验 |
| `test_graph_agent.py` | 中断、恢复、路由和旧检查点 |
| `test_mcp_client.py` | MCP 工具调用、确认和幂等 |
| `test_mcp_server.py` | 真实 stdio MCP 冒烟 |
| `test_mcp_boundary.py` | Agent/UI/CLI 不绕过 MCP |
| `test_repository.py` | SQLite 事务、查询和乐观锁 |
| `test_record_updates.py` | Patch、提醒重排、幂等、冲突和旧表迁移 |
| `test_status_updates.py` | 状态白名单、提醒取消、幂等和事务回滚 |
| `test_record_lifecycle.py` | 归档/恢复事务、查询范围、幂等、回滚、MCP 和 Worker 防线 |
| `test_update_graph_agent.py` | 自然更新选目标、冻结、纠正、恢复和版本冲突 |
| `test_audit.py` | 审计事务和隐私允许列表 |
| `test_worker.py` | 通知、免打扰、失败和周期推进 |
| `test_app.py` | Streamlit 校对、提醒选择、记录编辑和归档/恢复视图 |
| `test_cli.py` | CLI 结构化校对、局部更新和归档/恢复命令 |
| `test_backup.py` | 加密认证、恶意归档、结构/用户边界、安全备份、双库回滚、Graph 重载和 Worker 暂停 |

常用验证命令：

```bash
python3 -m unittest discover -s tests -v
python3 -m lifevault.cli eval
python3 -m lifevault.cli eval-updates
python3 -m lifevault.cli eval-updates --use-qwen
python3 -m lifevault.cli mcp-smoke
python3 -m py_compile \
  lifevault/models/schemas.py \
  lifevault/agent/service.py \
  lifevault/agent/graph_agent.py \
  lifevault/agent/update_graph_agent.py \
  lifevault/records/update_planner.py \
  lifevault/storage/repository.py
git diff --check
```

## 23. 推荐学习顺序

### 第一阶段：理解确定性内核

1. 阅读 `models/schemas.py`。
2. 阅读 `tools/date_tools.py`。
3. 运行 `tests/test_date_tools.py`。
4. 阅读 `storage/database.py` 和 `storage/repository.py`。

目标：理解模型输出最终为什么不能直接保存。

### 第二阶段：理解模型与业务的边界

1. 阅读 `models/llm_factory.py`。
2. 阅读 `models/update_extractor.py`，对比两阶段 Prompt、fallback 和合并规则。
3. 阅读 `eval/runner.py`、`eval/update_runner.py` 和两个 JSONL 样例集。
4. 分别运行 `eval` 和 `eval-updates --use-qwen`。
5. 修改一条样例并观察 mismatch 报告。

目标：理解大模型负责模糊理解，评测负责暴露不稳定部分。

### 第三阶段：理解工作流

1. 阅读 `agent/service.py`。
2. 阅读 `agent/graph_agent.py` 的 `_build_graph()`。
3. 跟踪一个 `missing_fields` 流程。
4. 跟踪一个校对后查重流程。
5. 阅读 `agent/update_graph_agent.py`，跟踪选目标、补丁确认和版本冲突流程。
6. 阅读旧检查点兼容和更新日期冻结测试。

目标：理解业务逻辑、状态和副作用为什么分层。

### 第四阶段：理解 MCP

1. 阅读 `mcp_server/server.py`。
2. 阅读 `mcp_server/client.py`。
3. 运行 `mcp-smoke`。
4. 阅读 `test_mcp_boundary.py`。
5. 对比 Repository 方法和 MCP Tool 的职责。
6. 跟踪一次 `preview_record_update` 到 `update_record` 的提交过程。
7. 对比 `preview_record_status_update` 和 `update_record_status` 的事务职责。
8. 跟踪 `preview_record_archive` 到 `archive_record`，再对比 `restore_record` 为什么不重建提醒。

目标：理解 MCP 不是数据库 ORM，而是受控能力边界。

### 第五阶段：理解长期任务

1. 阅读 `worker/reminder_worker.py`。
2. 阅读提醒状态转换。
3. 阅读免打扰和 snooze 测试。
4. 阅读订阅周期推进事务。

目标：理解 Agent 对话结束后，长期任务如何继续可靠运行。

### 第六阶段：理解备份与恢复

1. 阅读 `backup/locking.py` 和 `backup/runtime.py`。
2. 阅读 `backup/service.py` 的 `_encrypt_container()` 与 `_decrypt_container()`。
3. 跟踪 `create_backup()` 的锁释放点。
4. 跟踪 `restore_backup()`、恢复日志阶段和 `_rollback_journal()`。
5. 运行 `tests/test_backup.py`，重点阅读 WAL、双库故障和 Graph 代次测试。
6. 对比 MCP 工具列表，确认备份权限为何留在可信本地服务。

目标：理解加密文件、数据库快照、跨文件事务和长期进程重载如何组合成可用的恢复能力。

## 24. 用 Git 学习每次迭代

查看自然语言更新版本的完整实现提交：

```bash
git show e9cace7
```

查看两个版本之间的变化：

```bash
git diff 8cf4704..e9cace7
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
e9cace7  v0.16 自然语言更新
af6ef69  v0.17 记录归档与恢复
e91b0c2  v0.18 加密整库备份与恢复
```

## 25. 尚未实现的能力

当前明确未实现：

- 已保存记录的类型转换、物理删除、保留期和自动清理。
- OCR、截图和 PDF 导入。
- 邮件、微信和云端推送。
- 云同步和多用户权限。
- 自动付款、退款或取消订阅。
- 草稿版本历史与撤销。
- 多条记录批量更新和跨记录原子操作。
- JSON/CSV、选择性、合并、增量、云端和计划备份。
- PostgreSQL 服务端存储和多用户备份权限。

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
