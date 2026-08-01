from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests
from pydantic import ValidationError

from lifevault.config import Settings
from lifevault.models.schemas import ExtractedRecordCandidate, RecordType
from lifevault.skills import load_skill
from lifevault.tools.date_tools import parse_int


KNOWN_SUBSCRIPTION_SERVICES = [
    "GitHub Copilot",
    "ChatGPT Plus",
    "Apple Music",
    "Netflix",
    "Spotify",
    "Dropbox",
    "Notion",
    "Adobe",
    "Zoom",
    "WPS",
    "腾讯视频",
    "爱奇艺",
    "B 站大",
    "优酷",
    "飞书",
]

KNOWN_BILL_NAMES = [
    "车位管理费",
    "健身房尾款",
    "手机话费",
    "信用卡",
    "水电费",
    "物业费",
    "燃气费",
    "停车费",
    "医保",
    "学费",
    "房贷",
    "花呗",
    "保险",
    "宽带",
    "房租",
]


class QwenExtractionError(RuntimeError):
    pass


class QwenClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False

    def extract_record(self, text: str, now: datetime) -> ExtractedRecordCandidate:
        selected_skill = select_extraction_skill(text)
        skill_content = load_skill(selected_skill) if selected_skill else None
        prompt = build_extraction_prompt(text, now, skill_content=skill_content)
        response = self.session.post(
            f"{self.settings.qwen_base_url.rstrip('/')}/chat/completions",
            json={
                "model": self.settings.qwen_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 LifeVault 的本地信息抽取器。只输出一个 JSON 对象，不要 Markdown。"
                            "你可以理解用户意图和字段，但不要计算截止日期。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 700,
            },
            timeout=self.settings.qwen_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        data = extract_json_object(content)
        try:
            return ExtractedRecordCandidate.model_validate(data)
        except ValidationError as exc:
            raise QwenExtractionError(str(exc)) from exc


class FallbackExtractor:
    def extract_record(self, text: str, now: datetime) -> ExtractedRecordCandidate:
        if _guess_intent(text) == "search_records":
            record_type = _guess_record_type_for_search(text)
            query = _extract_search_query(text)
            return ExtractedRecordCandidate.model_validate(
                {
                    "intent": "search_records",
                    "record_type": record_type,
                    "title": query,
                    "search_query": query,
                }
            )

        record_type = _guess_record_type(text)
        amount = _extract_amount(text)
        reminder_requested = _extract_reminder_requested(text)
        remind_before_days = _extract_remind_before_days(text)
        return_reminder_requested, warranty_reminder_requested = _extract_purchase_reminder_targets(
            text,
            reminder_requested,
        )

        common: dict[str, Any] = {
            "intent": "create_record",
            "record_type": record_type,
            "title": _extract_title(text, record_type),
            "amount": amount,
            "event_date_text": (
                _extract_subscription_event_date_text(text)
                if record_type == RecordType.SUBSCRIPTION
                else _extract_event_date_text(text)
            ),
            "reminder_requested": reminder_requested,
            "return_reminder_requested": return_reminder_requested,
            "warranty_reminder_requested": warranty_reminder_requested,
            "remind_before_days": remind_before_days,
            "return_remind_before_days": _extract_target_remind_before_days(text, "退货"),
            "warranty_remind_before_days": _extract_target_remind_before_days(text, "保修|质保"),
            "reminder_time": _extract_reminder_time(text),
            "tool_plan": [
                "parse_relative_date",
                "calculate_deadline",
                "calculate_reminder_at",
                "find_duplicate",
            ],
        }

        if record_type == RecordType.PURCHASE:
            common.update(
                {
                    "merchant": _extract_merchant(text),
                    "order_number": _extract_order_number(text),
                    "return_days": _extract_return_days(text),
                    "warranty_months": _extract_warranty_months(text),
                    "return_deadline_text": _extract_purchase_deadline_text(text, "退货"),
                    "warranty_deadline_text": _extract_purchase_deadline_text(text, "保修|质保"),
                }
            )
        elif record_type == RecordType.SUBSCRIPTION:
            title = common.get("title")
            common.update(
                {
                    "service_name": title,
                    "billing_cycle": _extract_billing_cycle(text),
                    "auto_renew": _extract_auto_renew(text),
                    "next_renewal_text": _extract_subscription_renewal_text(text),
                }
            )
        elif record_type == RecordType.BILL:
            title = common.get("title")
            common.update(
                {
                    "bill_name": title,
                    "billing_period": _extract_billing_cycle(text),
                    "due_date_text": _extract_due_text(text),
                }
            )

        return ExtractedRecordCandidate.model_validate(common)


class Extractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.qwen = QwenClient(settings)
        self.fallback = FallbackExtractor()

    def extract_record(self, text: str, now: datetime) -> tuple[ExtractedRecordCandidate, list[str]]:
        warnings: list[str] = []
        if self.settings.use_qwen:
            try:
                model_candidate = self.qwen.extract_record(text, now)
                deterministic_candidate = self.fallback.extract_record(text, now)
                return reconcile_extracted_candidate(
                    model_candidate,
                    deterministic_candidate,
                ), warnings
            except Exception as exc:
                warnings.append(f"Qwen unavailable or invalid output, used fallback extractor: {exc}")
        return self.fallback.extract_record(text, now), warnings


COMMON_DETERMINISTIC_FIELDS = frozenset(
    {
        "amount",
        "event_date_text",
        "deadline_text",
        "reminder_requested",
        "return_reminder_requested",
        "warranty_reminder_requested",
        "remind_before_days",
        "return_remind_before_days",
        "warranty_remind_before_days",
        "reminder_time",
    }
)

TYPE_DETERMINISTIC_FIELDS = {
    RecordType.PURCHASE: frozenset(
        {
            "title",
            "merchant",
            "order_number",
            "return_days",
            "warranty_months",
            "return_deadline_text",
            "warranty_deadline_text",
        }
    ),
    RecordType.SUBSCRIPTION: frozenset(
        {
            "title",
            "service_name",
            "billing_cycle",
            "next_renewal_text",
            "auto_renew",
        }
    ),
    RecordType.BILL: frozenset(
        {
            "title",
            "bill_name",
            "billing_period",
            "due_date_text",
        }
    ),
}


def reconcile_extracted_candidate(
    model_candidate: ExtractedRecordCandidate,
    deterministic_candidate: ExtractedRecordCandidate,
) -> ExtractedRecordCandidate:
    """Constrain model output with values directly evidenced by deterministic parsing."""
    merged = model_candidate.model_dump(mode="python")
    deterministic = deterministic_candidate.model_dump(mode="python")

    if model_candidate.intent == "unknown":
        merged["intent"] = deterministic_candidate.intent
    if model_candidate.record_type is None:
        merged["record_type"] = deterministic_candidate.record_type

    effective_intent = merged["intent"]
    effective_record_type = merged["record_type"]

    if deterministic_candidate.intent == effective_intent == "search_records":
        if deterministic_candidate.record_type is not None:
            merged["record_type"] = deterministic_candidate.record_type
        if deterministic_candidate.search_query is not None:
            merged["title"] = deterministic_candidate.title
            merged["search_query"] = deterministic_candidate.search_query
        return ExtractedRecordCandidate.model_validate(merged)

    for field in COMMON_DETERMINISTIC_FIELDS:
        value = deterministic[field]
        if value is not None:
            merged[field] = value

    if effective_record_type == deterministic_candidate.record_type:
        for field in TYPE_DETERMINISTIC_FIELDS.get(
            deterministic_candidate.record_type,
            frozenset(),
        ):
            value = deterministic[field]
            if value is not None:
                merged[field] = value

    return ExtractedRecordCandidate.model_validate(merged)


def build_extraction_prompt(
    text: str,
    now: datetime,
    *,
    skill_content: str | None = None,
) -> str:
    skill_section = ""
    if skill_content:
        skill_section = f"""

本次只加载以下可信任务 Skill，并将其作为字段提取约束：
<lifevault-skill>
{skill_content}
</lifevault-skill>
"""
    return f"""
当前时间：{now.isoformat()}
{skill_section}

请把用户输入转成下面 JSON Schema 兼容的对象：
{{
  "intent": "create_record | search_records | update_status | unknown",
  "record_type": "purchase | subscription | bill | null",
  "title": "事项名称，不知道则 null",
  "amount": 3499.0,
  "currency": "CNY",
  "event_date_text": "用户原话中的日期，如 昨天；不知道则 null",
  "event_date": null,
  "deadline_text": "用户给出的截止日期原文；不知道则 null",
  "merchant": "商家，仅订单有",
  "order_number": "订单号，不要猜",
  "return_days": 7,
  "warranty_months": null,
  "return_deadline_text": "明确退货截止日期原文",
  "warranty_deadline_text": "明确保修截止日期原文",
  "service_name": "订阅服务名",
  "billing_cycle": "monthly | yearly | weekly | unknown | null",
  "next_renewal_text": "订阅续费日期原文",
  "auto_renew": true,
  "bill_name": "账单名",
  "billing_period": "账单周期原文",
  "due_date_text": "缴费截止日期原文",
  "reminder_requested": true,
  "return_reminder_requested": true,
  "warranty_reminder_requested": false,
  "remind_before_days": 2,
  "return_remind_before_days": 2,
  "warranty_remind_before_days": 30,
  "reminder_time": "09:00 或用户明确时间",
  "notes": "其他备注",
  "search_query": "查询意图时的关键词",
  "tool_plan": ["parse_relative_date", "calculate_deadline", "calculate_reminder_at", "find_duplicate"]
}}

规则：
- 只输出 JSON 对象。
- 不要编造订单号、商家、金额、日期或政策。
- 相对日期保留在 *_text 字段，由工具解析。
- 截止日期不要自行计算，除非用户明确给出具体截止日期。
- 商品保修时长统一换算为 warranty_months，例如一年填 12、两年填 24。
- 退货和保修提醒意图分别填 return_reminder_requested 与 warranty_reminder_requested。
- 只有“提醒我”但没有指定退货或保修时，只填 reminder_requested=true。
- 用户没有要求提醒或明确说不用提醒时，所有 reminder_requested 字段为 false。
- 退货和保修的提前天数分别填对应字段；通用提前天数填 remind_before_days。
- 用户只是查询时，intent=search_records，search_query 填关键词。
- 订阅/会员的 billing_cycle 只填 monthly/yearly/weekly/unknown/null。
- 订阅/会员的 next_renewal_text 保留续费日期或规则原文，例如 2026-08-15、下个月15号、每月15号、每年7月15日。
- 订阅/会员如果只有开通日期或上次付款日期，填 event_date_text，不要把开通日期当成 next_renewal_text。

用户输入：
{text}
""".strip()


def select_extraction_skill(text: str) -> str | None:
    if _guess_intent(text) == "search_records":
        return None
    return _guess_record_type(text).value


def extract_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.S)
        if not match:
            raise QwenExtractionError(f"No JSON object found: {content[:200]}")
        return json.loads(match.group(0))


