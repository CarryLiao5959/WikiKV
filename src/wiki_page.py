"""Wiki 页面模型 — 解析 Markdown 文件，提取 frontmatter、wikilinks、元信息。"""

import re
import unicodedata
import yaml
from dataclasses import dataclass, field
from pathlib import Path

import config

WIKILINK_PATTERN = re.compile(r'\[\[([^\]]+)\]\]')


def _normalize_width(s: str) -> str:
    """全角→半角归一化（NFKC）+ 零宽字符移除 + 多空格压缩，用于链接名匹配。

    LLM 生成的链接可能使用全角标点（如 ？ ！ ：），而文件名使用半角（? ! :）。
    通过 NFKC 归一化消除这种差异，避免误判为断链。
    同时移除零宽字符（U+200B/200C/200D/FEFF/00AD），这些不可见字符来源于
    原始 CSV 数据，会导致文件名/链接匹配失败。
    LLM 生成的链接可能保留原始标题中的多个连续空格，而文件名只保留单空格，
    通过多空格压缩消除这种差异。
    """
    s = re.sub(r'[\u200b\u200c\u200d\ufeff\u00ad]', '', s)
    s = re.sub(r' +', ' ', s)  # 多空格→单空格
    return unicodedata.normalize('NFKC', s)


def _extract_page_name(link: str) -> str:
    """从 wikilink 目标中提取页面名。

    支持多种格式：
    - [[贝多芬]] → "贝多芬"
    - [[composers/贝多芬]] → "贝多芬"（取最后一个 / 后的部分）
    - [[sources/digests/2024-01-01-xxx]] → "2024-01-01-xxx"
    - [[上海 | Shanghai]] → "上海"（| 前为目标页面名，| 后为显示文本）
    - [[destinations/上海 | Shanghai]] → "上海"
    - [[2026-04-16-新宠打造|太出圈了]] → "2026-04-16-新宠打造|太出圈了"（文件名含|，不截断）

    注意：只有 ' | '（两边有空格）才视为 Obsidian 显示文本分隔符。
    文件名中的 '|'（无空格包围）保留原样。
    """
    if ' | ' in link:
        link = link.split(' | ', 1)[0].strip()
    if '/' in link:
        return link.rsplit('/', 1)[-1]
    return link


