import re
import urllib.parse
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .rss import RSSToolRepository


class RSSTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        db_path = Path(get_astrbot_data_path()) / "plugin_data" / self.name / "rss_tool.db"
        self.repo = RSSToolRepository(db_path, config)

    async def initialize(self):
        await self.repo.initialize()

    @filter.command_group("feed")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def feed(self, _: AstrMessageEvent):
        """管理 Atom/RSS Feed 订阅"""
        pass

    @feed.command("list")
    async def feed_list(self, event: AstrMessageEvent):
        """查看 Atom/RSS Feed 订阅"""
        yield event.plain_result("\n".join([
            f'{site["title"]}:\t{site["url"]}'
            for site in self.repo.sites
        ]))

    @feed.command("add")
    async def feed_add(self, event: AstrMessageEvent, url: str, comma_sep_tags: str = ""):
        """添加 Atom/RSS Feed 订阅"""
        yield event.plain_result(await self.add_feed_common(url, comma_sep_tags, False))

    async def add_feed_common(self, url: str, comma_sep_tags: str, llm: bool):
        url = url.strip()
        result = urllib.parse.urlparse(url)
        if "." not in result.netloc or result.scheme not in ["http", "https"]:
            return (
                "invalid link" if llm else
                f"无效的链接: {url}\n链接形式通常为 https://XXX.com/YYY.rss"
            )

        tags = [
            tag
            for tag in (tag.strip() for tag in re.split(r"[,;，；、]+", comma_sep_tags))
            if tag != ""
        ]
        same_url_sites = [site for site in self.repo.sites if site["url"] == url]
        if len(same_url_sites) > 0:
            merged_tags = set(same_url_sites[0]["tags"] + tags)
            if len(merged_tags) != len(same_url_sites[0]["tags"]):
                same_url_sites[0]["tags"] = list(merged_tags)
                await self.repo.sync_feeds()
                return "updated" if llm else "已更新订阅标签"
            else:
                return "already exists" if llm else "已存在相同的订阅"
        await self.repo.add_feed(url, tags)
        return "ok" if llm else "添加成功"

    @feed.command("delete")
    async def feed_delete(self, event: AstrMessageEvent, url: str):
        """删除 Atom/RSS Feed 订阅"""
        await self.repo.delete_feed(url)
        yield event.plain_result("删除成功")

    @feed.command("preview")
    async def feed_preview(self, event: AstrMessageEvent, tag: str = "", limit: int = 10):
        """预览 Atom/RSS Feed 订阅"""
        yield event.plain_result(
            await self.repo.query("title,link", {"tag": tag, "limit": limit}, False),
        )

    @feed.command("refresh")
    async def feed_refresh(self, event: AstrMessageEvent, force: bool = False):
        """刷新 Atom/RSS Feed 订阅"""
        await self.repo.refresh(force)
        yield event.plain_result("刷新成功")

    @filter.llm_tool(name="rss_tool_add")
    async def rss_tool_add(self, _: AstrMessageEvent, url: str, tags: str = ""):
        """Subscribe to an Atom/RSS Feed.

Args:
        url(string): Feed url.
        tags(string): Comma separated tags.
        """
        yield await self.add_feed_common(url, tags, True)

    @filter.llm_tool(name="rss_tool_delete")
    async def rss_tool_delete(self, _: AstrMessageEvent, url: str):
        """Unsubscribe from an Atom/RSS Feed.

Args:
        url(string): Feed url.
        """
        if await self.repo.delete_feed(url):
            return "ok"
        return "not found"

    @filter.llm_tool(name="rss_tool_list")
    async def rss_tool_list(self, _: AstrMessageEvent):
        """List Atom/RSS Feed subscriptions."""
        await self.repo.refresh()
        lines = []
        for site in self.repo.sites:
            lines.append('------')
            lines.append(f'- title: {site["title"]}')
            lines.append(f'- url: {site["url"]}')
            lines.append(f'- tags: {",".join(site["tags"])}')
        return "\n".join(lines)

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
        await self.repo.refresh()
        return await self.repo.query(columns, query, True)

    async def terminate(self):
        await self.repo.close()

