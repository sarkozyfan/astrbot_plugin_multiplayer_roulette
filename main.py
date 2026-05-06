import asyncio
import datetime
import json
import os
import random
import re
from pathlib import Path
from typing import Any

import yaml

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_multiplayer_roulette"
PLUGIN_AUTHOR = "sarkozyfan"
PLUGIN_DESCRIPTION = "基于恶魔轮盘赌规则的qq群游戏"
PLUGIN_REPO = ""
_FALLBACK_VERSION = "2.0.0"

try:
    from astrbot.core.star.filter.event_message_type import EventMessageType
except ImportError:
    EventMessageType = None


LIVE = "live"
BLANK = "blank"

ITEM_ALIASES = {
    "放大镜": "magnifier",
    "镜子": "magnifier",
    "啤酒": "beer",
    "退弹": "beer",
    "手铐": "cuffs",
    "铐子": "cuffs",
    "香烟": "cigarette",
    "烟": "cigarette",
    "锯子": "saw",
    "手锯": "saw",
    "逆转器": "inverter",
    "逆转": "inverter",
    "手机": "phone",
    "过期药": "expired_medicine",
    "药": "expired_medicine",
    "注射器": "syringe",
    "针": "syringe",
    "针筒": "syringe",
}

ITEM_NAMES = {
    "magnifier": "放大镜",
    "beer": "啤酒",
    "cuffs": "手铐",
    "cigarette": "香烟",
    "saw": "锯子",
    "inverter": "逆转器",
    "phone": "手机",
    "expired_medicine": "过期药",
    "syringe": "注射器",
}

ITEM_POOL = list(ITEM_NAMES.keys())

DEFAULT_SETTINGS = {
    "roulette_timeout_seconds": 180,
    "roulette_min_hp": 2,
    "roulette_max_hp": 5,
    "roulette_min_shells": 2,
    "roulette_max_shells": 9,
    "roulette_min_live_shells": 1,
    "roulette_items_per_round": 3,
    "roulette_allow_ai_trigger": True,
    "ai_trigger_delay": 2,
}

MAX_ITEMS_PER_PLAYER = 8


