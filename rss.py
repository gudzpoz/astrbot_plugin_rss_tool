"""RSS Feed 数据仓库模块。

提供 RSS/Atom Feed 的订阅管理、抓取、存储和查询功能。
使用 aiosqlite 作为本地持久化存储，aiohttp 进行异步网络请求，
fastfeedparser 解析 Feed 内容。
"""

import asyncio
import random
import re
import time
import typing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import ParseResult, parse_qs, urlencode, urlparse, urlunparse

import aiohttp
import aiosqlite
import fastfeedparser
import lxml.html

try:
    # lxml >= 5.2 将 clean 模块移至独立包 lxml_html_clean
    import lxml_html_clean as _html_clean
except ImportError:
    import lxml.html.clean as _html_clean

from astrbot.api import AstrBotConfig, logger

# HTTP Accept 头，优先接受 Atom/RSS 格式
REQUEST_ACCEPT = (
    "application/atom+xml,"
    "application/rss+xml;q=0.9,"
    "application/rdf+xml;q=0.8,"
    "application/xml;q=0.7,text/xml;q=0.7,"
    "*/*;q=0.1"
)

# 网络请求默认超时（秒）
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# query() 允许的列名白名单
ALLOWED_QUERY_COLUMNS = frozenset(
    ["title", "link", "description", "published", "author", "content"]
)


class RSSToolConfigSite(typing.TypedDict):
    """单个 Feed 订阅的配置项，与 _conf_schema.json 中 feeds 模板对应。"""

    __template_key: str
    url: str
    enabled: bool
    title: str
    tags: list[str]
    frequency_hours: int


class RSSToolConfig(typing.TypedDict):
    """插件顶层配置，与 _conf_schema.json 对应。"""

    allow_agents: bool
    user_agent: str
    feeds: list[RSSToolConfigSite]


class RSSToolQuery(typing.TypedDict, total=False):
    """Feed 条目查询参数。所有字段均可选。"""

    feed: str | None
    tag: str | None
    unread_only: bool | None
    since: str | None
    limit: int | None


class FastFeedParserItem(typing.TypedDict):
    """fastfeedparser 解析出的单条 Feed 条目结构。"""

    title: str
    link: str
    description: str
    published: str
    author: str
    content: str


@dataclass
class RSSToolFeed:
    """运行时 Feed 对象，关联数据库记录与配置。"""

    id: int
    last_fetch_time: int
    config_site: RSSToolConfigSite

    def need_update(self) -> bool:
        """根据配置的更新频率判断是否需要重新抓取。"""
        return (
            time.time() - self.last_fetch_time
            > self.config_site["frequency_hours"] * 3600
        )

    def next_update_time(self) -> int:
        """根据配置的更新频率计算下次更新时间。"""
        return self.last_fetch_time + self.config_site["frequency_hours"] * 3600


