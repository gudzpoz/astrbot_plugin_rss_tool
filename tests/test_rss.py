"""test_rss.py — RSSToolRepository 核心逻辑测试。

覆盖：数据库初始化/迁移、Feed 增删改查、标签管理、查询、
HTML 清理、URL 清理、mark_read 等。
"""

import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from rss import (
    RSSToolConfigSite,
    RSSToolFeed,
    RSSToolRepository,
    _prune_html,
    _prune_url,
)

# ── 纯函数测试 ──────────────────────────────────────────────────


class TestPruneHtml:
    """测试 _prune_html HTML 清理函数。"""

    def test_empty_string(self):
        assert _prune_html("") == ""

    def test_whitespace_only(self):
        assert _prune_html("   ") == ""

    def test_none_like(self):
        """空字符串直接返回。"""
        assert _prune_html("") == ""

    def test_plain_text(self):
        assert _prune_html("hello world") == "hello world"

    def test_simple_html(self):
        result = _prune_html("<p>hello <b>world</b></p>")
        assert "hello" in result
        assert "world" in result
        assert "<p>" not in result
        assert "<b>" not in result

    def test_script_tag_removed(self):
        """确保 script 标签被清理。"""
        result = _prune_html("<p>safe</p><script>alert('xss')</script>")
        assert "alert" not in result
        assert "safe" in result

    def test_malformed_html_fallback(self):
        """畸形 HTML 应回退到正则清理。"""
        result = _prune_html("<p>unclosed")
        assert "unclosed" in result


class TestPruneUrl:
    """测试 _prune_url URL 清理函数。"""

    def test_no_utm_params(self):
        url = "https://example.com/article?id=123"
        result = _prune_url(url)
        assert "id=123" in result
        assert "example.com/article" in result

    def test_remove_utm_params(self):
        url = "https://example.com/article?id=123&utm_source=twitter&utm_medium=social"
        result = _prune_url(url)
        assert "utm_source" not in result
        assert "utm_medium" not in result
        assert "id=123" in result

    def test_all_utm_params_removed(self):
        url = "https://example.com/?utm_source=x&utm_campaign=y"
        result = _prune_url(url)
        assert "utm_source" not in result
        assert "utm_campaign" not in result

    def test_preserves_fragment(self):
        url = "https://example.com/page#section1"
        assert _prune_url(url) == url

    def test_preserves_multi_value_params(self):
        """测试 doseq=True 正确处理多值参数。"""
        url = "https://example.com/?tag=a&tag=b"
        result = _prune_url(url)
        assert "tag=a" in result
        assert "tag=b" in result

    def test_invalid_url_returns_original(self):
        bad_url = "not a url at all"
        assert _prune_url(bad_url) == bad_url


# ── RSSToolFeed 数据类测试 ───────────────────────────────────────


class TestRSSToolFeed:
    """测试 RSSToolFeed 数据类方法。"""

    def _make_feed(self, last_fetch: int = 0, freq_hours: int = 6) -> RSSToolFeed:
        config_site = RSSToolConfigSite(
            {
                "__template_key": "site",
                "url": "https://example.com/feed.xml",
                "enabled": True,
                "title": "Test",
                "tags": [],
                "frequency_hours": freq_hours,
            }
        )
        return RSSToolFeed(id=1, last_fetch_time=last_fetch, config_site=config_site)

    def test_need_update_stale(self):
        """超过更新频率应返回 True。"""
        feed = self._make_feed(last_fetch=int(time.time()) - 7 * 3600, freq_hours=6)
        assert feed.need_update() is True

    def test_need_update_fresh(self):
        """未超过更新频率应返回 False。"""
        feed = self._make_feed(last_fetch=int(time.time()), freq_hours=6)
        assert feed.need_update() is False

    def test_next_update_time(self):
        now = int(time.time())
        feed = self._make_feed(last_fetch=now, freq_hours=6)
        assert feed.next_update_time() == now + 6 * 3600


# ── Repository 数据库操作测试 ────────────────────────────────────


