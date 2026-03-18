"""conftest.py — 测试共享 fixtures。

提供 RSSToolRepository 的异步 fixture，使用临时目录下的真实 SQLite 文件。
"""

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# 将插件目录加入 sys.path，使 astrbot_plugin_rss_tool 可作为顶层包导入
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from astrbot.api import AstrBotConfig  # noqa: E402


def _make_config(tmp_path: Path, feeds: list | None = None) -> AstrBotConfig:
    """创建一个基于临时文件的 AstrBotConfig 实例。

    Args:
        tmp_path: pytest 提供的临时目录。
        feeds: 初始 feeds 列表，默认为空。

    Returns:
        可用于 RSSToolRepository 的 AstrBotConfig 实例。
    """
    conf_path = tmp_path / "test_config.json"
    default = {
        "allow_agents": True,
        "user_agent": "TestAgent",
        "feeds": feeds or [],
    }
    conf_path.write_text(json.dumps(default), encoding="utf-8")
    return AstrBotConfig(config_path=str(conf_path), default_config=default)


@pytest.fixture
def make_config(tmp_path: Path):
    """返回一个工厂函数，用于创建测试用 AstrBotConfig。"""

    def factory(feeds: list | None = None) -> AstrBotConfig:
        return _make_config(tmp_path, feeds)

    return factory


@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    """创建一个已初始化的 RSSToolRepository，使用临时目录下的真实 SQLite 文件。

    测试结束后自动关闭数据库连接。
    """
    from rss import RSSToolRepository

    db_path = tmp_path / "test_rss.db"
    config = _make_config(tmp_path)
    repository = RSSToolRepository(db_path, config)
    await repository.initialize()
    yield repository
    await repository.close()


@pytest_asyncio.fixture
async def repo_with_feed(tmp_path: Path):
    """创建一个已初始化且包含一条 Feed 订阅的 RSSToolRepository。"""
    from rss import RSSToolRepository

    db_path = tmp_path / "test_rss.db"
    feeds = [
        {
            "__template_key": "site",
            "url": "https://example.com/feed.xml",
            "enabled": True,
            "title": "Example Feed",
            "tags": ["tech", "news"],
            "frequency_hours": 6,
        }
    ]
    config = _make_config(tmp_path, feeds)
    repository = RSSToolRepository(db_path, config)
    await repository.initialize()
    yield repository
    await repository.close()


# ── 用于 main.py 测试的 mock fixtures ──────────────────────────

SAMPLE_ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Feed</title>
  <link href="https://example.com"/>
  <entry>
    <title>Test Entry 1</title>
    <link href="https://example.com/entry1"/>
    <published>2025-01-01T00:00:00Z</published>
    <author><name>Author1</name></author>
    <summary>Summary of entry 1</summary>
    <content type="text/html">&lt;p&gt;Content of entry 1&lt;/p&gt;</content>
  </entry>
  <entry>
    <title>Test Entry 2</title>
    <link href="https://example.com/entry2"/>
    <published>2025-01-02T00:00:00Z</published>
    <author><name>Author2</name></author>
    <summary>Summary of entry 2</summary>
    <content type="text/html">&lt;p&gt;Content of entry 2&lt;/p&gt;</content>
  </entry>
</feed>
"""


@pytest.fixture
def sample_atom_xml() -> bytes:
    """返回一段用于测试的 Atom Feed XML。"""
    return SAMPLE_ATOM_XML
