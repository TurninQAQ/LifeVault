# LifeVault v1.0 三分钟演示

仓库内的 [无声演示视频](lifevault-v1-demo.mp4) 展示添加记录、记录管理、提醒中心和移动端布局。本页提供一套可重复的现场演示流程；所有演示数据写入临时目录，不接触真实知识库。

## 准备

```bash
export LIFEVAULT_HOME="$(mktemp -d /tmp/lifevault-demo-XXXXXX)"
export LIFEVAULT_USE_QWEN=0
lifevault init-db
lifevault doctor --no-qwen --strict
```

确定性 fallback 用于保证现场演示可重复。展示本地模型时，改为 `LIFEVAULT_USE_QWEN=1`，并先运行 `lifevault doctor` 确认 Qwen 模型可用。

## 时间线

### 0:00-0:25 架构与启动

说明四条边界：模型只生成候选；Python 计算日期；LangGraph 管理确认和恢复；MCP 执行受控写入。运行：

```bash
lifevault serve
```

展示监督器打印的本地 URL，以及 UI 与 Reminder Worker 同时启动。

### 0:25-1:05 自然语言创建

在“添加记录”输入：

```text
我 2099-07-25 在京东买了一个耳机，3499 元，订单号 DEMO-1001，七天退货，保修一年，退货前 2 天、保修前 30 天提醒我。
```

展示结构化校对、退货/保修日期计算、两个提醒预览和保存前确认。强调模型没有写数据库权限。

### 1:05-1:40 查询与修改

打开“我的记录”，展示 purchase、subscription、bill 三类记录。对耳机执行自然语言修改：

```text
把耳机的金额改成 3299 元
```

展示明确选择目标、更新预览、版本号、用户确认和提醒一致性。随后展示归档预览，但取消实际归档。

### 1:40-2:10 提醒与长期任务

打开“提醒中心”，展示 pending 提醒、取消和三个稍后提醒选项。说明 Worker 在发送前重新读取记录状态，免打扰时段会创建可审计的 snooze 链，而不是丢失任务。

### 2:10-2:40 备份恢复

打开“备份与恢复”，展示运行状态、密码输入和已有备份列表。说明 `.lvbackup` 使用 scrypt 与 AES-256-GCM，恢复前强制创建安全备份，并通过事务日志协调两个 SQLite 文件。不要在录屏中展示真实密码。

### 2:40-3:00 MCP 与发布证据

在终端运行：

```bash
lifevault mcp-smoke
lifevault eval
lifevault eval-updates
```

结束时展示 21 个 MCP Tool、72/72 创建评测、36/36 更新评测，并说明备份能力故意不暴露给模型可调用的 MCP。

## CLI 预置数据

需要先准备三个页面样例时，可在临时 `LIFEVAULT_HOME` 中执行：

```bash
lifevault add "我 2099-07-25 在京东买了一个耳机，3499 元，订单号 DEMO-1001，七天退货，保修一年，退货前 2 天、保修前 30 天提醒我。" --yes
lifevault add "我订阅了腾讯视频会员，每月 30 元，2099 年 9 月 15 日自动续费，续费前 3 天提醒我。" --yes
lifevault add "房租 4500 元，2099 年 8 月 10 日缴费，提前两天提醒我。" --yes
```

演示结束后关闭 `lifevault serve`。临时目录可直接丢弃；不要把它当成正式备份。
