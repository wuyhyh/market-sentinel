from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

_COMPLETENESS_LABELS = {
    "complete": "完整（complete）",
    "partial": "部分可用（partial）",
    "failed": "失败（failed）",
}
_MARKET_STATE_LABELS = {
    "auction": "集合竞价",
    "continuous_trading": "连续交易",
    "midday_break": "午间休市",
    "closed": "闭市",
    "unknown": "未知",
}
_FRESHNESS_LABELS = {
    "outside_continuous_trading": "非连续交易时段",
    "not_verified_continuous_trading": "连续交易新鲜度尚未验证",
    "unknown_market_state": "市场状态未知",
    "replay": "历史快照回放",
}
_EXPLICIT_SECRET = re.compile(
    r"(?i)\b(token|cookie|password|passwd|secret|api[_-]?key|account)"
    r"\s*([:=])\s*[^\s,;]+"
)
_EXPLICIT_CHINESE_SECRET = re.compile(
    r"(手机号|账号|密码|令牌|密钥)\s*([:=：])\s*[^\s,;，；]+"
)
_EMAIL = re.compile(r"(?<![\w.-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_MEMORY_ADDRESS = re.compile(r"\b0x[0-9A-Fa-f]{6,}\b")
_SDK_OBJECT = re.compile(r"<[^>\n]{1,160}\bobject at 0x[0-9A-Fa-f]+>")


class ShadowMarkdownRenderError(Exception):
    pass


class ShadowReportMarkdownRenderer:
    """Pure deterministic view over an already-built shadow JSON report."""

    def render(
        self,
        report: Mapping[str, object],
        *,
        json_output_path: Path | None,
    ) -> str:
        analysis = _mapping(report, "deterministic_analysis")
        risk = _mapping(report, "risk_result")
        warnings = _mapping_sequence(report, "warnings")
        provider_errors = _mapping_sequence(report, "provider_errors")
        critical_holdings = _mapping_sequence(analysis, "critical_holdings")
        narrative = report.get("narrative")

        lines = [
            "# MarketSentinel 市场简报",
            "",
            "## 报告信息",
            "",
            f"- 报告编号：{_text(report.get('report_id'))}",
            "- 数据模式：历史快照回放（replay）",
            "- 执行模式：Shadow",
            f"- 数据提供方：{_text(report.get('input_provider'))}",
            f"- 原始快照时间：{_text(report.get('original_completed_at'))}",
            f"- 报告生成时间：{_text(report.get('generated_at'))}",
            f"- 数据完整性：{_completeness(report.get('completeness'))}",
            f"- 原始市场状态：{_market_state(report.get('original_market_state'))}",
            (
                "- 原始新鲜度状态："
                f"{_freshness(report.get('original_freshness_status'))}"
            ),
            "",
            "> 本报告基于历史快照回放，不代表报告生成时的实时市场状态。",
            "",
            "## 数据质量",
            "",
            f"- 请求证券数：{_text(analysis.get('requested_count'))}",
            f"- 有效报价数：{_text(analysis.get('valid_quote_count'))}",
            f"- 缺失证券数：{_text(analysis.get('missing_count'))}",
            f"- 非法报价数：{_text(analysis.get('invalid_quote_count'))}",
            (
                "- 关键持仓缺失："
                f"{_symbol_list(report.get('critical_missing_symbols'))}"
            ),
            f"- Provider 错误数：{len(provider_errors)}",
            f"- Warning 数量：{len(warnings)}",
            f"- 质量结论：{_completeness(report.get('completeness'))}",
            "",
            "## 市场概览",
            "",
            f"- 上涨数量：{_text(analysis.get('advancer_count'))}",
            f"- 下跌数量：{_text(analysis.get('decliner_count'))}",
            f"- 平盘数量：{_text(analysis.get('unchanged_count'))}",
            f"- 平均涨跌幅：{_percent(analysis.get('average_change_pct'))}",
            f"- 中位数涨跌幅：{_percent(analysis.get('median_change_pct'))}",
            (
                "- 最大上涨证券："
                f"{_symbol_with_percent(analysis, 'maximum_gain_symbol', 'maximum_gain_change_pct')}"
            ),
            (
                "- 最大下跌证券："
                f"{_symbol_with_percent(analysis, 'maximum_loss_symbol', 'maximum_loss_change_pct')}"
            ),
            f"- 总成交额：{_text_or_dash(analysis.get('turnover_total'))}",
            "",
            "## 关键持仓观察",
            "",
            "| 代码 | 名称 | 最新价 | 昨收 | 涨跌额 | 涨跌幅 | 交易状态 | 行情时间 |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
        lines.extend(_holding_row(holding) for holding in critical_holdings)
        lines.extend(
            [
                "",
                (
                    "该表只展示快照中的价格事实；不包含持仓数量、成本、市值、盈亏、"
                    "仓位比例或买卖建议。"
                ),
                "",
                "## 风险检查",
                "",
                f"- action：{_text(risk.get('action'))}",
                (
                    "- portfolio_exposure_evaluated："
                    f"{_bool_text(risk.get('portfolio_exposure_evaluated'))}"
                ),
                f"- 原因：{_text(risk.get('reason'))}",
                "",
                (
                    "LLM 不能修改确定性的 risk_action；`no_action` 仅表示未触发动作，"
                    "不能据此推导任何持有或交易结论。"
                ),
                "",
            ]
        )
        risk_warnings = _mapping_sequence(risk, "warnings")
        if risk_warnings:
            lines.append("风险警告：")
            lines.extend(_warning_line(warning) for warning in risk_warnings)
        else:
            lines.append("风险警告：未发现确定性风险警告。")
        if risk.get("portfolio_exposure_evaluated") is False:
            lines.extend(
                [
                    "",
                    (
                        "组合敞口未评估：watchlist 不包含数量、成本和市值，"
                        "因此本报告不能判断组合风险高低。"
                    ),
                ]
            )

        lines.extend(["", "## 叙述摘要", ""])
        lines.extend(_narrative_lines(narrative, report.get("llm_status")))
        lines.extend(["", "## 警告与限制", ""])
        if warnings:
            lines.extend(_warning_line(warning) for warning in warnings)
        else:
            lines.append("未发现数据质量警告。")
        lines.extend(
            [
                "",
                f"- 关键持仓缺失：{_symbol_list(report.get('critical_missing_symbols'))}",
                f"- LLM 状态：{_text(report.get('llm_status'))}",
            ]
        )
        if provider_errors:
            lines.append("- Provider 错误：")
            lines.extend(
                f"  {_warning_line(error, prefix='-')}" for error in provider_errors
            )
        else:
            lines.append("- Provider 错误：无")
        lines.extend(
            [
                "- 数据限制：该报告仅描述录制快照，不包含新闻、公告、因果分析或实时更新。",
                "",
                "## 数据来源与审计",
                "",
                f"- 输入快照：{_text(report.get('input_snapshot_path'))}",
                f"- 输入提供方：{_text(report.get('input_provider'))}",
                (
                    "- source_time 范围："
                    f"{_time_range(report.get('source_time_range'))}"
                ),
                (
                    "- received_at 范围："
                    f"{_time_range(report.get('received_at_range'))}"
                ),
                f"- 回放时间：{_text(report.get('replayed_at'))}",
                f"- 生成时间：{_text(report.get('generated_at'))}",
                (
                    "- JSON 权威报告路径："
                    f"{_json_path(json_output_path)}"
                ),
                "",
            ]
        )
        return "\n".join(lines)


def _mapping(values: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise ShadowMarkdownRenderError(f"{key} must be an object")
    return value


def _mapping_sequence(
    values: Mapping[str, object],
    key: str,
) -> tuple[Mapping[str, object], ...]:
    value = values.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ShadowMarkdownRenderError(f"{key} must be a list")
    if any(not isinstance(item, Mapping) for item in value):
        raise ShadowMarkdownRenderError(f"{key} entries must be objects")
    return tuple(item for item in value if isinstance(item, Mapping))


def _text(value: object) -> str:
    if value is None:
        return "无"
    cleaned = _sanitize_text(str(value)) or "无"
    cleaned = cleaned.replace("&", "&amp;")
    cleaned = cleaned.replace("<", "&lt;").replace(">", "&gt;")
    for character in ("\\", "|", "`", "#"):
        cleaned = cleaned.replace(character, f"\\{character}")
    return cleaned


def _sanitize_text(value: str) -> str:
    cleaned = "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
        or character in {"\n", "\r", "\t"}
    )
    cleaned = _SDK_OBJECT.sub("[redacted-sdk-object]", cleaned)
    cleaned = _MEMORY_ADDRESS.sub("[redacted-memory-address]", cleaned)
    cleaned = _PHONE.sub("[redacted-phone]", cleaned)
    cleaned = _EMAIL.sub("[redacted-email]", cleaned)
    cleaned = _EXPLICIT_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{'[redacted-account]' if match.group(1).lower() == 'account' else '[redacted-secret]'}"
        ),
        cleaned,
    )
    cleaned = _EXPLICIT_CHINESE_SECRET.sub(
        lambda match: (
            f"{match.group(1)}{match.group(2)}"
            f"{'[redacted-account]' if match.group(1) == '账号' else '[redacted-secret]'}"
        ),
        cleaned,
    )
    return " ".join(cleaned.split())[:1000]