class RSSToolRepository:
    """RSS Feed 数据仓库。

    负责管理 Feed 订阅的增删改查、定时抓取与本地 SQLite 存储。
    """

    db: aiosqlite.Connection
    feeds: dict[str, RSSToolFeed]
    tags: dict[str, list[RSSToolFeed]]

    def __init__(self, db_path: Path, config: AstrBotConfig) -> None:
        self.db_path = db_path
        self.config = typing.cast(RSSToolConfig, config)
        self.config_saver = config
        self.db = typing.cast(aiosqlite.Connection, None)  # 延迟到 initialize()
        self.feeds = {}
        self.tags = {}

    @property
    def allow_agents(self) -> bool:
        """是否允许 LLM Agent 自主修改订阅列表。"""
        return bool(self.config.get("allow_agents", True))

    @property
    def sites(self) -> list[RSSToolConfigSite]:
        """当前所有订阅站点配置列表。"""
        return self.config["feeds"]

    # ── 内部配置存储（数据库 config 表） ──────────────────────────

    async def _get_config(self, key: str) -> str | None:
        """从数据库 config 表读取配置值。"""
        async with self.db.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                return typing.cast(str, row[0])
        return None

    async def _set_config(self, key: str, value: str) -> None:
        """写入或更新数据库 config 表中的配置值。"""
        async with self.db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        ) as cursor:
            await cursor.fetchone()
            await self.db.commit()

    async def _maybe_run_db_migration(self, version: int, *statements: str) -> None:
        """按版本号执行数据库迁移语句（仅在当前版本低于目标版本时执行）。"""
        current_version = int((await self._get_config("db_version")) or "0")
        if current_version < version:
            for statement in statements:
                await self.db.execute(statement)
            await self._set_config("db_version", str(version))
            await self.db.commit()

    # ── 初始化与关闭 ────────────────────────────────────────────

    async def initialize(self) -> None:
        """初始化数据库连接并执行必要的表创建/迁移。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)",
        )
        await self.db.commit()

        await self._maybe_run_db_migration(
            1,
            """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, last_fetched INTEGER
)""",
            """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER, title TEXT,
    link TEXT, description TEXT, published INTEGER, author TEXT, content TEXT,
    unread INTEGER DEFAULT 1,
    UNIQUE(link)
)""",
            "CREATE INDEX IF NOT EXISTS idx_items_feed_id ON items(feed_id, published)",
            "CREATE INDEX IF NOT EXISTS idx_items_published ON items(published)",
            "CREATE INDEX IF NOT EXISTS idx_items_unread ON items(unread)",
        )

        await self.sync_feeds_meta()

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self.db:
            await self.db.close()

    # ── Feed 同步 ───────────────────────────────────────────────

    def next_sync_time(self) -> datetime:
        """计算下次同步时间，用于定时任务。"""
        sync_time = int(time.time()) + 7 * 24 * 3600
        for feed in self.feeds.values():
            sync_time = min(sync_time, feed.next_update_time())
        sync_time = max(sync_time, int(time.time()))
        return datetime.fromtimestamp(sync_time + random.randint(10, 30))

    async def sync_feeds_meta(self):
        """将内存中的配置与数据库同步。

        1. 保存当前配置到磁盘
        2. 从数据库加载已有 Feed 记录
        3. 为新增 Feed 创建数据库记录
        4. 重建内存中的 feeds/tags 索引
        """
        self.config_saver.save_config()

        # 从数据库加载已存储的 feed 记录: url -> (id, last_fetched)
        stored: dict[str, tuple[int, int]] = {}
        async with self.db.execute("SELECT id, url, last_fetched FROM feeds") as cursor:
            for row in await cursor.fetchall():
                stored[row[1]] = (row[0], row[2])

        self.feeds = {}
        self.tags = {}
        newly_added: list[RSSToolFeed] = []

        for feed in self.config["feeds"]:
            if feed["url"] in stored:
                db_id, last_fetch_time = stored[feed["url"]]
                entry = RSSToolFeed(
                    config_site=feed, id=db_id, last_fetch_time=last_fetch_time
                )
            else:
                entry = RSSToolFeed(config_site=feed, id=0, last_fetch_time=0)
                newly_added.append(entry)

            # 构建 tag -> feed 列表的索引
            for tag in feed["tags"]:
                tag = tag.strip().lower()
                if tag not in self.tags:
                    self.tags[tag] = []
                self.tags[tag].append(entry)

            self.feeds[feed["url"]] = entry

        # 为新增 feed 创建数据库记录
        for entry in newly_added:
            async with self.db.execute(
                "INSERT INTO feeds (url, last_fetched) VALUES (?, ?)",
                (entry.config_site["url"], entry.last_fetch_time),
            ) as cursor:
                await cursor.fetchone()
                if not cursor.lastrowid:
                    raise RuntimeError(
                        f"Failed to insert feed: {entry.config_site['url']}"
                    )
                entry.id = cursor.lastrowid
            await self.db.commit()

    async def sync_feeds(self, force: bool = False) -> None:
        """触发需要更新的 Feed 抓取。"""

        await self.sync_feeds_meta()

        # 并发更新，使用 return_exceptions 避免单个失败影响全部
        results = await asyncio.gather(
            *[self.update_feed(feed, force) for feed in self.feeds.values()],
            return_exceptions=True,
        )
        for feed_entry, result in zip(self.feeds.values(), results):
            if isinstance(result, Exception):
                logger.warning(
                    "RSS 抓取失败 [%s]: %s", feed_entry.config_site["url"], result
                )

    # ── Feed 抓取与更新 ─────────────────────────────────────────

    async def mark_up_to_date(self, feed: RSSToolFeed) -> None:
        """将 Feed 的最后抓取时间更新为当前时间。"""
        feed.last_fetch_time = int(time.time())
        async with self.db.execute(
            "UPDATE feeds SET last_fetched = ? WHERE id = ?",
            (feed.last_fetch_time, feed.id),
        ) as cursor:
            await cursor.fetchone()
        await self.db.commit()

    async def update_feed(self, feed: RSSToolFeed, force: bool = False) -> int:
        """抓取并解析单个 Feed，将新条目写入数据库。

        Args:
            feed: 要更新的 Feed 对象。
            force: 为 True 时忽略更新频率限制强制抓取。

        Returns:
            抓取到的条目数量。
        """
        if not force and not feed.need_update():
            return 0

        url = feed.config_site["url"]

        try:
            async with aiohttp.ClientSession(
                trust_env=True,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": str(
                        self.config.get("user_agent") or "AstrBot RSS Tool"
                    ),
                    "Accept": REQUEST_ACCEPT,
                },
            ) as session:
                last_time = time.strftime(
                    "%a, %d %b %Y %H:%M:%S GMT",
                    time.gmtime(feed.last_fetch_time),
                )
                async with session.get(
                    url, headers={"If-Modified-Since": last_time}
                ) as response:
                    if response.status == 304:
                        await self.mark_up_to_date(feed)
                        return 0
                    if response.status != 200:
                        logger.warning(
                            "RSS 抓取 HTTP 错误 [%s]: %s", url, response.status
                        )
                        return 0
                    xml = await response.content.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("RSS 抓取网络异常 [%s]: %s", url, e)
            return 0

        try:
            parsed = fastfeedparser.parse(
                xml, include_media=False, include_enclosures=False
            )
        except Exception as e:
            logger.warning("RSS 解析失败 [%s]: %s", url, e)
            return 0

        # 自动填充 Feed 标题
        if feed.config_site["title"] == "":
            feed_title = parsed.get("feed", {}).get("title", "")
            if feed_title:
                feed.config_site["title"] = feed_title
                self.config_saver.save_config()

        last_published = 0
        async with self.db.execute(
            "SELECT MAX(published) FROM items WHERE feed_id = ?",
            (feed.id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                last_published = row[0]

        added = 0
        for item in typing.cast(list[FastFeedParserItem], parsed["entries"]):
            title = item["title"]
            link = item["link"]
            if not link or not title:
                continue

            try:
                published = datetime.fromisoformat(item["published"])
            except (ValueError, TypeError):
                published = datetime.now()

            if published.timestamp() <= last_published:
                continue

            link = _prune_url(link)
            author = item["author"] or ""
            description = _prune_html(item["description"] or "")
            content = typing.cast(list[dict[str, str]], item["content"])
            text_content = (
                ""
                if not content
                else _prune_html(
                    next(
                        iter(c for c in content if c["type"] == "text/html"),
                        content[0],
                    )["value"]
                )
            )

            # 使用 ON CONFLICT 避免覆盖已有条目的 unread 状态
            async with self.db.execute(
                """
