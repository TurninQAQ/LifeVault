from __future__ import annotations

from datetime import date

import streamlit as st

from lifevault.agent.service import ConfirmationRequired, LifeVaultAgent
from lifevault.config import get_settings
from lifevault.models.schemas import RecordStatus, ReminderStatus, UserPreference
from lifevault.storage.repository import VaultRepository


st.set_page_config(page_title="LifeVault", page_icon="LV", layout="wide")


@st.cache_resource
def get_services() -> tuple[LifeVaultAgent, VaultRepository]:
    settings = get_settings()
    repository = VaultRepository(settings.database_path)
    return LifeVaultAgent(settings, repository), repository


def main() -> None:
    agent, repository = get_services()
    settings = get_settings()

    st.title("LifeVault")
    tab_add, tab_records, tab_reminders, tab_settings = st.tabs(["添加记录", "我的记录", "提醒中心", "设置"])

    with tab_add:
        render_add_record(agent)

    with tab_records:
        render_records(repository, settings.default_user_id)

    with tab_reminders:
        render_reminders(repository, settings.default_user_id)

    with tab_settings:
        render_settings(repository, settings.default_user_id)


def render_add_record(agent: LifeVaultAgent) -> None:
    text = st.text_area(
        "自然语言输入",
        height=140,
        placeholder="例如：我昨天在京东买了一个耳机，3499 元，订单号 123456，七天无理由，退货前两天提醒我。",
    )
    if st.button("解析", type="primary", use_container_width=False):
        if text.strip():
            st.session_state["draft"] = agent.create_draft(text)

    draft = st.session_state.get("draft")
    if not draft:
        return

    if draft.warnings:
        for warning in draft.warnings:
            st.warning(warning)
    if draft.missing_fields:
        st.error("缺少必要字段：" + "，".join(draft.missing_fields))
        st.json(draft.candidate.model_dump(mode="json"))
        return

    left, right = st.columns(2)
    with left:
        st.subheader("记录预览")
        st.json(draft.record.model_dump(mode="json") if draft.record else {})
    with right:
        st.subheader("提醒预览")
        st.json(draft.reminder.model_dump(mode="json") if draft.reminder else {"reminder": None})

    if draft.duplicate_candidates:
        st.warning("发现疑似重复记录")
        for duplicate in draft.duplicate_candidates:
            st.write(f"{duplicate.title} | {duplicate.score:.2f} | {duplicate.reason}")

    create_reminder = st.checkbox("同时创建提醒", value=draft.reminder is not None, disabled=draft.reminder is None)
    if st.button("确认保存", type="primary"):
        try:
            result = agent.save_draft(
                draft,
                user_confirmed_record=True,
                user_confirmed_reminder=create_reminder,
            )
            st.success(f"已保存记录：{result.record.id}")
            if result.reminder:
                st.success(f"已创建提醒：{result.reminder.scheduled_at.isoformat()}")
            st.session_state.pop("draft", None)
        except ConfirmationRequired as exc:
            st.error(str(exc))


def render_records(repository: VaultRepository, user_id: str) -> None:
    query = st.text_input("关键词", key="record_query")
    records = repository.search_records(user_id, query=query or None)
    if not records:
        st.info("暂无记录")
        return

    for record in records:
        with st.container(border=True):
            cols = st.columns([3, 1.2, 1.2, 1.4, 1.4])
            cols[0].markdown(f"**{record.title}**")
            cols[1].write(record.record_type.value)
            cols[2].write(record.status.value)
            cols[3].write(record.deadline.isoformat() if record.deadline else "无截止日")
            cols[4].write(f"v{record.version}")
            st.caption(record.id)
            status = st.selectbox(
                "状态",
                options=[item.value for item in RecordStatus],
                index=[item.value for item in RecordStatus].index(record.status.value),
                key=f"status_{record.id}",
            )
            if st.button("更新状态", key=f"update_{record.id}"):
                repository.update_record_status(user_id, record.id, RecordStatus(status), record.version)
                st.rerun()


def render_reminders(repository: VaultRepository, user_id: str) -> None:
    status_value = st.selectbox("提醒状态", options=["all"] + [status.value for status in ReminderStatus])
    status = None if status_value == "all" else ReminderStatus(status_value)
    reminders = repository.list_reminders(user_id, status=status)
    if not reminders:
        st.info("暂无提醒")
        return

    for reminder in reminders:
        with st.container(border=True):
            st.write(f"**{reminder.scheduled_at.isoformat()}** | {reminder.status.value}")
            st.write(reminder.message)
            st.caption(f"{reminder.id} / record {reminder.record_id}")
            if reminder.status == ReminderStatus.PENDING:
                if st.button("取消提醒", key=f"cancel_{reminder.id}"):
                    repository.cancel_reminder(user_id, reminder.id, user_confirmed=True)
                    st.rerun()


def render_settings(repository: VaultRepository, user_id: str) -> None:
    preference = repository.get_preferences(user_id)
    default_time = st.text_input("默认提醒时间", value=preference.default_time)
    default_advance_days = st.number_input("默认提前天数", min_value=0, max_value=30, value=preference.default_advance_days)
    quiet_start = st.text_input("免打扰开始", value=preference.quiet_hours_start or "")
    quiet_end = st.text_input("免打扰结束", value=preference.quiet_hours_end or "")
    if st.button("保存设置", type="primary"):
        repository.update_preferences(
            UserPreference(
                user_id=user_id,
                default_time=default_time,
                default_advance_days=int(default_advance_days),
                quiet_hours_start=quiet_start or None,
                quiet_hours_end=quiet_end or None,
            )
        )
        st.success("设置已保存")


if __name__ == "__main__":
    main()