def _guess_record_type(text: str) -> RecordType:
    if any(keyword in text for keyword in ["订阅", "会员", "续费", "自动续费"]):
        return RecordType.SUBSCRIPTION
    if _find_known_phrase(text, KNOWN_SUBSCRIPTION_SERVICES):
        return RecordType.SUBSCRIPTION
    if any(keyword in text for keyword in ["账单", "水电", "房租", "信用卡", "缴费", "宽带"]):
        return RecordType.BILL
    if _find_known_phrase(text, KNOWN_BILL_NAMES):
        return RecordType.BILL
    if any(keyword in text for keyword in ["扣款", "月费", "年费"]):
        return RecordType.SUBSCRIPTION
    return RecordType.PURCHASE


def _guess_intent(text: str) -> str:
    if any(keyword in text for keyword in ["查询", "查一下", "查找", "搜索", "找一下", "有没有", "哪些", "列出", "显示一下", "显示所有"]):
        return "search_records"
    return "create_record"


def _guess_record_type_for_search(text: str) -> RecordType | None:
    if any(keyword in text for keyword in ["订阅", "会员", "续费", "自动扣款", "自动续费"]):
        return RecordType.SUBSCRIPTION
    if any(keyword in text for keyword in ["账单", "水电", "房租", "信用卡", "缴费", "宽带"]):
        return RecordType.BILL
    if any(keyword in text for keyword in ["订单", "购买", "买了", "退货", "保修"]):
        return RecordType.PURCHASE
    return None


