# main.py
import asyncio
import datetime
import json
import re
from pathlib import Path

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import (
    get_astrbot_data_path,
    get_astrbot_plugin_data_path,
)

try:
    from chinese_calendar import get_holiday_detail

    HAS_CHINESE_CALENDAR = True
except ImportError:
    HAS_CHINESE_CALENDAR = False
    logger.warning("未安装 chinese-calendar，本地缓存缺失时将仅按周末判断")

DEFAULT_CDN_BASE = "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master"
DEFAULT_FESTIVAL_ICS_URL = "https://yangh9.github.io/ChinaCalendar/cal_festival.ics"
DEFAULT_PLUGIN_NAME = "astrbot_plugin_calendar"

# chinese-calendar 的节日名为英文，这里映射为中文；未收录的名称原样返回
HOLIDAY_NAME_ZH: dict[str, str] = {
    "New Year's Day": "元旦",
    "Spring Festival": "春节",
    "Tomb-sweeping Day": "清明节",
    "Labour Day": "劳动节",
    "Dragon Boat Festival": "端午节",
    "Mid-autumn Festival": "中秋节",
    "National Day": "国庆节",
    "Anti-Fascist 70th Day": "抗战胜利70周年",
}

WEEKDAY_MAP = ["一", "二", "三", "四", "五", "六", "日"]