@pytest.mark.asyncio
class TestRepositoryInit:
    """测试 RSSToolRepository 初始化和数据库迁移。"""

    async def test_initialize_creates_tables(self, repo: RSSToolRepository):
        """初始化后应创建 config、feeds、items 表。"""
        async with repo.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert "config" in tables
        assert "feeds" in tables
        assert "items" in tables

    async def test_db_version_set(self, repo: RSSToolRepository):
        """初始化后 db_version 应为 '2'。"""
        version = await repo._get_config("db_version")
        assert version == "2"

    async def test_double_initialize_idempotent(self, repo: RSSToolRepository):
        """重复初始化不应报错。"""
        await repo.initialize()
        version = await repo._get_config("db_version")
        assert version == "2"


@pytest.mark.asyncio
class TestRepositoryFeedManagement:
    """测试 Feed 订阅的增删改查。"""

    async def test_add_feed(self, repo: RSSToolRepository):
        await repo.add_feed("https://example.com/rss", ["tech"])
        assert len(repo.sites) == 1
        assert repo.sites[0]["url"] == "https://example.com/rss"
        assert repo.sites[0]["tags"] == ["tech"]

    async def test_add_feed_creates_db_record(self, repo: RSSToolRepository):
        await repo.add_feed("https://example.com/rss", [])
        assert "https://example.com/rss" in repo.feeds
        assert repo.feeds["https://example.com/rss"].id > 0

    async def test_delete_feed_existing(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.delete_feed("https://example.com/feed.xml")
        assert result is True
        assert len(repo_with_feed.sites) == 0

    async def test_delete_feed_nonexistent(self, repo: RSSToolRepository):
        result = await repo.delete_feed("https://nonexistent.com/rss")
        assert result is False

    async def test_set_feed_enabled(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.set_feed_enabled(
            "https://example.com/feed.xml", False
        )
        assert result is True
        assert repo_with_feed.sites[0]["enabled"] is False

    async def test_set_feed_enabled_nonexistent(self, repo: RSSToolRepository):
        result = await repo.set_feed_enabled("https://nonexistent.com", True)
        assert result is False

    async def test_set_feed_frequency(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.set_feed_frequency(
            "https://example.com/feed.xml", 12
        )
        assert result is True
        assert repo_with_feed.sites[0]["frequency_hours"] == 12

    async def test_set_feed_frequency_minimum(self, repo_with_feed: RSSToolRepository):
        """频率最小值应为 1 小时。"""
        await repo_with_feed.set_feed_frequency("https://example.com/feed.xml", 0)
        assert repo_with_feed.sites[0]["frequency_hours"] == 1

    async def test_set_feed_frequency_nonexistent(self, repo: RSSToolRepository):
        result = await repo.set_feed_frequency("https://nonexistent.com", 6)
        assert result is False


@pytest.mark.asyncio
class TestRepositoryTagManagement:
    """测试 Feed 标签管理。"""

    async def test_update_tags_set(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.update_feed_tags(
            "https://example.com/feed.xml", set_tags=["python", "ai"]
        )
        assert result is True
        assert sorted(repo_with_feed.sites[0]["tags"]) == ["ai", "python"]

    async def test_update_tags_add(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.update_feed_tags(
            "https://example.com/feed.xml", add_tags=["python"]
        )
        assert result is True
        assert "python" in repo_with_feed.sites[0]["tags"]
        # 原有标签也应保留
        assert "tech" in repo_with_feed.sites[0]["tags"]

    async def test_update_tags_remove(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.update_feed_tags(
            "https://example.com/feed.xml", remove_tags=["tech"]
        )
        assert result is True
        assert "tech" not in repo_with_feed.sites[0]["tags"]
        assert "news" in repo_with_feed.sites[0]["tags"]

    async def test_update_tags_nonexistent_feed(self, repo: RSSToolRepository):
        result = await repo.update_feed_tags("https://nonexistent.com", set_tags=["x"])
        assert result is False

    async def test_tag_index_built(self, repo_with_feed: RSSToolRepository):
        """sync_feeds_meta 后应构建 tag 索引。"""
        assert "tech" in repo_with_feed.tags
        assert "news" in repo_with_feed.tags
        assert len(repo_with_feed.tags["tech"]) == 1


@pytest.mark.asyncio
class TestRepositoryMarkRead:
    """测试 mark_read 标记已读功能。"""

    async def test_mark_read_no_feeds(self, repo: RSSToolRepository):
        """无订阅时应返回 -1。"""
        result = await repo.mark_read()
        assert result == -1

    async def test_mark_read_nonexistent_feed(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.mark_read(feed_url="https://nonexistent.com")
        assert result == -1

    async def test_mark_read_nonexistent_tag(self, repo_with_feed: RSSToolRepository):
        result = await repo_with_feed.mark_read(tag="nonexistent")
        assert result == -1

    async def test_mark_read_by_feed_url(self, repo_with_feed: RSSToolRepository):
        """插入条目后按 feed_url 标记已读。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await repo_with_feed.db.execute(
            "INSERT INTO items (feed_id, title, link, published, unread) VALUES (?, ?, ?, ?, 1)",
            (feed.id, "Test", "https://example.com/1", int(time.time())),
        )
        await repo_with_feed.db.commit()

        result = await repo_with_feed.mark_read(feed_url="https://example.com/feed.xml")
        assert result == 1

    async def test_mark_read_by_tag(self, repo_with_feed: RSSToolRepository):
        """插入条目后按 tag 标记已读。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await repo_with_feed.db.execute(
            "INSERT INTO items (feed_id, title, link, published, unread) VALUES (?, ?, ?, ?, 1)",
            (feed.id, "Test", "https://example.com/2", int(time.time())),
        )
        await repo_with_feed.db.commit()

        result = await repo_with_feed.mark_read(tag="tech")
        assert result == 1


@pytest.mark.asyncio
class TestRepositoryQuery:
    """测试 Feed 条目查询。"""

    async def _insert_items(self, repo: RSSToolRepository, feed_id: int, count: int):
        """辅助方法：向数据库插入测试条目。"""
        for i in range(count):
            await repo.db.execute(
                """INSERT INTO items
                   (feed_id, title, link, description, published, author, content, unread)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    feed_id,
                    f"Title {i}",
                    f"https://example.com/item{i}",
                    f"Desc {i}",
                    int(time.time()) + i,
                    f"Author {i}",
                    f"Content {i}",
                ),
            )
        await repo.db.commit()

    async def test_query_empty(self, repo_with_feed: RSSToolRepository):
        """无条目时应返回 nothing found。"""
        result = await repo_with_feed.query("title,link", {}, False)
        assert result == "--- nothing found ---"

    async def test_query_with_items(self, repo_with_feed: RSSToolRepository):
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 3)

        result = await repo_with_feed.query(
            "title,link", {"unread_only": True, "limit": 10}, False
        )
        assert "Title 0" in result
        assert "Title 2" in result

    async def test_query_marks_as_read(self, repo_with_feed: RSSToolRepository):
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 2)

        await repo_with_feed.query("title", {"limit": 10}, True)

        # 再次查询 unread_only 应无结果
        result = await repo_with_feed.query(
            "title", {"unread_only": True, "limit": 10}, False
        )
        assert result == "--- nothing found ---"

    async def test_query_invalid_columns(self, repo_with_feed: RSSToolRepository):
        """非法列名应被过滤，全部非法时返回 nothing found。"""
        result = await repo_with_feed.query("invalid_col,drop_table", {}, False)
        assert result == "--- nothing found ---"

    async def test_query_limit_clamped(self, repo_with_feed: RSSToolRepository):
        """limit 应被限制在 [1, 100] 范围内。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 5)

        result = await repo_with_feed.query(
            "title", {"unread_only": True, "limit": 2}, False
        )
        # 应只返回 2 条
        assert result.count("------") == 2

    async def test_query_by_tag(self, repo_with_feed: RSSToolRepository):
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 1)

        result = await repo_with_feed.query(
            "title", {"tag": "tech", "unread_only": True}, False
        )
        assert "Title 0" in result

    async def test_query_by_nonexistent_tag(self, repo_with_feed: RSSToolRepository):
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 1)

        result = await repo_with_feed.query("title", {"tag": "nonexistent"}, False)
        assert result == "--- nothing found ---"

    async def test_query_by_since(self, repo_with_feed: RSSToolRepository):
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        await self._insert_items(repo_with_feed, feed.id, 1)

        before = datetime.fromtimestamp(int(time.time()) - 10, timezone.utc)
        after = datetime.fromtimestamp(int(time.time()) + 10, timezone.utc)

        result = await repo_with_feed.query(
            "title", {"since": before.isoformat()}, False
        )
        assert "Title 0" in result
        result = await repo_with_feed.query(
            "title", {"since": after.isoformat()}, False
        )
        assert result == "--- nothing found ---"

        result = await repo_with_feed.query(
            "title", {"since": before.isoformat().split("+")[0] + "Z"}, False
        )
        assert "Title 0" in result
        result = await repo_with_feed.query(
            "title", {"since": after.isoformat().split("+")[0] + "Z"}, False
        )
        assert result == "--- nothing found ---"


@pytest.mark.asyncio
class TestRepositoryUpdateFeed:
    """测试 Feed 抓取与更新逻辑。"""

    async def test_update_feed_skips_fresh(self, repo_with_feed: RSSToolRepository):
        """未到更新时间时应跳过抓取。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = int(time.time())  # 刚刚更新过
        result = await repo_with_feed.update_feed(feed)
        assert result == 0

    async def test_update_feed_force(self, repo_with_feed, sample_atom_xml):
        """force=True 时应忽略更新频率。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = int(time.time())  # 刚刚更新过

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"ETag": '"abc123"'}
        mock_response.content = MagicMock()
        mock_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "rss.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            result = await repo_with_feed.update_feed(feed, force=True)

        assert result == 2  # 2 entries in sample XML
        assert feed.etag == '"abc123"'  # ETag 应被保存
        assert feed.fail_count == 0  # 成功后应重置

    async def test_update_feed_304_not_modified(self, repo_with_feed):
        """304 响应应标记为最新但不添加条目。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0  # 强制需要更新

        mock_response = AsyncMock()
        mock_response.status = 304
        mock_response.headers = {}
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "rss.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            result = await repo_with_feed.update_feed(feed, force=True)

        assert result == 0
        assert feed.last_fetch_time > 0  # 应已更新时间戳
        assert feed.fail_count == 0  # 304 也算成功

    async def test_update_feed_network_error(self, repo_with_feed):
        """网络异常应返回 0 而非抛出异常。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0

        with patch(
            "rss.aiohttp.ClientSession",
            side_effect=aiohttp.ClientError("connection refused"),
        ):
            result = await repo_with_feed.update_feed(feed, force=True)
            assert result == 0


@pytest.mark.asyncio
class TestETagSupport:
    """测试 ETag 条件请求支持。"""

    async def test_etag_sent_in_request(self, repo_with_feed, sample_atom_xml):
        """已有 ETag 时应发送 If-None-Match 头。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0
        feed.etag = '"existing-etag"'

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"ETag": '"new-etag"'}
        mock_response.content = MagicMock()
        mock_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            await repo_with_feed.update_feed(feed, force=True)

        # 验证请求头包含 If-None-Match
        call_kwargs = mock_session.get.call_args
        headers = (
            call_kwargs.kwargs.get("headers", {})
            if call_kwargs.kwargs
            else call_kwargs[1].get("headers", {})
        )
        assert headers.get("If-None-Match") == '"existing-etag"'
        assert feed.etag == '"new-etag"'

    async def test_etag_persisted_to_db(self, repo_with_feed, sample_atom_xml):
        """ETag 应被持久化到数据库。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {"ETag": '"persisted-etag"'}
        mock_response.content = MagicMock()
        mock_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            await repo_with_feed.update_feed(feed, force=True)

        # 从数据库重新加载验证
        async with repo_with_feed.db.execute(
            "SELECT etag FROM feeds WHERE id = ?", (feed.id,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == '"persisted-etag"'


@pytest.mark.asyncio
class TestBackoff:
    """测试指数退避与 Retry-After 支持。"""

    async def test_failure_increments_fail_count(self, repo_with_feed):
        """HTTP 错误应增加 fail_count。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0
        assert feed.fail_count == 0

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.headers = {}
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            result = await repo_with_feed.update_feed(feed, force=True)

        assert result == 0
        assert feed.fail_count == 1
        assert feed.next_retry > int(time.time())

    async def test_backoff_skips_when_not_due(self, repo_with_feed):
        """未到重试时间时应跳过抓取。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0  # 需要更新
        feed.next_retry = int(time.time()) + 9999  # 远未到重试时间

        result = await repo_with_feed.update_feed(feed)
        assert result == 0

    async def test_backoff_force_ignores_retry(self, repo_with_feed, sample_atom_xml):
        """force=True 应忽略退避。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0
        feed.next_retry = int(time.time()) + 9999

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.content = MagicMock()
        mock_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            result = await repo_with_feed.update_feed(feed, force=True)

        assert result == 2  # 成功抓取
        assert feed.fail_count == 0  # 成功后重置
        assert feed.next_retry == 0

    async def test_success_resets_failure(self, repo_with_feed, sample_atom_xml):
        """成功抓取后应重置 fail_count 和 next_retry。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0
        feed.fail_count = 3
        feed.next_retry = int(time.time()) - 1  # 已过重试时间

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.content = MagicMock()
        mock_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            await repo_with_feed.update_feed(feed, force=True)

        assert feed.fail_count == 0
        assert feed.next_retry == 0

    async def test_retry_after_header_seconds(self, repo_with_feed):
        """Retry-After 为秒数时应使用较大值。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0

        mock_response = AsyncMock()
        mock_response.status = 429
        mock_response.headers = {"Retry-After": "3600"}
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            await repo_with_feed.update_feed(feed, force=True)

        assert feed.fail_count == 1
        # Retry-After 3600 秒 > 默认退避 60 秒，应使用 3600
        assert feed.next_retry >= int(time.time()) + 3500

    async def test_exponential_backoff_increases(self, repo_with_feed):
        """连续失败应指数增长退避时间。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0

        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.headers = {}
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        retries = []
        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            for _ in range(3):
                feed.next_retry = 0  # 允许重试
                await repo_with_feed.update_feed(feed, force=True)
                retries.append(feed.next_retry)

        assert feed.fail_count == 3
        # 退避应递增：60, 120, 240 秒
        assert retries[1] > retries[0]
        assert retries[2] > retries[1]


@pytest.mark.asyncio
class TestPermanentRedirect:
    """测试 301 永久重定向处理。"""

    async def test_301_updates_stored_url(self, repo_with_feed, sample_atom_xml):
        """301 应更新存储的 Feed URL。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.last_fetch_time = 0
        original_url = feed.config_site["url"]

        # 第一次请求返回 301
        mock_301_response = AsyncMock()
        mock_301_response.status = 301
        mock_301_response.headers = {"Location": "https://new.example.com/feed.xml"}
        mock_301_response.__aenter__ = AsyncMock(return_value=mock_301_response)
        mock_301_response.__aexit__ = AsyncMock(return_value=False)

        # 第二次请求（跟随重定向）返回 200
        mock_200_response = AsyncMock()
        mock_200_response.status = 200
        mock_200_response.headers = {"ETag": '"new-etag"'}
        mock_200_response.content = MagicMock()
        mock_200_response.content.read = AsyncMock(return_value=sample_atom_xml)
        mock_200_response.__aenter__ = AsyncMock(return_value=mock_200_response)
        mock_200_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=[mock_301_response, mock_200_response])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("rss.aiohttp.ClientSession", return_value=mock_session):
            result = await repo_with_feed.update_feed(feed, force=True)

        assert result == 2
        assert feed.config_site["url"] == "https://new.example.com/feed.xml"
        assert feed.config_site["url"] != original_url

        # 验证数据库中也更新了
        async with repo_with_feed.db.execute(
            "SELECT url FROM feeds WHERE id = ?", (feed.id,)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == "https://new.example.com/feed.xml"


@pytest.mark.asyncio
class TestConcurrencyLimit:
    """测试并发抓取限制。"""

    async def test_semaphore_exists(self, repo: RSSToolRepository):
        """Repository 应有 _fetch_semaphore 属性。"""
        import asyncio

        assert hasattr(repo, "_fetch_semaphore")
        assert isinstance(repo._fetch_semaphore, asyncio.Semaphore)


@pytest.mark.asyncio
class TestDbMigrationV2:
    """测试数据库迁移 v2 新增列。"""

    async def test_feeds_table_has_new_columns(self, repo: RSSToolRepository):
        """feeds 表应包含 etag, fail_count, next_retry 列。"""
        async with repo.db.execute("PRAGMA table_info(feeds)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        assert "etag" in columns
        assert "fail_count" in columns
        assert "next_retry" in columns

    async def test_db_version_is_2(self, repo: RSSToolRepository):
        """迁移后 db_version 应为 '2'。"""
        version = await repo._get_config("db_version")
        assert version == "2"

    async def test_new_columns_have_defaults(self, repo_with_feed: RSSToolRepository):
        """新列应有正确的默认值。"""
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        assert feed.etag == ""
        assert feed.fail_count == 0
        assert feed.next_retry == 0


@pytest.mark.asyncio
class TestRepositorySyncFeeds:
    """测试 sync_feeds 整体同步流程。"""

    async def test_sync_feeds_meta_builds_index(
        self, repo_with_feed: RSSToolRepository
    ):
        """sync_feeds_meta 应正确构建 feeds 和 tags 索引。"""
        assert "https://example.com/feed.xml" in repo_with_feed.feeds
        assert "tech" in repo_with_feed.tags
        assert "news" in repo_with_feed.tags

    async def test_next_sync_time_returns_future(
        self, repo_with_feed: RSSToolRepository
    ):
        """next_sync_time 应返回未来的时间。"""
        sync_time = repo_with_feed.next_sync_time()
        assert isinstance(sync_time, datetime)
        assert sync_time.timestamp() >= time.time()


@pytest.mark.asyncio
class TestPurgeOldItems:
    """测试 purge_old_items 定期清除旧条目功能。"""

    async def _insert_item(
        self,
        repo: RSSToolRepository,
        feed_id: int,
        link: str,
        published: int,
        unread: int = 1,
    ) -> None:
        """辅助方法：插入一条指定发布时间和已读状态的条目。"""
        await repo.db.execute(
            "INSERT INTO items (feed_id, title, link, published, unread) VALUES (?, ?, ?, ?, ?)",
            (feed_id, f"Title {link}", link, published, unread),
        )
        await repo.db.commit()

    async def test_purge_disabled_by_zero(self, repo_with_feed: RSSToolRepository):
        """cleanup_days=0 时不应清除任何条目。"""
        repo_with_feed.config["cleanup_days"] = 0
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        old_ts = int(time.time()) - 90 * 86400
        await self._insert_item(
            repo_with_feed, feed.id, "https://old/1", old_ts, unread=0
        )

        deleted = await repo_with_feed.purge_old_items()
        assert deleted == 0

        # 条目应仍然存在
        async with repo_with_feed.db.execute("SELECT COUNT(*) FROM items") as cur:
            row = await cur.fetchone()
            assert row
            assert row[0] == 1

    async def test_purge_read_items_older_than_n_days(
        self, repo_with_feed: RSSToolRepository
    ):
        """已读且超过 N 天的条目应被清除。"""
        repo_with_feed.config["cleanup_days"] = 30
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        old_ts = int(time.time()) - 31 * 86400
        recent_ts = int(time.time()) - 1 * 86400

        # 旧已读 — 应被清除
        await self._insert_item(
            repo_with_feed, feed.id, "https://old-read/1", old_ts, unread=0
        )
        # 新已读 — 不应被清除
        await self._insert_item(
            repo_with_feed, feed.id, "https://new-read/1", recent_ts, unread=0
        )
        # 旧未读（enabled feed）— 不应被清除
        await self._insert_item(
            repo_with_feed, feed.id, "https://old-unread/1", old_ts, unread=1
        )

        deleted = await repo_with_feed.purge_old_items()
        assert deleted == 1

        async with repo_with_feed.db.execute("SELECT COUNT(*) FROM items") as cur:
            row = await cur.fetchone()
            assert row
            assert row[0] == 2

    async def test_purge_unread_items_from_disabled_feed(
        self, repo_with_feed: RSSToolRepository
    ):
        """已禁用 Feed 中超过 N 天的未读条目应被清除。"""
        repo_with_feed.config["cleanup_days"] = 30
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        feed.config_site["enabled"] = False
        old_ts = int(time.time()) - 31 * 86400

        # 旧未读 + disabled feed — 应被清除
        await self._insert_item(
            repo_with_feed, feed.id, "https://old-unread-disabled/1", old_ts, unread=1
        )

        deleted = await repo_with_feed.purge_old_items()
        assert deleted == 1

        async with repo_with_feed.db.execute("SELECT COUNT(*) FROM items") as cur:
            row = await cur.fetchone()
            assert row
            assert row[0] == 0

    async def test_purge_keeps_unread_items_from_enabled_feed(
        self, repo_with_feed: RSSToolRepository
    ):
        """已启用 Feed 中超过 N 天的未读条目不应被清除。"""
        repo_with_feed.config["cleanup_days"] = 30
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        assert feed.config_site["enabled"] is True
        old_ts = int(time.time()) - 31 * 86400

        await self._insert_item(
            repo_with_feed, feed.id, "https://old-unread-enabled/1", old_ts, unread=1
        )

        deleted = await repo_with_feed.purge_old_items()
        assert deleted == 0

        async with repo_with_feed.db.execute("SELECT COUNT(*) FROM items") as cur:
            row = await cur.fetchone()
            assert row
            assert row[0] == 1

    async def test_purge_mixed_scenario(self, tmp_path):
        """混合场景：多个 Feed，不同 enabled 状态，不同已读状态。"""

        conf_path = tmp_path / "cfg.json"
        feeds_cfg = [
            {
                "__template_key": "site",
                "url": "https://enabled.com/feed",
                "enabled": True,
                "title": "Enabled Feed",
                "tags": [],
                "frequency_hours": 6,
            },
            {
                "__template_key": "site",
                "url": "https://disabled.com/feed",
                "enabled": False,
                "title": "Disabled Feed",
                "tags": [],
                "frequency_hours": 6,
            },
        ]
        default = {
            "allow_agents": True,
            "user_agent": "Test",
            "cleanup_days": 7,
            "feeds": feeds_cfg,
        }
        conf_path.write_text(json.dumps(default), encoding="utf-8")

        from astrbot.api import AstrBotConfig

        config = AstrBotConfig(config_path=str(conf_path), default_config=default)
        repo = RSSToolRepository(tmp_path / "test.db", config)
        await repo.initialize()

        enabled_feed = repo.feeds["https://enabled.com/feed"]
        disabled_feed = repo.feeds["https://disabled.com/feed"]
        old_ts = int(time.time()) - 8 * 86400
        recent_ts = int(time.time()) - 1 * 86400

        # enabled feed: 旧已读 → 清除
        await self._insert_item(
            repo, enabled_feed.id, "https://e/old-read", old_ts, unread=0
        )
        # enabled feed: 旧未读 → 保留
        await self._insert_item(
            repo, enabled_feed.id, "https://e/old-unread", old_ts, unread=1
        )
        # enabled feed: 新已读 → 保留
        await self._insert_item(
            repo, enabled_feed.id, "https://e/new-read", recent_ts, unread=0
        )
        # disabled feed: 旧未读 → 清除
        await self._insert_item(
            repo, disabled_feed.id, "https://d/old-unread", old_ts, unread=1
        )
        # disabled feed: 旧已读 → 清除
        await self._insert_item(
            repo, disabled_feed.id, "https://d/old-read", old_ts, unread=0
        )
        # disabled feed: 新未读 → 保留
        await self._insert_item(
            repo, disabled_feed.id, "https://d/new-unread", recent_ts, unread=1
        )

        deleted = await repo.purge_old_items()
        assert deleted == 3  # e/old-read + d/old-unread + d/old-read

        async with repo.db.execute("SELECT link FROM items ORDER BY link") as cur:
            remaining = [row[0] for row in await cur.fetchall()]
        assert remaining == [
            "https://d/new-unread",
            "https://e/new-read",
            "https://e/old-unread",
        ]

        await repo.close()

    async def test_purge_default_config_missing_key(
        self, repo_with_feed: RSSToolRepository
    ):
        """配置中无 cleanup_days 时应使用默认值 30 天。"""
        # 确保 config 中没有 cleanup_days 键
        repo_with_feed.config.pop("cleanup_days", None)
        feed = repo_with_feed.feeds["https://example.com/feed.xml"]
        old_ts = int(time.time()) - 91 * 86400

        await self._insert_item(
            repo_with_feed, feed.id, "https://default/1", old_ts, unread=0
        )

        # 默认 30 天，31 天前的已读条目应被清除
        deleted = await repo_with_feed.purge_old_items()
        assert deleted == 1
