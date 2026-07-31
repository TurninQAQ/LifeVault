from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import requests
from pydantic import ValidationError

from lifevault.config import Settings
from lifevault.models.llm_factory import (
    KNOWN_BILL_NAMES,
    KNOWN_SUBSCRIPTION_SERVICES,
    QwenExtractionError,
    extract_json_object,
)
from lifevault.models.schemas import (
    NaturalRecordUpdateIntent,
    RecordStatus,
    RecordTargetQuery,
    RecordType,
)


class QwenUpdateClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.trust_env = False

    def extract_target(self, text: str, now: datetime) -> RecordTargetQuery:
        data = self._complete(_target_prompt(text, now), max_tokens=350)
        try:
            return RecordTargetQuery.model_validate(data)
        except ValidationError as exc:
            raise QwenExtractionError(str(exc)) from exc

    def extract_update(
        self,
        text: str,
        record_type: RecordType,
        now: datetime,
    ) -> NaturalRecordUpdateIntent:
        data = self._complete(_update_prompt(text, record_type, now), max_tokens=650)
        try:
            return NaturalRecordUpdateIntent.model_validate(data)
        except ValidationError as exc:
            raise QwenExtractionError(str(exc)) from exc

    def _complete(self, prompt: str, max_tokens: int) -> dict[str, Any]:
        response = self.session.post(
            f"{self.settings.qwen_base_url.rstrip('/')}/chat/completions",
            json={
                "model": self.settings.qwen_model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是 LifeVault 的本地更新意图抽取器。只输出一个 JSON 对象。"
                            "你不执行工具、不选择记录、不确认操作，也不计算相对日期。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=self.settings.qwen_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return extract_json_object(payload["choices"][0]["message"]["content"])


class FallbackUpdateExtractor:
    def extract_target(self, text: str, now: datetime) -> RecordTargetQuery:
        del now
        operation = _operation(text)
        target_segment = _target_segment(text, operation)
        return RecordTargetQuery(
            operation=operation,
            record_type=_record_type(text),
            query=_target_query(target_segment),
            target_date_text=_target_date_text(target_segment),
        )

    def extract_update(
        self,
        text: str,
        record_type: RecordType,
        now: datetime,
    ) -> NaturalRecordUpdateIntent:
        del now
        operation = _operation(text)
        target_status = _target_status(text, record_type)
        values: dict[str, Any] = {
            "operation": operation,
            "target_status": target_status,
            "clear_fields": _clear_fields(text),
        }
        values.update(_content_values(text, record_type))
        has_content = bool(values["clear_fields"]) or any(
            value is not None
            for field, value in values.items()
            if field not in {"operation", "target_status", "clear_fields"}
        )
        if target_status is not None and has_content:
            values["operation"] = "unknown"
        elif target_status is not None:
            values["operation"] = "status_update"
        elif operation not in {"external_action", "archive_record", "restore_record"}:
            values["operation"] = "content_update"
        return NaturalRecordUpdateIntent.model_validate(values)


class UpdateExtractor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.qwen = QwenUpdateClient(settings)
        self.fallback = FallbackUpdateExtractor()

    def extract_target(
        self,
        text: str,
        now: datetime,
    ) -> tuple[RecordTargetQuery, list[str]]:
        warnings: list[str] = []
        fallback = self.fallback.extract_target(text, now)
        if fallback.operation == "unknown" and re.search(
            r"(?:恢复|找回|取消归档|归档|删除|删掉|移除)",
            text,
        ):
            return fallback, warnings
        if self.settings.use_qwen:
            try:
                qwen = self.qwen.extract_target(text, now)
                return _merge_target_queries(qwen, fallback), warnings
            except Exception as exc:
                warnings.append(
                    f"Qwen unavailable or invalid target output, used fallback extractor: {exc}"
                )
        return fallback, warnings

    def extract_update(
        self,
        text: str,
        record_type: RecordType,
        now: datetime,
    ) -> tuple[NaturalRecordUpdateIntent, list[str]]:
        warnings: list[str] = []
        fallback = self.fallback.extract_update(text, record_type, now)
        if self.settings.use_qwen:
            try:
                qwen = self.qwen.extract_update(text, record_type, now)
                return _merge_update_intents(qwen, fallback), warnings
            except Exception as exc:
                warnings.append(
                    f"Qwen unavailable or invalid update output, used fallback extractor: {exc}"
                )
        return fallback, warnings


def _target_prompt(text: str, now: datetime) -> str:
    return f"""
当前时间：{now.isoformat()}

只提取目标搜索信息，不要生成修改字段：
{{
  "operation": "content_update | status_update | archive_record | restore_record | external_action | unknown",
  "record_type": "purchase | subscription | bill | null",
  "query": "用于搜索现有记录的核心名称关键词，不知道则 null",
  "target_date_text": "只属于目标旧记录描述的日期，不知道则 null"
}}

规则：
- “已经支付/已经取消/标记为”是 status_update。
- “帮我付款/退款/停止扣款/帮我取消真实订阅”是 external_action。
- “归档这条记录/删除某某记录”是 archive_record，删除字段不是归档。
- 只有“恢复归档记录/取消归档/找回删除记录”等明确措辞才是 restore_record。
- “恢复 ChatGPT Plus”含义不明确，返回 unknown，不能猜成 restore_record。
- 修改后的新金额、新标题或新日期不能放进 query 或 target_date_text。
- query 只能是旧记录的核心名称，例如“把 ChatGPT Plus 的月费改成 25 美元”的 query 是“ChatGPT Plus”。
- 字段名（金额、月费、续费日、状态）和状态值（已支付、已取消）都不是 query。
- 不要输出记录 ID。

用户输入：
{text}
""".strip()


def _update_prompt(text: str, record_type: RecordType, now: datetime) -> str:
    type_fields = {
        RecordType.PURCHASE: {
            "merchant": None,
            "order_number": None,
            "return_deadline_text": None,
            "warranty_deadline_text": None,
        },
        RecordType.SUBSCRIPTION: {
            "service_name": None,
            "billing_cycle": None,
            "next_renewal_text": None,
            "auto_renew": None,
        },
        RecordType.BILL: {
            "bill_name": None,
            "billing_period": None,
            "due_date_text": None,
        },
    }[record_type]
    template = {
        "operation": "content_update | status_update | archive_record | restore_record | external_action | unknown",
        "title": None,
        "amount": None,
        "currency": None,
        "event_date_text": None,
        "notes": None,
        **type_fields,
        "target_status": "active | completed | returned | paid | cancelled | null",
        "clear_fields": [],
    }
    return f"""
当前时间：{now.isoformat()}
用户已经选择了一条 {record_type.value} 记录。

只提取用户明确要求的绝对新值：
{json.dumps(template, ensure_ascii=False, indent=2)}

通用字段：title, amount, currency, event_date_text, notes。
该类型字段：{', '.join(type_fields)}。

规则：
- 相对日期保留原文，不要计算。
- 只有“删除/清空/不再记录某字段”才把字段名放进 clear_fields。
- 未提到的字段保持 null，不能根据当前记录猜值。
- “商家从京东改成天猫”中 merchant 的新值是“天猫”，不能填“京东”。
- 日期更新必须写入对应的 *_text 字段，例如“续费日改到明天”写 next_renewal_text="明天"。
- “加 10 元/往后推三天/追加文字”等相对变换返回 operation=unknown。
- 同时要求内容和状态修改时返回 operation=unknown。
- “帮我付款/退款/停止扣款/帮我取消真实订阅”返回 external_action。
- 不要输出记录 ID、版本、确认或幂等字段。

用户输入：
{text}
""".strip()


def _merge_target_queries(
    qwen: RecordTargetQuery,
    fallback: RecordTargetQuery,
) -> RecordTargetQuery:
    model_date = (
        qwen.target_date_text
        if qwen.target_date_text and _looks_like_target_date(qwen.target_date_text)
        else None
    )
    return RecordTargetQuery(
        operation=(
            fallback.operation
            if fallback.operation != "unknown"
            else qwen.operation
        ),
        record_type=fallback.record_type or qwen.record_type,
        query=fallback.query or qwen.query,
        target_date_text=fallback.target_date_text or model_date,
    )


def _merge_update_intents(
    qwen: NaturalRecordUpdateIntent,
    fallback: NaturalRecordUpdateIntent,
) -> NaturalRecordUpdateIntent:
    if fallback.target_status is not None:
        return fallback
    data = qwen.model_dump(mode="python")
    fallback_data = fallback.model_dump(mode="python")
    for field, value in fallback_data.items():
        if field in {"operation", "clear_fields"}:
            continue
        if value is not None:
            data[field] = value
    # Destructive clearing is accepted only when the deterministic extractor saw
    # explicit clear language and an allowed field label.
    data["clear_fields"] = fallback.clear_fields

    has_content = bool(data["clear_fields"]) or any(
        value is not None
        for field, value in data.items()
        if field not in {"operation", "target_status", "clear_fields"}
    )
    if qwen.operation == "external_action" or fallback.operation == "external_action":
        data["operation"] = "external_action"
    elif data.get("target_status") is not None and has_content:
        data["operation"] = "unknown"
    elif data.get("target_status") is not None:
        data["operation"] = "status_update"
    elif has_content:
        data["operation"] = "content_update"
    else:
        data["operation"] = "unknown"
    return NaturalRecordUpdateIntent.model_validate(data)


def _looks_like_target_date(value: str) -> bool:
    return bool(
        re.search(
            r"\d{4}[年/-]\d{1,2}|\d{1,2}月|今天|昨天|前天|明天|后天|"
            r"上个月|这个月|本月|去年|今年",
            value,
        )
    )


def _operation(text: str) -> str:
    if _is_external_action(text):
        return "external_action"
    if _is_restore_record(text):
        return "restore_record"
    if _is_archive_record(text):
        return "archive_record"
    if _target_status(text, _record_type(text) or RecordType.PURCHASE) is not None:
        return "status_update"
    if _clear_fields(text):
        return "content_update"
    if any(
        token in text
        for token in [
            "改成",
            "改为",
            "修改",
            "更正",
            "更新",
            "设为",
            "设置",
            "清空",
            "不再记录",
        ]
    ):
        return "content_update"
    return "unknown"


def _is_restore_record(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(
        "取消归档" in compact
        or re.search(
            r"(?:恢复|找回).{0,20}(?:归档|删除).{0,20}(?:记录|订单|账单|会员|订阅)",
            compact,
        )
        or re.search(
            r"(?:归档|删除)(?:的)?.{0,20}(?:记录|订单|账单|会员|订阅).{0,10}(?:恢复|找回)",
            compact,
        )
    )


def _is_archive_record(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    action = r"(?:归档|删除|删掉|移除)"
    record = r"(?:这条|该条|那条|整个)?(?:购买|订单|订阅|会员|账单)?记录"
    return bool(
        re.search(rf"{action}.{{0,30}}{record}", compact)
        or re.search(rf"(?:把|将).{{1,30}}{record}.{{0,10}}{action}", compact)
    )


def _target_segment(text: str, operation: str) -> str:
    if operation in {"archive_record", "restore_record"}:
        value = re.sub(r"取消归档", "", text)
        value = re.sub(r"(?:恢复|找回).{0,3}(?:归档|删除)(?:的)?", "", value)
        value = re.sub(r"(?:归档|删除|删掉|移除)", "", value)
        return value
    return re.split(
        r"(?:改成|改为|修改为|更正为|更新为|设为|设置为|标记为|清空|删除|移除)",
        text,
        maxsplit=1,
    )[0]


def _is_external_action(text: str) -> bool:
    factual = any(token in text for token in ["已经", "已取消", "标记为", "记录为"])
    if factual:
        return False
    return bool(
        re.search(r"(?:帮我|替我|请)(?:去)?(?:付款|支付|退款|取消.*订阅)", text)
        or any(token in text for token in ["停止扣款", "停止自动扣款", "申请退款"])
    )


def _record_type(text: str) -> RecordType | None:
    if any(token.lower() in text.lower() for token in KNOWN_SUBSCRIPTION_SERVICES):
        return RecordType.SUBSCRIPTION
    if any(token in text for token in ["订阅", "会员", "续费", "自动扣款"]):
        return RecordType.SUBSCRIPTION
    if any(token in text for token in KNOWN_BILL_NAMES):
        return RecordType.BILL
    if any(token in text for token in ["账单", "缴费", "还款"]):
        return RecordType.BILL
    if any(token in text for token in ["订单", "购买", "买的", "退货", "保修", "商家"]):
        return RecordType.PURCHASE
    return None


def _target_query(segment: str) -> str | None:
    for phrase in [*KNOWN_SUBSCRIPTION_SERVICES, *KNOWN_BILL_NAMES]:
        if phrase.lower() in segment.lower():
            return phrase
    value = segment.strip(" ，,。；;？?")
    value = re.sub(r"^(?:请|帮我|给我|我想|把|将|我的|那条|这个)+", "", value)
    value = re.sub(r"(?:这条|那条|记录)$", "", value)
    value = re.sub(r"^(?:上个月|这个月|本月|去年|今年|\d{4}年?\d{0,2}月?)", "", value)
    value = re.split(
        r"的(?:金额|价格|标题|名称|日期|续费日|截止日|备注|状态)",
        value,
        maxsplit=1,
    )[0]
    value = re.sub(r"(?:订单|订阅|会员|账单|记录)$", "", value)
    value = value.strip(" 的，,。；;")
    if not value or value in {"这个", "那个", "它", "这条", "那条"}:
        return None
    return value


def _target_date_text(segment: str) -> str | None:
    patterns = [
        r"(\d{4}年\d{1,2}月\d{1,2}[日号]?)",
        r"(\d{4}-\d{1,2}-\d{1,2})",
        r"(\d{4}年\d{1,2}月)",
        r"(上个月|这个月|本月|去年|今年)",
        r"(\d{1,2}月)",
    ]
    for pattern in patterns:
        match = re.search(pattern, segment)
        if match:
            return match.group(1)
    return None


def _target_status(text: str, record_type: RecordType) -> RecordStatus | None:
    if any(token in text for token in ["恢复为未支付", "改回未支付", "重新启用", "恢复为有效", "标记为进行中"]):
        return RecordStatus.ACTIVE
    if any(token in text for token in ["已退货", "已经退货", "标记为已退货"]):
        return RecordStatus.RETURNED
    if any(token in text for token in ["已完成", "已收货", "标记为已完成"]):
        return RecordStatus.COMPLETED
    if any(token in text for token in ["已支付", "已付款", "已缴费", "已经付了", "标记为已支付"]):
        return RecordStatus.PAID
    if any(token in text for token in ["已取消", "已经取消", "标记为已取消", "记录为已取消"]):
        return RecordStatus.CANCELLED
    if record_type == RecordType.BILL and "付了" in text:
        return RecordStatus.PAID
    return None


def _clear_fields(text: str) -> list[str]:
    labels = {
        "购买日期": "event_date",
        "发生日期": "event_date",
        "备注": "notes",
        "商家": "merchant",
        "订单号": "order_number",
        "退货截止日": "return_deadline",
        "保修截止日": "warranty_deadline",
        "服务名": "service_name",
        "付费周期": "billing_cycle",
        "下次续费日": "next_renewal_date",
        "自动续费": "auto_renew",
        "账单名": "bill_name",
        "账期": "billing_period",
        "缴费截止日": "due_date",
    }
    cleared: list[str] = []
    for label, field in labels.items():
        if re.search(rf"(?:删除|清空|移除|不再记录).{{0,12}}{label}|{label}.{{0,8}}(?:删除|清空|移除)", text):
            cleared.append(field)
    return cleared


def _content_values(text: str, record_type: RecordType) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": _text_value(text, r"(?:标题|记录名称)"),
        "amount": None,
        "currency": None,
        "event_date_text": _date_value(text, r"(?:购买日期|发生日期|事项日期)"),
        "notes": _text_value(text, r"备注"),
    }
    amount_match = re.search(
        r"(?:金额|价格|月费|年费)(?:改成|改为|修改为|更正为|设为|是|到)?\s*"
        r"(\d+(?:\.\d+)?)\s*(元|块|人民币|CNY|美元|美金|USD|\$)?",
        text,
        re.I,
    )
    if amount_match and re.search(r"(?:改成|改为|修改|更正|更新|设为)", amount_match.group(0)):
        values["amount"] = float(amount_match.group(1))
        currency_token = (amount_match.group(2) or "").upper()
        if currency_token in {"美元", "美金", "USD", "$"}:
            values["currency"] = "USD"
        elif currency_token:
            values["currency"] = "CNY"
    currency_match = re.search(
        r"币种(?:改成|改为|修改为|设为)\s*([A-Za-z]{3})",
        text,
    )
    if currency_match:
        values["currency"] = currency_match.group(1).upper()

    if record_type == RecordType.PURCHASE:
        values.update(
            {
                "merchant": _text_value(text, r"商家"),
                "order_number": _token_value(text, r"订单号"),
                "return_deadline_text": _date_value(text, r"退货(?:截止)?日期|退货截止日"),
                "warranty_deadline_text": _date_value(text, r"(?:保修|质保)(?:截止)?日期|(?:保修|质保)截止日"),
            }
        )
    elif record_type == RecordType.SUBSCRIPTION:
        values.update(
            {
                "service_name": _text_value(text, r"(?:服务名称|服务名)"),
                "billing_cycle": _cycle_value(text),
                "next_renewal_text": _date_value(text, r"(?:下次)?续费(?:日期|日)|扣款日"),
                "auto_renew": _auto_renew_value(text),
            }
        )
    elif record_type == RecordType.BILL:
        values.update(
            {
                "bill_name": _text_value(text, r"账单(?:名称|名)"),
                "billing_period": _text_value(text, r"(?:账单周期|账期)"),
                "due_date_text": _date_value(text, r"(?:缴费|账单)(?:截止)?日期|缴费截止日"),
            }
        )
    return values


def _text_value(text: str, label_pattern: str) -> str | None:
    match = re.search(
        rf"(?:{label_pattern})(?:改成|改为|修改为|更正为|更新为|设为|是)\s*([^\uff0c,。；;]+)",
        text,
    )
    return match.group(1).strip() if match else None


def _token_value(text: str, label_pattern: str) -> str | None:
    value = _text_value(text, label_pattern)
    if value is None:
        return None
    match = re.match(r"[A-Za-z0-9_-]+", value)
    return match.group(0) if match else None


def _date_value(text: str, label_pattern: str) -> str | None:
    date_pattern = (
        r"\d{4}[年/-]\d{1,2}[月/-]\d{1,2}[日号]?"
        r"|(?:明年|下年)\s*[一二两三四五六七八九十\d]+\s*月\s*[一二两三四五六七八九十\d]+\s*[日号]"
        r"|(?:下个月|下月)\s*[一二两三四五六七八九十\d]+\s*[日号]"
        r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,2}-\d{1,2}|前天|昨天|今天|明天|后天|月底|本月底"
    )
    match = re.search(
        rf"(?:{label_pattern})(?:改到|改成|改为|修改为|更正为|更新为|设为|是)?\s*({date_pattern})",
        text,
    )
    return re.sub(r"\s+", "", match.group(1)) if match else None


def _cycle_value(text: str) -> str | None:
    match = re.search(r"(?:付费周期|计费周期)(?:改成|改为|设为)\s*([^\uff0c,。；;]+)", text)
    if not match:
        return None
    value = match.group(1)
    if any(token in value for token in ["每月", "月付", "按月"]):
        return "monthly"
    if any(token in value for token in ["每年", "年付", "按年"]):
        return "yearly"
    if any(token in value for token in ["每周", "周付", "按周"]):
        return "weekly"
    if "未知" in value:
        return "unknown"
    return None


def _auto_renew_value(text: str) -> bool | None:
    if re.search(r"自动续费(?:改成|改为|设为)\s*(?:是|开启|打开|true)", text, re.I):
        return True
    if re.search(r"自动续费(?:改成|改为|设为)\s*(?:否|关闭|false)", text, re.I):
        return False
    return None