def _text_or_dash(value: object) -> str:
    return "—" if value is None else _text(value)


def _completeness(value: object) -> str:
    raw = str(value)
    return _COMPLETENESS_LABELS.get(raw, f"未知（{_text(value)}）")


def _market_state(value: object) -> str:
    raw = str(value)
    label = _MARKET_STATE_LABELS.get(raw, "未知")
    return f"{label}（{_text(value)}）"


def _freshness(value: object) -> str:
    raw = str(value)
    label = _FRESHNESS_LABELS.get(raw, "未知")
    return f"{label}（{_text(value)}）"


def _percent(value: object) -> str:
    return "—" if value is None else f"{_text(value)}%"


def _symbol_with_percent(
    analysis: Mapping[str, object],
    symbol_key: str,
    percentage_key: str,
) -> str:
    symbol = analysis.get(symbol_key)
    percentage = analysis.get(percentage_key)
    if symbol is None:
        return "—"
    return f"{_text(symbol)}（{_percent(percentage)}）"


def _symbol_list(value: object) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return "无"
    symbols = tuple(_text(item) for item in value)
    return "、".join(symbols) if symbols else "无"


def _holding_row(holding: Mapping[str, object]) -> str:
    return "| " + " | ".join(
        (
            _text_or_dash(holding.get("symbol")),
            _text_or_dash(holding.get("name")),
            _text_or_dash(holding.get("last_price")),
            _text_or_dash(holding.get("previous_close")),
            _text_or_dash(holding.get("change")),
            _percent(holding.get("change_pct")),
            _text_or_dash(holding.get("trading_status")),
            _text_or_dash(holding.get("source_time")),
        )
    ) + " |"