def _extract_search_query(text: str) -> str | None:
    query = text.strip()
    query = re.sub(r"^(?:帮我|给我|我想|请)?(?:查询|查一下|查找|搜索|找一下|找|列出|显示|看看)", "", query)
    query = re.sub(r"(?:有没有|哪些)", "", query)
    query = re.sub(r"(?:记录|订单|账单|订阅|会员|服务|快到期|快续费|到期|续费|一下|的|吗|呢|[？?。])", "", query)
    query = query.strip(" ，,。；;")
    return query or None


def _extract_amount(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|CNY|人民币|美元|美金|USD|\$)", text, re.I)
    return float(match.group(1)) if match else None


def _extract_event_date_text(text: str) -> str | None:
    for token in ["前天", "昨天", "今天", "明天", "后天", "月底", "本月底"]:
        if token in text:
            return token
    match = re.search(r"(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}日?|\d{1,2}月\d{1,2}日|\d{1,2}-\d{1,2})", text)
    return match.group(1) if match else None


def _extract_subscription_event_date_text(text: str) -> str | None:
    for token in ["前天", "昨天", "今天", "明天", "后天"]:
        if re.search(rf"{token}.*?(?:订阅|开通|购买|买了|付款|付费)", text):
            return token

    match = re.search(
        r"(?:上次(?:付款|扣款|续费)|开通(?:日期|时间)?|订阅(?:日期|时间)?)(?:是|在|到)?\s*"
        r"(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?|\d{1,2}月\d{1,2}[日号]|\d{1,2}-\d{1,2})",
        text,
    )
    if match:
        return match.group(1)

    match = re.search(
        r"(\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?|\d{1,2}月\d{1,2}[日号]|\d{1,2}-\d{1,2})"
        r".*?(?:订阅|开通|购买|买了|付款|付费)",
        text,
    )
    return match.group(1) if match else None


