import re
from typing import Literal

from nonebot import get_driver, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata

from .config import Config, load_config
from .service import MIN_RECALL_DELAY, SlowRecallService
from .store import DelayedRecallRule, Scope, SlowModeAction, SlowModeRule

__plugin_meta__ = PluginMetadata(
    name="聊天慢速与延迟撤回",
    description="分群控制聊天慢速模式和延迟撤回，支持全体或指定成员规则",
    usage=(
        "慢速 全体 5 分钟 撤回 | 慢速 @用户 2 秒 禁言撤回 | 慢速 全体 0\n"
        "慢速列表 | 慢速列表 全体 | 慢速列表 @用户\n"
        "延迟撤回 全体 3秒 | 延迟撤回 @用户 0.5秒 | 延迟撤回 全体 0\n"
        "延迟撤回列表 | 延迟撤回列表 全体 | 延迟撤回列表 @用户\n"
        "规则列表"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
    config=Config,
)

config = load_config()
service = SlowRecallService(config)

command_matcher = on_message(priority=18, block=False)
message_matcher = on_message(priority=90, block=False)

TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(秒|秒钟|s|sec|secs|second|seconds)?\s*$", re.IGNORECASE)
SLOW_TIME_UNIT = r"秒|秒钟|s|sec|secs|second|seconds|分钟|分|m|min|mins|minute|minutes|小时|时|h|hr|hrs|hour|hours|天|日|d|day|days"
SLOW_RE = re.compile(
    r"^慢速\s+(全体)?\s*(\d+(?:\.\d+)?)(?:\s*((?:\d+(?:\.\d+)?\s*)?(?:"
    + SLOW_TIME_UNIT
    + r")))?\s*(撤回禁言|禁言撤回|撤回\+禁言|禁言\+撤回|撤回|禁言)?$",
    re.IGNORECASE,
)
SLOW_OFF_RE = re.compile(r"^慢速(?:关闭|停止|关)\s*(全体)?$", re.IGNORECASE)
SLOW_LIST_RE = re.compile(r"^慢速列表(?:\s*(全体))?$", re.IGNORECASE)
RECALL_RE = re.compile(r"^延迟撤回\s+(全体)?\s*(.+)$", re.IGNORECASE)
RECALL_LIST_RE = re.compile(r"^延迟撤回列表(?:\s*(全体))?$", re.IGNORECASE)
ALL_LIST_RE = re.compile(r"^(?:规则|全部)列表$", re.IGNORECASE)

CommandType = Literal["slow_on", "slow_off", "slow_list", "recall", "recall_list", "all_list"]


def _is_superuser(bot: Bot, user_id: int) -> bool:
    superusers = getattr(bot.config, "superusers", set())
    return str(user_id) in {str(item) for item in superusers}


def _can_manage(bot: Bot, event: GroupMessageEvent) -> bool:
    if config.slow_recall_allow_superuser and _is_superuser(bot, event.user_id):
        return True
    role = getattr(event.sender, "role", None)
    if config.slow_recall_allow_group_owner and role == "owner":
        return True
    if config.slow_recall_allow_group_admin and role == "admin":
        return True
    return False


def _extract_target_user(message: Message) -> int | None:
    for segment in message:
        if segment.type == "at":
            qq = segment.data.get("qq")
        elif segment.type == "text":
            match = re.search(r"\[CQ:at,qq=(\d+)\]", str(segment.data.get("text", "")))
            qq = match.group(1) if match else None
        else:
            continue
        if not qq or qq == "all":
            continue
        try:
            return int(qq)
        except (TypeError, ValueError):
            return None
    return None


def _message_text_without_at(message: Message) -> str:
    parts: list[str] = []
    for segment in message:
        if segment.type == "text":
            text = str(segment.data.get("text", ""))
            text = re.sub(r"\[CQ:at,qq=\d+\]", " ", text)
            parts.append(text)
    return " ".join(parts).strip()


def _parse_scope(message: Message, explicit_all: str | None) -> tuple[Scope, int | None] | None:
    target_user_id = _extract_target_user(message)
    if target_user_id is not None:
        return "user", target_user_id
    if explicit_all:
        return "all", None
    return None


def _parse_delay(text: str) -> float:
    match = TIME_RE.match(text)
    if not match:
        raise ValueError("延迟时间格式错误")
    return float(match.group(1))


def _parse_slow_limit(count_text: str, time_text: str | None) -> tuple[float, float]:
    count = float(count_text)
    if count <= 0:
        return count, 0
    if time_text is None:
        raise ValueError("慢速条件格式错误，请使用 数量 时间，例如 5 分钟、2 秒、2 5秒")

    seconds = _parse_duration_seconds(time_text)
    if seconds <= 0:
        raise ValueError("慢速时间必须大于 0")
    return count, seconds


def _parse_duration_seconds(text: str) -> float:
    match = re.match(rf"^\s*(?:(\d+(?:\.\d+)?)\s*)?({SLOW_TIME_UNIT})\s*$", text, re.IGNORECASE)
    if not match:
        raise ValueError("慢速时间格式错误，支持 秒、分钟、小时、天 及对应英文缩写")

    value = float(match.group(1) or 1)
    unit = match.group(2).lower()
    if unit in {"秒", "秒钟", "s", "sec", "secs", "second", "seconds"}:
        return value
    if unit in {"分钟", "分", "m", "min", "mins", "minute", "minutes"}:
        return value * 60
    if unit in {"小时", "时", "h", "hr", "hrs", "hour", "hours"}:
        return value * 60 * 60
    if unit in {"天", "日", "d", "day", "days"}:
        return value * 24 * 60 * 60
    raise ValueError("慢速时间格式错误，支持 秒、分钟、小时、天 及对应英文缩写")


def _describe_scope(scope: Scope, user_id: int | None) -> str:
    return "全体" if scope == "all" else f"@{user_id}"


def _describe_slow_action(action: SlowModeAction) -> str:
    if action == "recall":
        return "撤回"
    if action == "mute":
        return "禁言"
    return "撤回并禁言"


def _format_slow_rule(rule: SlowModeRule) -> str:
    target = "全体" if rule.scope == "all" else f"@{rule.user_id}"
    limit_text = f"{rule.limit:g}"
    return f"慢速 {target} {limit_text} 条/{_format_duration(rule.window_seconds)}，超限{_describe_slow_action(rule.action)}"


def _format_duration(seconds: float) -> str:
    if seconds % (24 * 60 * 60) == 0:
        return f"{seconds / (24 * 60 * 60):g}天"
    if seconds % (60 * 60) == 0:
        return f"{seconds / (60 * 60):g}小时"
    if seconds % 60 == 0:
        return f"{seconds / 60:g}分钟"
    return f"{seconds:g}秒"


def _format_recall_rule(rule: DelayedRecallRule) -> str:
    target = "全体" if rule.scope == "all" else f"@{rule.user_id}"
    return f"延迟撤回 {target} {rule.delay:.1f}秒"


def _format_rule_list(title: str, items: list[str]) -> str:
    if not items:
        return f"{title}：暂无"
    return f"{title}：\n" + "\n".join(f"- {item}" for item in items)


def _filter_rules_by_scope(rules: list, scope: Scope, user_id: int | None) -> list:
    if scope == "all" and user_id is None:
        return rules
    return [
        rule
        for rule in rules
        if getattr(rule, "scope", None) == scope and getattr(rule, "user_id", None) == user_id
    ]


def _parse_command(message: Message) -> tuple[CommandType, Scope, int | None, float | None, float | None, SlowModeAction | None, float | None] | None:
    text = _message_text_without_at(message)
    has_invalid_at_text = _extract_target_user(message) is None and re.search(r"@\S+", text)
    if has_invalid_at_text and (text.startswith("慢速") or text.startswith("延迟撤回")):
        raise ValueError("指定用户时请使用 QQ 的真实 @")

    if ALL_LIST_RE.match(text):
        return "all_list", "all", None, None, None, None, None

    slow_list_match = SLOW_LIST_RE.match(text)
    if slow_list_match:
        scope = _parse_scope(message, slow_list_match.group(1))
        if scope is None:
            scope = ("all", None)
        return "slow_list", scope[0], scope[1], None, None, None, None

    recall_list_match = RECALL_LIST_RE.match(text)
    if recall_list_match:
        scope = _parse_scope(message, recall_list_match.group(1))
        if scope is None:
            scope = ("all", None)
        return "recall_list", scope[0], scope[1], None, None, None, None

    slow_match = SLOW_RE.match(text)
    if slow_match:
        scope = _parse_scope(message, slow_match.group(1))
        if scope is None:
            return None
        action_text = slow_match.group(4) or "撤回"
        if "撤回" in action_text and "禁言" in action_text:
            action: SlowModeAction = "both"
        else:
            action = "mute" if action_text == "禁言" else "recall"
        limit, window_seconds = _parse_slow_limit(slow_match.group(2), slow_match.group(3))
        return "slow_on", scope[0], scope[1], limit, window_seconds, action, None

    slow_off_match = SLOW_OFF_RE.match(text)
    if slow_off_match:
        scope = _parse_scope(message, slow_off_match.group(1))
        if scope is None:
            return None
        return "slow_off", scope[0], scope[1], None, None, None, None

    recall_match = RECALL_RE.match(text)
    if recall_match:
        scope = _parse_scope(message, recall_match.group(1))
        if scope is None:
            return None
        delay = _parse_delay(recall_match.group(2))
        return "recall", scope[0], scope[1], None, None, None, delay

    return None


@command_matcher.handle()
async def handle_command(bot: Bot, event: Event, matcher: Matcher) -> None:
    if not isinstance(event, GroupMessageEvent):
        return

    try:
        parsed = _parse_command(event.message)
    except ValueError as exc:
        await matcher.finish(str(exc) or "参数格式错误")

    if parsed is None:
        return

    if not _can_manage(bot, event):
        await matcher.finish("你没有管理权限")

    command, scope, user_id, limit, window_seconds, action, delay = parsed
    response_text: str
    try:
        if command == "slow_on":
            assert limit is not None
            assert window_seconds is not None
            assert action is not None
            if limit <= 0:
                await service.unset_slow_mode(event.group_id, scope, user_id)
                response_text = f"已关闭慢速模式：{_describe_scope(scope, user_id)}"
            else:
                rule = await service.set_slow_mode(event.group_id, scope, limit, window_seconds, action, user_id)
                response_text = f"已开启：{_format_slow_rule(rule)}"
        elif command == "slow_off":
            await service.unset_slow_mode(event.group_id, scope, user_id)
            response_text = f"已关闭慢速模式：{_describe_scope(scope, user_id)}"
        elif command == "slow_list":
            rules = _filter_rules_by_scope(await service.list_slow_rules(event.group_id), scope, user_id)
            response_text = _format_rule_list("慢速模式规则", [_format_slow_rule(rule) for rule in rules])
        elif command == "recall":
            assert delay is not None
            if delay <= 0:
                await service.unset_delayed_recall(event.group_id, scope, user_id)
                response_text = f"已关闭延迟撤回：{_describe_scope(scope, user_id)}"
            else:
                rule = await service.set_delayed_recall(event.group_id, scope, max(delay, MIN_RECALL_DELAY), user_id)
                response_text = f"已开启：{_format_recall_rule(rule)}"
        elif command == "recall_list":
            rules = _filter_rules_by_scope(await service.list_recall_rules(event.group_id), scope, user_id)
            response_text = _format_rule_list("延迟撤回规则", [_format_recall_rule(rule) for rule in rules])
        else:
            slow_rules = await service.list_slow_rules(event.group_id)
            recall_rules = await service.list_recall_rules(event.group_id)
            response_text = "\n\n".join(
                [
                    _format_rule_list("慢速模式规则", [_format_slow_rule(rule) for rule in slow_rules]),
                    _format_rule_list("延迟撤回规则", [_format_recall_rule(rule) for rule in recall_rules]),
                ]
            )
    except Exception as exc:
        logger.warning("设置聊天慢速/延迟撤回规则失败，group_id={}, error={}", event.group_id, exc)
        response_text = "执行失败"

    await matcher.finish(response_text)


@message_matcher.handle()
async def handle_group_message(bot: Bot, event: Event) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    try:
        is_manage_command = _parse_command(event.message) is not None
    except ValueError:
        is_manage_command = False
    if is_manage_command and _can_manage(bot, event):
        return

    await service.apply_slow_mode(bot, event)
    await service.schedule_delayed_recall(bot, event)


driver = get_driver()


@driver.on_startup
async def startup_plugin() -> None:
    await service.startup()
    logger.info("nonebot_plugin_slow_recall 已启动，数据文件: {}", config.data_path)


@driver.on_shutdown
async def shutdown_plugin() -> None:
    await service.shutdown()
