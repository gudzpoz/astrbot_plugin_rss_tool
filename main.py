"""AstrBot RSS Tool 插件主模块。

为 AstrBot 提供 RSS/Atom Feed 订阅管理和 LLM 工具调用能力。
支持通过命令行指令管理订阅，也支持 LLM Agent 自主调用。

面向用户的命令使用中文，暂不考虑多语言支持。
面向 Agent 的 Tool 使用英文。
"""

import re
import urllib.parse

from apscheduler.triggers.date import DateTrigger

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .rss import RSSToolRepository


class RSSTool(Star):
    """RSS/Atom Feed 订阅管理插件。

    提供 feed 命令组用于手动管理订阅，以及 LLM tool 供 Agent 自主调用。
    """

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        db_path = StarTools.get_data_dir(self.name) / "rss_tool.db"
        self.repo = RSSToolRepository(db_path, config)
        self.cron = context.cron_manager

    async def initialize(self) -> None:
        """插件激活时初始化数据库连接和 Feed 同步。"""
        await self.repo.initialize()
        logger.info("RSS Tool 插件已初始化")
        self.add_cron_job()

    def add_cron_job(self) -> None:
        """添加定时任务，定时同步 Feed。"""
        time = self.repo.next_sync_time()
        self.cron.scheduler.add_job(
            self.cron_refresh,
            id="rss_tool_feed_sync",
            trigger=DateTrigger(run_date=time),
            replace_existing=True,
            misfire_grace_time=60,
        )
        logger.info("RSS Tool Feed 下次同步时间: %s", time)

    async def cron_refresh(self) -> None:
        """更新定时任务，定时同步 Feed。"""
        await self.repo.sync_feeds()
        self.add_cron_job()

    # ── 命令组：feed ─────────────────────────────────────────────

    @filter.command_group("feed")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def feed(self, _: AstrMessageEvent):
        """管理 Atom/RSS Feed 订阅"""
        pass

    @feed.command("list")
    async def feed_list(self, event: AstrMessageEvent):
        """查看当前所有 Atom/RSS Feed 订阅"""
        if not self.repo.sites:
            yield event.plain_result("暂无订阅")
            return
        yield event.plain_result(
            "\n".join(f"{site['title']}:\t{site['url']}" for site in self.repo.sites)
        )

    @feed.command("add")
    async def feed_add(
        self, event: AstrMessageEvent, url: str, comma_sep_tags: str = ""
    ):
        """添加 Atom/RSS Feed 订阅"""
        yield event.plain_result(await self.add_feed_common(url, comma_sep_tags, False))

    async def add_feed_common(self, url: str, comma_sep_tags: str, llm: bool) -> str:
        """添加 Feed 的通用逻辑，供命令和 LLM tool 共用。

        Args:
            url: Feed 链接。
            comma_sep_tags: 逗号/分号分隔的标签字符串。
            llm: 是否为 LLM 调用（影响返回消息语言）。

        Returns:
            操作结果提示文本。
        """
        url = url.strip()
        result = urllib.parse.urlparse(url)
        if "." not in result.netloc or result.scheme not in ["http", "https"]:
            return (
                "invalid link"
                if llm
                else f"无效的链接: {url}\n链接形式通常为 https://XXX.com/YYY.rss"
            )

        # 解析标签列表，支持多种分隔符
        tags = [
            tag
            for tag in (tag.strip() for tag in re.split(r"[,;，；、]+", comma_sep_tags))
            if tag != ""
        ]

        # 检查是否已存在相同 URL 的订阅
        same_url_sites = [site for site in self.repo.sites if site["url"] == url]
        if len(same_url_sites) > 0:
            merged_tags = set(same_url_sites[0]["tags"] + tags)
            if len(merged_tags) != len(same_url_sites[0]["tags"]):
                same_url_sites[0]["tags"] = list(merged_tags)
                await self.repo.sync_feeds_meta()
                return "updated" if llm else "已更新订阅标签"
            else:
                return "already exists" if llm else "已存在相同的订阅"

        await self.repo.add_feed(url, tags)
        logger.info(f"RSS 订阅已添加: {url}")
        return "ok" if llm else "添加成功"

    @feed.command("delete")
    async def feed_delete(self, event: AstrMessageEvent, url: str):
        """删除 Atom/RSS Feed 订阅"""
        deleted = await self.repo.delete_feed(url)
        if deleted:
            logger.info(f"RSS 订阅已删除: {url}")
            yield event.plain_result("删除成功")
        else:
            yield event.plain_result("未找到该订阅")

    @feed.command("preview")
    async def feed_preview(
        self, event: AstrMessageEvent, tag: str = "", limit: int = 10
    ):
        """预览 Atom/RSS Feed 订阅内容"""
        yield event.plain_result(
            await self.repo.query("title,link", {"tag": tag, "limit": limit}, False),
        )

    @feed.command("refresh")
    async def feed_refresh(self, event: AstrMessageEvent, force: bool = False):
        """刷新所有 Atom/RSS Feed 订阅"""
        await self.repo.sync_feeds(force)
        yield event.plain_result("刷新成功")

    # ── LLM Tool ─────────────────────────────────────────────────

    @filter.llm_tool(name="rss_tool_add")
    async def rss_tool_add(self, _: AstrMessageEvent, url: str, tags: str = ""):
        """Subscribe to an Atom/RSS Feed.

        Args:
            url(string): Feed url.
            tags(string): Comma separated tags.
        """
        if not self.repo.allow_agents:
            return "agent modification is disabled by config"
        return await self.add_feed_common(url, tags, True)

    @filter.llm_tool(name="rss_tool_delete")
    async def rss_tool_delete(self, _: AstrMessageEvent, url: str):
        """Unsubscribe from an Atom/RSS Feed.

        Args:
            url(string): Feed url.
        """
        if not self.repo.allow_agents:
            return "agent modification is disabled by config"
        if await self.repo.delete_feed(url):
            return "ok"
        return "not found"

    @filter.llm_tool(name="rss_tool_list")
    async def rss_tool_list(self, _: AstrMessageEvent):
        """List Atom/RSS Feed subscriptions."""
        await self.repo.sync_feeds()
        lines: list[str] = []
        for site in self.repo.sites:
            lines.append("------")
            lines.append(f"- title: {site['title']}")
            lines.append(f"- url: {site['url']}")
            lines.append(f"- tags: {','.join(site['tags'])}")
        return "\n".join(lines) if lines else "no subscriptions"

    @filter.llm_tool(name="rss_tool_query")
    async def rss_tool_query(self, _: AstrMessageEvent, columns: str, query: object):
        """Query Atom/RSS feed entries.

        Args:
            columns(string): Comma separated column names: [title, link, description,
                published, author, content].
            query(object): A query object. All fields are optional:
                {
                    "feed": "https://xxx.com/xxx.rss",
                    "tag": "xxx",
                    "unread_only": true,
                    "since": "2022-01-01T00:00:00Z",
                    "limit": 10
                }
        """
        await self.repo.sync_feeds()
        return await self.repo.query(columns, query, True)

    async def terminate(self) -> None:
        """插件停用时关闭数据库连接。"""
        await self.repo.close()
        logger.info("RSS Tool 插件已停止")