@register(
    PLUGIN_NAME,
    PLUGIN_AUTHOR,
    PLUGIN_DESCRIPTION,
    _FALLBACK_VERSION,
    PLUGIN_REPO,
)
class MultiplayerRoulettePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.plugin_version = self._load_plugin_version()

        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.records_file = self.data_dir / "roulette_records.json"

        self.games: dict[int, dict[str, Any]] = {}
        self.timeout_tasks: dict[int, asyncio.Task] = {}
        self.records: dict[str, dict[str, int]] = {}
        self.ai_trigger_queue: dict[str, dict[str, Any]] = {}
        self.ai_trigger_counter = 0

        self.timeout = DEFAULT_SETTINGS["roulette_timeout_seconds"]
        self.min_hp = DEFAULT_SETTINGS["roulette_min_hp"]
        self.max_hp = DEFAULT_SETTINGS["roulette_max_hp"]
        self.min_shells = DEFAULT_SETTINGS["roulette_min_shells"]
        self.max_shells = DEFAULT_SETTINGS["roulette_max_shells"]
        self.min_live_shells = DEFAULT_SETTINGS["roulette_min_live_shells"]
        self.items_per_round = DEFAULT_SETTINGS["roulette_items_per_round"]
        self.ai_trigger_delay = DEFAULT_SETTINGS["ai_trigger_delay"]
        self.allow_ai_start = DEFAULT_SETTINGS["roulette_allow_ai_trigger"]
        self._config_file_candidates = self._find_config_files()
        self._refresh_settings()

        self._load_records()
        self._register_function_tools()

    def _find_config_files(self) -> list[Path]:
        names = {
            f"{PLUGIN_NAME}_config.json",
            f"{Path(__file__).parent.name}_config.json",
        }
        candidates: list[Path] = []
        for parent in Path(__file__).resolve().parents:
            for name in names:
                candidates.append(parent / "config" / name)
                candidates.append(parent / "data" / "config" / name)
        unique: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path)
            if key not in seen:
                unique.append(path)
                seen.add(key)
        return unique

    def _runtime_config(self) -> dict[str, Any]:
        data: dict[str, Any] = dict(DEFAULT_SETTINGS)
        try:
            if isinstance(self.config, dict):
                data.update(self.config)
            elif self.config:
                data.update(dict(self.config))
        except Exception as e:
            logger.debug(f"读取内存配置失败: {e}")

        for path in self._config_file_candidates:
            try:
                if path.exists():
                    with open(path, encoding="utf-8-sig") as f:
                        file_data = json.load(f)
                    if isinstance(file_data, dict):
                        data.update(file_data)
                        logger.debug(f"已从配置文件同步轮盘设置: {path}")
                    break
            except Exception as e:
                logger.warning(f"读取插件配置文件失败 {path}: {e}")
        return data

    def _active_config_file(self) -> str:
        for path in self._config_file_candidates:
            try:
                if path.exists():
                    return str(path)
            except Exception:
                continue
        return "未找到配置文件，使用内存配置/默认值"

    def _to_int(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _to_bool(self, value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in ("true", "1", "yes", "on", "开启"):
                return True
            if normalized in ("false", "0", "no", "off", "关闭"):
                return False
        return default

    def _refresh_settings(self) -> None:
        cfg = self._runtime_config()

        self.timeout = max(
            1,
            self._to_int(
                cfg.get("roulette_timeout_seconds"),
                DEFAULT_SETTINGS["roulette_timeout_seconds"],
            ),
        )
        self.min_hp = max(
            1,
            self._to_int(cfg.get("roulette_min_hp"), DEFAULT_SETTINGS["roulette_min_hp"]),
        )
        self.max_hp = self._to_int(
            cfg.get("roulette_max_hp"), DEFAULT_SETTINGS["roulette_max_hp"]
        )
        if self.max_hp < self.min_hp:
            self.max_hp = self.min_hp

        self.min_shells = max(
            2,
            self._to_int(
                cfg.get("roulette_min_shells"), DEFAULT_SETTINGS["roulette_min_shells"]
            ),
        )
        self.max_shells = self._to_int(
            cfg.get("roulette_max_shells"), DEFAULT_SETTINGS["roulette_max_shells"]
        )
        if self.max_shells < self.min_shells:
            self.max_shells = self.min_shells
        self.min_live_shells = max(
            1,
            self._to_int(
                cfg.get("roulette_min_live_shells"),
                DEFAULT_SETTINGS["roulette_min_live_shells"],
            ),
        )
        if self.min_live_shells >= self.max_shells:
            self.min_live_shells = max(1, self.max_shells - 1)

        self.items_per_round = max(
            0,
            self._to_int(
                cfg.get("roulette_items_per_round"),
                DEFAULT_SETTINGS["roulette_items_per_round"],
            ),
        )
        self.ai_trigger_delay = max(
            0,
            self._to_int(
                cfg.get("ai_trigger_delay"), DEFAULT_SETTINGS["ai_trigger_delay"]
            ),
        )
        self.allow_ai_start = self._to_bool(
            cfg.get("roulette_allow_ai_trigger"),
            DEFAULT_SETTINGS["roulette_allow_ai_trigger"],
        )

    def _load_plugin_version(self) -> str:
        try:
            metadata_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
            if os.path.exists(metadata_path):
                with open(metadata_path, encoding="utf-8") as f:
                    metadata = yaml.safe_load(f) or {}
                return str(metadata.get("version", _FALLBACK_VERSION))
        except Exception as e:
            logger.error(f"读取插件版本失败: {e}")
        return _FALLBACK_VERSION

    def _register_function_tools(self):
        try:
            from .tools.roulette_game_tool import MultiplayerRouletteTool

            tool = MultiplayerRouletteTool(plugin_instance=self)
            if hasattr(self.context, "add_llm_tools"):
                self.context.add_llm_tools(tool)
            else:
                self.context.provider_manager.llm_tools.func_list.append(tool)
            logger.info("多人霰弹轮盘 AI 触发器注册成功")
        except Exception as e:
            logger.error(f"注册 AI 工具失败: {e}", exc_info=True)

    def _load_records(self):
        try:
            if self.records_file.exists():
                with open(self.records_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.records = {
                        str(k): {
                            "wins": int(v.get("wins", 0)),
                            "losses": int(v.get("losses", 0)),
                            "shots": int(v.get("shots", 0)),
                            "hits": int(v.get("hits", 0)),
                        }
                        for k, v in data.items()
                        if isinstance(v, dict)
                    }
        except Exception as e:
            logger.error(f"读取战绩失败: {e}")
            self.records = {}

    def _save_records(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.records_file, "w", encoding="utf-8") as f:
                json.dump(self.records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存战绩失败: {e}")

    def _record_for(self, user_id: int) -> dict[str, int]:
        key = str(user_id)
        self.records.setdefault(key, {"wins": 0, "losses": 0, "shots": 0, "hits": 0})
        return self.records[key]

    def _get_group_id(self, event: AstrMessageEvent) -> int | None:
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

    def _get_user_name(self, event: AstrMessageEvent) -> str:
        return event.get_sender_name() or "玩家"

    async def _get_member_name(self, event: AstrMessageEvent, group_id: int, user_id: int) -> str:
        if str(user_id) == str(event.get_sender_id()):
            return self._get_user_name(event)
        try:
            if hasattr(event.bot, "get_group_member_info"):
                info = await event.bot.get_group_member_info(
                    group_id=group_id, user_id=user_id, no_cache=False
                )
                if isinstance(info, dict):
                    return (
                        str(info.get("card") or info.get("nickname") or info.get("name") or user_id)
                    )
                return str(getattr(info, "card", "") or getattr(info, "nickname", "") or user_id)
        except Exception:
            pass
        return str(user_id)

    def _message_segments(self, event: AstrMessageEvent) -> list[Any]:
        message_obj = getattr(event, "message_obj", None)
        segments: list[Any] = []
        for attr in ("message", "message_chain", "messages", "chain"):
            value = getattr(message_obj, attr, None)
            if isinstance(value, list):
                segments.extend(value)
        value = getattr(event, "message_chain", None)
        if isinstance(value, list):
            segments.extend(value)
        value = getattr(event, "message", None)
        if isinstance(value, list):
            segments.extend(value)
        return segments

    def _extract_at_user_ids(self, event: AstrMessageEvent) -> list[int]:
        result: list[int] = []

        def add(value: Any):
            try:
                uid = int(value)
            except (TypeError, ValueError):
                return
            if uid not in result and str(uid) != "0":
                result.append(uid)

        def inspect(value: Any):
            if value is None:
                return
            if isinstance(value, str):
                for match in re.findall(r"\[CQ:at,qq=(\d+)\]", value):
                    add(match)
                return
            if isinstance(value, list) or isinstance(value, tuple):
                for item in value:
                    inspect(item)
                return
            if isinstance(value, dict):
                seg_type = str(
                    value.get("type")
                    or value.get("seg_type")
                    or value.get("message_type")
                    or ""
                ).lower()
                data = value.get("data") or value.get("attrs") or value
                if seg_type == "at" or "qq" in value or "user_id" in value:
                    if isinstance(data, dict):
                        add(
                            data.get("qq")
                            or data.get("user_id")
                            or data.get("id")
                            or data.get("target")
                        )
                    add(
                        value.get("qq")
                        or value.get("user_id")
                        or value.get("id")
                        or value.get("target")
                    )
                inspect(value.get("data"))
                return

            seg_type = str(
                getattr(value, "type", "")
                or getattr(value, "seg_type", "")
                or getattr(value, "message_type", "")
                or getattr(value, "name", "")
            ).lower()
            data = getattr(value, "data", None) or getattr(value, "attrs", None)
            if seg_type == "at" or value.__class__.__name__.lower() in ("at", "atsegment"):
                if isinstance(data, dict):
                    add(
                        data.get("qq")
                        or data.get("user_id")
                        or data.get("id")
                        or data.get("target")
                    )
                else:
                    add(
                        getattr(data, "qq", None)
                        or getattr(data, "user_id", None)
                        or getattr(data, "id", None)
                        or getattr(data, "target", None)
                    )
                add(
                    getattr(value, "qq", None)
                    or getattr(value, "user_id", None)
                    or getattr(value, "id", None)
                    or getattr(value, "target", None)
                )
            inspect(data)

        for seg in self._message_segments(event):
            inspect(seg)

        message_obj = getattr(event, "message_obj", None)
        for attr in ("raw_message", "message_str", "message"):
            inspect(getattr(message_obj, attr, None))
        inspect(getattr(event, "message_str", None))
        inspect(getattr(event, "raw_message", None))

        logger.debug(f"轮盘解析到@用户: {result}")
        return result

    def _extract_target_from_message(self, event: AstrMessageEvent, game: dict[str, Any]) -> int | None:
        current = int(event.get_sender_id())
        at_users = [uid for uid in self._extract_at_user_ids(event) if uid != current]
        alive = set(self._alive_players(game))
        for uid in at_users:
            if uid in alive:
                return uid
        opponents = [uid for uid in self._alive_players(game) if uid != current]
        if len(opponents) == 1:
            return opponents[0]
        return None

    def _extract_item_name(self, event: AstrMessageEvent) -> str:
        words = (event.message_str or "").strip().split()
        for word in words:
            normalized = word.lstrip("/")
            if normalized in ("轮盘", "使用"):
                continue
            if normalized in ITEM_ALIASES:
                return normalized
        return ""

    def _shell_name(self, shell: str) -> str:
        return "实弹" if shell == LIVE else "空包弹"

    def _make_shells(self, player_count: int) -> list[str]:
        self._refresh_settings()
        shell_count = random.randint(self.min_shells, self.max_shells)
        min_live = min(self.min_live_shells, max(1, shell_count - 1))
        live_count = random.randint(min_live, max(1, shell_count - 1))
        blank_count = shell_count - live_count
        shells = [LIVE] * live_count + [BLANK] * blank_count
        random.shuffle(shells)
        return shells

    def _add_item(self, player: dict[str, Any], item: str) -> bool:
        if len(player["items"]) >= MAX_ITEMS_PER_PLAYER:
            return False
        player["items"].append(item)
        return True

    def _return_item(self, player: dict[str, Any], item: str) -> None:
        if not self._add_item(player, item):
            logger.warning(f"道具返还失败，{player['name']} 道具已达上限")

    def _deal_items(self, game: dict[str, Any]) -> list[str]:
        self._refresh_settings()
        lines: list[str] = []
        if self.items_per_round <= 0:
            return lines
        lines.append("抽取道具：")
        for uid in self._alive_players(game):
            player = game["players"][uid]
            available_slots = max(0, MAX_ITEMS_PER_PLAYER - len(player["items"]))
            draw_count = min(self.items_per_round, available_slots)
            if draw_count <= 0:
                lines.append(f"- {player['name']} 道具已满，未获得新道具")
                continue
            gained = random.choices(ITEM_POOL, k=draw_count)
            for item in gained:
                self._add_item(player, item)
            suffix = "" if draw_count == self.items_per_round else f"（道具上限 {MAX_ITEMS_PER_PLAYER}）"
            lines.append(f"- {player['name']} 获得：{self._format_items(gained)}{suffix}")
        return lines

    def _reload_shells(self, game: dict[str, Any]) -> list[str]:
        game["shells"] = self._make_shells(len(game["players"]))
        game["known_current"] = None
        lines = [self._shell_summary(game["shells"])]
        lines.extend(self._deal_items(game))
        return lines

    def _shell_summary(self, shells: list[str]) -> str:
        live = shells.count(LIVE)
        blank = shells.count(BLANK)
        return f"重新装填：{len(shells)} 发弹药，其中 {live} 发实弹、{blank} 发空包弹。"

    def _format_items(self, items: list[str]) -> str:
        if not items:
            return "无"
        counts: dict[str, int] = {}
        for item in items:
            counts[item] = counts.get(item, 0) + 1
        return "、".join(
            f"{ITEM_NAMES.get(item, item)}x{count}" if count > 1 else ITEM_NAMES.get(item, item)
            for item, count in counts.items()
        )

    def _alive_players(self, game: dict[str, Any]) -> list[int]:
        return [uid for uid in game["order"] if game["players"][uid]["hp"] > 0]

    def _next_turn(self, game: dict[str, Any], current: int):
        alive = self._alive_players(game)
        if len(alive) <= 1:
            return

        current_idx = alive.index(current) if current in alive else -1
        next_uid = alive[(current_idx + 1) % len(alive)]
        skipped = None

        if game.get("skip_user") == next_uid:
            skipped = next_uid
            game["skip_user"] = None
            if current in alive and len(alive) == 2:
                next_uid = current
            else:
                skipped_idx = alive.index(skipped)
                for offset in range(1, len(alive) + 1):
                    candidate = alive[(skipped_idx + offset) % len(alive)]
                    if candidate != skipped:
                        next_uid = candidate
                        break

        game["last_skip"] = skipped
        if next_uid == current and skipped is None and len(alive) > 1:
            next_uid = alive[(current_idx + 1) % len(alive)]
        if next_uid not in alive:
            next_uid = alive[0]
        game["turn"] = next_uid

    def _turn_summary(self, game: dict[str, Any]) -> str:
        alive = self._alive_players(game)
        hp_line = "；".join(
            f"{game['players'][uid]['name']} HP {game['players'][uid]['hp']}/{game['players'][uid]['max_hp']}"
            for uid in game["order"]
        )
        return (
            "回合摘要：\n"
            f"- 存活：{len(alive)}/{len(game['players'])}\n"
            f"- 弹仓：剩余 {len(game['shells'])} 发\n"
            f"- 血量：{hp_line}\n"
            f"- 当前回合：{game['players'][game['turn']]['name']}"
        )

    def _cleanup_game(self, group_id: int):
        task = self.timeout_tasks.pop(group_id, None)
        if task and not task.done():
            task.cancel()
        self.games.pop(group_id, None)

    async def _start_timeout(self, event: AstrMessageEvent, group_id: int):
        self._refresh_settings()
        old_task = self.timeout_tasks.get(group_id)
        if old_task and not old_task.done():
            old_task.cancel()
        bot = event.bot

        async def timeout_check():
            try:
                await asyncio.sleep(self.timeout)
                if group_id in self.games:
                    self.games.pop(group_id, None)
                    if hasattr(bot, "send_group_msg"):
                        await bot.send_group_msg(
                            group_id=group_id,
                            message=f"时间耗尽，房间已自动解散。\n{self.timeout} 秒无人操作。",
                        )
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"超时检查失败: {e}")

        self.timeout_tasks[group_id] = asyncio.create_task(timeout_check())

    async def _start_game(self, event: AstrMessageEvent, group_id: int) -> list[str]:
        self._refresh_settings()
        if group_id in self.games:
            return ["当前群已有一局正在进行。使用 /轮盘 状态 查看，或管理员使用 /轮盘 结束。"]

        starter_id = int(event.get_sender_id())
        player_ids = [starter_id]
        for uid in self._extract_at_user_ids(event):
            if uid != starter_id and uid not in player_ids:
                player_ids.append(uid)

        if len(player_ids) < 2:
            return ["至少需要 @1 名玩家：/轮盘 开始 @对手。@几个人，房间就有 发起人+被@的人。"]
        if len(player_ids) > 8:
            return ["人数太多了，当前最多支持 8 人一局。"]

        game_hp = random.randint(self.min_hp, self.max_hp)
        players: dict[int, dict[str, Any]] = {}
        for uid in player_ids:
            players[uid] = {
                "name": await self._get_member_name(event, group_id, uid),
                "hp": game_hp,
                "max_hp": game_hp,
                "items": [],
                "saw_active": False,
            }

        random.shuffle(player_ids)
        game = {
            "players": players,
            "order": player_ids,
            "turn": player_ids[0],
            "shells": self._make_shells(len(player_ids)),
            "known_current": None,
            "skip_user": None,
            "last_skip": None,
            "started_at": datetime.datetime.now(),
        }
        self.games[group_id] = game
        await self._start_timeout(event, group_id)
        lines = [
            f"多人轮盘开局，{len(player_ids)} 人入场。",
            "玩家：" + "、".join(players[uid]["name"] for uid in player_ids),
            "血量：" + "；".join(
                f"{players[uid]['name']} HP {players[uid]['hp']}/{players[uid]['max_hp']}"
                for uid in player_ids
            ),
            self._shell_summary(game["shells"]),
        ]
        lines.extend(self._deal_items(game))
        lines.append(f"先手：{players[game['turn']]['name']}")
        lines.append(
            "行动：\n"
            "- /轮盘 自己：朝自己开枪\n"
            "- /轮盘 对方 @玩家：朝目标开枪，2 人局可省略 @\n"
            "- /轮盘 使用 道具名 [@玩家]：使用道具\n"
            "- /轮盘 帮助：查看完整规则"
        )
        return ["\n".join(lines)]

    def _status(self, group_id: int) -> str:
        game = self.games.get(group_id)
        if not game:
            return "当前没有进行中的房间。使用 /轮盘 开始 @玩家 创建。"
        lines = ["多人轮盘状态"]
        lines.append(f"当前回合：{game['players'][game['turn']]['name']}")
        lines.append(f"弹仓：剩余 {len(game['shells'])} 发")
        if game.get("known_current"):
            lines.append(f"已知首发：{self._shell_name(game['known_current'])}")
        for uid in game["order"]:
            player = game["players"][uid]
            state = "存活" if player["hp"] > 0 else "出局"
            marker = " <-" if uid == game["turn"] else ""
            lines.append(
                f"{player['name']}：HP {player['hp']}/{player['max_hp']}，{state}，道具 {self._format_items(player['items'])}{marker}"
            )
        return "\n".join(lines)

    async def _finish_if_needed(self, group_id: int, game: dict[str, Any]) -> list[str]:
        alive = self._alive_players(game)
        if len(alive) > 1:
            return []
        if not alive:
            self._cleanup_game(group_id)
            return ["所有人都倒下了，没有赢家。"]
        winner = alive[0]
        for uid, player in game["players"].items():
            record = self._record_for(uid)
            if uid == winner:
                record["wins"] += 1
            else:
                record["losses"] += 1
        self._save_records()
        winner_name = game["players"][winner]["name"]
        self._cleanup_game(group_id)
        return [f"游戏结束，{winner_name} 活到了最后。"]

    async def _shoot(self, event: AstrMessageEvent, group_id: int, target_self: bool) -> list[str]:
        game = self.games.get(group_id)
        if not game:
            return ["当前没有进行中的房间。"]

        shooter = int(event.get_sender_id())
        if shooter not in game["players"] or game["players"][shooter]["hp"] <= 0:
            return ["你不在本局，或已经出局。"]
        if shooter != game["turn"]:
            return [f"现在是 {game['players'][game['turn']]['name']} 的回合。"]

        target = shooter if target_self else self._extract_target_from_message(event, game)
        if target is None:
            return ["多人局打对方需要 @ 一个仍存活的目标。"]
        if target not in self._alive_players(game):
            return ["目标不在本局，或已经出局。"]

        if not game["shells"]:
            reload_lines = self._reload_shells(game)
        else:
            reload_lines = []

        shell = game["shells"].pop(0)
        game["known_current"] = None
        shooter_player = game["players"][shooter]
        target_player = game["players"][target]
        record = self._record_for(shooter)
        record["shots"] += 1

        lines = reload_lines
        target_word = "自己" if target == shooter else target_player["name"]
        lines.append(f"{shooter_player['name']} 朝 {target_word} 扣下扳机。")

        if shell == LIVE:
            saw_active = bool(shooter_player.get("saw_active", False))
            damage = 2 if saw_active else 1
            shooter_player["saw_active"] = False
            target_player["hp"] = max(0, target_player["hp"] - damage)
            record["hits"] += 1
            lines.append(f"实弹。{target_player['name']} 受到 {damage} 点伤害，剩余 HP {target_player['hp']}。")
            if target_player["hp"] <= 0:
                lines.append(f"{target_player['name']} 出局。")
            self._next_turn(game, shooter)
        else:
            lines.append("空包弹。")
            if target == shooter:
                lines.append("空包打自己，继续当前回合。")
            else:
                self._next_turn(game, shooter)

        if game.get("last_skip"):
            skipped = game["players"][game["last_skip"]]["name"]
            lines.append(f"{skipped} 被手铐限制，跳过一回合。")
            game["last_skip"] = None

        if not game["shells"] and len(self._alive_players(game)) > 1:
            lines.extend(self._reload_shells(game))

        lines.extend(await self._finish_if_needed(group_id, game))
        if group_id in self.games:
            await self._start_timeout(event, group_id)
            lines.append(self._turn_summary(game))
        self._save_records()
        return ["\n".join(lines)]

    async def _use_item(self, event: AstrMessageEvent, group_id: int, item_text: str) -> list[str]:
        game = self.games.get(group_id)
        if not game:
            return ["当前没有进行中的房间。"]

        user_id = int(event.get_sender_id())
        if user_id != game["turn"]:
            return [f"现在是 {game['players'][game['turn']]['name']} 的回合。"]
        player = game["players"].get(user_id)
        if not player or player["hp"] <= 0:
            return ["你不在本局，或已经出局。"]

        item_key = ITEM_ALIASES.get(item_text.strip())
        if not item_key:
            return ["未知道具。可用：放大镜、啤酒、手铐、香烟、锯子、逆转器、手机、过期药、注射器。"]
        if item_key not in player["items"]:
            return [f"你没有 {ITEM_NAMES[item_key]}。"]
        player["items"].remove(item_key)

        lines = [f"{player['name']} 使用了 {ITEM_NAMES[item_key]}。"]

        if not game["shells"]:
            lines.extend(self._reload_shells(game))

        if item_key == "magnifier":
            game["known_current"] = game["shells"][0]
            lines.append(f"你看到了当前第一发：{self._shell_name(game['shells'][0])}。")
        elif item_key == "beer":
            shell = game["shells"].pop(0)
            game["known_current"] = None
            lines.append(f"退掉了一发：{self._shell_name(shell)}。")
            if not game["shells"]:
                lines.extend(self._reload_shells(game))
        elif item_key == "cuffs":
            opponents = [uid for uid in self._alive_players(game) if uid != user_id]
            target = self._extract_target_from_message(event, game)
            if target is None and len(opponents) == 1:
                target = opponents[0]
            if target is None:
                self._return_item(player, item_key)
                return ["手铐需要 @ 一个仍存活的目标。"]
            game["skip_user"] = target
            lines.append(f"{game['players'][target]['name']} 被铐住了，下一次本该行动时会被跳过。")
        elif item_key == "cigarette":
            before = player["hp"]
            player["hp"] = min(player["max_hp"], player["hp"] + 1)
            lines.append(f"恢复 {player['hp'] - before} 点 HP，当前 HP {player['hp']}。")
        elif item_key == "saw":
            if player.get("saw_active"):
                lines.append("锯子效果已经生效中，下一发实弹会造成 2 点伤害。")
            else:
                player["saw_active"] = True
                lines.append("下一发实弹伤害 +1。空包不会消耗锯子效果。")
        elif item_key == "inverter":
            game["shells"][0] = BLANK if game["shells"][0] == LIVE else LIVE
            game["known_current"] = None
            lines.append("当前第一发已经被逆转。")
        elif item_key == "phone":
            idx = random.randrange(len(game["shells"]))
            lines.append(f"手机显示：第 {idx + 1} 发是 {self._shell_name(game['shells'][idx])}。")
        elif item_key == "syringe":
            targets = [
                uid
                for uid in self._alive_players(game)
                if uid != user_id and game["players"][uid]["items"]
            ]
            target = self._extract_target_from_message(event, game)
            if target is not None and (
                target == user_id
                or target not in self._alive_players(game)
                or not game["players"][target]["items"]
            ):
                self._return_item(player, item_key)
                return ["注射器目标必须是一个仍存活且拥有道具的玩家。"]
            if target is None:
                if len(targets) == 1:
                    target = targets[0]
                elif not targets:
                    self._return_item(player, item_key)
                    return ["没有可抢夺的目标：其他存活玩家都没有道具。"]
                else:
                    self._return_item(player, item_key)
                    return ["多人局使用注射器需要 @ 一个拥有道具的目标。"]
            stolen = random.choice(game["players"][target]["items"])
            game["players"][target]["items"].remove(stolen)
            if self._add_item(player, stolen):
                lines.append(
                    f"从 {game['players'][target]['name']} 那里抢到了 {ITEM_NAMES.get(stolen, stolen)}。"
                )
            else:
                game["players"][target]["items"].append(stolen)
                lines.append(f"你的道具已达上限 {MAX_ITEMS_PER_PLAYER}，注射器没有抢到道具。")
        elif item_key == "expired_medicine":
            if random.random() < 0.5:
                before = player["hp"]
                player["hp"] = min(player["max_hp"], player["hp"] + 2)
                lines.append(f"药效不错，恢复 {player['hp'] - before} 点 HP。")
            else:
                player["hp"] = max(0, player["hp"] - 1)
                lines.append(f"药坏了，损失 1 点 HP，当前 HP {player['hp']}。")
                if player["hp"] <= 0:
                    lines.append(f"{player['name']} 出局。")
                    self._next_turn(game, user_id)

        lines.extend(await self._finish_if_needed(group_id, game))
        if group_id in self.games:
            await self._start_timeout(event, group_id)
            lines.append(self._turn_summary(game))
        return ["\n".join(lines)]

    def _help_text(self) -> str:
        return f"""多人轮盘 v{self.plugin_version}

/轮盘 开始 @玩家1 @玩家2 - 创建房间，发起人也会入场
/轮盘 自己 - 朝自己开枪
/轮盘 对方 @玩家 - 朝目标开枪，2人局可省略@
/轮盘 道具 - 查看自己的道具
/轮盘 使用 道具名 [@目标] - 使用道具
/轮盘 状态 - 查看房间状态
/轮盘 战绩 - 查看个人战绩
/轮盘 排行榜 - 查看胜场排行
/轮盘 结束 - 解散当前房间
/轮盘 帮助 - 查看本说明

行动：
- 自己：消耗当前第一发；实弹伤害自己并切换回合，空包无伤并继续当前回合
- 对方：消耗当前第一发，实弹伤害目标，空包无伤
- 使用：道具不消耗子弹，使用后会显示当前回合摘要

道具：
- 放大镜：查看当前第一发
- 啤酒：退掉当前第一发并公开弹种
- 手铐：跳过目标下一次回合
- 香烟：恢复 1 点 HP，不超过自身上限
- 锯子：下一发实弹伤害 +1，空包不消耗效果
- 逆转器：反转当前第一发实弹/空包，不显示结果
- 手机：查看随机位置的一发弹药
- 过期药：50% 回血，50% 扣血
- 注射器：抢夺目标随机一个道具

规则：实弹造成伤害，空包不伤人；空包打自己会继续当前回合，其余射击结算后切换到下一名存活玩家。弹仓打空会重新随机装填并发放道具。每名玩家最多持有 8 个道具。"""

    def _personal_records(self, user_id: int, user_name: str) -> str:
        r = self._record_for(user_id)
        shots = r["shots"]
        hit_rate = f"{r['hits'] / shots:.0%}" if shots else "0%"
        return (
            f"{user_name} 的战绩\n"
            f"胜场：{r['wins']}，败场：{r['losses']}\n"
            f"开枪：{r['shots']}，命中：{r['hits']}，命中率：{hit_rate}"
        )

    def _leaderboard(self) -> str:
        rows = sorted(
            self.records.items(),
            key=lambda kv: (kv[1].get("wins", 0), kv[1].get("hits", 0)),
            reverse=True,
        )[:10]
        if not rows:
            return "暂无战绩。"
        lines = ["多人轮盘排行榜"]
        for i, (uid, r) in enumerate(rows, start=1):
            lines.append(f"{i}. {uid}：{r.get('wins', 0)} 胜 / {r.get('losses', 0)} 败 / {r.get('hits', 0)} 命中")
        return "\n".join(lines)

    def _config_status(self) -> str:
        self._refresh_settings()
        return (
            "当前轮盘配置：\n"
            f"- 超时：{self.timeout} 秒\n"
            f"- 初始 HP：{self.min_hp}-{self.max_hp}\n"
            f"- 装弹数量：{self.min_shells}-{self.max_shells}\n"
            f"- 保底实弹：{self.min_live_shells}\n"
            f"- 每轮道具：{self.items_per_round}\n"
            f"- AI 触发：{'开启' if self.allow_ai_start else '关闭'}\n"
            f"- AI 延迟：{self.ai_trigger_delay} 秒\n"
            f"- 配置来源：{self._active_config_file()}"
        )

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        try:
            if event.is_admin():
                return True
            group_id = self._get_group_id(event)
            if not group_id or not hasattr(event.bot, "get_group_member_info"):
                return False
            user_id = int(event.get_sender_id())
            info = await event.bot.get_group_member_info(group_id=group_id, user_id=user_id, no_cache=True)
            role = info.get("role", "") if isinstance(info, dict) else getattr(info, "role", "")
            return role in ("owner", "admin")
        except Exception:
            return False

    @filter.command_group("轮盘")
    def roulette_group(self):
        pass

    async def _cmd_start(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        for msg in await self._start_game(event, group_id):
            yield event.plain_result(msg)

    async def _cmd_shoot_self(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        for msg in await self._shoot(event, group_id, target_self=True):
            yield event.plain_result(msg)

    async def _cmd_shoot_other(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        for msg in await self._shoot(event, group_id, target_self=False):
            yield event.plain_result(msg)

    async def _cmd_items(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        game = self.games.get(group_id) if group_id else None
        user_id = int(event.get_sender_id())
        if not game or user_id not in game["players"]:
            yield event.plain_result("你不在进行中的房间里。")
            return
        player = game["players"][user_id]
        yield event.plain_result(f"{player['name']} 的道具：{self._format_items(player['items'])}")

    async def _cmd_use_item(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        item_text = self._extract_item_name(event)
        if not item_text:
            yield event.plain_result("用法：/轮盘 使用 道具名 [@目标]")
            return
        for msg in await self._use_item(event, group_id, item_text):
            yield event.plain_result(msg)

    async def _cmd_status(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        yield event.plain_result(self._status(group_id))

    async def _cmd_records(self, event: AstrMessageEvent):
        yield event.plain_result(
            self._personal_records(int(event.get_sender_id()), self._get_user_name(event))
        )

    async def _cmd_leaderboard(self, event: AstrMessageEvent):
        yield event.plain_result(self._leaderboard())

    async def _cmd_config(self, event: AstrMessageEvent):
        yield event.plain_result(self._config_status())

    async def _cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    async def _cmd_end(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("仅限群聊使用。")
            return
        game = self.games.get(group_id)
        if not game:
            yield event.plain_result("当前没有进行中的房间。")
            return
        if int(event.get_sender_id()) not in game["players"] and not await self._is_group_admin(event):
            yield event.plain_result("只有本局玩家或群管理员可以结束房间。")
            return
        self._cleanup_game(group_id)
        yield event.plain_result("房间已解散。")

    @roulette_group.command("开始")
    async def roulette_start(self, event: AstrMessageEvent):
        async for result in self._cmd_start(event):
            yield result

    @roulette_group.command("自己")
    async def roulette_shoot_self(self, event: AstrMessageEvent):
        async for result in self._cmd_shoot_self(event):
            yield result

    @roulette_group.command("对方")
    async def roulette_shoot_other(self, event: AstrMessageEvent):
        async for result in self._cmd_shoot_other(event):
            yield result

    @roulette_group.command("道具")
    async def roulette_items(self, event: AstrMessageEvent):
        async for result in self._cmd_items(event):
            yield result

    @roulette_group.command("使用")
    async def roulette_use_item(self, event: AstrMessageEvent):
        async for result in self._cmd_use_item(event):
            yield result

    @roulette_group.command("状态")
    async def roulette_status(self, event: AstrMessageEvent):
        async for result in self._cmd_status(event):
            yield result

    @roulette_group.command("战绩")
    async def roulette_records(self, event: AstrMessageEvent):
        async for result in self._cmd_records(event):
            yield result

    @roulette_group.command("排行榜")
    async def roulette_leaderboard(self, event: AstrMessageEvent):
        async for result in self._cmd_leaderboard(event):
            yield result

    @roulette_group.command("配置")
    async def roulette_config(self, event: AstrMessageEvent):
        async for result in self._cmd_config(event):
            yield result

    @roulette_group.command("帮助")
    async def roulette_help(self, event: AstrMessageEvent):
        async for result in self._cmd_help(event):
            yield result

    @roulette_group.command("help")
    async def roulette_help_en(self, event: AstrMessageEvent):
        async for result in self._cmd_help(event):
            yield result

    @roulette_group.command("?")
    async def roulette_help_symbol(self, event: AstrMessageEvent):
        async for result in self._cmd_help(event):
            yield result

    @roulette_group.command("结束")
    async def roulette_end(self, event: AstrMessageEvent):
        async for result in self._cmd_end(event):
            yield result

    def _register_ai_trigger(self, action: str, event: AstrMessageEvent) -> str:
        self.ai_trigger_counter += 1
        unique_id = f"trigger_{self.ai_trigger_counter}_{event.get_sender_id()}"
        self.ai_trigger_queue[unique_id] = {
            "action": action,
            "event": event,
            "timestamp": datetime.datetime.now(),
        }
        return unique_id

    async def _execute_ai_trigger(self, unique_id: str):
        if unique_id not in self.ai_trigger_queue:
            return
        data = self.ai_trigger_queue.pop(unique_id)
        action = data["action"]
        event = data["event"]
        if action == "start":
            await self.ai_start_game(event)
        elif action == "self":
            await self.ai_shoot_self(event)
        elif action == "target":
            await self.ai_shoot_target(event)
        elif action == "status":
            await self.ai_check_status(event)

    @filter.after_message_sent(priority=10)
    async def _on_message_sent(self, event: AstrMessageEvent):
        try:
            self._refresh_settings()
            if self.ai_trigger_queue:
                oldest_id = min(
                    self.ai_trigger_queue.keys(),
                    key=lambda k: self.ai_trigger_queue[k]["timestamp"],
                )
                await asyncio.sleep(self.ai_trigger_delay)
                await self._execute_ai_trigger(oldest_id)
        except Exception as e:
            logger.error(f"AI 触发器执行失败: {e}")

    async def ai_start_game(self, event: AstrMessageEvent, bullets: int | None = None):
        self._refresh_settings()
        if not self.allow_ai_start:
            return
        group_id = self._get_group_id(event)
        if not group_id:
            return
        for msg in await self._start_game(event, group_id):
            await event.bot.send_group_msg(group_id=group_id, message=msg)

    async def ai_shoot_self(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        for msg in await self._shoot(event, group_id, target_self=True):
            await event.bot.send_group_msg(group_id=group_id, message=msg)

    async def ai_shoot_target(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        for msg in await self._shoot(event, group_id, target_self=False):
            await event.bot.send_group_msg(group_id=group_id, message=msg)

    async def ai_join_game(self, event: AstrMessageEvent):
        await self.ai_shoot_target(event)

    async def ai_check_status(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            return
        await event.bot.send_group_msg(group_id=group_id, message=self._status(group_id))

    @filter.event_message_type(
        EventMessageType.GROUP_MESSAGE if EventMessageType else "group"
    )
    async def on_group_message(self, event: AstrMessageEvent):
        return

    async def terminate(self):
        try:
            for task in self.timeout_tasks.values():
                if not task.done():
                    task.cancel()
            self.games.clear()
            self.timeout_tasks.clear()
            self.ai_trigger_queue.clear()
            self._save_records()
            logger.info(f"多人霰弹轮盘插件 v{self.plugin_version} 已卸载")
        except Exception as e:
            logger.error(f"插件卸载失败: {e}")