class HolidayPlugin(Star):
    """节假日、调休与工作日查询插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.cdn_base = str(self.config.get("cdn_base") or DEFAULT_CDN_BASE).rstrip("/")
        self.festival_ics_url = str(
            self.config.get("festival_ics_url") or DEFAULT_FESTIVAL_ICS_URL
        ).strip()
        self.cache_dir = self._resolve_cache_dir()
        Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
        self._initial_task: asyncio.Task | None = None
        self._monthly_task: asyncio.Task | None = None
        self._festivals: dict[datetime.date, list[str]] | None = None
        self._festival_years: tuple[int, int] | None = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "实例化时没有运行中的事件循环，后台任务将在 initialize() 启动"
            )
        else:
            self._start_background_tasks()

    # ---------- 生命周期 ----------
    def _start_background_tasks(self) -> None:
        """启动后台任务（保存引用，避免任务被垃圾回收）。"""
        if self._initial_task is None:
            self._initial_task = asyncio.create_task(self._initial_check())
        if self._monthly_task is None and self.config.get("auto_update", True):
            self._monthly_task = asyncio.create_task(self._monthly_check_loop())

    async def initialize(self) -> None:
        """AstrBot 载入插件完成后调用；幂等，仅在任务尚未启动时补启。"""
        self._start_background_tasks()

    async def terminate(self) -> None:
        """插件被卸载/停用时由 AstrBot 调用，用于清理后台任务。"""
        for task in (self._initial_task, self._monthly_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._initial_task = None
        self._monthly_task = None
        logger.info("日历插件已停止，后台任务已清理")

    # ---------- 路径与配置 ----------
    def _plugin_name(self) -> str:
        name = getattr(type(self), "name", None)  # AstrBot 载入时注入
        return name if isinstance(name, str) and name else DEFAULT_PLUGIN_NAME

    def _resolve_cache_dir(self) -> str:
        custom = str(self.config.get("cache_dir") or "").strip()
        if custom:
            path = Path(custom)
            if not path.is_absolute():
                # 相对路径统一落在 AstrBot 的 data 目录下，遵守存储规范
                path = Path(get_astrbot_data_path()) / custom
            return str(path)
        return str(Path(get_astrbot_plugin_data_path()) / self._plugin_name())

    def _config_int(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    def _guard_enabled(self, event: AstrMessageEvent) -> bool:
        """配置 enable=false 时静默忽略指令。"""
        if self.config.get("enable", True):
            return True
        event.stop_event()
        return False

    # ---------- 缓存 ----------
    def _cache_path(self, year: int) -> Path:
        return Path(self.cache_dir) / f"{year}.json"

    def _etag_path(self, year: int) -> Path:
        return Path(self.cache_dir) / f"{year}.json.etag"

    def _load_local_data(self, year: int) -> dict | None:
        path = self._cache_path(year)
        if not path.exists():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"读取本地缓存失败 {path}: {e}")
            return None
        return data if isinstance(data, dict) else None

    def _save_local_data(self, year: int, data: dict) -> None:
        path = self._cache_path(year)
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info(f"已保存 {year} 年节假日数据到 {path}")
        except OSError as e:
            logger.error(f"保存本地缓存失败 {path}: {e}")

    def _load_etag(self, year: int) -> str | None:
        path = self._etag_path(year)
        try:
            etag = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return etag or None

    def _save_etag(self, year: int, etag: str | None) -> None:
        if not etag:
            return
        try:
            self._etag_path(year).write_text(etag, encoding="utf-8")
        except OSError as e:
            logger.warning(f"保存 ETag 失败: {e}")

    # ---------- 传统节日（ChinaCalendar iCal） ----------
    def _festival_ics_path(self) -> Path:
        return Path(self.cache_dir) / "cal_festival.ics"

    def _festival_etag_path(self) -> Path:
        return Path(self.cache_dir) / "cal_festival.ics.etag"

    def _load_festival_ics(self) -> str | None:
        path = self._festival_ics_path()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"读取传统节日日历失败 {path}: {e}")
            return None

    def _save_festival_ics(self, text: str) -> bool:
        path = self._festival_ics_path()
        try:
            path.write_text(text, encoding="utf-8")
            logger.info(f"已保存传统节日日历到 {path}")
            return True
        except OSError as e:
            logger.error(f"保存传统节日日历失败 {path}: {e}")
            return False

    def _load_festival_etag(self) -> str | None:
        try:
            etag = self._festival_etag_path().read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return etag or None

    def _save_festival_etag(self, etag: str | None) -> None:
        if not etag:
            return
        try:
            self._festival_etag_path().write_text(etag, encoding="utf-8")
        except OSError as e:
            logger.warning(f"保存 ETag 失败: {e}")

    # ---------- 网络 ----------
    async def _fetch_year(
        self, year: int, etag: str | None = None
    ) -> tuple[str, dict | None, str | None]:
        """请求某年数据。

        返回 (状态, 数据, ETag)，状态取值 updated / not-modified / failed。
        携带 If-None-Match 时，数据未变化会得到 HTTP 304，不下载正文。
        """
        url = f"{self.cdn_base}/{year}.json"
        headers = {"If-None-Match": etag} if etag else {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.error(f"请求 {url} 失败: {e}")
            return "failed", None, None
        if resp.status_code == 304:
            return "not-modified", None, etag
        if resp.status_code != 200:
            logger.warning(f"获取 {year} 年数据失败，状态码 {resp.status_code}")
            return "failed", None, None
        try:
            data = resp.json()
        except ValueError as e:
            logger.error(f"{year} 年数据解析失败: {e}")
            return "failed", None, None
        if not isinstance(data, dict) or not isinstance(data.get("days"), list):
            logger.error(f"{year} 年数据格式异常，已忽略")
            return "failed", None, None
        return "updated", data, resp.headers.get("etag")

    async def _fetch_festival_ics(
        self, etag: str | None = None
    ) -> tuple[str, str | None, str | None]:
        """请求传统节日 iCal 日历。

        返回 (状态, 文本, ETag)，状态取值 updated / not-modified / failed。
        """
        headers = {"If-None-Match": etag} if etag else {}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.festival_ics_url, headers=headers)
        except httpx.HTTPError as e:
            logger.error(f"请求 {self.festival_ics_url} 失败: {e}")
            return "failed", None, None
        if resp.status_code == 304:
            return "not-modified", None, etag
        if resp.status_code != 200:
            logger.warning(f"获取传统节日日历失败，状态码 {resp.status_code}")
            return "failed", None, None
        text = resp.text
        if "BEGIN:VCALENDAR" not in text or "BEGIN:VEVENT" not in text:
            logger.error("传统节日日历格式异常，已忽略")
            return "failed", None, None
        return "updated", text, resp.headers.get("etag")

    # ---------- 更新 ----------
    async def _initial_check(self) -> None:
        year_now = datetime.datetime.now().astimezone().year
        for year in (year_now, year_now + 1):
            if self._load_local_data(year) is not None:
                continue
            logger.info(f"本地缺少 {year} 年数据，正在下载...")
            status, data, etag = await self._fetch_year(year)
            if status == "updated" and data is not None:
                self._save_local_data(year, data)
                self._save_etag(year, etag)
        await self._update_festival_ics()

    async def _check_and_update(self) -> None:
        """按月检查更新：带 ETag 的条件请求，未变更时仅消耗一次 304。"""
        year_now = datetime.datetime.now().astimezone().year
        for year in (year_now, year_now + 1):
            if self._load_local_data(year) is None:
                status, data, etag = await self._fetch_year(year)
                if status == "updated" and data is not None:
                    self._save_local_data(year, data)
                    self._save_etag(year, etag)
                continue
            status, data, etag = await self._fetch_year(year, self._load_etag(year))
            if status == "updated" and data is not None:
                logger.info(f"{year} 年节假日数据有更新，正在保存...")
                self._save_local_data(year, data)
                self._save_etag(year, etag)
            elif status == "not-modified":
                logger.debug(f"{year} 年节假日数据无变化（HTTP 304）")
        await self._update_festival_ics()

    async def _update_festival_ics(self) -> None:
        """下载/更新传统节日 iCal（带 ETag 条件请求，未变更时不下载正文）。"""
        if not self.config.get("festival_enable", True):
            return
        has_local = self._festival_ics_path().exists()
        etag = self._load_festival_etag() if has_local else None
        status, text, new_etag = await self._fetch_festival_ics(etag)
        if status == "updated" and text is not None:
            if self._save_festival_ics(text):
                self._save_festival_etag(new_etag)
                self._festivals = None  # 触发重新解析
        elif status == "not-modified":
            logger.debug("传统节日日历无变化（HTTP 304）")

    @staticmethod
    def _next_run(now: datetime.datetime, day: int, hour: int) -> datetime.datetime:
        """计算下一次检查时间：每月 day 日 hour 点（本地时区）。"""
        candidate = now.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
        if now.month == 12:
            year, month = now.year + 1, 1
        else:
            year, month = now.year, now.month + 1
        return datetime.datetime(year, month, day, hour, 0, 0, tzinfo=now.tzinfo)

    async def _monthly_check_loop(self) -> None:
        day = self._config_int("update_day", 1, 1, 28)
        hour = self._config_int("update_hour", 3, 0, 23)
        while True:
            now = datetime.datetime.now().astimezone()
            next_run = self._next_run(now, day, hour)
            logger.info(f"下次节假日数据检查：{next_run:%Y-%m-%d %H:%M:%S}")
            await asyncio.sleep((next_run - now).total_seconds())
            try:
                await self._check_and_update()
            except Exception as e:  # noqa: BLE001 - 保底，不让后台循环因单次错误退出
                logger.error(f"节假日数据更新出错: {e}")

    # ---------- 查询 ----------
    def _get_day_status(
        self, target_date: datetime.date
    ) -> tuple[bool, str | None, bool]:
        """返回 (是否休息, 节日名称, 是否调休补班)。"""
        data = self._load_local_data(target_date.year)
        if data is not None:
            date_str = target_date.isoformat()
            for day in data.get("days", []):
                if day.get("date") == date_str:
                    is_off = bool(day.get("isOffDay", False))
                    name = day.get("name")
                    name = name if isinstance(name, str) and name else None
                    is_makeup = (not is_off) and name is not None
                    return is_off, name, is_makeup
            return target_date.weekday() >= 5, None, False
        return self._offline_status(target_date)

    def _offline_status(
        self, target_date: datetime.date
    ) -> tuple[bool, str | None, bool]:
        """本地缓存缺失时的回退：优先 chinese-calendar，否则按周末判断。"""
        weekend_only = (target_date.weekday() >= 5, None, False)
        if not (HAS_CHINESE_CALENDAR and self.config.get("fallback_enable", True)):
            return weekend_only
        try:
            on_holiday, holiday_name = get_holiday_detail(target_date)
        except (NotImplementedError, KeyError, ValueError, OSError) as e:
            logger.warning(f"离线数据无法回答 {target_date}，回退为周末判断: {e}")
            return weekend_only
        name = self._zh_holiday_name(holiday_name)
        if on_holiday:
            return True, name, False
        if name:
            return False, name, True
        return False, None, False

    @staticmethod
    def _zh_holiday_name(name: object) -> str | None:
        if not isinstance(name, str) or not name:
            return None
        return HOLIDAY_NAME_ZH.get(name, name)

    def _build_status_text(self, target_date: datetime.date) -> str:
        is_off, name, is_makeup = self._get_day_status(target_date)
        weekday_str = WEEKDAY_MAP[target_date.weekday()]
        date_str = target_date.strftime("%Y-%m-%d")

        if is_off:
            if name:
                status = f"🎉 法定节假日：{name}"
            else:
                status = "🛋️ 周末休息日"
        else:
            if is_makeup:
                status = f"💼 调休补班日（{name}）"
            else:
                status = "💼 工作日"

        # 与法定节日同名的传统节日不重复展示（如春节、中秋）
        festival_names = self._festival_names_on(target_date, exclude=name)
        festival_line = ""
        if festival_names:
            festival_line = f"├ 传统节日：{'、'.join(festival_names)}\n"

        return (
            f"📅 {date_str}（星期{weekday_str}）\n"
            f"├ 状态：{status}\n"
            f"├ 是否休息：{'是 ✅' if is_off else '否 ❌'}\n"
            f"{festival_line}"
            f"└ 是否调休：{'是 🔄' if is_makeup else '否'}"
        )

    def _build_month_text(self, year: int, month: int) -> str:
        lines = [f"📆 **{year}年{month}月 节日/调休概览**\n"]
        day = datetime.date(year, month, 1)
        found = False
        while day.month == month:
            is_off, name, is_makeup = self._get_day_status(day)
            if is_off and name:
                lines.append(
                    f"• {day.strftime('%m-%d')}（星期{WEEKDAY_MAP[day.weekday()]}）"
                    f"：{name} 🎉"
                )
                found = True
            elif is_makeup:
                lines.append(
                    f"• {day.strftime('%m-%d')}（星期{WEEKDAY_MAP[day.weekday()]}）"
                    f"：调休补班（{name}）🔄"
                )
                found = True
            day += datetime.timedelta(days=1)
        if not found:
            lines.append("本月没有法定节假日或调休安排")
        return "\n".join(lines)

    def _find_next_holiday(
        self, start: datetime.date, max_days: int = 400
    ) -> tuple[datetime.date, str] | None:
        for i in range(1, max_days):
            check = start + datetime.timedelta(days=i)
            is_off, name, _ = self._get_day_status(check)
            if is_off and name:
                return check, name
        return None

    @staticmethod
    def _today() -> datetime.date:
        return datetime.datetime.now().astimezone().date()

    # ---------- 传统节日查询 ----------
    @staticmethod
    def _clean_festival_name(summary: str) -> str:
        """去掉 iCal SUMMARY 的『』包裹，保留附加说明（如数九日期范围）。"""
        return re.sub(r"『(.*?)』", r"\1 ", summary).strip()

    @staticmethod
    def _parse_festival_ics(text: str) -> dict[datetime.date, list[str]]:
        """解析 iCal 文本为 {日期: [节日名]}。

        兼容 RFC 5545 折叠行；DTEND 为排除端（全天事件 DTEND=次日），
        多天事件（数九、三伏）逐日展开。
        """
        lines: list[str] = []
        for raw in text.splitlines():
            if raw[:1] in (" ", "\t") and lines:
                lines[-1] += raw[1:]
            elif raw.strip():
                lines.append(raw)

        def as_date(value: str) -> datetime.date | None:
            m = re.match(r"^(\d{4})(\d{2})(\d{2})", value)
            if not m:
                return None
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None

        festivals: dict[datetime.date, list[str]] = {}
        in_event = False
        start: datetime.date | None = None
        end: datetime.date | None = None
        summary: str | None = None

        for line in lines:
            if line == "BEGIN:VEVENT":
                in_event, start, end, summary = True, None, None, None
                continue
            if line == "END:VEVENT":
                if in_event and start is not None and summary:
                    name = HolidayPlugin._clean_festival_name(summary)
                    stop = (
                        end
                        if end and end > start
                        else start + datetime.timedelta(days=1)
                    )
                    day = start
                    while day < stop:
                        names = festivals.setdefault(day, [])
                        if name and name not in names:
                            names.append(name)
                        day += datetime.timedelta(days=1)
                        if (day - start).days > 62:  # 防御异常的超长事件
                            break
                in_event = False
                continue
            if not in_event or ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.split(";")[0]
            if key == "SUMMARY":
                summary = value.strip()
            elif key == "DTSTART":
                start = as_date(value)
            elif key == "DTEND":
                end = as_date(value)
        return festivals

    def _get_festivals(self) -> dict[datetime.date, list[str]] | None:
        """传统节日数据；未启用或未下载时返回 None。"""
        if not self.config.get("festival_enable", True):
            return None
        if self._festivals is None:
            text = self._load_festival_ics()
            if text is None:
                return None
            self._festivals = self._parse_festival_ics(text)
            years = {d.year for d in self._festivals}
            self._festival_years = (min(years), max(years)) if years else None
        return self._festivals

    def _festival_names_on(
        self, target_date: datetime.date, exclude: str | None = None
    ) -> list[str]:
        data = self._get_festivals()
        if not data:
            return []
        return [n for n in data.get(target_date, []) if n != exclude]

    def _build_festival_month_text(self, year: int, month: int) -> str:
        data = self._get_festivals()
        if data is None:
            return "🧨 传统节日数据尚未下载，请稍后再试"
        if self._festival_years is None:
            return "🧨 传统节日数据格式异常，请稍后重试"
        lo, hi = self._festival_years
        if not lo <= year <= hi:
            return f"🧨 传统节日数据源暂未收录 {year} 年（已收录 {lo}-{hi} 年）"
        lines = [f"🏮 **{year}年{month}月 传统节日与纪念日**\n"]
        found = False
        day = datetime.date(year, month, 1)
        while day.month == month:
            names = data.get(day)
            if names:
                lines.append(
                    f"• {day.strftime('%m-%d')}（星期{WEEKDAY_MAP[day.weekday()]}）"
                    f"：{'、'.join(names)}"
                )
                found = True
            day += datetime.timedelta(days=1)
        if not found:
            lines.append("本月没有传统节日或纪念日")
        return "\n".join(lines)

    # ---------- 指令 ----------
    @filter.command("节日")
    async def today_holiday(self, event: AstrMessageEvent):
        """查询今天是节日/调休/工作日"""
        if not self._guard_enabled(event):
            return
        yield event.plain_result(self._build_status_text(self._today()))

    @filter.command("节日查询")
    async def query_holiday(self, event: AstrMessageEvent):
        """查询指定日期的节日状态，用法：/节日查询 2026-10-01"""
        if not self._guard_enabled(event):
            return
        parts = event.message_str.split()
        if len(parts) < 2:
            yield event.plain_result("❌ 请提供日期，格式：/节日查询 2026-10-01")
            return
        try:
            target = datetime.date.fromisoformat(parts[1])
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，请使用 YYYY-MM-DD 格式")
            return
        yield event.plain_result(self._build_status_text(target))

    @filter.command("下个节日")
    async def next_holiday(self, event: AstrMessageEvent):
        """查询从今天起下一个法定节假日"""
        if not self._guard_enabled(event):
            return
        today = self._today()
        result = self._find_next_holiday(today)
        if result is None:
            yield event.plain_result("未找到未来的法定节假日")
            return
        check, name = result
        days_left = (check - today).days
        yield event.plain_result(
            f"🔜 下一个法定节假日：**{name}**\n"
            f"📅 日期：{check.strftime('%Y-%m-%d')}"
            f"（星期{WEEKDAY_MAP[check.weekday()]}）\n"
            f"⏳ 距今还有 **{days_left}** 天"
        )

    @filter.command("节日列表")
    async def holiday_list(self, event: AstrMessageEvent):
        """查询某月所有节日/调休，用法：/节日列表 2026-10 或 /节日列表（默认当月）"""
        if not self._guard_enabled(event):
            return
        parts = event.message_str.split()
        if len(parts) >= 2:
            matched = re.fullmatch(r"(\d{4})-(\d{1,2})", parts[1])
            if not matched:
                yield event.plain_result("❌ 格式错误，请使用：/节日列表 2026-10")
                return
            year, month = int(matched.group(1)), int(matched.group(2))
            if year < 1 or not 1 <= month <= 12:
                yield event.plain_result(
                    "❌ 格式错误，请使用：/节日列表 2026-10（月份 01-12）"
                )
                return
        else:
            today = self._today()
            year, month = today.year, today.month
        yield event.plain_result(self._build_month_text(year, month))

    # ---------- LLM 工具 ----------
    @filter.command("传统节日")
    async def traditional_festival(self, event: AstrMessageEvent):
        """查询某月传统节日与纪念日，用法：/传统节日 2026-02 或 /传统节日（默认当月）"""
        if not self._guard_enabled(event):
            return
        parts = event.message_str.split()
        if len(parts) >= 2:
            matched = re.fullmatch(r"(\d{4})-(\d{1,2})", parts[1])
            if not matched:
                yield event.plain_result("❌ 格式错误，请使用：/传统节日 2026-02")
                return
            year, month = int(matched.group(1)), int(matched.group(2))
            if year < 1 or not 1 <= month <= 12:
                yield event.plain_result(
                    "❌ 格式错误，请使用：/传统节日 2026-02（月份 01-12）"
                )
                return
        else:
            today = self._today()
            year, month = today.year, today.month
        yield event.plain_result(self._build_festival_month_text(year, month))

    @filter.llm_tool(name="calendar_check_date")
    async def tool_check_date(self, event: AstrMessageEvent, date: str = "") -> str:
        """查询某一天的节日、调休和工作日状态。

        当用户询问"今天是什么节"、"某天是否放假"、"某天要不要上班"、
        "这个日期有没有调休"时调用。

        Args:
            date (string): 要查询的日期，格式 YYYY-MM-DD；留空或传"今天"表示查询当天
        """
        if not self.config.get("enable", True):
            return "插件已停用"
        target = self._today()
        text = (date or "").strip()
        if text and text not in ("今天", "today", "今日"):
            try:
                target = datetime.date.fromisoformat(text)
            except ValueError:
                return f"无法识别的日期：{date}，请使用 YYYY-MM-DD 格式"
        return self._build_status_text(target)

    @filter.llm_tool(name="calendar_next_holiday")
    async def tool_next_holiday(self, event: AstrMessageEvent) -> str:
        """查询从今天起下一个法定节假日及其倒计时。

        当用户询问"下一个节日是什么"、"离放假还有多久"、"什么时候放假"
        时调用。
        """
        if not self.config.get("enable", True):
            return "插件已停用"
        today = self._today()
        result = self._find_next_holiday(today)
        if result is None:
            return "未找到未来的法定节假日"
        check, name = result
        days_left = (check - today).days
        return (
            f"下一个法定节假日：{name}\n"
            f"日期：{check.strftime('%Y-%m-%d')}"
            f"（星期{WEEKDAY_MAP[check.weekday()]}）\n"
            f"距今还有 {days_left} 天"
        )

    @filter.llm_tool(name="calendar_month_list")
    async def tool_month_list(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询某个月的所有节日和调休安排。

        当用户询问"这个月有哪些节日"、"十月放假怎么安排"、"本月调休"
        时调用。

        Args:
            month (string): 要查询的月份，格式 YYYY-MM；留空表示当月
        """
        if not self.config.get("enable", True):
            return "插件已停用"
        text = (month or "").strip()
        if text:
            matched = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
            if not matched:
                return f"无法识别的月份：{month}，请使用 YYYY-MM 格式"
            year, mon = int(matched.group(1)), int(matched.group(2))
            if year < 1 or not 1 <= mon <= 12:
                return "月份需在 01-12 之间"
        else:
            today = self._today()
            year, mon = today.year, today.month
        return self._build_month_text(year, mon)

    @filter.llm_tool(name="calendar_traditional_festival")
    async def tool_traditional_festival(
        self, event: AstrMessageEvent, month: str = ""
    ) -> str:
        """查询某个月的传统节日与纪念日（含数九、三伏时段）。

        当用户询问"元宵节是哪天"、"七夕是什么时候"、"最近有什么传统节日或纪念日"
        时调用。

        Args:
            month (string): 要查询的月份，格式 YYYY-MM；留空表示当月
        """
        if not self.config.get("enable", True):
            return "插件已停用"
        text = (month or "").strip()
        if text:
            matched = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
            if not matched:
                return f"无法识别的月份：{month}，请使用 YYYY-MM 格式"
            year, mon = int(matched.group(1)), int(matched.group(2))
            if year < 1 or not 1 <= mon <= 12:
                return "月份需在 01-12 之间"
        else:
            today = self._today()
            year, mon = today.year, today.month
        return self._build_festival_month_text(year, mon)