INSERT INTO items (feed_id, title, link, description, published, author, content, unread)
VALUES (?, ?, ?, ?, ?, ?, ?, 1)
ON CONFLICT(link) DO UPDATE SET
    feed_id = excluded.feed_id,
    title = excluded.title,
    description = excluded.description,
    published = excluded.published,
    author = excluded.author,
    content = excluded.content
""",
                (
                    feed.id,
                    title,
                    link,
                    description,
                    int(published.timestamp()),
                    author,
                    text_content,
                ),
            ) as cursor:
                await cursor.fetchone()
            added += 1

        if added > 0:
            await self.db.commit()

        await self.mark_up_to_date(feed)
        return added

    # ── 查询 ────────────────────────────────────────────────────

    async def query(self, fields: str, query: object, mark_as_read: bool) -> str:
        """查询 Feed 条目并返回格式化文本。

        Args:
            fields: 逗号分隔的列名（白名单过滤）。
            query: 查询参数对象，参见 RSSToolQuery。
            mark_as_read: 是否将返回的条目标记为已读。

        Returns:
            格式化的查询结果文本，无结果时返回 "--- nothing found ---"。
        """
        query = typing.cast(RSSToolQuery, query)

        # 白名单过滤列名，防止 SQL 注入
        field_names = [
            f
            for f in (f.strip() for f in fields.split(","))
            if f in ALLOWED_QUERY_COLUMNS
        ]
        if not field_names:
            return "--- nothing found ---"

        # 构建参数化查询
        params: list[int | str] = []
        feed_ids: set[int] = set()
        clauses: list[str] = []

        feed_url = query.get("feed")
        tag = query.get("tag")

        if feed_url is not None:
            feed_object = self.feeds.get(feed_url)
            if feed_object is not None:
                feed_ids.add(feed_object.id)
        elif tag is not None:
            tag_list = self.tags.get(tag.lower())
            if tag_list is not None:
                feed_ids.update(f.id for f in tag_list if f.config_site["enabled"])
        else:
            feed_ids.update(
                f.id for f in self.feeds.values() if f.config_site["enabled"]
            )

        if not feed_ids:
            return "--- nothing found ---"

        # feed_id IN (?, ?, ...) — 参数化
        placeholders = ",".join("?" for _ in feed_ids)
        clauses.append(f"feed_id IN ({placeholders})")
        params.extend(feed_ids)

        if query.get("unread_only", True):
            clauses.append("unread = 1")

        since = query.get("since")
        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since)
                clauses.append("published >= ?")
                params.append(int(since_dt.timestamp()))
            except (ValueError, TypeError):
                logger.warning("RSS 查询 since 参数格式错误: %s", since)

        # 安全拼接列名（已经过白名单过滤）+ 参数化 WHERE/LIMIT
        columns_sql = ",".join(field_names)
        where_sql = " AND ".join(clauses)
        q = f"SELECT id,{columns_sql} FROM items WHERE {where_sql} ORDER BY published ASC LIMIT ?"

        try:
            limit = max(1, min(int(query.get("limit", 10) or 10), 100))
        except (ValueError, TypeError):
            limit = 10
        params.append(limit)

        ids: set[int] = set()
        formatted: list[str] = []

        async with self.db.execute(q, params) as cursor:
            for row in await cursor.fetchall():
                ids.add(row[0])
                formatted.append("------")
                for field, value in zip(field_names, row[1:]):
                    if value is None:
                        value = ""
                    elif field == "published":
                        value = datetime.fromtimestamp(value).isoformat(
                            timespec="seconds"
                        )
                    value = str(value)
                    if "\n" in value:
                        value = re.sub(r"\n+", "<br>", value)
                    formatted.append(f'- "{field}": "{value}"')

        if mark_as_read and ids:
            await self.db.executemany(
                "UPDATE items SET unread = 0 WHERE id = ?",
                ((item_id,) for item_id in ids),
            )
            await self.db.commit()

        return "--- nothing found ---" if not formatted else "\n".join(formatted)

    # ── 订阅管理 ────────────────────────────────────────────────

    async def add_feed(self, url: str, tags: list[str]) -> None:
        """添加新的 Feed 订阅。"""
        self.config["feeds"].append(
            RSSToolConfigSite(
                __template_key="site",
                url=url,
                enabled=True,
                title="",
                tags=tags,
                frequency_hours=6,
            )
        )
        await self.sync_feeds_meta()

    async def delete_feed(self, url: str) -> bool:
        """删除指定 URL 的 Feed 订阅。

        Returns:
            True 表示成功删除，False 表示未找到对应订阅。
        """
        new_feeds = [site for site in self.config["feeds"] if site["url"] != url]
        if len(new_feeds) == len(self.config["feeds"]):
            return False
        self.config["feeds"] = new_feeds
        await self.sync_feeds_meta()
        return True

    def _find_site(self, url: str) -> RSSToolConfigSite | None:
        """根据 URL 查找对应的配置项。"""
        for site in self.config["feeds"]:
            if site["url"] == url:
                return site
        return None

    async def update_feed_tags(
        self,
        url: str,
        *,
        set_tags: list[str] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> bool:
        """修改指定 Feed 的标签。

        支持三种操作（按优先级）：set 覆盖、add 追加、remove 移除。
        同一次调用中 set 优先级最高，若提供 set_tags 则忽略 add/remove。

        Returns:
            True 表示成功修改，False 表示未找到对应订阅。
        """
        site = self._find_site(url)
        if site is None:
            return False

        if set_tags is not None:
            site["tags"] = [t.strip() for t in set_tags if t.strip()]
        else:
            current = set(site["tags"])
            if add_tags:
                current.update(t.strip() for t in add_tags if t.strip())
            if remove_tags:
                current -= {t.strip() for t in remove_tags}
            site["tags"] = sorted(current)

        await self.sync_feeds_meta()
        return True

    async def set_feed_enabled(self, url: str, enabled: bool) -> bool:
        """启用或禁用指定 Feed。

        Returns:
            True 表示成功修改，False 表示未找到对应订阅。
        """
        site = self._find_site(url)
        if site is None:
            return False
        site["enabled"] = enabled
        await self.sync_feeds_meta()
        return True

    async def set_feed_frequency(self, url: str, hours: int) -> bool:
        """修改指定 Feed 的更新频率。

        Args:
            url: Feed 链接。
            hours: 更新间隔（小时），最小 1 小时。

        Returns:
            True 表示成功修改，False 表示未找到对应订阅。
        """
        site = self._find_site(url)
        if site is None:
            return False
        hours = max(1, hours)
        site["frequency_hours"] = hours
        await self.sync_feeds_meta()
        return True

    async def mark_read(
        self,
        *,
        feed_url: str | None = None,
        tag: str | None = None,
    ) -> int:
        """将指定范围的条目标记为已读。

        Args:
            feed_url: 按 Feed URL 过滤。
            tag: 按标签过滤。
            若两者均为 None，则标记所有条目为已读。

        Returns:
            受影响的条目数，-1 表示未找到对应条目。
        """
        feed_ids: set[int] = set()

        if feed_url is not None:
            feed_obj = self.feeds.get(feed_url)
            if feed_obj is not None:
                feed_ids.add(feed_obj.id)
        elif tag is not None:
            tag_list = self.tags.get(tag.lower())
            if tag_list is not None:
                feed_ids.update(f.id for f in tag_list)
        else:
            feed_ids.update(f.id for f in self.feeds.values())

        if not feed_ids:
            return -1

        placeholders = ",".join("?" for _ in feed_ids)
        async with self.db.execute(
            f"UPDATE items SET unread = 0 WHERE feed_id IN ({placeholders}) AND unread = 1",
            list(feed_ids),
        ) as cursor:
            await cursor.fetchall()
            count = cursor.rowcount
        await self.db.commit()
        return count


def _prune_html(text: str) -> str:
    """清理 HTML 标签，返回纯文本内容。

    对输入进行安全处理：空字符串直接返回，解析或清理失败时回退到原始文本。
    """
    if not text or not text.strip():
        return ""
    try:
        tree = lxml.html.fromstring(text)
        cleaned = _html_clean.clean_html(tree)
        return cleaned.text_content().strip()
    except Exception:
        # 解析失败时回退到去除标签的简单处理
        return re.sub(r"<[^>]+>", "", text).strip()


def _prune_url(url: str) -> str:
    """清理 URL，去除 utm_ 参数，返回纯文本内容。"""
    try:
        parsed = urlparse(url)
        query = {
            k: v for k, v in parse_qs(parsed.query).items() if not k.startswith("utm_")
        }
        pruned = ParseResult(
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(query, doseq=True),
            parsed.fragment,
        )
        return urlunparse(pruned)
    except Exception:
        return url
