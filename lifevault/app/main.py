from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from typing import Any
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
    if st.button("解析", type="primary", width="content"):
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
    if turn.warnings:
        for warning in turn.warnings:
            st.warning(warning)

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
        render_record_review(agent, turn)
        return

    if turn.interrupt_type == "reminder_confirmation":
        left, right = st.columns(2)
        with left:
            st.subheader("已保存记录")
            st.json(turn.record or {})
        with right:
            st.subheader("提醒预览")
            selected_keys: list[str] = []
            for index, reminder in enumerate(turn.reminders):
                reminder_type = reminder["reminder_type"]
                reminder_key = f"{reminder_type}|{reminder['scheduled_at']}"
                selected = st.checkbox(
                    f"{reminder_type} · {reminder['scheduled_at']}",
                    value=True,
                    key=f"select_{turn.thread_id}_{index}_{reminder_key}",
                )
                st.caption(reminder["message"])
                if selected:
                    selected_keys.append(reminder_key)
        cols = st.columns(2)
        if cols[0].button("创建所选提醒", type="primary"):
            st.session_state["graph_turn"] = agent.resume(
                turn.thread_id,
                {
                    "action": "confirm",
                    "selected_reminder_keys": selected_keys,
                },
            )
            st.rerun()
        if cols[1].button("跳过提醒"):
            st.session_state["graph_turn"] = agent.resume(turn.thread_id, {"action": "skip"})
            st.rerun()
        return

    if not turn.record and not turn.reminders:
        return
    left, right = st.columns(2)
    with left:
        st.subheader("记录")
        st.json(turn.record or {})
    with right:
        st.subheader("提醒")
        st.json(turn.reminders)


