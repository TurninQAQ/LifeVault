from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests
from pydantic import ValidationError

from lifevault.config import Settings
from lifevault.models.schemas import ExtractedRecordCandidate, RecordType
from lifevault.tools.date_tools import parse_int


class QwenExtractionError(RuntimeError):
    pass


class QwenClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False

    def extract_record(self, text: str, now: datetime) -> ExtractedRecordCandidate:
        prompt = build_extraction_prompt(text, now)
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
        record_type = _guess_record_type(text)
        amount = _extract_amount(text)
        reminder_requested = any(keyword in text for keyword in ["提醒", "到期", "续费前", "截止前"])
        remind_before_days = _extract_remind_before_days(text)
        if reminder_requested and remind_before_days is None:
            remind_before_days = 2

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
            "remind_before_days": remind_before_days,
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
                return self.qwen.extract_record(text, now), warnings
            except Exception as exc:
                warnings.append(f"Qwen unavailable or invalid output, used fallback extractor: {exc}")
        return self.fallback.extract_record(text, now), warnings


def build_extraction_prompt(text: str, now: datetime) -> str:
    return f"""
当前时间：{now.isoformat()}

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
  "service_name": "订阅服务名",
  "billing_cycle": "monthly | yearly | weekly | unknown | null",
  "next_renewal_text": "订阅续费日期原文",
  "auto_renew": true,
  "bill_name": "账单名",
  "billing_period": "账单周期原文",
  "due_date_text": "缴费截止日期原文",
  "reminder_requested": true,
  "remind_before_days": 2,
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
- 用户只是查询时，intent=search_records，search_query 填关键词。
- 订阅/会员的 billing_cycle 只填 monthly/yearly/weekly/unknown/null。
- 订阅/会员的 next_renewal_text 保留续费日期或规则原文，例如 2026-08-15、下个月15号、每月15号、每年7月15日。
- 订阅/会员如果只有开通日期或上次付款日期，填 event_date_text，不要把开通日期当成 next_renewal_text。

用户输入：
{text}
""".strip()


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
    if any(keyword in text for keyword in ["订阅", "会员", "续费", "自动扣款", "自动续费"]):
        return RecordType.SUBSCRIPTION
    if any(keyword in text for keyword in ["账单", "水电", "房租", "信用卡", "缴费", "宽带"]):
        return RecordType.BILL
    return RecordType.PURCHASE


def _extract_amount(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|CNY|人民币)", text, re.I)
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
    match = re.search(r"(?:截止|到期|续费|缴费|扣款)(?!前)(?:日|日期|时间)?(?:是|在|到)?\s*([^，,。；;\s]+)", text)
    if match:
        return match.group(1)
    return _extract_event_date_text(text)


def _extract_subscription_renewal_text(text: str) -> str | None:
    patterns = [
        r"((?:每个?月|月付|包月|月度|按月)\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"([一二两三四五六七八九十\d]+\s*[日号]\s*(?:自动续费|自动扣款|扣款|续费))",
        r"((?:下个?月|下月)\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:每年|每一年|年付|包年|年度|按年)\s*[一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:明年|下年|下一年)\s*[一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号])",
        r"((?:每周|每星期)[一二三四五六日天])",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return re.sub(r"\s+", "", match.group(1))

    date_expr = (
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?"
        r"|\d{1,2}月\d{1,2}[日号]"
        r"|\d{1,2}-\d{1,2}"
        r"|今天|明天|后天|[一二两三四五六七八九十\d]+天后"
    )
    match = re.search(rf"(?:到期|续费|扣款)(?!前)(?:日|日期|时间)?(?:是|在|到)?\s*({date_expr})", text)
    if match:
        return re.sub(r"\s+", "", match.group(1))

    match = re.search(rf"({date_expr}).*?(?:到期|续费|扣款)(?!前)", text)
    if match:
        return re.sub(r"\s+", "", match.group(1))

    return None


def _extract_remind_before_days(text: str) -> int | None:
    match = re.search(r"(?:提前|前)\s*([一二两三四五六七八九十\d]+)\s*天提醒", text)
    if not match:
        match = re.search(r"提醒我.*?([一二两三四五六七八九十\d]+)\s*天", text)
    return parse_int(match.group(1)) if match else None


def _extract_return_days(text: str) -> int | None:
    match = re.search(r"([一二两三四五六七八九十\d]+)\s*天(?:无理由|退货|可退)", text)
    return parse_int(match.group(1)) if match else None


def _extract_order_number(text: str) -> str | None:
    match = re.search(r"订单号\s*[:：]?\s*([A-Za-z0-9_-]+)", text)
    return match.group(1) if match else None


def _extract_merchant(text: str) -> str | None:
    match = re.search(r"在([^，,。；;\s]+)(?:买|购买|下单)", text)
    if match:
        return match.group(1)
    match = re.search(r"从([^，,。；;\s]+)(?:买|购买|下单)", text)
    return match.group(1) if match else None


def _extract_title(text: str, record_type: RecordType) -> str | None:
    if record_type == RecordType.PURCHASE:
        patterns = [
            r"买了(?:一个|一台|一件|一份)?([^，,。；;\s]+)",
            r"购买了(?:一个|一台|一件|一份)?([^，,。；;\s]+)",
            r"下单(?:了)?(?:一个|一台|一件|一份)?([^，,。；;\s]+)",
        ]
    elif record_type == RecordType.SUBSCRIPTION:
        patterns = [
            r"(?:订阅了|订阅|开通了|开通|购买了|买了|续费了|续费)\s*([^，,。；;\s]+)",
            r"([^，,。；;\s]+?)(?:会员|订阅)",
        ]
    else:
        patterns = [r"([^，,。；;\s]+)(?:账单|房租|水电费|信用卡|缴费)"]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            title = match.group(1)
            if record_type == RecordType.SUBSCRIPTION:
                title = _clean_subscription_title(title)
            return title or None
    return None


def _extract_billing_cycle(text: str) -> str | None:
    if any(keyword in text for keyword in ["每月", "每个月", "月付", "月费", "包月", "月度", "按月"]):
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
    value = re.sub(r"(?:会员|订阅|服务|自动续费|续费)$", "", value)
    value = value.strip(" 的")
    return value or None