def _bool_text(value: object) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "未知"


def _warning_line(
    warning: Mapping[str, object],
    *,
    prefix: str = "-",
) -> str:
    code = _text(warning.get("code") or warning.get("category"))
    severity = _text(warning.get("severity"))
    symbol = _text(warning.get("symbol"))
    message = _text(warning.get("message"))
    return f"{prefix} [{severity}] {code} / {symbol}：{message}"


def _narrative_lines(narrative: object, llm_status: object) -> list[str]:
    if not isinstance(narrative, Mapping):
        return [
            (
                f"Mock LLM 未生成叙述（状态：{_text(llm_status)}）；"
                "事实和确定性分析仍保留。"
            )
        ]
    lines = [f"**摘要：** {_text(narrative.get('summary'))}", ""]
    observations = _text_sequence(narrative.get("observations"))
    limitations = _text_sequence(narrative.get("limitations"))
    lines.append("**观察：**")
    lines.extend(f"- {item}" for item in observations)
    lines.extend(["", "**限制：**"])
    lines.extend(f"- {item}" for item in limitations)
    return lines


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ("无",)
    rendered = tuple(_text(item) for item in value)
    return rendered or ("无",)


def _time_range(value: object) -> str:
    if not isinstance(value, Mapping):
        return "无"
    return f"{_text(value.get('oldest'))} 至 {_text(value.get('newest'))}"


def _json_path(path: Path | None) -> str:
    if path is None:
        return "本次未生成（使用 `--format both` 可同时生成权威 JSON）"
    return _text(path.as_posix())
