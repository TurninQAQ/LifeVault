from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from lifevault.agent.graph_agent import GraphAgent
from lifevault.config import get_settings
from lifevault.mcp_server.client import InProcessPersonalVaultMcpClient, PersonalVaultMcpClient
from lifevault.models.schemas import GraphTurn, RecordStatus, ReminderStatus
from lifevault.storage.repository import VaultRepository


st.set_page_config(page_title="LifeVault", page_icon="LV", layout="wide")


@st.cache_resource
def get_services() -> tuple[GraphAgent, PersonalVaultMcpClient]:
    settings = get_settings()
    repository = VaultRepository(settings.database_path)
    mcp_client = InProcessPersonalVaultMcpClient(settings, repository)
    return GraphAgent(settings, repository, mcp_client=mcp_client), mcp_client


def main() -> None:
    agent, mcp_client = get_services()
    settings = get_settings()

    st.title("LifeVault")
    tab_add, tab_records, tab_reminders, tab_audit, tab_settings = st.tabs(
        ["添加记录", "我的记录", "提醒中心", "审计", "设置"]
    )

    with tab_add:
        render_add_record(agent)

    with tab_records:
        render_records(mcp_client)

    with tab_reminders:
        render_reminders(mcp_client, settings.default_timezone)

    with tab_audit:
        render_audit(mcp_client)

    with tab_settings:
        render_settings(mcp_client)


def render_add_record(agent: GraphAgent) -> None:
    recover_cols = st.columns([2, 1])
    thread_id = recover_cols[0].text_input("thread_id", value=st.session_state.get("thread_id", ""))
    if recover_cols[1].button("恢复"):
        if thread_id.strip():
            turn = agent.get_state(thread_id.strip())
            if turn:
                st.session_state["graph_turn"] = turn
                st.session_state["thread_id"] = turn.thread_id
            else:
                st.error("没有找到这个 thread_id")

    text = st.text_area(
        "自然语言输入",
        height=140,
        placeholder="例如：我昨天在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。",
    )
    if st.button("解析", type="primary", use_container_width=False):
        if text.strip():
            turn = agent.start_create_record(text)
            st.session_state["graph_turn"] = turn
            st.session_state["thread_id"] = turn.thread_id

    turn: GraphTurn | None = st.session_state.get("graph_turn")
    if not turn:
        return

    render_graph_turn(agent, turn)


def render_graph_turn(agent: GraphAgent, turn: GraphTurn) -> None:
    st.caption(f"thread_id: {turn.thread_id}")
    if turn.status == "cancelled":
        st.warning("流程已取消")
    elif turn.status == "completed":
        st.success("流程已完成")

    if turn.errors:
        for error in turn.errors:
            st.error(error)

    if turn.interrupt_type == "missing_fields":
        st.error("缺少必要字段：" + "，".join(turn.missing_fields))
        if turn.candidate:
            st.json(turn.candidate)
        supplement = st.text_area("补充内容", key=f"supplement_{turn.thread_id}", height=100)
        cols = st.columns(2)
        if cols[0].button("提交补充", type="primary"):
            if supplement.strip():
                st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"text": supplement})
                st.rerun()
        if cols[1].button("取消流程"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "cancel"})
            st.rerun()
        return

    if turn.interrupt_type == "duplicate_review":
        st.warning("发现疑似重复记录")
        for duplicate in turn.duplicate_candidates:
            st.write(f"{duplicate.get('title')} | {duplicate.get('score', 0):.2f} | {duplicate.get('reason')}")
        if turn.record:
            st.subheader("新记录预览")
            st.json(turn.record)
        cols = st.columns(2)
        if cols[0].button("继续保存", type="primary"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "continue"})
            st.rerun()
        if cols[1].button("取消流程"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "cancel"})
            st.rerun()
        return

    if turn.interrupt_type == "record_confirmation":
        st.subheader("记录预览")
        st.json(turn.record or {})
        cols = st.columns(2)
        if cols[0].button("确认保存", type="primary"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "confirm"})
            st.rerun()
        if cols[1].button("取消流程"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "cancel"})
            st.rerun()
        return

    if turn.interrupt_type == "reminder_confirmation":
        left, right = st.columns(2)
        with left:
            st.subheader("已保存记录")
            st.json(turn.record or {})
        with right:
            st.subheader("提醒预览")
            st.json(turn.reminder or {})
        cols = st.columns(2)
        if cols[0].button("确认提醒", type="primary"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "confirm"})
            st.rerun()
        if cols[1].button("跳过提醒"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "skip"})
            st.rerun()
        return

    if not turn.record and not turn.reminder:
        return
    left, right = st.columns(2)
    with left:
        st.subheader("记录")
        st.json(turn.record or {})
    with right:
        st.subheader("提醒")
        st.json(turn.reminder or {})