def _extract_due_text(text: str) -> str | None:
    date_expr = _date_text_pattern()
    match = re.search(rf"({date_expr})(?:前)?\s*(?:缴费|还款|截止|到期|扣款)", text)
    if match:
        return _compact_phrase(match.group(1))
    match = re.search(rf"(?:截止|到期|缴费|还款|扣款)(?:日|日期|时间)?(?:是|在|到)?\s*({date_expr})", text)
    if match:
        return _compact_phrase(match.group(1))
    return _extract_event_date_text(text)


def _extract_subscription_renewal_text(text: str) -> str | None:
    patterns = [
        r"((?:下个?月|下月)\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:明年|下年|下一年)\s*[一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:每年|每一年|年付|包年|年度|按年)\s*[一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"([一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号]\s*(?:每年|每一年|年付|包年|年度|按年)\s*(?:自动)?(?:续费|扣款)?)",
        r"((?:每个?月|月付|包月|月度|按月)\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:每周|每星期)[一二三四五六日天])",
        r"([一二两三四五六七八九十\d]+\s*[日号]\s*(?:自动续费|自动扣款|扣款|续费))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _compact_phrase(match.group(1))

    date_expr = (
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?"
        r"|\d{1,2}月\d{1,2}[日号]"
        r"|\d{1,2}-\d{1,2}"
        r"|今天|明天|后天|[零一二两三四五六七八九十百\d]+天后"
    )
    match = re.search(rf"(?:到期|续费|扣款)(?!前)(?:日|日期|时间)?(?:是|在|到)?\s*({date_expr})", text)
    if match:
        return _compact_phrase(match.group(1))

    match = re.search(rf"({date_expr}).*?(?:到期|续费|扣款)(?!前)", text)
    if match:
        return _compact_phrase(match.group(1))

    return None


def _extract_remind_before_days(text: str) -> int | None:
    match = re.search(r"(?:提前|前)\s*([零一二两三四五六七八九十百\d]+)\s*天提醒", text)
    if not match:
        match = re.search(r"提醒我.*?([零一二两三四五六七八九十百\d]+)\s*天", text)
    return parse_int(match.group(1)) if match else None


def _extract_reminder_requested(text: str) -> bool:
    if any(token in text for token in ["不用提醒", "不提醒", "无需提醒", "别提醒", "取消提醒"]):
        return False
    return "提醒" in text


def _extract_purchase_reminder_targets(text: str, reminder_requested: bool) -> tuple[bool, bool]:
    if not reminder_requested:
        return False, False
    if any(token in text for token in ["都提醒", "全部提醒", "两个期限都提醒", "这些期限都提醒"]):
        return True, True

    return_requested = bool(
        re.search(r"退货(?:截止|到期)?\s*(?:前|提前)\s*[零一二两三四五六七八九十百\d]+\s*天", text)
        or re.search(r"退货(?:截止|到期)?\s*提醒", text)
    )
    warranty_requested = bool(
        re.search(
            r"(?:保修|质保)(?:截止|到期)?\s*(?:前|提前)\s*[零一二两三四五六七八九十百\d]+\s*天",
            text,
        )
        or re.search(r"(?:保修|质保)(?:截止|到期)?\s*提醒", text)
    )
    return return_requested, warranty_requested


def _extract_target_remind_before_days(text: str, target: str) -> int | None:
    patterns = [
        rf"(?:{target})(?:截止|到期)?\s*(?:前|提前)\s*([零一二两三四五六七八九十百\d]+)\s*天",
        rf"(?:{target}).{{0,8}}?提前\s*([零一二两三四五六七八九十百\d]+)\s*天",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return parse_int(match.group(1))
    return None


def _extract_return_days(text: str) -> int | None:
    match = re.search(r"([零一二两三四五六七八九十百\d]+)\s*天(?:无理由|退货|可退)", text)
    return parse_int(match.group(1)) if match else None


def _extract_warranty_months(text: str) -> int | None:
    match = re.search(r"(?:保修|质保)(?:期)?\s*([零一二两三四五六七八九十百\d]+)\s*(年|个?月)", text)
    if not match:
        return None
    value = parse_int(match.group(1))
    if value is None:
        return None
    return value * 12 if match.group(2) == "年" else value


def _extract_purchase_deadline_text(text: str, target: str) -> str | None:
    date_expr = _date_text_pattern()
    patterns = [
        rf"(?:{target})(?:期)?(?:截止|到期)?(?:日|日期|时间)?(?:是|为|在|到|至)\s*({date_expr})",
        rf"({date_expr})\s*(?:前)?(?:完成)?(?:{target})(?:截止|到期)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _compact_phrase(match.group(1))
    return None


def _extract_reminder_time(text: str) -> str | None:
    match = re.search(
        r"(?:(上午|早上|下午|晚上)\s*)?(\d{1,2})\s*(?::|：)\s*(\d{1,2})"
        r"|(?:(上午|早上|下午|晚上)\s*)?(\d{1,2})\s*(?:点|时)(?:\s*(\d{1,2})\s*分?)?",
        text,
    )
    if not match:
        return None
    period = match.group(1) or match.group(4)
    hour_text = match.group(2) or match.group(5)
    minute_text = match.group(3) or match.group(6)
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period in {"上午", "早上"} and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _extract_order_number(text: str) -> str | None:
    match = re.search(r"订单号\s*[:：]?\s*([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def _extract_merchant(text: str) -> str | None:
    match = re.search(r"在\s*(.+?)\s*(?:购买|买|下单)", text)
    if match:
        return _clean_merchant(match.group(1))
    match = re.search(r"从\s*(.+?)\s*(?:购买|买|下单)", text)
    return _clean_merchant(match.group(1)) if match else None


def _extract_title(text: str, record_type: RecordType) -> str | None:
    if record_type == RecordType.PURCHASE:
        patterns = [
            r"买了\s*(?:一个|一台|一件|一份|一双)?\s*([^，,。；;]+)",
            r"购买了\s*(?:一个|一台|一件|一份|一双)?\s*([^，,。；;]+)",
            r"下单(?:了)?\s*(?:一个|一台|一件|一份|一双)?\s*([^，,。；;]+)",
        ]
    elif record_type == RecordType.SUBSCRIPTION:
        known = _find_known_phrase(text, KNOWN_SUBSCRIPTION_SERVICES)
        if known:
            return known
        patterns = [
            r"(?:订阅了|订阅|开通了|开通|购买了|买了|续费了)\s*([^，,。；;]+)",
            r"([^，,。；;]+?)(?:会员|订阅|月费|年费)",
        ]
    else:
        known = _find_known_phrase(text, KNOWN_BILL_NAMES)
        if known:
            return known
        patterns = [r"([^，,。；;\s]+)(?:账单|房租|水电费|信用卡|缴费|还款)"]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            title = _clean_title(match.group(1), record_type)
            if record_type == RecordType.SUBSCRIPTION:
                title = _clean_subscription_title(title)
            return title or None
    return None


def _extract_billing_cycle(text: str) -> str | None:
    if any(keyword in text for keyword in ["每月", "每个月", "月付", "月费", "包月", "月度", "按月", "这个月", "本月"]):
        return "monthly"
    if any(keyword in text for keyword in ["每年", "每一年", "年付", "年费", "包年", "年度", "按年"]):
        return "yearly"
    if any(keyword in text for keyword in ["每周", "每星期", "周付", "周费", "按周"]):
        return "weekly"
    return None


def _extract_auto_renew(text: str) -> bool | None:
    if any(keyword in text for keyword in ["不自动续费", "不会自动续费", "无需自动续费", "手动续费", "不自动扣款"]):
        return False
    if any(keyword in text for keyword in ["自动续费", "自动扣款", "自动付款", "自动支付"]):
        return True
    return None


def _clean_subscription_title(raw: str) -> str | None:
    value = raw.strip()
    value = re.sub(r"^(?:我|帮我|给我)?(?:已经)?(?:订阅了|订阅|开通了|开通|购买了|买了|续费了|续费)", "", value)
    value = re.sub(r"(?:会员|订阅|服务|自动续费|续费|月费|年费)$", "", value)
    value = value.strip(" 的")
    return value or None


def _date_text_pattern() -> str:
    number = r"[零一二两三四五六七八九十百\d]+"
    return (
        rf"(?:\d{{4}}\s*[年/-]\s*\d{{1,2}}\s*[月/-]\s*\d{{1,2}}\s*[日号]?"
        rf"|(?:明年|下年|下一年)\s*{number}\s*月\s*{number}\s*[日号]"
        rf"|(?:下个?月|下月)\s*{number}\s*[日号]"
        rf"|{number}\s*月\s*{number}\s*[日号]"
        rf"|{number}\s*-\s*{number}"
        rf"|下周[一二三四五六日天]"
        rf"|[零一二两三四五六七八九十百\d]+\s*天后"
        rf"|今天|明天|后天|月底|本月底|这个月底)"
    )


def _compact_phrase(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def _find_known_phrase(text: str, phrases: list[str]) -> str | None:
    normalized_text = _compact_phrase(text).lower()
    for phrase in phrases:
        if _compact_phrase(phrase).lower() in normalized_text:
            return phrase
    return None


def _clean_merchant(raw: str) -> str | None:
    value = raw.strip(" ，,。；;")
    value = re.sub(r"^(?:我|昨天|今天|前天|在|从)\s*", "", value)
    return value or None


def _clean_title(raw: str, record_type: RecordType) -> str:
    value = raw.strip(" ，,。；;")
    if record_type == RecordType.PURCHASE:
        value = re.sub(r"^(?:一个|一台|一件|一份|一双)", "", value)
    return value.strip()
