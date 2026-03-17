import asyncio
import re
import time
import typing
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import aiohttp
import aiosqlite
import fastfeedparser
import lxml.html
import lxml.html.clean
from pydantic import Field

from astrbot.core.config.astrbot_config import AstrBotConfig

REQUEST_ACCEPT = (
    "application/atom+xml,"
    "application/rss+xml;q=0.9,"
    "application/rdf+xml;q=0.8,"
    "application/xml;q=0.7,text/xml;q=0.7,"
    "*/*;q=0.1"
)

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
    user_agent: str
    feeds: list[RSSToolConfigSite]

class RSSToolQuery(typing.TypedDict):
    feed: str | None
    tag: str | None
    unread_only: bool | None
    since: str | None
    limit: int | None

class FastFeedParserItem(typing.TypedDict):
    title: str
    link: str
    description: str
    published: str
    author: str
    content: str


@dataclass
class RSSToolFeed:
    id: int
    last_fetch_time: int
    config_site: RSSToolConfigSite

    def need_update(self):
        return time.time() - self.last_fetch_time > self.config_site["frequency_hours"] * 3600


class RSSToolRepository:
    name = "rss_tool"
    description = "Fetch and filter RSS feeds."
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
    })

    db: aiosqlite.Connection
    feeds: dict[str, RSSToolFeed]
    tags: dict[str, list[RSSToolFeed]]

    def __init__(self, db_path: Path, config: AstrBotConfig):
        self.db_path = db_path
        self.config = typing.cast(RSSToolConfig, config)
        self.config_saver = config
        self.db = typing.cast(aiosqlite.Connection, None) # self.initialize()
        self.user_agent = "AstrBot RSS Tool"
        self.feeds = {}
        self.tags = {}

    @property
    def sites(self):
        return self.config["feeds"]

    async def _get_config(self, key: str):
        async with self.db.execute("SELECT value FROM config WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
            if row is not None:
                return typing.cast(str, row[0])
        return None

    async def _set_config(self, key: str, value: str):
        async with self.db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        ) as cursor:
            await cursor.fetchone()
            await self.db.commit()

    async def _maybe_run_db_migration(self, version: int, *statements: str):
        current_version = int((await self._get_config("db_version")) or "0")
        if current_version < version:
            for statement in statements:
                await self.db.execute(statement)
            await self._set_config("db_version", str(version))
            await self.db.commit()

    async def initialize(self):
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

            "CREATE INDEX IF NOT EXISTS idx_items_link ON items(feed_id)",
            "CREATE INDEX IF NOT EXISTS idx_items_link ON items(published)",
            "CREATE INDEX IF NOT EXISTS idx_items_link ON items(unread)",
        )

        await self.sync_feeds()

    async def sync_feeds(self):
        self.config_saver.save_config()

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
                entry = RSSToolFeed(config_site=feed, id=db_id, last_fetch_time=last_fetch_time)
            else:
                entry = RSSToolFeed(config_site=feed, id=0, last_fetch_time=0)
                newly_added.append(entry)

            for tag in feed["tags"]:
                tag = tag.strip().lower()
                if tag not in self.tags:
                    self.tags[tag] = []
                self.tags[tag].append(entry)

            self.feeds[feed["url"]] = entry

        for entry in newly_added:
            async with self.db.execute(
                "INSERT INTO feeds (url, last_fetched) VALUES (?, ?)",
                (entry.config_site["url"], entry.last_fetch_time),
            ) as cursor:
                await cursor.fetchone()
                assert cursor.lastrowid
                entry.id = cursor.lastrowid
            await self.db.commit()

        await asyncio.gather(*[self.update_feed(feed) for feed in self.feeds.values()])

    async def mark_up_to_date(self, feed: RSSToolFeed):
        feed.last_fetch_time = int(time.time())
        async with self.db.execute(
            "UPDATE feeds SET last_fetched = ? WHERE id = ?",
            (feed.last_fetch_time, feed.id),
        ) as cursor:
            await cursor.fetchone()
        await self.db.commit()

    async def update_feed(self, feed: RSSToolFeed, force: bool = False):
        if not force and not feed.need_update():
            return

        async with aiohttp.ClientSession(trust_env=True, headers={
            "User-Agent": self.user_agent,
            "Accept": REQUEST_ACCEPT,
        }) as session:
            url = feed.config_site["url"]
            last_time = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(feed.last_fetch_time))
            async with session.get(url, headers={
                "If-Modified-Since": last_time,
            }) as response:
                if response.status not in [200, 304]:
                    raise Exception(f"HTTP Error: {response.status}")
                if response.status == 304:
                    await self.mark_up_to_date(feed)
                    return
                xml = await response.content.read()

        parsed = fastfeedparser.parse(xml, include_media=False, include_enclosures=False)
        if feed.config_site["title"] == "":
            feed.config_site["title"] = parsed["feed"]["title"]

        for item in typing.cast(list[FastFeedParserItem], parsed["entries"]):
            title = item["title"]
            link = item["link"]
            if not link or not title:
                continue
            published = datetime.fromisoformat(item["published"])
            author = item["author"] or ""
            description = _prune_html(item["description"] or "")
            content = typing.cast(list[dict[str, str]], item["content"])
            text_content = "" if not content else _prune_html(next(
                iter(c for c in content if c["type"] == "text/html"),
                content[0],
            )["value"])
            async with self.db.execute(
                """
INSERT OR REPLACE INTO items (feed_id, title, link, description, published, author, content, unread)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
""",
                (
                    feed.id,
                    title,
                    link,
                    description,
                    int(published.timestamp()),
                    author,
                    text_content,
                    1,
                ),
            ) as cursor:
                await cursor.fetchone()
            await self.db.commit()
        await self.mark_up_to_date(feed)

    async def query(self, fields: str, query: object, mark_as_read: bool):
        query = typing.cast(RSSToolQuery, query)
        field_names = [
            f for f in (f.strip() for f in fields.split(","))
            if f in ["title", "link", "description", "published", "author", "content"]
        ]
        fields = ",".join(field_names)
        q = f"SELECT id,{fields} FROM items WHERE "
        feed_ids = set()
        clauses = []

        feed = query.get("feed")
        tag = query.get("tag")
        if feed is not None:
            feed_object = self.feeds.get(feed)
            if feed_object is not None:
                feed_ids.add(feed_object.id)
        elif tag is not None and len(feed_ids) == 0:
            tag_list = self.tags.get(tag.lower())
            if tag_list is not None:
                feed_ids.update(f.id for f in tag_list if f.config_site["enabled"])
        elif len(feed_ids) == 0:
            feed_ids.update(f.id for f in self.feeds.values() if f.config_site["enabled"])
        if len(feed_ids) == 0:
            return "--- nothing found ---"
        clauses.append(f"feed_id IN ({','.join(map(str, feed_ids))})")

        if query.get("unread_only", True):
            clauses.append("unread = 1")

        since = query.get("since")
        if since is not None:
            since = datetime.fromisoformat(since)
            clauses.append(f"published >= {int(since.timestamp())}")

        q += " AND ".join(clauses)
        q += " ORDER BY published ASC"

        limit = query.get("limit", 10) or 10
        q += f" LIMIT {limit}"

        ids: set[int] = set()
        formatted: list[str] = []
        async with self.db.execute(q) as cursor:
            for row in await cursor.fetchall():
                ids.add(row[0])
                formatted.append("------")
                for field, value in zip(field_names, row[1:]):
                    if field == "published":
                        value = datetime.fromtimestamp(value).isoformat(timespec="seconds")
                    if "\n" in value:
                        value = re.sub(r"\n+", "<br>", value)
                    formatted.append(f'- "{field}": "{value}"')

        if mark_as_read:
            await self.db.executemany(
                "UPDATE items SET unread = 0 WHERE id = ?",
                ((id,) for id in ids),
            )
            await self.db.commit()

        return "--- nothing found ---" if len(formatted) == 0 else "\n".join(formatted)

    async def add_feed(self, url: str, tags: list[str]):
        self.config["feeds"].append(RSSToolConfigSite(
            __template_key="site",
            url=url,
            enabled=True,
            title="",
            tags=tags,
            frequency_hours=6,
        ))
        await self.sync_feeds()

    async def delete_feed(self, url: str):
        new_feeds = [site for site in self.config["feeds"] if site["url"] != url]
        if len(new_feeds) == len(self.config["feeds"]):
            return False
        self.config["feeds"] = new_feeds
        await self.sync_feeds()
        return True

    async def refresh(self, force: bool = False):
        await self.sync_feeds()
        await asyncio.gather(*(self.update_feed(feed, force) for feed in self.feeds.values()))

    async def close(self):
        await self.db.close()


def _prune_html(text: str) -> str:
    tree = lxml.html.fromstring(text)
    return lxml.html.clean.clean_html(tree).text_content().strip()
