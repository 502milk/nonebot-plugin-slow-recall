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
        "慢速 全体 5 撤回 | 慢速 @用户 3 禁言 | 慢速关闭 全体 | 慢速关闭 @用户\n"
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
SLOW_RE = re.compile(r"^慢速\s+(全体)?\s*(\d+(?:\.\d+)?)\s*(撤回|禁言)?$", re.IGNORECASE)
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
        if segment.type != "at":
            continue
        qq = segment.data.get("qq")
        if not qq or qq == "all":
            continue
        try:
            return int(qq)
        except ValueError:
            return None
    return None


def _message_text_without_at(message: Message) -> str:
    parts: list[str] = []
    for segment in message:
        if segment.type == "text":
            parts.append(str(segment.data.get("text", "")))
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


def _describe_scope(scope: Scope, user_id: int | None) -> str:
    return "全体" if scope == "all" else f"@{user_id}"


def _describe_slow_action(action: SlowModeAction) -> str:
    return "撤回" if action == "recall" else "禁言"


def _format_slow_rule(rule: SlowModeRule) -> str:
    target = "全体" if rule.scope == "all" else f"@{rule.user_id}"
    limit_text = f"{rule.limit:g}"
    return f"慢速 {target} {limit_text} 次/60秒，超限{_describe_slow_action(rule.action)}"


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


def _parse_command(message: Message) -> tuple[CommandType, Scope, int | None, float | None, SlowModeAction | None, float | None] | None:
    text = _message_text_without_at(message)

    if ALL_LIST_RE.match(text):
        return "all_list", "all", None, None, None, None

    slow_list_match = SLOW_LIST_RE.match(text)
    if slow_list_match:
        scope = _parse_scope(message, slow_list_match.group(1))
        if scope is None:
            scope = ("all", None)
        return "slow_list", scope[0], scope[1], None, None, None

    recall_list_match = RECALL_LIST_RE.match(text)
    if recall_list_match:
        scope = _parse_scope(message, recall_list_match.group(1))
        if scope is None:
            scope = ("all", None)
        return "recall_list", scope[0], scope[1], None, None, None

    slow_match = SLOW_RE.match(text)
    if slow_match:
        scope = _parse_scope(message, slow_match.group(1))
        if scope is None:
            return None
        action_text = slow_match.group(3) or "撤回"
        action: SlowModeAction = "mute" if action_text == "禁言" else "recall"
        return "slow_on", scope[0], scope[1], float(slow_match.group(2)), action, None

    slow_off_match = SLOW_OFF_RE.match(text)
    if slow_off_match:
        scope = _parse_scope(message, slow_off_match.group(1))
        if scope is None:
            return None
        return "slow_off", scope[0], scope[1], None, None, None

    recall_match = RECALL_RE.match(text)
    if recall_match:
        scope = _parse_scope(message, recall_match.group(1))
        if scope is None:
            return None
        delay = _parse_delay(recall_match.group(2))
        return "recall", scope[0], scope[1], None, None, delay

    return None


@command_matcher.handle()
async def handle_command(bot: Bot, event: Event, matcher: Matcher) -> None:
    if not isinstance(event, GroupMessageEvent):
        return

    try:
        parsed = _parse_command(event.message)
    except ValueError:
        await matcher.finish("参数格式错误")

    if parsed is None:
        return

    if not _can_manage(bot, event):
        await matcher.finish("你没有管理权限")

    command, scope, user_id, limit, action, delay = parsed
    response_text: str
    try:
        if command == "slow_on":
            assert limit is not None
            assert action is not None
            rule = await service.set_slow_mode(event.group_id, scope, limit, action, user_id)
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

    slow_result = await service.apply_slow_mode(bot, event)
    if slow_result is not None and slow_result.exceeded and slow_result.rule.action == "recall":
        return

    await service.schedule_delayed_recall(bot, event)


driver = get_driver()


@driver.on_startup
async def startup_plugin() -> None:
    await service.startup()
    logger.info("nonebot_plugin_slow_recall 已启动，数据文件: {}", config.data_path)


@driver.on_shutdown
async def shutdown_plugin() -> None:
    await service.shutdown()