def render_record_review(agent: GraphAgent, turn: GraphTurn) -> None:
    candidate = turn.candidate or {}
    record_type = str(candidate.get("record_type") or "")
    revision = hashlib.sha256(
        json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    key_prefix = f"review_{turn.thread_id}_{revision}"
    baseline = _candidate_form_payload(candidate)

    st.subheader("记录校对")
    left, right = st.columns([1.45, 1])
    with left:
        title = st.text_input(
            "标题",
            value=str(candidate.get("title") or ""),
            max_chars=200,
            key=f"{key_prefix}_title",
            persist_state="session",
        )
        _render_field_errors(turn, "title")

        common_cols = st.columns(3)
        amount = common_cols[0].number_input(
            "金额",
            min_value=0.0,
            value=float(candidate.get("amount") or 0),
            step=1.0,
            key=f"{key_prefix}_amount",
            persist_state="session",
        )
        currency = common_cols[1].text_input(
            "币种",
            value=str(candidate.get("currency") or "CNY"),
            max_chars=3,
            key=f"{key_prefix}_currency",
            persist_state="session",
        )
        event_date = common_cols[2].date_input(
            "事项日期",
            value=_as_date(candidate.get("event_date")),
            format="YYYY-MM-DD",
            key=f"{key_prefix}_event_date",
            persist_state="session",
        )
        _render_field_errors(turn, "amount")
        _render_field_errors(turn, "currency")
        _render_field_errors(turn, "event_date")

        corrections: dict[str, Any] = {
            "title": title,
            "amount": amount,
            "currency": currency,
            "event_date": _date_value(event_date),
        }
        if record_type == "purchase":
            corrections.update(
                _render_purchase_fields(turn, candidate, key_prefix)
            )
        elif record_type == "subscription":
            corrections.update(
                _render_subscription_fields(turn, candidate, key_prefix)
            )
        elif record_type == "bill":
            corrections.update(
                _render_bill_fields(turn, candidate, key_prefix)
            )

        reminder_time = st.time_input(
            "提醒时间",
            value=_as_time(candidate.get("reminder_time")),
            step=timedelta(minutes=15),
            key=f"{key_prefix}_reminder_time",
            persist_state="session",
        )
        corrections["reminder_time"] = (
            reminder_time.strftime("%H:%M") if reminder_time else None
        )
        _render_field_errors(turn, "reminder_time")

        notes = st.text_input(
            "备注",
            value=str(candidate.get("notes") or ""),
            max_chars=1000,
            key=f"{key_prefix}_notes",
            persist_state="session",
        )
        corrections["notes"] = notes.strip() or None
        _render_field_errors(turn, "notes")

    with right:
        _render_computed_record(turn)

    normalized = _normalize_form_payload(corrections)
    dirty = normalized != baseline
    actions = st.columns(3)
    if actions[0].button(
        "应用修改",
        type="primary",
        disabled=not dirty,
        key=f"{key_prefix}_apply",
    ):
        st.session_state["graph_turn"] = agent.resume(
            turn.thread_id,
            {"action": "apply", "corrections": normalized},
        )
        st.rerun()
    if actions[1].button(
        "确认保存",
        disabled=dirty or bool(turn.field_errors),
        key=f"{key_prefix}_confirm",
    ):
        st.session_state["graph_turn"] = agent.resume(
            turn.thread_id,
            {"action": "confirm"},
        )
        st.rerun()
    if actions[2].button("取消流程", key=f"{key_prefix}_cancel"):
        st.session_state["graph_turn"] = agent.resume(
            turn.thread_id,
            {"action": "cancel"},
        )
        st.rerun()


def _render_purchase_fields(
    turn: GraphTurn,
    candidate: dict[str, Any],
    key_prefix: str,
) -> dict[str, Any]:
    merchant_cols = st.columns(2)
    merchant = merchant_cols[0].text_input(
        "商家",
        value=str(candidate.get("merchant") or ""),
        max_chars=200,
        key=f"{key_prefix}_merchant",
        persist_state="session",
    )
    order_number = merchant_cols[1].text_input(
        "订单号",
        value=str(candidate.get("order_number") or ""),
        max_chars=128,
        key=f"{key_prefix}_order_number",
        persist_state="session",
    )
    _render_field_errors(turn, "merchant")
    _render_field_errors(turn, "order_number")

    policy_cols = st.columns(2)
    return_days = policy_cols[0].number_input(
        "退货天数",
        min_value=1,
        max_value=3650,
        value=candidate.get("return_days"),
        step=1,
        key=f"{key_prefix}_return_days",
        persist_state="session",
    )
    warranty_months = policy_cols[1].number_input(
        "保修月数",
        min_value=1,
        max_value=1200,
        value=candidate.get("warranty_months"),
        step=1,
        key=f"{key_prefix}_warranty_months",
        persist_state="session",
    )
    _render_field_errors(turn, "return_days")
    _render_field_errors(turn, "warranty_months")

    deadline_cols = st.columns(2)
    return_deadline = deadline_cols[0].date_input(
        "明确退货截止日期",
        value=_as_date(candidate.get("return_deadline_date")),
        format="YYYY-MM-DD",
        key=f"{key_prefix}_return_deadline_date",
        persist_state="session",
    )
    warranty_deadline = deadline_cols[1].date_input(
        "明确保修截止日期",
        value=_as_date(candidate.get("warranty_deadline_date")),
        format="YYYY-MM-DD",
        key=f"{key_prefix}_warranty_deadline_date",
        persist_state="session",
    )
    _render_field_errors(turn, "return_deadline_date")
    _render_field_errors(turn, "warranty_deadline_date")

    return_available = bool(return_days or return_deadline)
    warranty_available = bool(warranty_months or warranty_deadline)
    has_target = bool(
        candidate.get("return_reminder_requested")
        or candidate.get("warranty_reminder_requested")
    )
    generic = bool(candidate.get("reminder_requested"))
    reminder_cols = st.columns(2)
    return_reminder = reminder_cols[0].toggle(
        "创建退货提醒",
        value=bool(candidate.get("return_reminder_requested"))
        or (generic and not has_target and return_available),
        key=f"{key_prefix}_return_reminder",
        persist_state="session",
    )
    warranty_reminder = reminder_cols[1].toggle(
        "创建保修提醒",
        value=bool(candidate.get("warranty_reminder_requested"))
        or (generic and not has_target and warranty_available),
        key=f"{key_prefix}_warranty_reminder",
        persist_state="session",
    )

    advance_cols = st.columns(2)
    return_advance = advance_cols[0].number_input(
        "退货提前天数",
        min_value=0,
        max_value=365,
        value=candidate.get("return_remind_before_days"),
        step=1,
        key=f"{key_prefix}_return_advance",
        persist_state="session",
    )
    warranty_advance = advance_cols[1].number_input(
        "保修提前天数",
        min_value=0,
        max_value=365,
        value=candidate.get("warranty_remind_before_days"),
        step=1,
        key=f"{key_prefix}_warranty_advance",
        persist_state="session",
    )
    _render_field_errors(turn, "return_remind_before_days")
    _render_field_errors(turn, "warranty_remind_before_days")
    return {
        "merchant": merchant.strip() or None,
        "order_number": order_number.strip() or None,
        "return_days": return_days,
        "warranty_months": warranty_months,
        "return_deadline_date": _date_value(return_deadline),
        "warranty_deadline_date": _date_value(warranty_deadline),
        "return_reminder_requested": return_reminder,
        "warranty_reminder_requested": warranty_reminder,
        "return_remind_before_days": return_advance,
        "warranty_remind_before_days": warranty_advance,
    }


def _render_subscription_fields(
    turn: GraphTurn,
    candidate: dict[str, Any],
    key_prefix: str,
) -> dict[str, Any]:
    service_name = st.text_input(
        "服务名称",
        value=str(candidate.get("service_name") or ""),
        max_chars=200,
        key=f"{key_prefix}_service_name",
        persist_state="session",
    )
    _render_field_errors(turn, "service_name")
    cols = st.columns(2)
    cycles = [None, "monthly", "yearly", "weekly", "unknown"]
    current_cycle = candidate.get("billing_cycle")
    billing_cycle = cols[0].selectbox(
        "付费周期",
        cycles,
        index=cycles.index(current_cycle) if current_cycle in cycles else 0,
        format_func=lambda value: value or "未设置",
        key=f"{key_prefix}_billing_cycle",
        persist_state="session",
    )
    renewal_date = cols[1].date_input(
        "下一续费日期",
        value=_as_date(candidate.get("next_renewal_date")),
        format="YYYY-MM-DD",
        key=f"{key_prefix}_next_renewal_date",
        persist_state="session",
    )
    _render_field_errors(turn, "billing_cycle")
    _render_field_errors(turn, "next_renewal_date")

    reminder_cols = st.columns(3)
    auto_renew = reminder_cols[0].toggle(
        "自动续费",
        value=bool(candidate.get("auto_renew")),
        key=f"{key_prefix}_auto_renew",
        persist_state="session",
    )
    reminder_requested = reminder_cols[1].toggle(
        "创建续费提醒",
        value=bool(candidate.get("reminder_requested")),
        key=f"{key_prefix}_reminder_requested",
        persist_state="session",
    )
    remind_before_days = reminder_cols[2].number_input(
        "提前天数",
        min_value=0,
        max_value=365,
        value=candidate.get("remind_before_days"),
        step=1,
        key=f"{key_prefix}_advance",
        persist_state="session",
    )
    _render_field_errors(turn, "auto_renew")
    _render_field_errors(turn, "remind_before_days")
    return {
        "service_name": service_name.strip() or None,
        "billing_cycle": billing_cycle,
        "next_renewal_date": _date_value(renewal_date),
        "auto_renew": auto_renew,
        "reminder_requested": reminder_requested,
        "remind_before_days": remind_before_days,
    }


def _render_bill_fields(
    turn: GraphTurn,
    candidate: dict[str, Any],
    key_prefix: str,
) -> dict[str, Any]:
    bill_name = st.text_input(
        "账单名称",
        value=str(candidate.get("bill_name") or ""),
        max_chars=200,
        key=f"{key_prefix}_bill_name",
        persist_state="session",
    )
    _render_field_errors(turn, "bill_name")
    cols = st.columns(2)
    billing_period = cols[0].text_input(
        "账单周期",
        value=str(candidate.get("billing_period") or ""),
        max_chars=1000,
        key=f"{key_prefix}_billing_period",
        persist_state="session",
    )
    due_date = cols[1].date_input(
        "缴费截止日期",
        value=_as_date(candidate.get("due_date")),
        format="YYYY-MM-DD",
        key=f"{key_prefix}_due_date",
        persist_state="session",
    )
    _render_field_errors(turn, "billing_period")
    _render_field_errors(turn, "due_date")

    reminder_cols = st.columns(2)
    reminder_requested = reminder_cols[0].toggle(
        "创建缴费提醒",
        value=bool(candidate.get("reminder_requested")),
        key=f"{key_prefix}_reminder_requested",
        persist_state="session",
    )
    remind_before_days = reminder_cols[1].number_input(
        "提前天数",
        min_value=0,
        max_value=365,
        value=candidate.get("remind_before_days"),
        step=1,
        key=f"{key_prefix}_advance",
        persist_state="session",
    )
    _render_field_errors(turn, "remind_before_days")
    return {
        "bill_name": bill_name.strip() or None,
        "billing_period": billing_period.strip() or None,
        "due_date": _date_value(due_date),
        "reminder_requested": reminder_requested,
        "remind_before_days": remind_before_days,
    }


def _render_computed_record(turn: GraphTurn) -> None:
    record = turn.record or {}
    details = record.get("details") or {}
    st.subheader("计算结果")
    st.markdown(f"**{record.get('title') or '-'}**")
    st.write(f"类型：{record.get('record_type') or '-'}")
    st.write(f"金额：{record.get('amount')} {record.get('currency') or ''}")
    st.write(f"事项日期：{record.get('event_date') or '-'}")
    st.write(f"主要截止日期：{record.get('deadline') or '-'}")
    if record.get("record_type") == "purchase":
        st.write(f"退货截止：{details.get('return_deadline') or '-'}")
        st.write(f"保修截止：{details.get('warranty_deadline') or '-'}")
    elif record.get("record_type") == "subscription":
        st.write(f"服务名称：{details.get('service_name') or '-'}")
    elif record.get("record_type") == "bill":
        st.write(f"账单名称：{details.get('bill_name') or '-'}")
    if turn.reminders:
        st.dataframe(
            [
                {
                    "类型": reminder["reminder_type"],
                    "提醒时间": reminder["scheduled_at"],
                    "内容": reminder["message"],
                }
                for reminder in turn.reminders
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("不创建提醒")
    with st.expander("调试数据"):
        st.json({"candidate": turn.candidate, "record": turn.record})


def _candidate_form_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    record_type = candidate.get("record_type")
    payload: dict[str, Any] = {
        "title": candidate.get("title"),
        "amount": candidate.get("amount"),
        "currency": candidate.get("currency"),
        "event_date": candidate.get("event_date"),
        "notes": candidate.get("notes"),
        "reminder_time": candidate.get("reminder_time"),
    }
    if record_type == "purchase":
        has_target = bool(
            candidate.get("return_reminder_requested")
            or candidate.get("warranty_reminder_requested")
        )
        generic = bool(candidate.get("reminder_requested"))
        return_available = bool(
            candidate.get("return_days") or candidate.get("return_deadline_date")
        )
        warranty_available = bool(
            candidate.get("warranty_months") or candidate.get("warranty_deadline_date")
        )
        payload.update(
            {
                "merchant": candidate.get("merchant"),
                "order_number": candidate.get("order_number"),
                "return_days": candidate.get("return_days"),
                "warranty_months": candidate.get("warranty_months"),
                "return_deadline_date": candidate.get("return_deadline_date"),
                "warranty_deadline_date": candidate.get("warranty_deadline_date"),
                "return_reminder_requested": bool(
                    candidate.get("return_reminder_requested")
                    or (generic and not has_target and return_available)
                ),
                "warranty_reminder_requested": bool(
                    candidate.get("warranty_reminder_requested")
                    or (generic and not has_target and warranty_available)
                ),
                "return_remind_before_days": candidate.get("return_remind_before_days"),
                "warranty_remind_before_days": candidate.get("warranty_remind_before_days"),
            }
        )
    elif record_type == "subscription":
        payload.update(
            {
                "service_name": candidate.get("service_name"),
                "billing_cycle": candidate.get("billing_cycle"),
                "next_renewal_date": candidate.get("next_renewal_date"),
                "auto_renew": bool(candidate.get("auto_renew")),
                "reminder_requested": bool(candidate.get("reminder_requested")),
                "remind_before_days": candidate.get("remind_before_days"),
            }
        )
    elif record_type == "bill":
        payload.update(
            {
                "bill_name": candidate.get("bill_name"),
                "billing_period": candidate.get("billing_period"),
                "due_date": candidate.get("due_date"),
                "reminder_requested": bool(candidate.get("reminder_requested")),
                "remind_before_days": candidate.get("remind_before_days"),
            }
        )
    return _normalize_form_payload(payload)


def _normalize_form_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for field, value in payload.items():
        if isinstance(value, date):
            normalized[field] = value.isoformat()
        elif isinstance(value, str):
            stripped = value.strip()
            normalized[field] = stripped or None
        else:
            normalized[field] = value
    if isinstance(normalized.get("currency"), str):
        normalized["currency"] = normalized["currency"].upper()
    return normalized


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _as_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value
    if isinstance(value, str) and value:
        try:
            return time.fromisoformat(value)
        except ValueError:
            return None
    return None


def _date_value(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _render_field_errors(turn: GraphTurn, field: str) -> None:
    for message in turn.field_errors.get(field, []):
        st.error(message)


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
            details = record.get("details") or {}
            if record["record_type"] == "purchase":
                st.caption(
                    "退货截止："
                    f"{details.get('return_deadline') or '-'} · "
                    "保修截止："
                    f"{details.get('warranty_deadline') or '-'}"
                )
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