@dataclass
class WikiPage:
    """单个 Wiki 页面。"""
    path: Path
    name: str = ""                           # 文件名（不含 .md）
    page_type: str = ""                      # 动态类型，由目录名决定
    content: str = ""                        # 完整文本
    frontmatter: dict = field(default_factory=dict)
    outgoing_links: set = field(default_factory=set)   # 出链 [[xxx]]
    tags: set = field(default_factory=set)

    def __post_init__(self):
        self.name = self.path.stem
        if self.path.exists():
            self._parse()

    def _parse(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            self.content = ""
            return
        self.content = text
        self._parse_frontmatter(text)
        self._parse_links(text)

    def _parse_frontmatter(self, text: str):
        """提取 YAML frontmatter。"""
        result = config.split_frontmatter(text)
        if result is not None:
            _, fm_text, _ = result
            try:
                fm = yaml.safe_load(fm_text)
            except yaml.YAMLError:
                fm = self._retry_yaml_with_quoted_flow(fm_text)
            if isinstance(fm, dict):
                self.frontmatter = fm
                self.page_type = fm.get("type", "") or self._infer_type_from_path()
                tags = fm.get("tags", [])
                if isinstance(tags, list):
                    self.tags = set(tags)

    @staticmethod
    def _retry_yaml_with_quoted_flow(fm_text: str):
        """对 YAML flow sequence [a, b, c] 中的值加双引号后重试解析。

        处理如 [你刚才说了足球?, >>P按钮] 等含 YAML 特殊字符的值。
        """
        def _quote_flow_values(match):
            inner = match.group(1)
            items = []
            for item in inner.split(','):
                item = item.strip()
                if item and not (item.startswith('"') and item.endswith('"')):
                    item = '"' + item.replace('\\', '\\\\').replace('"', '\\"') + '"'
                items.append(item)
            return '[' + ', '.join(items) + ']'

        fixed = re.sub(r'\[([^\[\]]+)\]', _quote_flow_values, fm_text)
        try:
            return yaml.safe_load(fixed)
        except yaml.YAMLError:
            return None

    def _infer_type_from_path(self) -> str:
        """当 frontmatter 缺少 type 字段时，从文件路径推导 page_type。

        规则：
        - sources/ 下的所有子目录 → "source"
        - syntheses/ → "synthesis"
        - 其他目录 → 目录名（如 composers, works 等）
        """
        try:
            rel = self.path.relative_to(config.WIKI_DIR)
            parts = rel.parts
            if parts and parts[0] == "sources":
                return "source"
            if parts and parts[0] == "syntheses":
                return "synthesis"
            if len(parts) >= 2:
                return parts[0]
        except ValueError:
            pass
        return ""

    def _parse_links(self, text: str):
        """提取所有 [[wikilink]]。"""
        self.outgoing_links = set(WIKILINK_PATTERN.findall(text))

    @property
    def category(self) -> str:
        """返回页面所在的子目录名。"""
        return self.path.parent.name

    @property
    def summary_line(self) -> str:
        """生成 index.md 中的摘要行。"""
        tags_str = ", ".join(sorted(self.tags)[:3]) if self.tags else ""
        return f"- [[{self.name}]] — {tags_str}" if tags_str else f"- [[{self.name}]]"


class WikiGraph:
    """Wiki 知识图谱 — 管理所有页面和它们之间的关系。

    关于 `pages` 字典的说明：
    - key 仍然是 page.name（不含 .md 的文件 stem），兼容所有调用方用 name 查找。
    - 当同名冲突时（例如 sources/articles/X.md 与 sources/digests/X.md 同名），
      知识页优先，sources/digests 次之，sources/articles 最次。被挤掉的页面
      仍保存在 `all_pages` 中，参与链接/反向链接/断链/孤立检测。
    - `_index.md` 不进 `pages`（文件名都叫 _index，没法用 name 做 key），
      但会被加载到 `index_pages` 里，其出链参与 incoming_links 统计。
    """

    _PRIORITY_SOURCE_ARTICLE = 30  # 最低，冲突时被挤掉
    _PRIORITY_SOURCE_DIGEST = 20
    _PRIORITY_SYNTHESIS = 10
    _PRIORITY_KNOWLEDGE = 0        # 最高，知识页永远保留

    def __init__(self):
        self.pages: dict[str, WikiPage] = {}  # name → WikiPage（冲突时按优先级保留）
        self.all_pages: list[WikiPage] = []   # 所有非 _index 页面（含被冲突挤掉的）
        self.index_pages: list[WikiPage] = [] # 所有 _index.md 页面
        self._by_relpath: dict[str, WikiPage] = {}  # 相对路径(去掉.md) → WikiPage

    def _priority(self, page: WikiPage) -> int:
        rel = str(page.path.relative_to(config.WIKI_DIR))
        if rel.startswith("sources/articles/"):
            return self._PRIORITY_SOURCE_ARTICLE
        if rel.startswith("sources/digests/"):
            return self._PRIORITY_SOURCE_DIGEST
        if rel.startswith("syntheses/"):
            return self._PRIORITY_SYNTHESIS
        return self._PRIORITY_KNOWLEDGE

    def load_all(self):
        """加载 wiki/ 下所有 .md 页面。

        - `_index.md` 单独存到 `index_pages`（它们是关键的反向引用来源）
        - 其他文件都进 `all_pages`，并按优先级填入 `pages` 字典
        - 同名冲突时，优先级高的保留在 `pages`，其他仍在 `all_pages` 和 `_by_relpath`
        """
        skip = {"index", "overview", "log", "lint-report"}
        for md_file in config.WIKI_DIR.rglob("*.md"):
            if md_file.stem in skip:
                continue
            try:
                page = WikiPage(path=md_file)
                rel_key = str(md_file.relative_to(config.WIKI_DIR))[:-3]  # 去 .md
            except (OSError, ValueError):
                continue  # 文件读取失败或路径异常 → 跳过
            self._by_relpath[rel_key] = page

            if md_file.stem == "_index":
                self.index_pages.append(page)
                continue

            self.all_pages.append(page)
            existing = self.pages.get(page.name)
            if existing is None or self._priority(page) < self._priority(existing):
                self.pages[page.name] = page

    def get_page(self, name: str) -> WikiPage | None:
        """按页面名查找，兼容 [[目录名/页面名]] 路径格式。

        - 简单名 "贝多芬" → 走 `pages`（知识页优先）
        - 路径 "composers/贝多芬"、"sources/digests/xxx" → 先查 `_by_relpath`
          精确路径匹配，命中更快更准；fallback 走原有的逻辑。
        - 全角/半角差异：LLM 可能生成全角标点的链接名（如"？""！"），
          而文件名用半角。精确匹配失败后尝试 NFKC 归一化匹配。
        - 别名格式 "上海 | Shanghai" → 取 | 前面的部分作为目标页面名
        """
        if ' | ' in name:
            name = name.split(' | ', 1)[0].strip()
        if '/' in name:
            exact = self._by_relpath.get(name)
            if exact is not None:
                return exact
            page_name = _extract_page_name(name)
            if page_name in self.pages:
                page = self.pages[page_name]
                expected_dirs = name.rsplit('/', 1)[0]
                actual_dir = str(page.path.relative_to(config.WIKI_DIR).parent)
                if actual_dir == expected_dirs:
                    return page
            norm = self._by_relpath.get(_normalize_width(name))
            if norm is not None:
                return norm
            norm_page_name = _normalize_width(page_name)
            for key, page in self.pages.items():
                if _normalize_width(key) == norm_page_name:
                    if '/' in name:
                        expected_dirs = name.rsplit('/', 1)[0]
                        actual_dir = str(page.path.relative_to(config.WIKI_DIR).parent)
                        if _normalize_width(actual_dir) == _normalize_width(expected_dirs):
                            return page
                    else:
                        return page
            return None
        if name in self.pages:
            return self.pages[name]
        norm = _normalize_width(name)
        for key, page in self.pages.items():
            if _normalize_width(key) == norm:
                return page
        return None

    def find_page_by_alias(self, name: str) -> WikiPage | None:
        """通过别名/外文名/不同译名查找页面。"""
        if name in self.pages:
            return self.pages[name]
        for page in self.pages.values():
            aliases = page.frontmatter.get("aliases", [])
            if isinstance(aliases, list) and name in aliases:
                return page
        return None

    def _all_link_sources(self):
        """所有出链来源页面（含被同名冲突挤掉的 + _index.md）。"""
        yield from self.all_pages
        yield from self.index_pages

    def get_incoming_links(self, name: str) -> set[str]:
        """获取所有指向 name 的页面。

        链接格式为 [[目录名/页面名]]，提取页面名后与 name 比较。
        会遍历所有源页面（含 _index.md 与被同名冲突挤掉的 source），
        确保 orphan 判定准确。
        """
        result = set()
        for p in self._all_link_sources():
            for link in p.outgoing_links:
                if _extract_page_name(link) == name:
                    ident = str(p.path.relative_to(config.WIKI_DIR))[:-3]
                    result.add(ident)
                    break
        return result

    def get_orphan_pages(self) -> list[str]:
        """获取无入站链接的页面。"""
        orphans = []
        for name in self.pages:
            if not self.get_incoming_links(name):
                orphans.append(name)
        return orphans

    def get_broken_links(self) -> list[tuple[str, str]]:
        """获取断裂链接：(源页面, 目标不存在）。兼容路径格式链接。"""
        broken = []
        for page in self._all_link_sources():
            for link in page.outgoing_links:
                if self.get_page(link) is None:
                    broken.append((page.name, link))
        return broken

    def get_pages_by_type(self, page_type: str) -> list[WikiPage]:
        return [p for p in self.pages.values() if p.page_type == page_type]

    @property
    def stats(self) -> dict:
        type_counts = {}
        for p in self.pages.values():
            t = p.page_type or "unknown"
            type_counts[t] = type_counts.get(t, 0) + 1
        total_links = sum(len(p.outgoing_links) for p in self.pages.values())
        return {
            "total_pages": len(self.pages),
            "by_type": type_counts,
            "total_links": total_links,
            "avg_links": total_links / max(len(self.pages), 1),
            "orphans": len(self.get_orphan_pages()),
            "broken_links": len(self.get_broken_links()),
        }
