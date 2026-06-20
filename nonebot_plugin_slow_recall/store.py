import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

SlowModeAction = Literal["recall", "mute"]
Scope = Literal["all", "user"]


@dataclass
class SlowModeRule:
    group_id: int
    scope: Scope
    limit: float
    action: SlowModeAction
    user_id: int | None = None

    @property
    def key(self) -> str:
        return rule_key(self.group_id, self.scope, self.user_id)

    @classmethod
    def from_dict(cls, payload: dict) -> "SlowModeRule":
        return cls(
            group_id=int(payload["group_id"]),
            scope=payload.get("scope", "all"),
            limit=max(float(payload.get("limit", 1)), 0.001),
            action=payload.get("action", "recall"),
            user_id=(
                int(payload["user_id"])
                if payload.get("user_id") is not None
                else None
            ),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DelayedRecallRule:
    group_id: int
    scope: Scope
    delay: float
    user_id: int | None = None

    @property
    def key(self) -> str:
        return rule_key(self.group_id, self.scope, self.user_id)

    @classmethod
    def from_dict(cls, payload: dict) -> "DelayedRecallRule":
        return cls(
            group_id=int(payload["group_id"]),
            scope=payload.get("scope", "all"),
            delay=max(float(payload.get("delay", 0.2)), 0.2),
            user_id=(
                int(payload["user_id"])
                if payload.get("user_id") is not None
                else None
            ),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def rule_key(group_id: int, scope: Scope, user_id: int | None = None) -> str:
    if scope == "all":
        return f"{group_id}:all"
    return f"{group_id}:user:{user_id}"


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self._slow_rules: dict[str, SlowModeRule] = {}
        self._recall_rules: dict[str, DelayedRecallRule] = {}

    async def load(self) -> None:
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                await self._write_unlocked()
                return
            raw_text = self.path.read_text(encoding="utf-8").strip()
            if not raw_text:
                await self._write_unlocked()
                return
            payload = json.loads(raw_text)
            self._slow_rules = {
                rule.key: rule
                for rule in (
                    SlowModeRule.from_dict(item)
                    for item in payload.get("slow_mode", [])
                )
            }
            self._recall_rules = {
                rule.key: rule
                for rule in (
                    DelayedRecallRule.from_dict(item)
                    for item in payload.get("delayed_recall", [])
                )
            }

    async def get_slow_rule(self, group_id: int, user_id: int) -> SlowModeRule | None:
        async with self._lock:
            return self._slow_rules.get(rule_key(group_id, "user", user_id)) or self._slow_rules.get(rule_key(group_id, "all"))

    async def list_slow_rules(self, group_id: int) -> list[SlowModeRule]:
        async with self._lock:
            return [rule for rule in self._slow_rules.values() if rule.group_id == group_id]

    async def upsert_slow_rule(self, rule: SlowModeRule) -> None:
        async with self._lock:
            self._slow_rules[rule.key] = rule
            await self._write_unlocked()

    async def delete_slow_rule(self, group_id: int, scope: Scope, user_id: int | None = None) -> None:
        async with self._lock:
            self._slow_rules.pop(rule_key(group_id, scope, user_id), None)
            await self._write_unlocked()

    async def get_recall_rule(self, group_id: int, user_id: int) -> DelayedRecallRule | None:
        async with self._lock:
            return self._recall_rules.get(rule_key(group_id, "user", user_id)) or self._recall_rules.get(rule_key(group_id, "all"))

    async def list_recall_rules(self, group_id: int) -> list[DelayedRecallRule]:
        async with self._lock:
            return [rule for rule in self._recall_rules.values() if rule.group_id == group_id]

    async def upsert_recall_rule(self, rule: DelayedRecallRule) -> None:
        async with self._lock:
            self._recall_rules[rule.key] = rule
            await self._write_unlocked()

    async def delete_recall_rule(self, group_id: int, scope: Scope, user_id: int | None = None) -> None:
        async with self._lock:
            self._recall_rules.pop(rule_key(group_id, scope, user_id), None)
            await self._write_unlocked()

    async def _write_unlocked(self) -> None:
        payload = {
            "version": 1,
            "slow_mode": [rule.to_dict() for rule in self._slow_rules.values()],
            "delayed_recall": [rule.to_dict() for rule in self._recall_rules.values()],
        }
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(self.path)
