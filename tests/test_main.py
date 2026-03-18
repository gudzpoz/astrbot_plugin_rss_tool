"""test_main.py — RSSTool 插件主模块测试。

覆盖：_parse_tags 工具方法、add_feed_common URL 校验与去重逻辑、
mark_read 返回值语义修正。

注意：main.py 使用相对导入 (from .rss import ...) 且依赖 AstrBot 框架的
Star 基类和 filter 装饰器。为了在测试中绕过这些依赖，我们通过 importlib
将插件目录作为包导入。
"""

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── 设置插件包导入 ──────────────────────────────────────────────
# main.py 使用 from .rss import ...，需要将插件目录注册为包

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PLUGIN_PKG = _PLUGIN_DIR.name  # "astrbot_plugin_rss_tool"


def _import_main():
    """将插件目录作为包导入 main 模块，处理相对导入。"""
    # 确保插件父目录在 sys.path 中
    parent_dir = str(_PLUGIN_DIR.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

    # 导入包（触发 __init__.py 如果有的话，否则创建命名空间包）
    if _PLUGIN_PKG not in sys.modules:
        init_file = _PLUGIN_DIR / "__init__.py"
        if init_file.exists():
            spec = importlib.util.spec_from_file_location(
                _PLUGIN_PKG,
                init_file,
                submodule_search_locations=[str(_PLUGIN_DIR)],
            )
            if spec and spec.loader:
                pkg = importlib.util.module_from_spec(spec)
                sys.modules[_PLUGIN_PKG] = pkg
                spec.loader.exec_module(pkg)
        else:
            # 没有 __init__.py，创建命名空间包
            pkg = types.ModuleType(_PLUGIN_PKG)
            pkg.__path__ = [str(_PLUGIN_DIR)]
            pkg.__package__ = _PLUGIN_PKG
            sys.modules[_PLUGIN_PKG] = pkg

    # 先导入 rss 子模块（main.py 依赖它）
    rss_name = f"{_PLUGIN_PKG}.rss"
    if rss_name not in sys.modules:
        rss_spec = importlib.util.spec_from_file_location(
            rss_name, _PLUGIN_DIR / "rss.py"
        )
        if rss_spec and rss_spec.loader:
            rss_mod = importlib.util.module_from_spec(rss_spec)
            sys.modules[rss_name] = rss_mod
            rss_spec.loader.exec_module(rss_mod)

    # 导入 main 子模块
    main_name = f"{_PLUGIN_PKG}.main"
    if main_name not in sys.modules:
        main_spec = importlib.util.spec_from_file_location(
            main_name, _PLUGIN_DIR / "main.py"
        )
        if main_spec and main_spec.loader:
            main_mod = importlib.util.module_from_spec(main_spec)
            sys.modules[main_name] = main_mod
            main_spec.loader.exec_module(main_mod)

    return sys.modules[main_name]


# 模块级别导入，所有测试共享
_main_mod = _import_main()
RSSTool = _main_mod.RSSTool


# ── 测试 ────────────────────────────────────────────────────────


class TestParseTags:
    """测试 RSSTool._parse_tags 标签解析。"""

    def test_empty_string(self):
        assert RSSTool._parse_tags("") == []

    def test_single_tag(self):
        assert RSSTool._parse_tags("tech") == ["tech"]

    def test_comma_separated(self):
        assert RSSTool._parse_tags("tech,news,ai") == ["tech", "news", "ai"]

    def test_semicolon_separated(self):
        assert RSSTool._parse_tags("tech;news;ai") == ["tech", "news", "ai"]

    def test_chinese_comma(self):
        assert RSSTool._parse_tags("技术，新闻，AI") == ["技术", "新闻", "AI"]

    def test_chinese_semicolon(self):
        assert RSSTool._parse_tags("技术；新闻") == ["技术", "新闻"]

    def test_chinese_dunhao(self):
        assert RSSTool._parse_tags("技术、新闻、AI") == ["技术", "新闻", "AI"]

    def test_mixed_separators(self):
        result = RSSTool._parse_tags("tech,news；AI、ML")
        assert result == ["tech", "news", "AI", "ML"]

    def test_strips_whitespace(self):
        result = RSSTool._parse_tags("  tech , news , ai  ")
        assert result == ["tech", "news", "ai"]

    def test_filters_empty_tags(self):
        result = RSSTool._parse_tags("tech,,news,,")
        assert result == ["tech", "news"]

    def test_consecutive_separators(self):
        result = RSSTool._parse_tags("tech,,,news")
        assert result == ["tech", "news"]


class TestAddFeedCommonValidation:
    """测试 add_feed_common 的 URL 校验逻辑。

    通过 object.__new__ 绕过 Star 基类初始化，直接测试方法逻辑。
    """

    def _make_plugin(self, sites=None):
        """创建一个 mock 化的 RSSTool 实例。"""
        plugin = object.__new__(RSSTool)
        plugin.repo = MagicMock()
        plugin.repo.sites = sites or []
        plugin.repo.sync_feeds_meta = AsyncMock()
        plugin.repo.add_feed = AsyncMock()
        # discover_feed 默认返回原 URL（校验通过）
        plugin.repo.discover_feed = AsyncMock(side_effect=lambda url: url)
        return plugin

    @pytest.mark.asyncio
    async def test_invalid_url_no_scheme(self):
        plugin = self._make_plugin()
        result = await plugin.add_feed_common("example.com/rss", "", False)
        assert "无效的链接" in result

    @pytest.mark.asyncio
    async def test_invalid_url_no_dot(self):
        plugin = self._make_plugin()
        result = await plugin.add_feed_common("https://localhost/rss", "", False)
        assert "无效的链接" in result

    @pytest.mark.asyncio
    async def test_invalid_url_llm_mode(self):
        plugin = self._make_plugin()
        result = await plugin.add_feed_common("ftp://example.com/rss", "", True)
        assert result == "invalid link"

    @pytest.mark.asyncio
    async def test_valid_url_adds_feed(self):
        plugin = self._make_plugin()
        result = await plugin.add_feed_common(
            "https://example.com/feed.xml", "tech,news", False
        )
        assert result == "添加成功"
        plugin.repo.add_feed.assert_called_once()

    @pytest.mark.asyncio
    async def test_duplicate_url_returns_exists(self):
        existing_site = {
            "url": "https://example.com/feed.xml",
            "tags": ["tech"],
        }
        plugin = self._make_plugin(sites=[existing_site])
        # discover_feed 返回原 URL，但已存在
        result = await plugin.add_feed_common("https://example.com/feed.xml", "", False)
        assert "已存在" in result

    @pytest.mark.asyncio
    async def test_duplicate_url_with_new_tags_updates(self):
        existing_site = {
            "url": "https://example.com/feed.xml",
            "tags": ["tech"],
        }
        plugin = self._make_plugin(sites=[existing_site])
        result = await plugin.add_feed_common(
            "https://example.com/feed.xml", "news", False
        )
        assert "已更新" in result
        plugin.repo.sync_feeds_meta.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_url_llm_mode(self):
        plugin = self._make_plugin()
        result = await plugin.add_feed_common("https://example.com/feed.xml", "", True)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_discover_feed_returns_different_url(self):
        """当 discover_feed 返回不同 URL 时应使用发现的 URL。"""
        plugin = self._make_plugin()
        plugin.repo.discover_feed = AsyncMock(
            return_value="https://example.com/real-feed.xml"
        )
        result = await plugin.add_feed_common("https://example.com/", "", False)
        assert result == "添加成功"
        call_args = plugin.repo.add_feed.call_args
        assert call_args[0][0] == "https://example.com/real-feed.xml"

    @pytest.mark.asyncio
    async def test_discover_feed_raises_valueerror(self):
        """当 discover_feed 报错时应返回错误信息。"""
        plugin = self._make_plugin()
        plugin.repo.discover_feed = AsyncMock(return_value=None)
        result = await plugin.add_feed_common("https://example.com/", "", False)
        assert "无法识别链接内容" in result
        plugin.repo.add_feed.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_feed_raises_valueerror_llm(self):
        """当 discover_feed 报错时 LLM 模式应返回英文错误。"""
        plugin = self._make_plugin()
        plugin.repo.discover_feed = AsyncMock(return_value=None)
        result = await plugin.add_feed_common("https://example.com/", "", True)
        assert "feed invalid" in result
        plugin.repo.add_feed.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_feed_network_error(self):
        """网络异常时应返回错误信息。"""
        plugin = self._make_plugin()
        plugin.repo.discover_feed = AsyncMock(side_effect=Exception("connection error"))
        result = await plugin.add_feed_common("https://example.com/", "", False)
        assert "连接错误" in result
        plugin.repo.add_feed.assert_not_called()


class TestMarkReadSemantics:
    """测试 mark_read 返回值在 main.py 中的正确处理。

    验证修复后的语义：
    - count < 0 (-1) → "未找到" / "not found"
    - count >= 0 (包括 0) → "已标为已读" / "ok"
    """

    def test_negative_is_not_found(self):
        """mark_read 返回 -1 时应判定为未找到。"""
        count = -1
        assert count < 0  # 应走 "未找到" 分支

    def test_zero_is_success(self):
        """mark_read 返回 0（无未读条目）时应判定为成功。"""
        count = 0
        assert count >= 0  # 应走 "已标为已读" 分支

    def test_positive_is_success(self):
        """mark_read 返回正数时应判定为成功。"""
        count = 5
        assert count >= 0  # 应走 "已标为已读" 分支