def render_records(mcp_client: PersonalVaultMcpClient) -> None:
    query = st.text_input("关键词", key="record_query")
    result = mcp_client.search_records(query=query or None)
    if not render_mcp_error("search_records", result):
        return
    records = result.get("records", [])
    if not records:
        st.info("暂无记录")
        return

    for record in records:
        with st.container(border=True):
            cols = st.columns([3, 1.2, 1.2, 1.4, 1.4])
            cols[0].markdown(f"**{record['title']}**")
            cols[1].write(record["record_type"])
            cols[2].write(record["status"])
            cols[3].write(record.get("deadline") or "无截止日")
            cols[4].write(f"v{record['version']}")
            st.caption(record["id"])
            status = st.selectbox(
                "状态",
                options=[item.value for item in RecordStatus],
                index=[item.value for item in RecordStatus].index(record["status"]),
                key=f"status_{record['id']}",
            )
            if st.button("更新状态", key=f"update_{record['id']}"):
                update_result = mcp_client.update_record_status(record["id"], status, record["version"])
                if render_mcp_error("update_record_status", update_result):
                    st.rerun()


def render_reminders(mcp_client: PersonalVaultMcpClient, timezone_name: str) -> None:
    status_value = st.selectbox("提醒状态", options=["all"] + [status.value for status in ReminderStatus])
    status = None if status_value == "all" else status_value
    result = mcp_client.list_reminders(status=status)
    if not render_mcp_error("list_reminders", result):
        return
    reminders = result.get("reminders", [])
    if not reminders:
        st.info("暂无提醒")
        return

    for reminder in reminders:
        with st.container(border=True):
            st.write(f"**{reminder['scheduled_at']}** | {reminder['status']}")
            st.write(reminder["message"])
            st.caption(f"{reminder['id']} / record {reminder['record_id']}")
            if reminder["status"] == ReminderStatus.PENDING.value:
                cols = st.columns(4)
                if cols[0].button("取消提醒", key=f"cancel_{reminder['id']}"):
                    cancel_result = mcp_client.cancel_reminder(reminder["id"], user_confirmed=True)
                    if render_mcp_error("cancel_reminder", cancel_result):
                        st.rerun()
                for index, (label, scheduled_at) in enumerate(fixed_snooze_options(timezone_name), start=1):
                    if cols[index].button(label, key=f"snooze_{label}_{reminder['id']}"):
                        snooze_result = mcp_client.snooze_reminder(reminder["id"], scheduled_at.isoformat())
                        if render_mcp_error("snooze_reminder", snooze_result):
                            st.rerun()


