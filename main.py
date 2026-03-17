from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig

import urllib.parse
import re
import typing


class RSSToolConfigSite(typing.TypedDict):
    __template_key: str
    url: str
    enabled: bool
    title: str
    tags: list[str]
    frequency_hours: int

class RSSToolConfig(typing.TypedDict):
    """与 _conf_schema.json 对应"""
    allow_agents: str
    feeds: list[RSSToolConfigSite]


class RSSTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = typing.cast(RSSToolConfig, config)
        self.config_saver = config

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.command_group("feed")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def feed(self, _: AstrMessageEvent):
        """管理 Atom/RSS Feed 订阅"""
        pass

    @feed.command("list")
    async def feed_list(self, event: AstrMessageEvent):
        """查看 Atom/RSS Feed 订阅"""
        yield event.plain_result(f"{self.config}" + "\n".join([
            f'{site["title"]}:\t{site["url"]}'
            for site in self.config["feeds"]
        ]))

    @feed.command("add")
    async def feed_add(self, event: AstrMessageEvent, url: str, tag: str = ""):
        """添加 Atom/RSS Feed 订阅"""
        url = url.strip()
        result = urllib.parse.urlparse(url)
        if "." not in result.netloc or result.scheme not in ["http", "https"]:
            yield event.plain_result(f"无效的链接: {url}\n链接形式通常为 https://XXX.com/YYY.rss")
            return

        self.config["feeds"].append(RSSToolConfigSite(
            __template_key="site",
            url=url,
            enabled=True,
            title="",
            tags=[
                tag
                for tag in (tag.strip() for tag in re.split(r"[,;，；、]+", tag))
                if tag != ""
            ],
            frequency_hours=6,
        ))
        self.config_saver.save_config()
        yield event.plain_result("添加成功")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
