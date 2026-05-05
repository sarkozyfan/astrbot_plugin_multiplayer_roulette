import asyncio
from typing import Optional

from astrbot.api import FunctionTool, logger
from astrbot.api.event import AstrMessageEvent


class BaseRouletteTool:
    def _get_group_id(self, event: AstrMessageEvent) -> Optional[int]:
        group_id = getattr(event.message_obj, "group_id", None)
        if group_id:
            return int(group_id)
        try:
            origin = getattr(event, "unified_msg_origin", "")
            if origin and ":group:" in origin:
                parts = origin.split(":")
                if len(parts) >= 3:
                    return int(parts[2])
        except (ValueError, AttributeError):
            pass
        return None


class MultiplayerRouletteTool(FunctionTool, BaseRouletteTool):
    """多人轮盘 AI 触发器。"""

    def __init__(self, plugin_instance=None):
        self.name = "multiplayer_roulette"
        self.description = """多人霰弹轮盘赌游戏控制器。

IMPORTANT: This is ONLY a trigger tool. The plugin handles all game logic, messaging, items, HP, and records.
Do not narrate results yourself.

ACTIONS:
- "start": 开始多人轮盘。User should @ one or more players; the starter is included automatically.
- "self": shoot self / 朝自己开枪。
- "target": shoot an opponent. In multiplayer the user should @ a live target; in 2-player games target can be omitted.
- "status": query current room status.

Use this when users say things like:
- 开始轮盘、来一局霰弹轮盘、和 @某人 玩
- 打自己、朝自己开枪、shoot myself
- 打对方、开枪、射他、shoot target
- 当前状态、还剩几发、谁的回合"""

        self.parameters = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "操作类型: start/self/target/status",
                    "enum": ["start", "self", "target", "status"],
                },
            },
            "required": ["action"],
        }
        self.plugin = plugin_instance

    async def run(self, event: AstrMessageEvent, action: str) -> str:
        try:
            if action not in ["start", "self", "target", "status"]:
                return f"PARAM_ERROR: Invalid action '{action}'"
            if not self.plugin:
                return "SYSTEM_ERROR: Plugin instance unavailable"

            timeout = getattr(self.plugin, "ai_trigger_delay", 2)
            if hasattr(self.plugin, "_register_ai_trigger"):
                trigger_id = self.plugin._register_ai_trigger(action, event)
                return f"TRIGGER_QUEUED: {action} queued as {trigger_id}, delay={timeout}s"

            await asyncio.sleep(timeout)
            await self._execute_action(action, event)
            return f"TRIGGER_SUCCESS: {action} executed"
        except Exception as e:
            logger.error(f"MultiplayerRouletteTool trigger failed: {e}", exc_info=True)
            return f"SYSTEM_ERROR: Failed to trigger {action}"

    async def _execute_action(self, action: str, event: AstrMessageEvent):
        if action == "start":
            await self.plugin.ai_start_game(event)
        elif action == "self":
            await self.plugin.ai_shoot_self(event)
        elif action == "target":
            await self.plugin.ai_shoot_target(event)
        elif action == "status":
            await self.plugin.ai_check_status(event)