def render_audit(mcp_client: PersonalVaultMcpClient) -> None:
    filters = st.columns([1, 1.4, 1, 0.8])
    actor_value = filters[0].selectbox(
        "执行者",
        options=["all", "mcp", "agent", "user", "worker"],
        key="audit_actor",
    )
    action_value = filters[1].selectbox(
        "操作",
        options=[
            "all",
            "save_record",
            "update_record_status",
            "create_reminder",
            "snooze_reminder",
            "cancel_reminder",
            "send_reminder",
            "update_preferences",
        ],
        key="audit_action",
    )
    result_value = filters[2].selectbox(
        "结果",
        options=["all", "ok", "rejected", "failed"],
        key="audit_result",
    )
    limit = filters[3].number_input("条数", min_value=1, max_value=200, value=50)
    result = mcp_client.list_audit_logs(
        actor=None if actor_value == "all" else actor_value,
        action=None if action_value == "all" else action_value,
        result=None if result_value == "all" else result_value,
        limit=int(limit),
    )
    if not render_mcp_error("list_audit_logs", result):
        return
    audit_logs = result.get("audit_logs", [])
    if not audit_logs:
        st.info("暂无审计记录")
        return

    rows = [
        {
            "ID": log["id"],
            "时间": log["created_at"],
            "执行者": log["actor"],
            "操作": log["action"],
            "结果": log["result"],
            "目标": log.get("target_id") or "",
            "参数摘要": log.get("params_summary") or "",
        }
        for log in audit_logs
    ]
    st.dataframe(rows, width="stretch", hide_index=True)


def render_settings(mcp_client: PersonalVaultMcpClient) -> None:
    result = mcp_client.get_preferences()
    if not render_mcp_error("get_preferences", result):
        return
    preference = result["preference"]
    quiet_enabled_value = bool(preference.get("quiet_hours_start") and preference.get("quiet_hours_end"))

    with st.form("preferences_form"):
        default_time = st.time_input(
            "默认提醒时间",
            value=time.fromisoformat(preference["default_time"]),
            step=60,
        )
        default_advance_days = st.number_input(
            "默认提前天数",
            min_value=0,
            max_value=30,
            value=int(preference["default_advance_days"]),
        )
        quiet_enabled = st.checkbox("启用免打扰", value=quiet_enabled_value)
        quiet_columns = st.columns(2)
        quiet_start = quiet_columns[0].time_input(
            "免打扰开始",
            value=time.fromisoformat(preference["quiet_hours_start"])
            if preference.get("quiet_hours_start")
            else time(hour=22),
            step=60,
            disabled=not quiet_enabled,
        )
        quiet_end = quiet_columns[1].time_input(
            "免打扰结束",
            value=time.fromisoformat(preference["quiet_hours_end"])
            if preference.get("quiet_hours_end")
            else time(hour=8),
            step=60,
            disabled=not quiet_enabled,
        )
        submitted = st.form_submit_button("保存设置", type="primary")

    if not submitted:
        return
    patch = {
        "default_time": default_time.strftime("%H:%M"),
        "default_advance_days": int(default_advance_days),
        "quiet_hours_start": quiet_start.strftime("%H:%M") if quiet_enabled else None,
        "quiet_hours_end": quiet_end.strftime("%H:%M") if quiet_enabled else None,
    }
    update_result = mcp_client.update_preferences(patch, user_confirmed=True)
    if not render_mcp_error("update_preferences", update_result):
        return
    if update_result["changed"]:
        st.success("设置已保存")
    else:
        st.info("设置没有变化")
    st.json(update_result["preference"])


def render_mcp_error(tool_name: str, result: dict) -> bool:
    if result.get("ok"):
        return True
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        st.error(f"MCP {tool_name} failed: {error.get('code', 'unknown_error')}: {error.get('message', '')}")
    else:
        st.error(f"MCP {tool_name} failed.")
    return False


def fixed_snooze_options(timezone_name: str) -> list[tuple[str, datetime]]:
    now = datetime.now(ZoneInfo(timezone_name))
    tomorrow = now.date() + timedelta(days=1)
    days_until_next_monday = (7 - now.weekday()) % 7 or 7
    next_monday = now.date() + timedelta(days=days_until_next_monday)
    return [
        ("1 小时后", now + timedelta(hours=1)),
        ("明天 09:00", datetime.combine(tomorrow, time(hour=9), tzinfo=now.tzinfo)),
        ("下周一 09:00", datetime.combine(next_monday, time(hour=9), tzinfo=now.tzinfo)),
    ]


if __name__ == "__main__":
    main()
