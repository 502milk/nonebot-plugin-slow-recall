import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed

from .config import Config
from .store import DelayedRecallRule, JsonStore, Scope, SlowModeAction, SlowModeRule

WINDOW_SECONDS = 60.0
MIN_RECALL_DELAY = 0.2


@dataclass
class SlowModeResult:
    rule: SlowModeRule
    exceeded: bool
    first_message_at: float
    message_count: int


class SlowRecallService:
    def __init__(self, config: Config):
        self.config = config
        self.store = JsonStore(config.data_path)
        self._message_windows: dict[str, deque[float]] = defaultdict(deque)
        self._delayed_tasks: set[asyncio.Task] = set()

    async def startup(self) -> None:
        await self.store.load()

    async def shutdown(self) -> None:
        for task in list(self._delayed_tasks):
            task.cancel()
        if self._delayed_tasks:
            await asyncio.gather(*self._delayed_tasks, return_exceptions=True)
        self._delayed_tasks.clear()

    async def set_slow_mode(
        self,
        group_id: int,
        scope: Scope,
        limit: float,
        action: SlowModeAction,
        user_id: int | None = None,
    ) -> SlowModeRule:
        rule = SlowModeRule(
            group_id=group_id,
            scope=scope,
            limit=limit,
            action=action,
            user_id=user_id if scope == "user" else None,
        )
        await self.store.upsert_slow_rule(rule)
        self._clear_window(group_id, scope, user_id)
        return rule

    async def unset_slow_mode(self, group_id: int, scope: Scope, user_id: int | None = None) -> None:
        await self.store.delete_slow_rule(group_id, scope, user_id if scope == "user" else None)
        self._clear_window(group_id, scope, user_id)

    async def list_slow_rules(self, group_id: int) -> list[SlowModeRule]:
        return await self.store.list_slow_rules(group_id)

    async def set_delayed_recall(
        self,
        group_id: int,
        scope: Scope,
        delay: float,
        user_id: int | None = None,
    ) -> DelayedRecallRule:
        rule = DelayedRecallRule(
            group_id=group_id,
            scope=scope,
            delay=max(delay, MIN_RECALL_DELAY),
            user_id=user_id if scope == "user" else None,
        )
        await self.store.upsert_recall_rule(rule)
        return rule

    async def unset_delayed_recall(self, group_id: int, scope: Scope, user_id: int | None = None) -> None:
        await self.store.delete_recall_rule(group_id, scope, user_id if scope == "user" else None)

    async def list_recall_rules(self, group_id: int) -> list[DelayedRecallRule]:
        return await self.store.list_recall_rules(group_id)

    async def apply_slow_mode(self, bot: Bot, event: GroupMessageEvent) -> SlowModeResult | None:
        rule = await self.store.get_slow_rule(event.group_id, event.user_id)
        if rule is None or rule.limit <= 0:
            return None

        now = time.monotonic()
        window = self._window_for(rule, event.user_id)
        window_seconds, capacity = self._slow_window(rule.limit)
        while window and now - window[0] >= window_seconds:
            window.popleft()
        window.append(now)

        result = SlowModeResult(
            rule=rule,
            exceeded=len(window) > capacity,
            first_message_at=window[0],
            message_count=len(window),
        )
        if not result.exceeded:
            return result

        if rule.action in {"recall", "both"}:
            await self._delete_message(bot, event.message_id)

        if rule.action in {"mute", "both"}:
            duration = max(int(round(window_seconds - (now - window[0]))), 1)
            await self._mute_member(bot, event.group_id, event.user_id, duration)
        return result

    async def schedule_delayed_recall(self, bot: Bot, event: GroupMessageEvent) -> None:
        rule = await self.store.get_recall_rule(event.group_id, event.user_id)
        if rule is None:
            return
        task = asyncio.create_task(self._delayed_recall(bot, event.message_id, rule.delay))
        self._delayed_tasks.add(task)
        task.add_done_callback(self._delayed_tasks.discard)

    @staticmethod
    def _slow_window(limit: float) -> tuple[float, int]:
        if limit >= 1:
            return WINDOW_SECONDS, int(limit)
        return WINDOW_SECONDS / limit, 1

    def _window_for(self, rule: SlowModeRule, message_user_id: int) -> deque[float]:
        user_id = rule.user_id if rule.scope == "user" else message_user_id
        key = f"{rule.group_id}:{user_id}"
        return self._message_windows[key]

    def _clear_window(self, group_id: int, scope: Scope, user_id: int | None = None) -> None:
        if scope == "user" and user_id is not None:
            self._message_windows.pop(f"{group_id}:{user_id}", None)
            return
        prefix = f"{group_id}:"
        for key in list(self._message_windows):
            if key.startswith(prefix):
                self._message_windows.pop(key, None)

    async def _delayed_recall(self, bot: Bot, message_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._delete_message(bot, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("延迟撤回消息失败，message_id={}, error={}", message_id, exc)

    async def _delete_message(self, bot: Bot, message_id: int) -> None:
        try:
            await bot.call_api("delete_msg", message_id=message_id)
        except ActionFailed as exc:
            logger.warning("撤回消息失败，message_id={}, error={}", message_id, exc)

    async def _mute_member(self, bot: Bot, group_id: int, user_id: int, duration: int) -> None:
        try:
            await bot.call_api(
                "set_group_ban",
                group_id=group_id,
                user_id=user_id,
                duration=duration,
            )
        except ActionFailed as exc:
            logger.warning(
                "慢速模式禁言失败，group_id={}, user_id={}, duration={}, error={}",
                group_id,
                user_id,
                duration,
                exc,
            )
