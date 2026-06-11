"""Ingest 引擎 — 批量摄入。

支持单篇和批量模式。批量模式下每批 N 篇文章合并一次 LLM 调用，
避免多篇写入 _index.md 时互相覆盖。
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, date as date_type
from pathlib import Path

import config


def _is_pruning_disabled() -> bool:
    """是否关闭 Step 2 输入裁剪（消融实验 w/o Pruning 模式）。"""
    if os.environ.get("WIKI_DISABLE_PRUNING", "").strip() in ("1", "true", "True", "yes"):
        return True
    cur = config.get_current_user() if hasattr(config, "get_current_user") else None
    if cur and cur.endswith("_no_pruning"):
        return True
    return False

def _robust_fm_split(text):
    """兼容 text.split('---', 2) 的返回格式，但使用行首 --- 匹配。
    
    返回 [before, fm_text, body] 列表（与 split 返回格式一致），
    如果没有合法 frontmatter 则返回 [text]（与 split 行为一致）。
    """
    result = config.split_frontmatter(text)
    if result is None:
        return [text]
    return list(result)


def _yaml_quote_value(value: str) -> str:
    """对包含 YAML 特殊字符的字符串值加双引号，防止 yaml.safe_load 解析失败。

    YAML 中冒号 `:` 后跟空格会被解析为 key-value 分隔符，
    `#` 后跟空格会被解析为注释，`[` `]` `{` `}` 是流式语法。
    如果值中包含这些字符，需要用引号包裹。
    """
    if not value:
        return value
    if (value.startswith('"') and value.endswith('"')) or \
       (value.startswith("'") and value.endswith("'")):
        return value
    needs_quote = False
    if ': ' in value or value.endswith(':'):
        needs_quote = True
    elif '# ' in value or value.startswith('#'):
        needs_quote = True
    elif any(ch in value for ch in '[]{}'):
        needs_quote = True
    if needs_quote:
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    return value


from config import ensure_wiki_dirs, register_page_type, apply_dir_changes, get_page_types, get_page_dirs, get_all_dir_info
from llm_client import call_llm, call_llm_json


class _TeeHandler(logging.StreamHandler):
    """将日志写到文件（只写文件，不写 stdout — stdout 由 print tee 负责）。"""
    def __init__(self, log_path: Path):
        super().__init__()
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(log_path, "a", encoding="utf-8")

    def emit(self, record):
        msg = self.format(record) + self.terminator
        self._file.write(msg)
        self._file.flush()

    def close(self):
        if self._file and not self._file.closed:
            self._file.close()


_logger = logging.getLogger("ingest")
_logger.setLevel(logging.INFO)


def setup_run_logger():
    """为本次 ingest 运行设置详细日志文件。

    日志保存到 {wiki}/logs/YYYY-MM-DD_HHMMSS.log
    同时接入 ingest.llm logger，记录 LLM 调用详情。

    机制：
    - builtins.print 被 tee 到 stdout + 日志文件（覆盖所有 print 输出）
    - logging.getLogger("ingest.*") 的 handler 只写日志文件
    - 两者共享同一个文件句柄，确保所有输出都在同一文件中
    """
    log_dir = getattr(config, 'INGEST_LOG_DIR', config.BASE_DIR / "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_path = log_dir / f"{timestamp}.log"

    _logger.handlers.clear()

    handler = _TeeHandler(log_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)

    llm_logger = logging.getLogger("ingest.llm")
    llm_logger.handlers.clear()
    llm_logger.setLevel(logging.INFO)
    llm_logger.propagate = False  # 避免父 logger ingest 重复处理
    file_only_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_only_handler.setFormatter(logging.Formatter("%(message)s"))
    llm_logger.addHandler(file_only_handler)

    if not hasattr(setup_run_logger, '_original_print'):
        setup_run_logger._original_print = print

    _orig_print = setup_run_logger._original_print

    class _PrintTee:
        def __init__(self, log_file, orig_print):
            self.log_file = log_file
            self.orig_print = orig_print

        def __call__(self, *args, **kwargs):
            self.orig_print(*args, **kwargs)
            try:
                msg = " ".join(str(a) for a in args)
                self.log_file.write(msg + "\n")
                self.log_file.flush()
            except Exception:
                pass

    import builtins
    builtins.print = _PrintTee(handler._file, _orig_print)

    print(f"📝 运行日志: {log_path}")
    return log_path

_graph_cache: "WikiGraph | None" = None


def load_cache() -> dict:
    if config.CACHE_FILE.exists():
        try:
            return json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            print(f"  ⚠️ 缓存文件损坏，将使用空缓存: {config.CACHE_FILE}")
            return {}
    return {}


def save_cache(cache: dict):
    config.CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _wfs_mirror_write(local_path: Path, content: str | None = None) -> None:
    """将本地 wiki 文件同步镜像写入 WFS 目录。

    Args:
        local_path: 本地文件的绝对路径（必须在 config.WIKI_DIR 下）
        content: 文件内容字符串；为 None 时表示二进制复制（用于 shutil.copy2 场景）
    """
    wfs_wiki_dir = getattr(config, "WFS_WIKI_DIR", None)
    if not wfs_wiki_dir:
        return  # 未设置用户或 WFS 未配置，跳过

    try:
        rel = local_path.relative_to(config.WIKI_DIR)
    except ValueError:
        return  # 不在 WIKI_DIR 下的文件不镜像

    wfs_path = Path(wfs_wiki_dir) / rel
    try:
        wfs_path.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            wfs_path.write_text(content, encoding="utf-8")
        else:
            import shutil as _shutil
            _shutil.copy2(local_path, wfs_path)
    except Exception as e:
        print(f"  ⚠️ WFS 镜像写入失败 {rel}: {e}")


def _wfs_mirror_delete(local_path: Path) -> None:
    """将本地 wiki 文件/目录的删除操作同步到 WFS 镜像。

    Args:
        local_path: 本地文件或目录的绝对路径（必须在 config.WIKI_DIR 下）
    """
    wfs_wiki_dir = getattr(config, "WFS_WIKI_DIR", None)
    if not wfs_wiki_dir:
        return

    try:
        rel = local_path.relative_to(config.WIKI_DIR)
    except ValueError:
        return

    wfs_path = Path(wfs_wiki_dir) / rel
    try:
        if wfs_path.is_file():
            wfs_path.unlink()
        elif wfs_path.is_dir():
            import shutil as _shutil
            _shutil.rmtree(wfs_path, ignore_errors=True)
    except Exception as e:
        print(f"  ⚠️ WFS 镜像删除失败 {rel}: {e}")


def _wfs_mirror_move(src_local: Path, dst_local: Path) -> None:
    """将本地 wiki 文件的移动操作同步到 WFS 镜像。

    Args:
        src_local: 源文件的本地绝对路径
        dst_local: 目标文件的本地绝对路径
    """
    wfs_wiki_dir = getattr(config, "WFS_WIKI_DIR", None)
    if not wfs_wiki_dir:
        return

    try:
        src_rel = src_local.relative_to(config.WIKI_DIR)
        dst_rel = dst_local.relative_to(config.WIKI_DIR)
    except ValueError:
        return

    wfs_src = Path(wfs_wiki_dir) / src_rel
    wfs_dst = Path(wfs_wiki_dir) / dst_rel
    try:
        if wfs_src.exists():
            wfs_dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.move(str(wfs_src), str(wfs_dst))
    except Exception as e:
        print(f"  ⚠️ WFS 镜像移动失败 {src_rel} → {dst_rel}: {e}")


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()



def _get_graph() -> "WikiGraph":
    """获取 WikiGraph 实例（缓存，写入后需 invalidate）。"""
    global _graph_cache
    if _graph_cache is None:
        from wiki_page import WikiGraph
        _graph_cache = WikiGraph()
        _graph_cache.load_all()
    return _graph_cache


def invalidate_graph_cache():
    """写入 Wiki 文件后调用，使缓存失效。"""
    global _graph_cache
    _graph_cache = None


def _read_file_safe(path: Path, max_len: int = 5000) -> str:
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "(读取失败)"
        return text[:max_len] if len(text) > max_len else text
    return "(空)"


def _get_existing_page_names(
    expand_dirs: set[str] | None = None,
    hot_names: set[str] | None = None,
) -> str:
    """获取已有知识页面名+别名列表，供去重/合并参考。

    输出为紧凑格式：
        【dir】
          主名 | 别名1, 别名2
          主名2
        【dir2】(共 N 页，本批未选中)
    使用时统一写成 `[[dir/主名]]`。

    参数：
      expand_dirs: 需要完整展开页面名的目录集合（通常由阶段 1 选中页面所在目录推导得到）。
                   白名单外的目录只给出"目录名 + 页面数"的一行摘要。
                   传 None 表示不裁剪、全部展开。
      hot_names:   本批文章粗抽出的候选实体词集合。只有主名或任一别名落在其中的页面
                   才展开 aliases；未命中的页面只列主名，显著压缩 prompt。
                   传 None 表示保留所有 aliases（向后兼容）。
    """
    graph = _get_graph()
    if not graph.pages:
        return "(暂无页面)"
    by_dir = {}
    for name, page in graph.pages.items():
        pt = page.page_type or "unknown"
        by_dir.setdefault(pt, []).append(page)
    lines = []
    for dir_name, pages in sorted(by_dir.items()):
        if dir_name in ("sources", "syntheses"):
            continue  # 来源页和沉淀页不需要列出去重
        if expand_dirs is not None and dir_name not in expand_dirs:
            lines.append(f"【{dir_name}】(共 {len(pages)} 页，本批未选中；如需跨领域去重请明示)")
            continue
        block = [f"【{dir_name}】"]
        for p in sorted(pages, key=lambda x: x.name):
            aliases_raw = p.frontmatter.get("aliases", []) or []
            aliases = [str(a) for a in aliases_raw] if isinstance(aliases_raw, list) else []
            if aliases and hot_names is not None:
                probe = {p.name, *aliases}
                mentioned = bool(probe & hot_names)
            else:
                mentioned = True  # 没传 hot_names 时保留所有别名
            if aliases and mentioned:
                block.append(f"  {p.name} | {', '.join(aliases)}")
            else:
                block.append(f"  {p.name}")
        lines.append("\n".join(block))
    return "\n".join(lines) if lines else "(暂无知识页)"


_ALWAYS_EXPAND_DIRS: set[str] = {"topics"}


def _expand_dirs_from_selected(selected_pages: list[str] | None) -> set[str] | None:
    """根据阶段 1 选中的页面路径，推导阶段 2 需要完整展开的目录白名单。
    selected_pages 为空/None 时返回 None（不裁剪，全量展开）。

    消融实验 w/o Pruning 模式：直接返回 None，强制展开所有目录。
    """
    if _is_pruning_disabled():
        return None
    if not selected_pages:
        return None
    dirs = {p.split("/", 1)[0] for p in selected_pages if "/" in p}
    if not dirs:
        return None
    return dirs | _ALWAYS_EXPAND_DIRS


_CAND_CJK_RE = re.compile(r"[\u4e00-\u9fa5]{2,15}")
_CAND_LATIN_RE = re.compile(r"\b[A-Z][A-Za-z.\-]{1,30}\b")


def _extract_candidate_names(articles: list[dict]) -> set[str]:
    """从文章标题 + 正文里粗提候选专名：
      - 连续中文 2~15 字
      - 首字母大写的拉丁词（含 . -，可覆盖 J.S.Bach / Jacob-Collier 等）
    返回候选词集合，供 _get_existing_page_names 的 hot_names 过滤 aliases。

    消融实验 w/o Pruning 模式：返回 None，让所有页面 aliases 都展开。
    """
    if _is_pruning_disabled():
        return None  # type: ignore[return-value]
    if not articles:
        return set()
    blobs = []
    for a in articles:
        blobs.append(str(a.get("title", "")))
        blobs.append(str(a.get("content", "")))
    text = "\n".join(blobs)
    names: set[str] = set()
    names.update(_CAND_CJK_RE.findall(text))
    names.update(_CAND_LATIN_RE.findall(text))
    return names


def _get_error_constraints() -> str:
    """从错题本加载活跃约束规则，用于注入 prompt。"""
    try:
        from error_book import get_active_constraints
        return get_active_constraints()
    except ImportError:
        return ""


def _count_knowledge_pages() -> int:
    """统计当前知识页数量（不含 sources/syntheses）。"""
    graph = _get_graph()
    return sum(1 for p in graph.pages.values() if p.page_type not in ("source", "sources", "synthesis", "syntheses"))


def _get_selected_pages_content(page_names: list[str], max_total_chars: int = 100000) -> str:
    """根据 LLM 选中的页面名列表，获取这些页面的完整内容。
    
    Args:
        page_names: LLM 选中的页面名列表
        max_total_chars: 页面内容总字符数上限（极端安全阀），仅当总量远超预期时报警
    """
    graph = _get_graph()
    parts = []
    total_chars = 0
    for name in page_names:
        page = graph.get_page(name)
        if not page:
            page = graph.find_page_by_alias(name)
        if not page:
            for pname, ppage in graph.pages.items():
                if name in pname or pname in name:
                    page = ppage
                    break
        if page:
            content = f"### {page.page_type}/{page.name}.md\n{page.content}"
            parts.append(content)
            total_chars += len(content)
    if total_chars > max_total_chars:
        print(f"  ⚠️ 选中页面内容总量 {total_chars} chars 超过安全阀 {max_total_chars}，请注意 prompt 长度")
    return "\n\n".join(parts) if parts else "(未找到选中的页面)"


def _get_all_pages_content(max_total_chars: int = 300_000) -> str:
    """消融实验 w/o Pruning 模式：把所有知识页内容（不含 sources/syntheses）全塞入 prompt。

    max_total_chars 设为 300_000（≈85k tokens）：
      - luxun 平均页面 3500 chars，约可容纳 85/156 页
      - 超过后截断 → LLM 看不到后半段页面 → 开始重复创建 → 页面膨胀加速
      - 相比之下 Pruning 模式只塞选中页面（通常 3-5 页），对比效果显著
      - 可通过 WIKI_NO_PRUNING_MAX_CHARS 环境变量覆盖
    """
    max_total_chars = int(os.environ.get("WIKI_NO_PRUNING_MAX_CHARS", max_total_chars))
    graph = _get_graph()
    parts = []
    total_chars = 0
    truncated = False
    for name in sorted(graph.pages.keys()):
        page = graph.pages[name]
        pt = (page.page_type or "").split("/", 1)[0]
        if pt in ("sources", "source", "syntheses", "synthesis"):
            continue
        content = f"### {page.page_type}/{page.name}.md\n{page.content}"
        if total_chars + len(content) > max_total_chars:
            truncated = True
            break
        parts.append(content)
        total_chars += len(content)
    if truncated:
        print(f"  ⚠️ [w/o Pruning] 全量页面内容超过 {max_total_chars} chars 安全阀，已截断")
    else:
        print(f"  📋 [w/o Pruning] 全量页面内容 {len(parts)} 页 / {total_chars} chars 全部塞入 prompt")
    return "\n\n".join(parts) if parts else "(暂无知识页)"




def build_select_pages_for_ingest_prompt(
    source_title: str, source_content: str
) -> tuple[str, str]:
    """第一步：让 LLM 读文章全文 + _index.md 目录索引，选出需要查看/更新的已有页面。"""

    index_contents = _get_all_index_content()
    index_text = ""
    for path, content in index_contents.items():
        index_text += f"\n### {path}\n{content}\n"
    if not index_text:
        index_text = "(暂无索引)"

    purpose_content = _read_file_safe(config.get_purpose_file(), 2000)
    if not purpose_content:
        purpose_content = "(暂无研究方向文件)"

    if len(source_content) > config.INGEST_MAX_CONTENT_LEN:
        source_content = source_content[:config.INGEST_MAX_CONTENT_LEN] + "\n...(内容已截断)"

    system = "你是知识库维护专家。请根据author研究方向、源文档全文和已有目录索引，先判断源文档是否有知识价值，再判断需要查看/更新哪些已有 Wiki 页面。"

    user = f"""## author研究方向（判断是否跳过时必须参考）
{purpose_content}

标题：{source_title}
全文：
{source_content}

{index_text}


这篇源文档（source article或video source内容）即将被摄入知识库。请先判断内容质量，再选择需要查看的页面。


⚠️ **最高优先级规则：参考上方"author研究方向"**
如果author的研究方向/摄入侧重表明其内容天然以某种特定形式呈现（如语录合集、文案分享、推广文案、活动规则、价格套餐等），则该形式的内容**不应被跳过**，而应视为该author的核心知识。例如：
- 语录/句子号：每条语录都是author精选的内容，是核心产出
- 游戏代练号：价格表、活动规则、赛季奖励是核心知识
- 电商导购号：产品推荐、价格对比、使用体验是核心知识
只有当内容**既不符合author研究方向，又属于下列无价值类型**时，才应跳过。

以下类型的源文档通常没有知识价值，应该跳过（输出 `"skip": true`）：
- **节日祝福/问候**：节日快乐、新年祝福、生日祝贺等纯社交内容（但如果author的定位就是文案/句子分享，则祝福文案也是核心内容，不跳过）
- **纯转载/导流**：内容极少，主要是引导关注、转发、点击外链，author没有表达自己的观点
- **招聘/求职**：招聘启事、求职信息（但如果author的定位涉及招聘/人力资源，则不跳过）
- **纯图片/视频集**：正文几乎没有文字知识内容，只是图片/视频合集

⚠️ 以下类型**不要简单跳过**，需要仔细判断：
- **广告/推广**：如果author表达了推荐理由、审美判断、使用体验等个人观点，这些是author身份的重要部分，不应跳过；只有纯硬广（只有产品参数、价格、购买链接，author没有个人观点）才跳过
- **活动通知**：如果author在通知中阐述了自己的理念、初衷或活动背后的思考，不应跳过；只有纯 logistical 通知（只有时间地点报名方式）才跳过

判断原则：**先看author研究方向，再看源文档内容**。如果内容符合author的核心定位和摄入侧重，即使形式上看起来像"无价值内容"，也必须保留。


1. 源文档涉及哪些已有页面中的实体/概念？（需要更新这些页面）
2. 还需要查看哪些已有页面来避免信息重复或矛盾？


如果是单篇源文档且应该跳过：
{{
  "skip": true,
  "skip_reason": "跳过原因（如：纯社交祝福、纯转载无观点等）",
  "pages_to_view": [],
  "reasoning": ""
}}

如果是多篇源文档且部分应该跳过（标题以【】标记的多篇）：
{{
  "skip": false,
  "skip_articles": [{{"title": "应跳过的源文档标题", "reason": "跳过原因"}}],
  "pages_to_view": ["页面名1", "页面名2", ...],
  "reasoning": "简要说明为什么需要查看这些页面（不超过100字）"
}}

如果所有源文档都应该跳过：
{{
  "skip": true,
  "skip_reason": "所有源文档均无知识价值",
  "pages_to_view": [],
  "reasoning": ""
}}

如果源文档有知识价值（无需跳过）：
{{
  "skip": false,
  "pages_to_view": ["页面名1", "页面名2", ...],
  "reasoning": "简要说明为什么需要查看这些页面（不超过100字）"
}}

注意：
- **reasoning 字段必须简短精炼，不超过 100 字**，只需一句话概括选页理由，禁止逐篇分析文档内容
- 只选与源文档内容**直接且深度相关**的页面，不要选所有沾边的页面
- **严格限制数量：最多选 10 个页面**，优先选最核心的（需要更新内容的 > 仅需对照参考的）
- 如果源文档涉及大量实体，只选最重要的几个，其余可以在知识页正文中用 [[链接]] 引用
- 页面名必须与索引中出现的 [[页面名]] 完全一致
- 注意匹配别名：索引中"[[巴赫]] (J.S. Bach)"，源文档提到"J.S. Bach"→选"巴赫\""""

    return system, user


def _get_all_index_content() -> dict[str, str]:
    """获取所有目录的 _index.md 内容。"""
    indexes = {}
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources/"):
            continue
        idx_path = dir_path / "_index.md"
        if idx_path.exists():
            try:
                indexes[f"wiki/{dir_name}/_index.md"] = idx_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return indexes



def build_ingest_prompt(source_title: str, source_time: str, source_content: str,
                        existing_pages_content: str = "", source_type: str = "source corpus",
                        selected_pages: list[str] | None = None) -> tuple[str, str]:
    """构建摄入 prompt，直接让 LLM 输出 Wiki 页面文件。
    existing_pages_content: 已有页面内容（一步模式传全部，两步模式传 LLM 选中的）。
    source_type: 来源类型，"source corpus" 或 "video source"。"""
    from config import get_dir_catalog_text

    today = datetime.now().strftime("%Y-%m-%d")
    dir_catalog = get_dir_catalog_text()
    _example_dirs = [n for n in get_page_types() if n != "composers"]
    example_dir = _example_dirs[0] if _example_dirs else "topics"
    purpose_content = _read_file_safe(config.get_purpose_file(), 3000)
    schema_content = _read_file_safe(config.SCHEMA_FILE, 5000)
    hot_names = _extract_candidate_names([{"title": source_title, "content": source_content}])
    existing_page_names = _get_existing_page_names(
        expand_dirs=_expand_dirs_from_selected(selected_pages),
        hot_names=hot_names,
    )

    page_types = get_page_types()
    page_types_desc = "\n".join(f"  - {name}: {info['description']}" for name, info in page_types.items())

    dir_info = get_all_dir_info()
    dirs_desc = "\n".join(f"  - wiki/{name}/: {info['description']}" for name, info in dir_info.items())

    if len(source_content) > config.INGEST_MAX_CONTENT_LEN:
        source_content = source_content[:config.INGEST_MAX_CONTENT_LEN] + "\n...(内容已截断)"

    error_constraints = _get_error_constraints()

    system = f"""你是知识库维护专家。此知识库供 AI 检索使用，不是给人阅读的文档。

核心原则：
- 信息密度高、关键词突出、格式统一、便于语义匹配
- 用结构化的事实列表，不要散文段落
- 每条信息一个要点，避免长段落
- 关键词、人名、作品名用完整表述（不要用代词"他""其"）
- frontmatter 的 aliases 字段填写常见别名/外文名/不同译名，方便检索匹配
- 标题用信息型而非文学型（如"生平与风格"而非"音乐人生"）

{error_constraints}你的任务：分析源文档，直接生成/更新 Wiki 页面文件。一步到位。"""

    user = f"""## Schema 规范
{schema_content}

{dir_catalog}

{purpose_content}

{page_types_desc}

{dirs_desc}

注意：只能将页面写入上述列表中的目录，不要直接使用列表之外的目录名。

{existing_page_names}

{existing_pages_content}

标题：{source_title}
时间：{source_time}
内容：
{source_content}


1. **分析**源文档，判断关键信息、涉及实体/概念、应创建/更新哪些页面
2. **直接生成** Wiki 页面文件

输出格式为 `---FILE: path---` 开头，后跟完整文件内容。

**去重规则**：
- 如果"已有页面名"中存在同一实体的不同译名/别名，必须更新已有页面而非创建新页面
- "已有页面名"按目录分块：`【目录名】` 下每行一个页面，格式 `主名 | 别名1, 别名2`（无别名时仅一行主名）
- 引用已有页面时统一写成 `[[目录名/主名]]`（别名只用于识别，不出现在链接里）
- 源文档中的名称若匹配到某页面的别名，应更新该页面而非新建
- 例如：`【composers】` 下有 `巴赫 | J.S. Bach, 巴哈`，源文档提到 "J.S. Bach" → 更新 `[[composers/巴赫]]`，不要创建新页面
- 标注 "(共 N 页，本批未选中)" 的目录：本批次大概率不涉及，仅告知其存在，不要为此目录新建页面

**需要输出的文件**：
1. **来源摘要页** (wiki/sources/digests/): 按 source 模板格式，文件名格式 `YYYY-MM-DD-标题关键词.md`
   - ⚠️ **以下 5 个章节全部必须输出，不可省略任何一个**（即使某章节内容很少也要写）
   - **## 摘要**：不超过200字的结构化摘要（必须）
   - **## 核心观点**：提取author的个人观点、判断、态度（不是百科知识，是author独有的见解）（必须）
   - **## 关键引用**：提取原文中可直接引用的精彩原话，加引号标注（必须，至少1条）
   - **## 关键信息**：提取源文档中的事实性信息，直接写事实（必须，至少2条）
   - **## 提及实体**：直接写实体名称，不加 [[]] 链接（必须，至少1个）。例如：`- 贝多芬`、`- 月光奏鸣曲`
2. **知识页** (根据内容放入最合适的目录):
   - **关键规则**：不同类型的知识页必须放入不同目录！参考"可用的 Wiki 目录"列表中实际存在的目录名，每个页面的 `type` 字段必须与所在目录名一致。**不要使用"可用的 Wiki 目录"列表之外的目录名**
   - ⚠️ **标题下方必须有一句话概括**：`# 页面名` 之后紧跟一行 `> 一句话概括`（blockquote 格式），简明描述该实体/概念的核心定位。例如 `> 巴洛克时期德国作曲家，西方近代音乐之父`。**每个知识页都必须有，不可省略**
   - 新页面：直接创建
   - 已有页面（内容已在「已有页面内容」中展示）：**必须输出更新后的完整内容**，将原文与新信息合并
   - ⚠️ **只能修改「已有页面内容」中展示过完整内容的页面**。「已有页面名」清单中的其他页面你只看到了名字没看到内容，不要尝试修改它们——否则会丢失原有内容。对这些页面只能在「## 相关页面」中用 `[[...]]` 引用
   - 已有页面但无需更新的：**跳过不输出**
   - **相关页面**中链接必须带目录路径：`[[目录名/页面名]]`（如 `[[composers/贝多芬]]`、`[[works/月光奏鸣曲]]`、`[[topics/纯音乐]]`），便于查询时直接定位文件
   - ⚠️ 每条相关页面必须有 `[[...]]` 链接，只写描述不写链接是格式错误。只能链接已有页面或本次新创建的页面；若概念值得关联但无对应页面，应创建该页面
   - aliases 字段填写该实体的常见别名/外文名/不同译名
   - ⚠️ **相关来源**中**只允许** `[[sources/digests/YYYY-MM-DD-标题关键词]]` 格式的链接，每条必须是本批次实际生成的 digest 文件名。如果该知识页在本批次没有对应的 digest 可链接，说明不应该输出该页面——请跳过不输出

注意：
- ⚠️ **不要输出任何 _index.md 文件**（目录索引由代码自动生成和维护，新知识页会自动追加到对应目录的待整理分区）
- **不要输出 wiki/index.md**（全局目录概览由程序自动生成）
- **不要输出 log.md**（由程序自动追加）
- **不要输出 wiki/sources/_index.md、wiki/sources/digests/_index.md 或 wiki/sources/articles/_index.md**（由代码自动生成）
- 广告/推广/活动通知类内容信息量少、relevance 低
- 中文页面名直接用中文
- frontmatter 中引用来源文件名时不要带 `.md` 后缀
- **控制生成数量**：每篇源文档最多生成 3-5 个知识页，只创建最重要的实体页面，不要为每个细小概念都建页。合并相关内容到同一页面优于创建多个小页面
- ⚠️ **禁止为author本人创建独立知识页**：author（source corpus/video source的作者）是知识库的"叙述者"而非"被记录的实体"。author的个人经历、观点、方法论应分散到对应的主题知识页中（如职场方法论、情绪管理、家庭经营等），而不是创建一个以author名字命名的"人物百科"页面。author的信息通过 digest 的"核心观点""关键引用"保留，通过各主题知识页的"核心事实"沉淀，无需单独建页


---FILE: wiki/sources/digests/{today}-标题关键词.md---
---
type: source
source_date: {source_time}
tags: [古典音乐, 钢琴]
---
> 来源：{source_type} | {source_time}
(不超过200字的结构化摘要)
- author认为XXX（author的个人判断和态度，非百科知识）
- "原文中值得引用的精彩原话"——author评价/观点
- 事实1
- 事实2
- [[贝多芬]]
- [[钢琴教育]]

---FILE: wiki/composers/贝多芬.md---
---
type: composers
aliases: [Beethoven, 路德维希·范·贝多芬]
tags: [作曲家, 古典主义]
---
> 德国作曲家，古典主义音乐代表人物
- 贝多芬（Ludwig van Beethoven）是德国作曲家，古典主义音乐代表人物
- [[{example_dir}/钢琴教育]] — 钢琴教育的核心方法
- [[sources/digests/{today}-标题关键词]] — 来源说明

---FILE: wiki/{example_dir}/钢琴教育.md---
---
type: {example_dir}
aliases: [学琴, 钢琴学习]
tags: [教育, 钢琴]
---
> 关于钢琴学习的方法与建议
- （文章中关于钢琴教育的要点）
- [[composers/贝多芬]] — 古典主义钢琴作品
- [[sources/digests/{today}-标题关键词]] — 来源说明

**重要**：以上只是示例格式。实际输出时，必须根据源文档内容将知识页放入最合适的目录（参考"可用的 Wiki 目录"列表），不要全部放入同一个目录。⚠️ 不要输出任何 _index.md 文件，目录索引由代码自动维护。
"""

    return system, user


def _predict_article_stem(title: str, source_time: str) -> str:
    """预测 _save_article_original 将生成的 article stem（与其保持同步）。

    格式：`YYYY-MM-DD-<标题前 30 字>`。source_time 缺失时用 `unknown`。
    **注意**：此函数的处理逻辑必须与 _save_article_original 完全一致！
    """
    date_prefix = source_time[:10] if source_time else "unknown"
    clean_title = re.sub(r'[\u200b\u200c\u200d\ufeff\u00ad]', '', title)
    safe_title = re.sub(r'[<>:"/\\|?*\[\]]', '', clean_title)[:30].strip()
    safe_title = safe_title.replace('\u2026', '')
    safe_title = re.sub(r'\.{2,}', '', safe_title)
    safe_title = safe_title.rstrip('.')
    stem = _normalize_filename(f"{date_prefix}-{safe_title}")
    if stem.endswith('.md'):
        stem = stem[:-3]
    return stem


def build_ingest_prompt_batch(articles: list[dict], existing_pages_content: str = "",
                              selected_pages: list[str] | None = None) -> tuple[str, str]:
    """构建批量摄入 prompt，多篇文章合并一次 LLM 调用。

    articles: list[dict]，每个 dict 含 title, source_time, content
    selected_pages: 阶段 1 选中的已有页面列表（格式 "目录/页面名"），用于裁剪
                    "已有页面名"清单只展开相关目录，大幅压缩 prompt 长度。
    """
    from config import get_dir_catalog_text

    today = datetime.now().strftime("%Y-%m-%d")
    dir_catalog = get_dir_catalog_text()
    _example_dirs = [n for n in get_page_types() if n != "composers"]
    example_dir = _example_dirs[0] if _example_dirs else "topics"
    purpose_content = _read_file_safe(config.get_purpose_file(), 3000)
    schema_content = _read_file_safe(config.SCHEMA_FILE, 5000)
    existing_page_names = _get_existing_page_names(
        expand_dirs=_expand_dirs_from_selected(selected_pages),
        hot_names=_extract_candidate_names(articles),
    )

    page_types = get_page_types()
    page_types_desc = "\n".join(f"  - {name}: {info['description']}" for name, info in page_types.items())

    dir_info = get_all_dir_info()
    dirs_desc = "\n".join(f"  - wiki/{name}/: {info['description']}" for name, info in dir_info.items())

    articles_text = ""
    expected_stems: list[str] = []
    for i, art in enumerate(articles):
        content = art["content"]
        if len(content) > config.INGEST_MAX_CONTENT_LEN:
            content = content[:config.INGEST_MAX_CONTENT_LEN] + "\n...(内容已截断)"
        st = art.get("source_type", "source corpus")
        stem = _predict_article_stem(art["title"], art.get("source_time", ""))
        expected_stems.append(stem)
        articles_text += f"""
标题：{art['title']}
来源：{st}
时间：{art['source_time']}
⚠️ 原文存档 stem（已由系统决定）：`{stem}`
   → 生成本篇 digest 时：**frontmatter 必须写 `source_article: {stem}`**（值原样复制，不加路径前缀、不加 `.md` 后缀）
内容：
{content}
"""

    error_constraints = _get_error_constraints()

    system = f"""你是知识库维护专家。此知识库供 AI 检索使用，不是给人阅读的文档。

核心原则：
- 信息密度高、关键词突出、格式统一、便于语义匹配
- 用结构化的事实列表，不要散文段落
- 每条信息一个要点，避免长段落
- 关键词、人名、作品名用完整表述（不要用代词"他""其"）
- frontmatter 的 aliases 字段填写常见别名/外文名/不同译名，方便检索匹配
- 标题用信息型而非文学型（如"生平与风格"而非"音乐人生"）

{error_constraints}你的任务：分析多篇源文档，一次性生成/更新所有 Wiki 页面文件。注意多篇源文档可能有重叠的实体，需要合并更新。"""

    user = f"""## Schema 规范
{schema_content}

{dir_catalog}

{purpose_content}

{page_types_desc}

{dirs_desc}

注意：只能将页面写入上述列表中的目录，不要直接使用列表之外的目录名。

{existing_page_names}

{existing_pages_content}

{articles_text}


1. **分析**所有源文档，判断关键信息、涉及实体/概念、应创建/更新哪些页面
2. **注意多篇源文档的关联**：如果多篇源文档涉及同一实体/概念，必须合并到同一个页面，不要创建重复页面
3. **直接生成**所有 Wiki 页面文件

输出格式为 `---FILE: path---` 开头，后跟完整文件内容。

**去重规则**：
- 如果"已有页面名"中存在同一实体的不同译名/别名，必须更新已有页面而非创建新页面
- "已有页面名"按目录分块：`【目录名】` 下每行一个页面，格式 `主名 | 别名1, 别名2`（无别名时仅一行主名）
- 引用已有页面时统一写成 `[[目录名/主名]]`（别名只用于识别，不出现在链接里）
- 源文档中的名称若匹配到某页面的别名，应更新该页面而非新建
- 标注 "(共 N 页，本批未选中)" 的目录：本批次大概率不涉及，仅告知其存在，不要为此目录新建页面
- 多篇源文档提到同一实体时，合并为一个页面
- 如果多篇源文档都涉及同一知识页，在"## 相关来源"中列出所有相关摘要页链接

**需要输出的文件**：
1. **来源摘要页** (wiki/sources/digests/): 每篇源文档一个 source 页，**文件名必须与上文给出的 stem 同名**（即 `{{stem}}.md`，不要自行改名、加前缀或用占位符）
   - ⚠️ **frontmatter 必须包含 `source_article: {{该篇对应的 stem}}`**（值原样复制上文给出的 stem，不要带 `.md`、不要带路径前缀）
   - ⚠️ **以下 5 个章节全部必须输出，不可省略任何一个**（即使某章节内容很少也要写）
   - **## 摘要**：不超过200字的结构化摘要（必须）
   - **## 核心观点**：提取author的个人观点、判断、态度（不是百科知识，是author独有的见解）（必须）
   - **## 关键引用**：提取原文中可直接引用的精彩原话，加引号标注（必须，至少1条）
   - **## 关键信息**：提取源文档中的事实性信息，直接写事实（必须，至少2条）
   - **## 提及实体**：直接写实体名称，不加 [[]] 链接（必须，至少1个）。例如：`- 贝多芬`、`- 月光奏鸣曲`
2. **知识页** (根据内容放入最合适的目录):
   - **关键规则**：不同类型的知识页必须放入不同目录！参考"可用的 Wiki 目录"列表中实际存在的目录名，每个页面的 `type` 字段必须与所在目录名一致。**不要使用"可用的 Wiki 目录"列表之外的目录名**
   - ⚠️ **标题下方必须有一句话概括**：`# 页面名` 之后紧跟一行 `> 一句话概括`（blockquote 格式），简明描述该实体/概念的核心定位。例如 `> 巴洛克时期德国作曲家，西方近代音乐之父`。**每个知识页都必须有，不可省略**
   - 新页面：直接创建
   - 已有页面（内容已在「已有页面内容」中展示）：**必须输出更新后的完整内容**，将原文与新信息合并
   - ⚠️ **只能修改「已有页面内容」中展示过完整内容的页面**。「已有页面名」清单中的其他页面你只看到了名字没看到内容，不要尝试修改它们——否则会丢失原有内容。对这些页面只能在「## 相关页面」中用 `[[...]]` 引用
   - 已有页面但无需更新的：**跳过不输出**
   - 📛 **命名规则（重要）**：
     - 页面名用**最常用的短名称**（中文优先）：`composers/巴赫`、`composers/维瓦尔第`、`composers/普罗科菲耶夫`；**禁止**用全名如「约翰·塞巴斯蒂安·巴赫」「安东尼奥·维瓦尔第」「谢尔盖·普罗科菲耶夫」等作为页面名
     - 全名、外文名、不同译名一律写进 `aliases` 字段
     - 若"已有页面名"里已有该实体（可能以别名出现），**必须复用已有页面名**，不要另起一个新名字
     - 页面名不含空格、`·`、`/`、方括号、引号等易产生歧义的符号（`·` 用于分隔中文音译全名的场合禁用；英文缩写单词若必须保留空格则用短横线连接，如 `Jacob-Collier`）
   - **相关页面**中链接必须带目录路径：`[[目录名/页面名]]`（如 `[[composers/贝多芬]]`、`[[works/月光奏鸣曲]]`、`[[topics/纯音乐]]`），便于查询时直接定位文件
   - ⚠️ 每条相关页面必须有 `[[...]]` 链接，只写描述不写链接是格式错误。只能链接已有页面或本次新创建的页面；若概念值得关联但无对应页面，应创建该页面
   - aliases 字段填写该实体的常见别名/外文名/不同译名
   - ⚠️ **相关来源**中**只允许** `[[sources/digests/<本批次给出的 stem>]]` 格式的链接（和 digest 文件名严格同名，不要用占位符）。每条必须是本批次实际生成的 digest 文件名。如果该知识页在本批次没有对应的 digest 可链接，说明不应该输出该页面——请跳过不输出

注意：
- ⚠️ **不要输出任何 _index.md 文件**（目录索引由代码自动生成和维护，新知识页会自动追加到对应目录的待整理分区）
- **不要输出 wiki/index.md**（全局目录概览由程序自动生成）
- **不要输出 log.md**（由程序自动追加）
- **不要输出 wiki/sources/_index.md、wiki/sources/digests/_index.md 或 wiki/sources/articles/_index.md**（由代码自动生成）
- 广告/推广/活动通知类内容信息量少、relevance 低
- 中文页面名直接用中文
- frontmatter 中引用来源文件名时不要带 `.md` 后缀
- **控制生成数量**：每篇源文档最多生成 3-5 个知识页，只创建最重要的实体页面，不要为每个细小概念都建页。合并相关内容到同一页面优于创建多个小页面
- ⚠️ **禁止为author本人创建独立知识页**：author（source corpus/video source的作者）是知识库的"叙述者"而非"被记录的实体"。author的个人经历、观点、方法论应分散到对应的主题知识页中（如职场方法论、情绪管理、家庭经营等），而不是创建一个以author名字命名的"人物百科"页面。author的信息通过 digest 的"核心观点""关键引用"保留，通过各主题知识页的"核心事实"沉淀，无需单独建页


1. **[[...]] 内只能是纯链接**：`[[目录/页面名]]`，不要把前面的描述文字、箭头、日期都塞进方括号。正确：`这是 [[works/G大调钢琴协奏曲]] — 拉威尔作品`；错误：`[[works/G大调钢琴协奏曲 → composers/拉威尔]]`
2. **知识页的「## 相关来源」只允许链本批次的 digest**：写成 `[[sources/digests/<本批次给出的 stem>]]`，stem 必须是上文给出的、本批次实际会生成的 digest 文件名。如果某知识页在本批次没有对应 digest 可链，则不要输出该页面
3. **链接目标要么已存在要么本次会创建**：不要链接一个既不在"已有页面名"里、本批次也不会创建的页面。如果某概念重要但无对应页面，要么本次就新建该页面，要么降级为纯文本提及（`## 提及实体` 下不加 `[[]]`）
4. **人物页面名只用短名**：用 `composers/巴赫`、`composers/拉威尔`；不要用 `composers/约翰·塞巴斯蒂安·巴赫`、`composers/莫里斯·拉威尔`。若"已有页面名"提示该作曲家已以某别名存在，复用它
5. **非音乐领域的人物/主题不要硬塞 composers/**：画家、作家、科学家等应进 `topics/` 或其他目录，不要放 `composers/`


（以下示例**假设**当前批次的两篇原文 stem 分别是 `2026-04-15-文章A标题` 和 `2026-04-16-文章B标题`——实际生成时必须使用**上文实际给出的 stem**，不要直接照抄示例里的日期）

---FILE: wiki/sources/digests/2026-04-15-文章A标题.md---
---
type: source
source_date: 2026-04-15
source_article: 2026-04-15-文章A标题
tags: [古典音乐, 钢琴]
---
> 来源：source corpus/video source | 2026-04-15
(不超过200字的结构化摘要)
- author认为XXX
- "精彩原话"
- 事实1
- 贝多芬

---FILE: wiki/sources/digests/2026-04-16-文章B标题.md---
---
type: source
source_date: 2026-04-16
source_article: 2026-04-16-文章B标题
tags: [古典音乐, 交响乐]
---
> 来源：source corpus/video source | 2026-04-16
(另一篇的摘要)
- author认为YYY
- "另一段精彩原话"
- 事实1
- 贝多芬

---FILE: wiki/composers/贝多芬.md---
---
type: composers
aliases: [Beethoven, 路德维希·范·贝多芬]
tags: [作曲家, 古典主义]
---
> 德国作曲家，古典主义音乐代表人物
- 贝多芬（Ludwig van Beethoven）是德国作曲家，古典主义音乐代表人物
- （来自文章A的新信息，用完整表述）
- （来自文章B的新信息，用完整表述）
- [[{example_dir}/钢琴教育]] — 贝多芬钢琴作品在教学中的应用
- [[sources/digests/2026-04-15-文章A标题]] — 文章A中关于贝多芬的内容
- [[sources/digests/2026-04-16-文章B标题]] — 文章B中关于贝多芬的内容

---FILE: wiki/{example_dir}/钢琴教育.md---
---
type: {example_dir}
aliases: [学琴, 钢琴学习]
tags: [教育, 钢琴]
---
> 关于钢琴学习的方法与建议
- （关于钢琴教育的要点）
- [[composers/贝多芬]] — 古典主义钢琴作品
- [[sources/digests/{today}-文章A关键词]] — 文章A中关于钢琴教育的内容

**重要**：以上只是示例格式。实际输出时，必须根据源文档内容将知识页放入最合适的目录（参考"可用的 Wiki 目录"列表），不要全部放入同一个目录。⚠️ 不要输出任何 _index.md 文件，目录索引由代码自动维护。
"""

    return system, user

def parse_file_outputs(text: str) -> dict[str, str]:
    """解析输出中的 ---FILE: path--- 块。

    当同一路径出现多次时（批量模式下多篇文章更新同一知识页），
    自动合并内容而非覆盖，避免丢失数据（E06 根因修复）。
    合并策略：保留第一次出现的 frontmatter，将后续出现的 body 中新增章节/条目追加到已有内容中。
    """
    dir_change_pos = text.find("---DIR_CHANGES---")
    file_text = text[:dir_change_pos] if dir_change_pos != -1 else text

    files = {}
    pattern = r'---FILE:\s*(.+?)---\s*\n(.*?)(?=---FILE:|\Z)'
    for match in re.finditer(pattern, file_text, re.DOTALL):
        path = match.group(1).strip()
        content = match.group(2).strip()
        if path and content:
            if not path.endswith(".md"):
                if path.endswith("md"):
                    path = path[:-2] + ".md"
                else:
                    path = path + ".md"
            if path not in files:
                files[path] = content
            else:
                files[path] = _merge_page_contents(files[path], content, path)
    return files


def _merge_page_contents(existing: str, incoming: str, path: str) -> str:
    """合并同一知识页的两次输出内容，避免覆盖丢失数据。

    策略：
    - frontmatter：保留 existing 的，用 incoming 中新增的字段补充
    - body 按 ## 章节拆分：
      - 相同章节名：将 incoming 中的新条目（- 开头的列表项、[[...]] 链接）追加到 existing 章节末尾，去重
      - incoming 独有的章节：追加到 existing 末尾
    """
    import re as _re

    def _split_fm_body(text):
        """拆分 frontmatter 和 body。"""
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                return parts[1], parts[2]
        return "", text

    def _split_sections(body):
        """将 body 按 ## 标题拆分为 [(section_name, content_lines)]。
        section_name="" 表示标题前的头部内容。"""
        sections = []
        current_name = ""
        current_lines = []
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## ") and stripped != "## ":
                if current_lines or current_name:
                    sections.append((current_name, current_lines))
                current_name = stripped[3:].strip()
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_lines or current_name:
            sections.append((current_name, current_lines))
        return sections

    fm_existing, body_existing = _split_fm_body(existing)
    fm_incoming, body_incoming = _split_fm_body(incoming)

    merged_fm = fm_existing
    if fm_incoming:
        existing_keys = set()
        for line in fm_existing.strip().split("\n"):
            if ":" in line:
                existing_keys.add(line.split(":")[0].strip())
        for line in fm_incoming.strip().split("\n"):
            if ":" in line:
                key = line.split(":")[0].strip()
                if key not in existing_keys:
                    merged_fm = merged_fm.rstrip() + "\n" + line

    sections_existing = _split_sections(body_existing)
    sections_incoming = _split_sections(body_incoming)

    existing_section_map = {}  # section_name → index in sections_existing
    for i, (name, _) in enumerate(sections_existing):
        if name:
            existing_section_map[name] = i

    for inc_name, inc_lines in sections_incoming:
        if not inc_name:
            continue  # 跳过 incoming 的头部（保留 existing 的头部）
        if inc_name in existing_section_map:
            idx = existing_section_map[inc_name]
            _, ext_lines = sections_existing[idx]
            ext_items = set()
            for line in ext_lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("## "):
                    ext_items.add(stripped)
            new_items = []
            for line in inc_lines:
                stripped = line.strip()
                if stripped.startswith("## "):
                    continue  # 跳过章节标题行
                if stripped and stripped not in ext_items:
                    new_items.append(line)
            if new_items:
                sections_existing[idx] = (inc_name, ext_lines + new_items)
        else:
            sections_existing.append((inc_name, inc_lines))

    merged_body_lines = []
    for name, lines in sections_existing:
        merged_body_lines.extend(lines)
    merged_body = "\n".join(merged_body_lines)

    merged_body = _re.sub(r'\n{3,}', '\n\n', merged_body)

    print(f"  🔀 合并同路径文件: {path}（多篇文章更新同一知识页，已合并而非覆盖）")
    return f"---{merged_fm}\n---{merged_body}"


def parse_dir_changes(text: str) -> list[dict]:
    """解析输出中的 ---DIR_CHANGES--- 块，返回目录变更列表。"""
    m = re.search(r'---DIR_CHANGES---\s*\n(.+)', text, re.DOTALL)
    if not m:
        return []
    raw = m.group(1).strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw)
    try:
        changes = json.loads(raw.strip())
        if isinstance(changes, list):
            return changes
    except json.JSONDecodeError:
        pass
    return []


def _apply_ingest_dir_changes(dir_changes: list[dict]):
    """执行摄入时 LLM 提议的目录变更。"""
    if not dir_changes:
        return

    if config.get_ablation_group() == "no_dynamic_dir":
        if dir_changes:
            print(f"  🚫 [消融实验 no_dynamic_dir] 跳过 LLM 提议的 {len(dir_changes)} 项目录变更")
        return

    safe_actions = {"split", "merge", "move_page"}
    safe_changes = [c for c in dir_changes if c.get("action") in safe_actions]
    if not safe_changes:
        return

    print(f"  📂 LLM 提议 {len(safe_changes)} 项目录变更：")
    for c in safe_changes:
        action = c.get("action", "")
        from_dir = c.get("from", "")
        to_dir = c.get("to", "")
        reason = c.get("reason", "")
        move_pages = c.get("move_pages", [])
        print(f"    [{action}] {from_dir} → {to_dir}: {reason[:60]}{'...' if len(reason) > 60 else ''}")
        if move_pages:
            print(f"      涉及页面: {', '.join(str(p) for p in move_pages[:5])}")

    try:
        from config import apply_dir_changes
        apply_dir_changes(safe_changes)
        _rebuild_global_index(update_overview=False)
        invalidate_graph_cache()
        print(f"  ✅ 目录变更已执行")
    except Exception as e:
        print(f"  ⚠️ 目录变更执行失败: {e}")


def _normalize_filename(filename: str) -> str:
    """归一化文件名：统一空格和全角/半角字符。"""
    result = []
    for ch in filename:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif code == 0x3000:
            result.append(' ')
        else:
            result.append(ch)
    name = "".join(result)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'\s*-\s*', '-', name)
    return name


def _sanitize_frontmatter(content: str) -> str:
    """清洗 frontmatter：
    - tags/aliases 中文逗号→英文逗号
    - 移除 confidence 字段
    - 多行列表格式转单行数组格式
    - 统一字段顺序：type → created → updated → aliases → tags → 其余
    """
    if not content.startswith("---"):
        return content
    parts = _robust_fm_split(content)
    if len(parts) < 3:
        return content

    fm_lines = parts[1].strip().split("\n")

    merged_lines = []
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        stripped = line.strip()
        m = re.match(r'^(\w+):\s*$', stripped)
        if m and i + 1 < len(fm_lines) and fm_lines[i + 1].strip().startswith("- "):
            key = m.group(1)
            items = []
            i += 1
            while i < len(fm_lines) and fm_lines[i].strip().startswith("- "):
                item = fm_lines[i].strip()[2:].strip().strip("'\"")
                items.append(item)
                i += 1
            merged_lines.append(f"{key}: [{', '.join(items)}]")
            continue
        merged_lines.append(line)
        i += 1

    ordered_keys = ["type", "created", "updated", "aliases", "tags"]
    ordered = {}
    rest_lines = []
    for line in merged_lines:
        stripped = line.strip()
        if stripped.startswith("confidence:"):
            continue
        if stripped.startswith(("tags:", "aliases:", "sources:")):
            line = line.replace("，", ", ").replace("、", ", ")
            line = re.sub(r',\s*', ', ', line)
        if stripped.startswith("source_article:"):
            val = stripped.split(":", 1)[1].strip()
            quoted = _yaml_quote_value(val)
            if quoted != val:
                line = f"source_article: {quoted}"
                stripped = line
        m = re.match(r'^(\w+):', stripped)
        if m and m.group(1) in ordered_keys:
            ordered[m.group(1)] = stripped
        else:
            rest_lines.append(stripped)

    final_lines = []
    for key in ordered_keys:
        if key in ordered:
            final_lines.append(ordered[key])
    final_lines.extend(rest_lines)

    return f"---\n{chr(10).join(final_lines)}\n---{parts[2]}"


def _sanitize_related_sources(content: str) -> str:
    """清洗知识页的「## 相关来源」章节：只保留 [[sources/digests/...]] 链接。

    E04 根因修复：LLM 有时会在相关来源中放入知识页链接（如 [[composers/莫扎特]]）
    或纯文本描述行，这些都不符合规范。
    - 非 digest 链接 → 删除该行
    - 纯文本行（无任何 [[...]] 链接）→ 删除该行
    - 混合行（同时含 digest 和非 digest 链接）→ 只保留 digest 链接部分
    """
    if "## 相关来源" not in content:
        return content

    header = ""
    body = content
    if content.startswith("---"):
        parts = _robust_fm_split(content)
        if len(parts) >= 3:
            header = "---" + parts[1] + "---"
            body = parts[2]

    lines = body.split("\n")
    new_lines = []
    in_source = False
    cleaned = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            in_source = (stripped == "## 相关来源")
            new_lines.append(line)
            continue

        if in_source:
            if not stripped:
                new_lines.append(line)
                continue
            all_links = re.findall(r'\[\[([^\]]+)\]\]', stripped)
            if not all_links:
                cleaned = True
                continue
            digest_links = [l for l in all_links if l.startswith("sources/digests/")]
            non_digest = [l for l in all_links if not l.startswith("sources/digests/")]
            if non_digest and not digest_links:
                cleaned = True
                continue
            if non_digest and digest_links:
                cleaned = True
                new_items = []
                for dl in digest_links:
                    desc_m = re.search(r'\[\[' + re.escape(dl) + r'\]\]\s*[—\-]\s*(.+?)(?:\s*\[\[|$)', stripped)
                    desc = desc_m.group(1).strip() if desc_m else ""
                    if desc:
                        new_items.append(f"- [[{dl}]] — {desc}")
                    else:
                        new_items.append(f"- [[{dl}]]")
                new_lines.extend(new_items)
                continue
            new_lines.append(line)
        else:
            new_lines.append(line)

    if cleaned:
        new_body = "\n".join(new_lines)
        result = header + new_body if header else new_body
        result = re.sub(r'\n{3,}', '\n\n', result)
        return result
    return content


def _sanitize_index_md(content: str) -> str:
    """清洗 _index.md：移除 > 关键词 和 > 覆盖 行。"""
    lines = content.split("\n")
    new_lines = [line for line in lines
                 if not line.strip().startswith("> 关键词") and not line.strip().startswith("> 覆盖")]
    result = "\n".join(new_lines)
    result = re.sub(r'(# .+目录索引\n)\n+', r'\1\n', result)
    return result


def _remove_phantom_index_entries(content: str, index_path: Path) -> str:
    """移除 _index.md 中引用了不存在页面的条目行（幽灵条目）。

    仅检查本次写入时应存在的页面：同目录下刚写入的知识页。
    对于已有页面（写入前就存在的），不在本函数范围内（由 lint 兜底）。
    """
    import config as cfg
    dir_path = index_path.parent
    existing_pages = {f.stem for f in dir_path.glob("*.md") if f.stem != "_index"}

    lines = content.split("\n")
    new_lines = []
    removed = []
    for line in lines:
        m = re.match(r'^(\s*-\s*)\[\[([^\]]+)\]\](.*)', line)
        if m:
            page_name = m.group(2).split('|')[0].strip()  # 处理 [[name|alias]]
            if page_name not in existing_pages:
                removed.append(page_name)
                continue  # 跳过幽灵条目
        new_lines.append(line)

    if removed:
        print(f"  🧹 清理幽灵索引条目: {removed}")

    result = "\n".join(new_lines)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def _check_frontmatter_complete(content: str, rel_path: str) -> bool:
    """检查 frontmatter 是否完整（没有被截断）。"""
    if not content.startswith("---"):
        return True  # 没有 frontmatter，不算截断
    parts = _robust_fm_split(content)
    if len(parts) < 3:
        print(f"  ⚠️ frontmatter 截断: {rel_path}（缺少结束 ---）")
        return False
    body = parts[2].strip()
    if len(body) < 20 and not rel_path.endswith("_index.md"):
        print(f"  ⚠️ 内容疑似截断: {rel_path}（正文仅 {len(body)} 字符）")
        return False
    return True


_DIGEST_REQUIRED_SECTIONS = ["摘要", "核心观点", "关键引用", "关键信息", "提及实体"]


def _check_digest_completeness(content: str, rel_path: str) -> str:
    """校验 digest 页面完整性，缺失必须章节时自动补上占位内容。

    digest 的 5 个章节（摘要、核心观点、关键引用、关键信息、提及实体）全部必须存在。
    注意：## 原文 由代码自动生成（_fix_digest_article_links），不在此处检查。
    缺失时按正确顺序插入占位内容，保证章节顺序统一。
    """
    parts = _robust_fm_split(content)
    if len(parts) < 3:
        return content

    body = parts[2]
    missing = []
    for section in _DIGEST_REQUIRED_SECTIONS:
        if f"## {section}" not in body:
            missing.append(section)

    if not missing:
        return content

    print(f"  ⚠️ digest 缺少必须章节 [{', '.join(missing)}]: {rel_path}，自动补上占位")

    section_defaults = {
        "摘要": "（待补充）",
        "核心观点": "- （待补充）",
        "关键引用": "- （待补充）",
        "关键信息": "- （待补充）",
        "提及实体": "- （待补充）",
    }

    lines = body.split("\n")

    segments = []  # [(section_name, [lines])]，section_name="" 表示头部
    current_section = ""
    current_lines = []

    for line in lines:
        if line.strip().startswith("## ") and line.strip() != "## ":
            if current_lines:
                segments.append((current_section, current_lines))
            current_section = line.strip()[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    if current_lines:
        segments.append((current_section, current_lines))

    header_lines = []
    first_section_idx = 0
    for i, (name, _) in enumerate(segments):
        if name:
            first_section_idx = i
            break
        header_lines.extend(segments[i][1])
    else:
        first_section_idx = len(segments)
        for name, ls in segments:
            header_lines.extend(ls)

    existing_sections = {}
    for i in range(first_section_idx, len(segments)):
        name, ls = segments[i]
        existing_sections[name] = ls

    new_body_lines = list(header_lines)
    non_required = [name for name in existing_sections if name not in _DIGEST_REQUIRED_SECTIONS]

    for section in _DIGEST_REQUIRED_SECTIONS:
        if section in existing_sections:
            new_body_lines.extend(existing_sections[section])
        else:
            new_body_lines.append(f"\n## {section}")
            new_body_lines.append(section_defaults[section])

    for name in non_required:
        new_body_lines.extend(existing_sections[name])

    new_body = "\n".join(new_body_lines)
    return f"{parts[0]}---{parts[1]}---{new_body}"


def write_wiki_files(file_outputs: dict[str, str], selected_pages: list[str] | None = None):
    """将解析出的文件写入 Wiki 目录。

    包含自动校验：
    1. 文件名归一化：全角→半角，空格归一，短横线标准化
    2. type 路径纠正：frontmatter type 与路径目录不一致时，以 type 为准
    3. 跨目录去重：同名文件已存在于其他目录时，合并到已有目录
    4. _index.md 自动维护：LLM 不再输出 _index.md，所有新写入的知识页自动追加到对应目录的待整理分区
    5. tags 清洗：中文逗号→英文逗号
    6. frontmatter 截断检测：跳过不完整的文件
    7. sources 路径自动纠正：wiki/sources/xxx.md → wiki/sources/digests/xxx.md
    8. 未选中页面保护：LLM 未看过内容的已有页面，不允许覆盖写入

    selected_pages: Step1 选中的已有页面名列表（如 ["贝多芬", "月光奏鸣曲"]），
                    用于防止 LLM 覆盖它没看过内容的已有页面。None 表示不做此校验。
    """
    from config import get_page_types, FIXED_DIRS

    known_dirs = set(get_page_types().keys()) | set(FIXED_DIRS.keys())

    _fixed_dir_basenames = set()
    for fd in FIXED_DIRS:
        base = fd.split("/")[0]  # sources/digests → sources
        _fixed_dir_basenames.add(base)
    _confusable_map: dict[str, str] = {}  # 易混淆名 → 正确的固定目录名
    for base in _fixed_dir_basenames:
        if base.endswith("s"):
            _confusable_map[base[:-1]] = base        # source → sources
            _confusable_map[base[:-2]] = base         # synthese → syntheses (如果 es 结尾)
            _confusable_map[base + "s"] = base        # sourcess → sources
        else:
            _confusable_map[base + "s"] = base
            _confusable_map[base + "es"] = base

    normalized_outputs = {}
    _filename_norm_map: dict[str, str] = {}  # "sources/digests/旧文件名" → "sources/digests/新文件名"
    for rel_path, content in file_outputs.items():
        if rel_path.startswith("wiki/"):
            if re.match(r'^wiki/sources/[^/]+\.md$', rel_path) and not rel_path.endswith("_index.md"):
                old_path = rel_path
                filename = rel_path.split("/")[-1]
                rel_path = f"wiki/sources/digests/{filename}"
                print(f"  🔧 sources 路径纠正: {old_path} → {rel_path}")

            parts_check = rel_path.split("/")
            if len(parts_check) >= 3 and not rel_path.endswith("_index.md"):
                dir_name = parts_check[1]
                if dir_name in _confusable_map and dir_name not in known_dirs:
                    correct_dir = _confusable_map[dir_name]
                    old_path = rel_path
                    if correct_dir == "sources":
                        filename = parts_check[-1]
                        content_check = file_outputs.get(old_path, "")
                        if "source_article:" in content_check or "type: source" in content_check:
                            rel_path = f"wiki/sources/digests/{filename}"
                        else:
                            rel_path = f"wiki/sources/digests/{filename}"
                    else:
                        parts_check[1] = correct_dir
                        rel_path = "/".join(parts_check)
                    print(f"  🔧 易混淆目录纠正: {old_path} → {rel_path}")

            parts = rel_path.split("/")
            if not rel_path.endswith("_index.md"):
                old_name = parts[-1]
                new_name = _normalize_filename(old_name)
                if new_name != old_name:
                    parts[-1] = new_name
                    new_path = "/".join(parts)
                    print(f"  🔤 文件名归一化: {old_name} → {new_name}")
                    old_link = rel_path[len("wiki/"):].removesuffix(".md")
                    new_link = new_path[len("wiki/"):].removesuffix(".md")
                    _filename_norm_map[old_link] = new_link
                    rel_path = new_path
        normalized_outputs[rel_path] = content
    file_outputs = normalized_outputs

    if _filename_norm_map:
        synced_outputs = {}
        for rel_path, content in file_outputs.items():
            for old_link, new_link in _filename_norm_map.items():
                if f"[[{old_link}]]" in content:
                    content = content.replace(f"[[{old_link}]]", f"[[{new_link}]]")
            synced_outputs[rel_path] = content
        file_outputs = synced_outputs
        print(f"  🔗 同步归一化 {len(_filename_norm_map)} 个 wikilink（文件名全角→半角）")

    def _normalize_wikilink_targets(content: str) -> str:
        """对内容中所有 [[...]] 链接的目标做归一化：全角→半角、多余末尾标点清理。"""
        def _norm_link(m):
            link = m.group(1)
            if "/" in link:
                prefix, name = link.rsplit("/", 1)
                normed = _normalize_filename(name)
                normed = normed.rstrip('.').rstrip(',')
                new_link = f"{prefix}/{normed}"
            else:
                new_link = _normalize_filename(link)
            return f"[[{new_link}]]" if new_link != link else m.group(0)
        return re.sub(r'\[\[([^\]]+)\]\]', _norm_link, content)

    wikilink_norm_count = 0
    normed_outputs = {}
    for rel_path, content in file_outputs.items():
        new_content = _normalize_wikilink_targets(content)
        if new_content != content:
            wikilink_norm_count += 1
        normed_outputs[rel_path] = new_content
    file_outputs = normed_outputs
    if wikilink_norm_count:
        print(f"  🔗 全局 wikilink 归一化: {wikilink_norm_count} 个文件中的链接已标准化")

    dirs_with_pages = set()       # 本次有知识页写入的目录
    corrected_paths = {}          # 原路径 → 纠正后路径

    def _get_top_dir(path: str) -> str:
        """从 wiki/xxx/... 中提取顶层目录名。"""
        parts = path.split("/")
        return parts[1] if len(parts) >= 3 else ""

    def _is_sources_path(path: str) -> bool:
        """判断是否是 sources 子目录路径。"""
        return path.startswith("wiki/sources/")

    for rel_path, content in file_outputs.items():
        if not rel_path.startswith("wiki/"):
            continue
        if rel_path.endswith("log.md") or rel_path == "wiki/index.md":
            continue

        path_parts = rel_path.split("/")
        if len(path_parts) < 3:
            continue

        if _is_sources_path(rel_path):
            dirs_with_pages.add("sources")
            continue

        if rel_path.endswith("_index.md"):
            continue  # LLM 不再输出 _index.md，跳过
        else:
            path_dir = path_parts[1]
            page_type = _extract_type_from_content(content)
            if page_type and page_type != path_dir and page_type in known_dirs:
                corrected_dir = page_type
                corrected_paths[rel_path] = f"wiki/{page_type}/{path_parts[-1]}"
            else:
                corrected_dir = path_dir
            dirs_with_pages.add(corrected_dir)

    _selected_names: set[str] | None = None
    if selected_pages is not None:
        _selected_names = set()
        for sp in selected_pages:
            name = sp.strip("[]")  # 去掉 [[...]] 双括号
            name = name.rsplit("/", 1)[-1] if "/" in name else name
            name = name.removesuffix(".md")
            _selected_names.add(name)
    _unseen_overwrite_skipped: list[str] = []  # 被跳过的页面路径，用于记入错题本
    _new_knowledge_pages: list[tuple[str, str]] = []  # (rel_path, actual_dir)：本次写入的知识页（全部自动追加到 _index.md）

    sorted_items = sorted(file_outputs.items(), key=lambda kv: kv[0].endswith("_index.md"))
    for rel_path, content in sorted_items:
        if rel_path.endswith("log.md"):
            continue
        if rel_path == "wiki/index.md":
            continue
        if rel_path in ("wiki/sources/_index.md", "wiki/sources/digests/_index.md", "wiki/sources/articles/_index.md"):
            print(f"  ⏭️ 跳过 {rel_path}（由代码自动生成）")
            continue
        if ".." in rel_path:
            print(f"  ⚠️ 跳过不安全路径: {rel_path}")
            continue

        if not _check_frontmatter_complete(content, rel_path):
            print(f"  ⏭️ 跳过 {rel_path}（内容不完整）")
            continue

        if (_selected_names is not None
                and rel_path.startswith("wiki/")
                and not rel_path.endswith("_index.md")
                and not _is_sources_path(rel_path)):
            page_name = rel_path.split("/")[-1].removesuffix(".md")
            actual_path = config.WIKI_DIR / rel_path[len("wiki/"):]
            if actual_path.exists() and page_name not in _selected_names and not _is_pruning_disabled():
                print(f"  🛡️ 跳过 {rel_path}（已有页面但未被 Step1 选中，LLM 未看过内容，覆盖会丢失数据）")
                _unseen_overwrite_skipped.append(rel_path)
                continue

        if rel_path.startswith("wiki/sources/digests/") and not rel_path.endswith("_index.md"):
            content = _check_digest_completeness(content, rel_path)

        if rel_path.startswith("wiki/") and not _is_sources_path(rel_path):
            path_parts = rel_path.split("/")
            if len(path_parts) >= 3:
                if rel_path.endswith("_index.md"):
                    print(f"  ⏭️ 跳过 {rel_path}（_index.md 由代码自动维护，不接受 LLM 输出）")
                    continue

        _type_corrected = False  # 标记本次是否经过 type 纠正
        if rel_path in corrected_paths:
            old_path = rel_path
            rel_path = corrected_paths[old_path]
            _type_corrected = True
            print(f"  🔧 自动纠正路径: {old_path} → {rel_path}")

        if rel_path.startswith("wiki/") and not rel_path.endswith("_index.md") and not _is_sources_path(rel_path):
            path_parts = rel_path.split("/")
            if len(path_parts) == 3:
                filename = path_parts[2]
                target_dir = path_parts[1]
                if config.WIKI_DIR.exists():
                    for d in config.WIKI_DIR.iterdir():
                        if d.is_dir() and d.name != target_dir and d.name != "sources":
                            existing = d / filename
                            if existing.exists():
                                if _type_corrected:
                                    try:
                                        existing.unlink()
                                        _wfs_mirror_delete(existing)
                                        print(f"  🔀 type 纠正移动: 删除旧位置 {d.name}/{filename}，写入到 {target_dir}/")
                                    except OSError as e:
                                        print(f"  ⚠️ 删除旧文件失败 {d.name}/{filename}: {e}")
                                else:
                                    print(f"  🔀 跨目录去重: {filename} 已存在于 {d.name}/，写入到已有位置")
                                    rel_path = f"wiki/{d.name}/{filename}"
                                break

        if rel_path.startswith("wiki/") and not _is_sources_path(rel_path) and not rel_path.endswith("_index.md"):
            final_parts = rel_path.split("/")
            if len(final_parts) >= 3:
                actual_dir = final_parts[1]
                if actual_dir not in ("syntheses",):
                    _new_knowledge_pages.append((rel_path, actual_dir))

        if rel_path.startswith("wiki/"):
            actual_rel = rel_path[len("wiki/"):]
            full_path = config.WIKI_DIR / actual_rel
        else:
            full_path = config.BASE_DIR / rel_path

        full_path.parent.mkdir(parents=True, exist_ok=True)

        if not rel_path.endswith("_index.md"):
            content = _inject_dates(content, full_path.exists(), existing_path=full_path)

        content = _sanitize_frontmatter(content)

        if (rel_path.startswith("wiki/") and not _is_sources_path(rel_path)
                and not rel_path.endswith("_index.md") and "## 相关来源" in content):
            content = _sanitize_related_sources(content)

        full_path.write_text(content, encoding="utf-8")
        _wfs_mirror_write(full_path, content)
        print(f"  📄 写入 {rel_path} ({len(content)} chars)")

    if _new_knowledge_pages:
        _ablation_no_dynamic = (config.get_ablation_group() == "no_dynamic_dir")
        registered_dirs = set(config.get_page_types().keys())
        for _, actual_dir in _new_knowledge_pages:
            if actual_dir not in registered_dirs and actual_dir not in ("sources", "syntheses"):
                if actual_dir in _confusable_map:
                    correct = _confusable_map[actual_dir]
                    print(f"  ⚠️ 拒绝注册易混淆目录 '{actual_dir}'（与固定目录 '{correct}' 过于相似）")
                    continue
                if _ablation_no_dynamic:
                    print(f"  🚫 [消融实验 no_dynamic_dir] 拒绝注册新目录 '{actual_dir}'（目录集合冻结）")
                    continue
                config.register_page_type(actual_dir, f"自动注册 — {actual_dir}", auto_created=True)
                registered_dirs.add(actual_dir)
                print(f"  📂 自动注册新目录到 page_types.yaml: {actual_dir}")

    _rebuild_sources_index()
    _rebuild_global_index(update_overview=False)
    invalidate_graph_cache()

    if _new_knowledge_pages:
        _auto_append_to_index(_new_knowledge_pages)

    if _unseen_overwrite_skipped:
        try:
            from error_book import record_lint_issues
            record_lint_issues({"unseen_page_overwrite": _unseen_overwrite_skipped})
        except ImportError:
            pass


def _build_full_index_entry(page_name: str, page_path: "Path") -> str:
    """根据页面文件的 frontmatter 和内容，生成完整的 _index.md 条目。

    格式：- [[页面名]] (别名1, 别名2) — 一句话概括 #标签1 #标签2
    如果文件不存在或解析失败，退化为最小格式 - [[页面名]]。
    """
    if not page_path.exists():
        return f"- [[{page_name}]]"
    try:
        text = page_path.read_text(encoding="utf-8")
    except Exception:
        return f"- [[{page_name}]]"

    aliases = []
    tags = []
    if text.startswith("---"):
        parts = _robust_fm_split(text)
        if len(parts) >= 3:
            import yaml
            try:
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict):
                    aliases = fm.get("aliases", []) or []
                    tags = fm.get("tags", []) or []
            except Exception:
                pass

    summary = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            for j in range(i + 1, min(i + 4, len(lines))):
                next_stripped = lines[j].strip()
                if next_stripped.startswith("> "):
                    summary = next_stripped[2:].strip()
                    break
                elif next_stripped and not next_stripped.startswith(">"):
                    break
            break

    alias_part = ""
    if isinstance(aliases, list) and aliases:
        alias_str = ", ".join(str(a) for a in aliases[:3])
        alias_part = f" ({alias_str})"

    summary_part = f" — {summary}" if summary else ""

    tag_part = ""
    if isinstance(tags, list) and tags:
        tag_str = " ".join(f"#{t}" for t in tags[:4])
        tag_part = f" {tag_str}"

    return f"- [[{page_name}]]{alias_part}{summary_part}{tag_part}"


def _auto_append_to_index(pages_without_index: list[tuple[str, str]]):
    """将无 _index.md 配对的知识页自动追加到对应 _index.md 的「待整理」分区。

    不做分类（分类交给 relocate_pending_entries 用 LLM 完成），
    只确保知识页不被丢弃、条目进入索引。
    """
    import config as cfg
    from collections import defaultdict

    by_dir: dict[str, list[str]] = defaultdict(list)
    for rel_path, actual_dir in pages_without_index:
        by_dir[actual_dir].append(rel_path)

    for dir_name, rel_paths in by_dir.items():
        idx_path = cfg.WIKI_DIR / dir_name / "_index.md"
        if not idx_path.exists():
            pt = cfg.get_page_types()
            type_info = pt.get(dir_name, {})
            description = type_info.get("description", dir_name)
            cn_name = description.split("—")[0].strip() if "—" in description else dir_name
            idx_path.parent.mkdir(parents=True, exist_ok=True)
            idx_content = f"# {cn_name}\n> {description}\n\n## 待整理\n"
            idx_path.write_text(idx_content, encoding="utf-8")
            _wfs_mirror_write(idx_path, idx_content)
            print(f"  📂 自动创建 {dir_name}/_index.md")

        text = idx_path.read_text(encoding="utf-8")

        existing_links = set(re.findall(r'\[\[([^\]]+)\]\]', text))

        appended = []
        for rel_path in rel_paths:
            page_name = rel_path.split("/")[-1].removesuffix(".md")
            if page_name in existing_links:
                continue  # 已在索引中

            page_path = cfg.WIKI_DIR / dir_name / f"{page_name}.md"
            if not page_path.exists():
                print(f"  ⚠️ 跳过跨目录引用: {page_name} 不存在于 {dir_name}/")
                continue

            entry = _build_full_index_entry(page_name, page_path)
            appended.append((page_name, entry))

        if not appended:
            continue

        pending_match = re.search(r'^(## .*待整理.*)$', text, re.MULTILINE)
        if pending_match:
            after_pending = text[pending_match.end():]
            next_section = re.search(r'^## ', after_pending, re.MULTILINE)
            if next_section:
                insert_pos = pending_match.end() + next_section.start()
                new_entries = "\n".join(e for _, e in appended)
                text = text[:insert_pos].rstrip() + "\n" + new_entries + "\n\n" + text[insert_pos:]
            else:
                new_entries = "\n".join(e for _, e in appended)
                text = text.rstrip() + "\n" + new_entries + "\n"
        else:
            new_entries = "\n".join(e for _, e in appended)
            text = text.rstrip() + "\n\n## 待整理\n\n" + new_entries + "\n"

        idx_path.write_text(text, encoding="utf-8")
        _wfs_mirror_write(idx_path, text)
        names = [n for n, _ in appended]
        print(f"  📑 自动追加到 {dir_name}/_index.md 待整理: {len(names)} 个知识页（{', '.join(names[:5])}{'...' if len(names) > 5 else ''}）")

        try:
            from error_book import record_lint_issues
            record_lint_issues({
                "pending_entries": [
                    {"name": f"{dir_name}/{n}", "context": f"新页面待归位到 {dir_name}/_index.md 的正确分区"}
                    for n in names
                ]
            })
        except Exception:
            pass


def _save_article_original(article_path: Path, source_time: str, title: str,
                           source_type: str = "source corpus") -> str | None:
    """将原文复制到 sources/articles/，按日期命名，保留 source_type 到 frontmatter。

    返回存档文件的 stem（如 "2025-10-10-音乐选秀是如何毁掉一代人音乐审美的？"），
    用于后续精确注入 digest 的原文链接。文件已存在时返回 None。
    """
    articles_dir = config.WIKI_DIR / "sources" / "articles"
    articles_dir.mkdir(parents=True, exist_ok=True)

    date_prefix = source_time[:10] if source_time else "unknown"
    clean_title = re.sub(r'[\u200b\u200c\u200d\ufeff\u00ad]', '', title)
    safe_title = re.sub(r'[<>:"/\\|?*\[\]]', '', clean_title)[:30].strip()
    safe_title = safe_title.replace('\u2026', '')
    safe_title = re.sub(r'\.{2,}', '', safe_title)
    safe_title = safe_title.rstrip('.')
    filename = _normalize_filename(f"{date_prefix}-{safe_title}.md")
    dest = articles_dir / filename

    if not dest.exists():
        import shutil
        shutil.copy2(article_path, dest)
        _ensure_source_type(dest, source_type)
        _wfs_mirror_write(dest)
        print(f"  📋 原文存档: sources/articles/{filename}")
        return dest.stem
    return None


def _ensure_source_type(filepath: Path, source_type: str):
    """确保文章存档的 frontmatter 中包含 source_type 字段。
    source_type: "source corpus" 或 "video source"，写入 frontmatter 时转为 article/video。"""
    type_map = {"source corpus": "article", "video source": "video"}
    type_val = type_map.get(source_type, "article")

    text = filepath.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = _robust_fm_split(text)
        if len(parts) >= 3:
            fm_text = parts[1].strip()
            if "source_type:" in fm_text:
                return  # 已有则不覆盖
            fm_text += f"\nsource_type: {type_val}"
            new_text = f"---\n{fm_text}\n---{parts[2]}"
            filepath.write_text(new_text, encoding="utf-8")
            return
    new_text = f"---\nsource_type: {type_val}\n---\n{text}"
    filepath.write_text(new_text, encoding="utf-8")


def _assert_digest_source_article(file_outputs: dict[str, str], batch_article_stems: list[str]):
    """ingest 写盘后断言：本批产生的 digest 必须带 frontmatter.source_article。

    仍缺失时：
      - 不再调用 LLM 重生成（保持主流程 LLM 开销不增）
      - 把 digest stem 与当批 article 候选一起写入错题本 (category=missing_source_article)
      - 后续 fix_missing_source_article_from_error_book 会读 sample.context.candidates
        给 LLM 做 N 选 1，命中率远高于全局 LCS
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return

    candidates = list(batch_article_stems or [])
    if not candidates:
        return

    batch_digest_stems: set[str] = set()
    for rel_path in file_outputs:
        if rel_path.startswith("wiki/sources/digests/") and not rel_path.endswith("_index.md"):
            name = rel_path.rsplit("/", 1)[-1]
            if name.endswith(".md"):
                batch_digest_stems.add(name[:-3])

    if not batch_digest_stems:
        return

    try:
        from error_book import record_sample_with_context
    except Exception:
        return

    template = {
        "description": "digest 缺少 frontmatter.source_article 字段：无法精确定位其原文",
        "constraint": (
            "生成 sources/digests/ 下摘要页时，frontmatter 必须包含 source_article 字段，"
            "值为对应原文 article 的文件名（不带 .md 后缀）"
        ),
        "needs_llm_fix": True,
    }

    missing_count = 0
    for stem in batch_digest_stems:
        md = digests_dir / f"{stem}.md"
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        parts = _robust_fm_split(text)
        if len(parts) < 3:
            continue
        fm_text = parts[1]
        has_src = any(
            line.strip().startswith("source_article:") and line.split(":", 1)[1].strip().strip('"').strip("'")
            for line in fm_text.split("\n")
        )
        if has_src:
            continue

        missing_count += 1
        record_sample_with_context(
            category="missing_source_article",
            sample_name=stem,
            context={
                "candidates": candidates,
                "batch_ingested_at": datetime.now().strftime("%Y-%m-%d"),
            },
            template=template,
        )

    if missing_count:
        print(f"  📔 {missing_count} 个 digest 缺 source_article，已登记错题本（候选 {len(candidates)} 篇）")


def _inject_article_link_to_digests(article_stems: list[str]):
    """为本次摄入的 digest 注入原文链接。

    调用场景：单篇摄入（1篇）或批量摄入每批（最多3篇）。
    digest 和 article 是一一对应的：每篇文章生成1个 digest、存1个 article。

    匹配策略：
    - 单篇：1对1，按 source_date 直接关联，百分百精确
    - 批量（同一天多篇）：按 LCS 贪心匹配，从本批 article 中选最佳配对
    """
    if not article_stems:
        return

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return

    art_by_date: dict[str, list[str]] = {}
    for stem in article_stems:
        if len(stem) >= 10 and stem[4] == '-' and stem[7] == '-':
            art_by_date.setdefault(stem[:10], []).append(stem)

    digest_by_date: dict[str, list[Path]] = {}
    for md in digests_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        text = md.read_text(encoding="utf-8")

        if "source_article:" in text[:500]:
            continue

        source_date = ""
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if line.strip().startswith("source_date:"):
                        source_date = line.split(":", 1)[1].strip().strip('"')[:10]
                        break
        if not source_date and len(md.stem) >= 10 and md.stem[4] == '-':
            source_date = md.stem[:10]

        if source_date and source_date in art_by_date:
            digest_by_date.setdefault(source_date, []).append(md)

    matched_pairs: list[tuple[Path, str]] = []  # (digest_path, article_stem)
    used_articles = set()

    for date, digests in digest_by_date.items():
        articles = art_by_date[date]

        if len(articles) == 1:
            for md in digests:
                matched_pairs.append((md, articles[0]))
            used_articles.add(articles[0])
            continue

        pairs = []
        for md in digests:
            dig_title = md.stem[11:] if len(md.stem) > 11 else md.stem
            for art_stem in articles:
                if art_stem in used_articles:
                    continue
                art_title = art_stem[11:] if len(art_stem) > 11 else art_stem
                lcs = _lcs_len(dig_title, art_title)
                score = lcs / max(len(dig_title), len(art_title), 1)
                pairs.append((score, md, art_stem))

        pairs.sort(key=lambda x: x[0], reverse=True)
        used_digests = set()
        for score, md, art_stem in pairs:
            if md in used_digests or art_stem in used_articles:
                continue
            if score >= 0.1:
                matched_pairs.append((md, art_stem))
                used_digests.add(md)
                used_articles.add(art_stem)

    injected_fm = 0
    injected_link = 0
    for md, art_stem in matched_pairs:
        text = md.read_text(encoding="utf-8")
        new_text = text

        if new_text.startswith("---"):
            parts = _robust_fm_split(new_text)
            if len(parts) >= 3:
                fm_text = parts[1].rstrip()
                fm_text += f"\nsource_article: {_yaml_quote_value(art_stem)}"
                new_text = f"---\n{fm_text}\n---{parts[2]}"
                injected_fm += 1

        if "## 原文" not in new_text:
            new_text = new_text.rstrip() + f"\n\n## 原文\n- [[sources/articles/{art_stem}]]\n"
            injected_link += 1

        if new_text != text:
            md.write_text(new_text, encoding="utf-8")
            _wfs_mirror_write(md, new_text)

    if injected_fm or injected_link:
        parts = []
        if injected_fm:
            parts.append(f"注入 {injected_fm} 个 source_article 字段")
        if injected_link:
            parts.append(f"添加 {injected_link} 个原文链接")
        print(f"  🔗 注入原文链接: {', '.join(parts)}")


def _lcs_len(a: str, b: str) -> int:
    """计算两个字符串的最长公共子序列长度。"""
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, [0] * (n + 1)
    return prev[n]


def _fix_digest_article_links():
    """修正 digest 文件中 [[sources/articles/...]] 链接，并自动为缺少 ## 原文 的 digest 添加链接。

    两步操作：
    1. 修正已有但名字不匹配的 [[sources/articles/...]] 链接
    2. 对缺少 ## 原文 章节的 digest，根据日期匹配自动添加原文链接（代码完成，不依赖 LLM）
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    articles_dir = config.WIKI_DIR / "sources" / "articles"
    if not digests_dir.exists() or not articles_dir.exists():
        return 0

    art_by_date: dict[str, list[str]] = {}
    for md in articles_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        stem = md.stem
        if len(stem) >= 10 and stem[4] == '-' and stem[7] == '-':
            date_prefix = stem[:10]
            art_by_date.setdefault(date_prefix, []).append(stem)

    fixed_count = 0
    added_count = 0
    for digest_md in digests_dir.glob("*.md"):
        if digest_md.name == "_index.md":
            continue
        text = digest_md.read_text(encoding="utf-8")
        new_text = text

        for match in re.finditer(r'\[\[sources/articles/([^\]]+)\]\]', text):
            link_name = match.group(1)
            real_path = articles_dir / f"{link_name}.md"
            if real_path.exists():
                continue

            if len(link_name) >= 10 and link_name[4] == '-' and link_name[7] == '-':
                date_prefix = link_name[:10]
                link_title_part = link_name[11:]
            else:
                continue

            candidates = art_by_date.get(date_prefix, [])
            if not candidates:
                continue

            best_match = _fuzzy_match_article(link_title_part, candidates)
            if best_match and best_match != link_name:
                old_link = f"[[sources/articles/{link_name}]]"
                new_link = f"[[sources/articles/{best_match}]]"
                new_text = new_text.replace(old_link, new_link)

        if '## 原文' not in new_text:
            art_link = None
            if new_text.startswith("---"):
                parts = _robust_fm_split(new_text)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if line.strip().startswith("source_article:"):
                            art_link = line.split(":", 1)[1].strip().strip('"')
                            break

            if art_link:
                new_text = new_text.rstrip() + f"\n\n## 原文\n- [[sources/articles/{art_link}]]\n"
                added_count += 1
            else:
                digest_stem = digest_md.stem
                if len(digest_stem) >= 10 and digest_stem[4] == '-' and digest_stem[7] == '-':
                    date_prefix = digest_stem[:10]
                    digest_title_part = digest_stem[11:]
                    candidates = art_by_date.get(date_prefix, [])
                    if candidates:
                        matched = _fuzzy_match_article(digest_title_part, candidates)
                        if matched:
                            new_text = new_text.rstrip() + f"\n\n## 原文\n- [[sources/articles/{matched}]]\n"
                            added_count += 1

        if new_text != text:
            digest_md.write_text(new_text, encoding="utf-8")
            _wfs_mirror_write(digest_md, new_text)
            fixed_count += 1

    if fixed_count:
        print(f"  🔗 修正/补充 {fixed_count} 个 digest 的原文链接（其中新增 {added_count} 个）")
    return fixed_count


def _fuzzy_match_article(title_part: str, candidates: list[str]) -> str | None:
    """从候选 article stems 中模糊匹配一个最佳结果。
    使用最长公共子序列 (LCS) 计算相似度，比逐字符检查更准确。
    """
    if len(candidates) == 1:
        return candidates[0]

    best_score = 0
    best_match = None
    for cand in candidates:
        cand_title = cand[11:] if len(cand) > 11 else cand  # 去掉日期前缀
        lcs = _lcs_len(title_part, cand_title)
        score = lcs / max(len(title_part), len(cand_title), 1)
        if score > best_score:
            best_score = score
            best_match = cand

    return best_match if best_score >= 0.3 else (candidates[0] if candidates else None)


def _rebuild_sources_index():
    """代码自动重建 sources/ 下的索引，确保每个 digest 页都有索引条目。

    生成三个索引：
    - sources/_index.md: 总索引（含 digests/articles/instructions 概览）
    - sources/digests/_index.md: 摘要页索引（按月份分组）
    - sources/articles/_index.md: 原文存档索引（按月份分组）
    """
    sources_dir = config.WIKI_DIR / "sources"
    digests_dir = sources_dir / "digests"
    articles_dir = sources_dir / "articles"

    if not sources_dir.exists():
        return

    if digests_dir.exists():
        entries_by_month = {}  # "2023-08" → [条目列表]
        for md in sorted(digests_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            text = md.read_text(encoding="utf-8")
            title = md.stem
            source_date = ""
            tags_str = ""

            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("## "):
                    title = stripped[2:].strip()
                    break

            if text.startswith("---"):
                parts = _robust_fm_split(text)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if line.strip().startswith("source_date:"):
                            source_date = line.split(":", 1)[1].strip()
                        elif line.strip().startswith("tags:"):
                            tag_part = line.split(":", 1)[1].strip().strip("[]")
                            tag_list = [t.strip() for t in tag_part.replace("，", ",").split(",")][:3]
                            tags_str = " ".join(f"#{t}" for t in tag_list if t)

            month = source_date[:7] if source_date and len(source_date) >= 7 else "未知日期"
            date_prefix = f"({source_date}) " if source_date else ""
            entry = f"- [[{md.stem}]] {date_prefix}— {title} {tags_str}"
            entries_by_month.setdefault(month, []).append(entry)

        digest_lines = ["# 摘要页索引\n"]
        for month in sorted(entries_by_month.keys(), reverse=True):
            digest_lines.append(f"\n## {month}\n")
            for entry in entries_by_month[month]:
                digest_lines.append(entry)

        digests_idx = digests_dir / "_index.md"
        digests_idx.write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
        _wfs_mirror_write(digests_idx, "\n".join(digest_lines) + "\n")

    video_count = 0
    article_count_files = 0
    if articles_dir.exists():
        art_entries_by_month = {}  # "2023-08" → [条目列表]
        for md in sorted(articles_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            text = md.read_text(encoding="utf-8")
            title = md.stem
            source_date = ""
            source_type_label = ""  # video source标注

            if text.startswith("---"):
                parts = _robust_fm_split(text)
                if len(parts) >= 3:
                    for line in parts[1].strip().split("\n"):
                        if line.strip().startswith("date:"):
                            val = line.split(":", 1)[1].strip().strip('"')
                            source_date = str(val)
                        elif line.strip().startswith("title:"):
                            val = line.split(":", 1)[1].strip().strip('"')
                            if val:
                                title = val
                        elif line.strip().startswith("source_type:"):
                            st_val = line.split(":", 1)[1].strip().strip('"')
                            if st_val == "video":
                                source_type_label = "🎬"
                                video_count += 1

            if not source_type_label:
                article_count_files += 1

            if not source_date and len(md.name) >= 10 and md.name[4] == '-':
                source_date = md.name[:10]

            month = source_date[:7] if source_date and len(source_date) >= 7 else "未知日期"
            date_prefix = f"({source_date}) " if source_date else ""
            type_tag = f" {source_type_label}" if source_type_label else ""
            entry = f"- [[{md.stem}]]{type_tag} {date_prefix}— {title}"
            art_entries_by_month.setdefault(month, []).append(entry)

        art_lines = ["# 原文存档索引\n"]
        if video_count > 0:
            art_lines[0] = f"# 原文存档索引\n\n> 📄 文章 {article_count_files} 篇 | 🎬 视频 {video_count} 条\n"
        for month in sorted(art_entries_by_month.keys(), reverse=True):
            art_lines.append(f"\n## {month}\n")
            for entry in art_entries_by_month[month]:
                art_lines.append(entry)

        articles_idx = articles_dir / "_index.md"
        articles_idx.write_text("\n".join(art_lines) + "\n", encoding="utf-8")
        _wfs_mirror_write(articles_idx, "\n".join(art_lines) + "\n")

    digest_count = len(list(digests_dir.glob("*.md"))) - 1 if digests_dir.exists() else 0  # -1 for _index.md
    article_count_total = len(list(articles_dir.glob("*.md"))) - 1 if articles_dir.exists() and (articles_dir / "_index.md").exists() else len(list(articles_dir.glob("*.md"))) if articles_dir.exists() else 0
    art_detail = ""
    if articles_dir.exists() and (video_count > 0 or article_count_files > 0):
        art_detail = f"（📄 文章 {article_count_files} / 🎬 视频 {video_count}）"
    instructions_dir = sources_dir / "instructions"
    instruction_count = len(list(instructions_dir.glob("*.md"))) - 1 if instructions_dir.exists() and (instructions_dir / "_index.md").exists() else len(list(instructions_dir.glob("*.md"))) if instructions_dir.exists() else 0

    sources_idx_content = f"""# sources 目录索引

- **digests/** ({digest_count} 页) — 每篇文章/视频的结构化摘要
- **articles/** ({article_count_total} 页){art_detail} — 原文存档（source article/video transcript 文本）
- **instructions/** ({instruction_count} 页) — author个人风格、写作习惯、核心观点

详细摘要索引见 [digests/_index.md](digests/_index.md)
原文存档索引见 [articles/_index.md](articles/_index.md)
"""
    sources_idx = sources_dir / "_index.md"
    sources_idx.write_text(sources_idx_content, encoding="utf-8")
    _wfs_mirror_write(sources_idx, sources_idx_content)


def _extract_type_from_content(content: str) -> str:
    """从 frontmatter 中提取 type 字段。"""
    if not content.startswith("---"):
        return ""
    parts = _robust_fm_split(content)
    if len(parts) < 3:
        return ""
    try:
        import yaml
        fm = yaml.safe_load(parts[1])
        if isinstance(fm, dict):
            return fm.get("type", "")
    except Exception:
        pass
    return ""


def _inject_dates(content: str, file_exists: bool, existing_path=None) -> str:
    """在 frontmatter 中注入 created/updated 日期。

    - 新文件：created=today, updated=today
    - 已有文件（更新）：从磁盘读旧 created 保留，updated=today
    - source 类型：只注入 created，不注入 updated（一次写入不再更新）
    - LLM 输出的 created/updated 一律删掉，由代码决定
    """
    if not content.startswith("---"):
        return content

    parts = _robust_fm_split(content)
    if len(parts) < 3:
        return content

    today = datetime.now().strftime("%Y-%m-%d")
    fm_text = parts[1]

    is_source = False
    for line in fm_text.strip().split("\n"):
        if line.strip().startswith("type:") and "source" in line.split(":", 1)[1].strip():
            is_source = True
            break

    fm_lines = [line for line in fm_text.strip().split("\n")
                if not line.strip().startswith("created:") and not line.strip().startswith("updated:")]

    created = today
    if file_exists and existing_path:
        try:
            old_text = existing_path.read_text(encoding="utf-8")
            if old_text.startswith("---"):
                old_parts = _robust_fm_split(old_text)
                if len(old_parts) >= 3:
                    for line in old_parts[1].strip().split("\n"):
                        if line.strip().startswith("created:"):
                            created = line.split(":", 1)[1].strip()
                            break
        except Exception:
            pass

    has_source_date = any(line.strip().startswith("source_date:") for line in fm_lines)
    source_date_val = None
    if is_source and not has_source_date:
        if existing_path:
            stem = existing_path.stem if hasattr(existing_path, 'stem') else str(existing_path).rsplit('/', 1)[-1].removesuffix('.md')
            if len(stem) >= 10 and stem[4] == '-' and stem[7] == '-':
                source_date_val = stem[:10]
        if not source_date_val:
            for line in fm_lines:
                stripped = line.strip()
                if stripped.startswith("date:"):
                    source_date_val = stripped.split(":", 1)[1].strip().strip('"')[:10]
                    break
                if stripped.startswith("publish_date:") or stripped.startswith("pub_date:"):
                    source_date_val = stripped.split(":", 1)[1].strip().strip('"')[:10]
                    break

    new_lines = []
    inserted = False
    for line in fm_lines:
        new_lines.append(line)
        if line.strip().startswith("type:") and not inserted:
            new_lines.append(f"created: {created}")
            if not is_source:
                new_lines.append(f"updated: {today}")
            if is_source and not has_source_date and source_date_val:
                new_lines.append(f"source_date: {source_date_val}")
            inserted = True

    if not inserted:
        new_lines.insert(0, f"created: {created}")
        if not is_source:
            new_lines.insert(1, f"updated: {today}")

    return f"---\n{chr(10).join(new_lines)}\n---{parts[2]}"


def _rebuild_global_index(update_overview: bool = False) -> bool:
    """重新生成全局 index.md（一级导航：知识概览 + 目录概览）。

    index.md 结构：
    1. 顶部：# 标题 + 知识概览段落（LLM 生成，跨所有来源的综合概述）
    2. 底部：目录概览（程序生成，各目录名+描述+页数）

    各子目录的详细页面列表由 _index.md 负责，不再重复拼入。
    update_overview=True 时才调 LLM 更新知识概览，否则保留旧版知识概览。

    返回 True 表示知识概览非空（写入成功），False 表示概览为空。
    """
    from config import get_dir_catalog_text

    existing = ""
    if config.WIKI_INDEX.exists():
        existing = config.WIKI_INDEX.read_text(encoding="utf-8")

    dir_catalog = get_dir_catalog_text()

    if update_overview:
        index_contents = _get_all_index_content()
        indexes_text = ""
        for path, content in index_contents.items():
            indexes_text += f"\n### {path}\n{content[:3000]}\n"
        overview = _generate_overview(existing, dir_catalog, indexes_text)
    else:
        overview = _extract_existing_overview(existing)

    today_str = datetime.now().strftime("%Y-%m-%d")
    parts = [f"# Wiki 目录概览\n"]
    if overview:
        overview_quoted = overview.replace("\n", "\n> ")
        parts.append(f"\n> **知识概览**（更新于 {today_str}）\n>\n> {overview_quoted}\n")
    parts.append(f"\n## 目录概览\n\n{dir_catalog}\n")

    config.WIKI_INDEX.write_text("\n".join(parts), encoding="utf-8")
    _wfs_mirror_write(config.WIKI_INDEX, "\n".join(parts))
    return bool(overview)


def _extract_existing_overview(existing_index: str) -> str:
    """从现有 index.md 中提取旧版知识概览文本（不调 LLM）。"""
    if "> **知识概览**" not in existing_index:
        return ""
    m = re.search(r'> \*\*知识概览\*\*[^\n]*\n((?:>.*\n)*)', existing_index)
    if m:
        return m.group(1).replace("> ", "").replace(">", "").strip()
    return ""


def _generate_overview(existing_index: str, dir_catalog: str, indexes_text: str) -> str:
    """让 LLM 生成一份跨所有来源的综合概述（知识概览）。

    这个综述会放在 index.md 顶部，查询时作为"零跳"上下文。
    """
    from config import get_dir_catalog_text

    page_count = _count_knowledge_pages()
    if page_count < 3:
        return ""

    purpose = _read_file_safe(config.get_purpose_file(), 2000)

    old_overview = _extract_existing_overview(existing_index)

    prompt = f"""你是知识库维护专家。请为这个知识库生成一段综合概述（知识概览）。

{purpose}

{dir_catalog}

{indexes_text}

{old_overview if old_overview else "(暂无)"}

生成一段 200-400 字的综合概述，要求：
1. 总结知识库的核心知识领域和覆盖范围
2. 列出最核心的实体/概念关键词（密集排列，便于语义匹配）
3. 指出知识库的主要脉络和主题关联
4. 如果有旧版综述，基于旧版更新而非重写

只输出综述文本，不要标题、不要 markdown 格式。"""

    try:
        result = call_llm("你是知识库维护专家。", prompt, max_tokens=1024,
                          model=config.LLM_ECONOMY_MODEL, temperature=0.3)
        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r"^```\w*\n?", "", result)
            result = re.sub(r"\n?```$", "", result)
        return result.strip()
    except Exception as e:
        print(f"  ⚠️ 知识概览生成失败: {e}")
        return old_overview


def append_log(summary: str, relevance: str):
    """追加 log.md 条目。"""
    today = datetime.now().strftime("%Y-%m-%d")
    log_entry = f"## [{today}] ingest | {summary} | {relevance}\n"
    with open(config.WIKI_LOG, "a", encoding="utf-8") as f:
        f.write(log_entry)




def ingest_article(article_path: Path, force: bool = False) -> dict | None:
    """摄入单篇文章（串行，计算+写入）。"""
    setup_run_logger()

    print(f"\n{'='*50}")
    print(f"📋 单篇摄入模式 | 文件: {article_path.name}")
    print(f"{'='*50}")

    if not article_path.exists():
        print(f"  ❌ 文件不存在: {article_path}")
        return None

    text = article_path.read_text(encoding="utf-8")

    content_hash = sha256_of(text)
    cache = load_cache()
    if not force and content_hash in cache:
        print(f"  ⏭️ 跳过（已摄入）: {article_path.name}")
        return None

    title = article_path.stem
    source_time = ""
    source_type = "source corpus"
    body = text

    if text.startswith("---"):
        parts = _robust_fm_split(text)
        if len(parts) >= 3:
            try:
                import yaml
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict):
                    title = fm.get("title", title)
                    if isinstance(title, str):
                        title = title.strip('"')
                    source_time = fm.get("date", "")
                    if isinstance(source_time, date_type):
                        source_time = source_time.isoformat()
                    st = fm.get("source_type", "")
                    if st == "video":
                        source_type = "video source"
                    body = parts[2].strip()
            except Exception:
                body = text

    body = _clean_noise(body, source_type=source_type)

    if len(body) < config.INGEST_MIN_CONTENT_LEN:
        print(f"  ⏭️ 跳过（内容太短）: {article_path.name}")
        return None

    print(f"  🔍 摄入 [{title[:40]}] ...")

    page_count = _count_knowledge_pages()
    print(f"  📦 两步模式（当前 {page_count} 个知识页）")

    t1 = time.time()
    sel_sys, sel_user = build_select_pages_for_ingest_prompt(title, body)
    sel_result = call_llm_json(sel_sys, sel_user, model=config.LLM_STEP1_MODEL, temperature=0.1)

    if sel_result.get("skip", False):
        skip_reason = sel_result.get("skip_reason", "LLM 判断无知识价值")
        t_step1 = time.time() - t1
        print(f"  🚫 跳过（{skip_reason}）({t_step1:.1f}s)")
        cache = load_cache()
        cache[content_hash] = {
            "title": title,
            "ingested_at": datetime.now().isoformat(),
            "relevance": "SKIP",
            "skip_reason": skip_reason,
        }
        save_cache(cache)
        return None

    selected_pages = sel_result.get("pages_to_view", [])
    reasoning = sel_result.get("reasoning", "")
    MAX_SELECTED_PAGES = 15
    if len(selected_pages) > MAX_SELECTED_PAGES:
        print(f"  ⚠️ LLM 选了 {len(selected_pages)} 个页面，截断到 {MAX_SELECTED_PAGES} 个")
        selected_pages = selected_pages[:MAX_SELECTED_PAGES]
    t_step1 = time.time() - t1
    print(f"  🔍 LLM 选中 {len(selected_pages)} 个页面: {selected_pages[:8]}{'...' if len(selected_pages) > 8 else ''} ({t_step1:.1f}s)")
    if reasoning:
        print(f"     理由: {reasoning[:100]}")

    t2 = time.time()
    if _is_pruning_disabled():
        pages_content = _get_all_pages_content()
    else:
        pages_content = _get_selected_pages_content(selected_pages) if selected_pages else "(LLM 未选择任何已有页面，可能是全新主题)"
    sys_prompt, user_prompt = build_ingest_prompt(title, source_time, body,
                                                   existing_pages_content=pages_content,
                                                   source_type=source_type,
                                                   selected_pages=selected_pages)

    result_text = call_llm(sys_prompt, user_prompt, max_tokens=8192, timeout=900,
                           model=config.LLM_STEP2_MODEL, temperature=config.LLM_STEP2_TEMPERATURE)
    t_step2 = time.time() - t2

    file_outputs = parse_file_outputs(result_text)
    if not file_outputs:
        print(f"  ⚠️ 未输出有效文件块，可能需要检查 LLM 输出格式")
        debug_path = config.BASE_DIR / f"debug_ingest_{title[:20]}.txt"
        debug_path.write_text(result_text, encoding="utf-8")
        print(f"     原始输出已保存到 {debug_path}")
        return None

    file_list = list(file_outputs.keys())
    print(f"  📦 LLM 输出 {len(file_outputs)} 个文件: {', '.join(f.split('/')[-1] for f in file_list[:10])}")

    article_stem = _save_article_original(article_path, source_time, title, source_type)

    write_wiki_files(file_outputs, selected_pages=selected_pages)

    if article_stem:
        _inject_article_link_to_digests([article_stem])

    _fix_digest_article_links()

    _run_quick_lint(article_stems=[article_stem] if article_stem else None)

    dir_changes = parse_dir_changes(result_text)
    _apply_ingest_dir_changes(dir_changes)

    relevance = "MEDIUM"
    summary = title[:30]
    append_log(summary, relevance)
    cache = load_cache()
    cache[content_hash] = {
        "title": title,
        "ingested_at": datetime.now().isoformat(),
        "relevance": relevance,
    }
    save_cache(cache)
    print(f"  ✅ 完成: 写入 {len(file_outputs)} 个文件 (选页 {t_step1:.1f}s + 生成 {t_step2:.1f}s)")

    return {"summary": summary, "relevance": relevance, "files": len(file_outputs)}


def ingest_batch(article_paths: list[Path], force: bool = False, batch_size: int = 3):
    """批量摄入：每批 batch_size 篇文章合并一次 LLM 调用。

    - batch_size: 每批合并的文章数（默认3）
    - 同一批内的文章一起传给 LLM，避免多篇写入 _index.md 互相覆盖
    - 批与批之间串行，每批写入后缓存失效
    """
    setup_run_logger()

    print(f"\n{'='*50}")
    print(f"📋 批量摄入模式 | 总计 {len(article_paths)} 篇 | batch_size={batch_size} | force={force}")
    print(f"{'='*50}")

    total = len(article_paths)

    cache = load_cache()
    to_process = []
    skipped = 0
    for path in article_paths:
        if not path.exists():
            skipped += 1
            continue
        text = path.read_text(encoding="utf-8")
        content_hash = sha256_of(text)
        if not force and content_hash in cache:
            skipped += 1
            continue
        to_process.append(path)

    if not to_process:
        print(f"全部已摄入或跳过 ({skipped} 篇)")
        return

    print(f"待摄入: {len(to_process)} 篇 | 已跳过: {skipped} 篇 | 批量大小: {batch_size}")

    parsed_articles = []  # [(path, parsed_dict)]
    filtered_hashes = {}   # 内容过短/无效被过滤的文章，需写入 cache 避免重复处理
    for path in to_process:
        art = _parse_article(path)
        if art:
            parsed_articles.append((path, art))
        else:
            skipped += 1
            try:
                text = path.read_text(encoding="utf-8")
                h = sha256_of(text)
                filtered_hashes[h] = {
                    "title": path.stem,
                    "ingested_at": datetime.now().isoformat(),
                    "relevance": "SKIP",
                    "skip_reason": "内容过短或清洗后无有效正文",
                }
            except (OSError, UnicodeDecodeError):
                pass

    if filtered_hashes:
        _cache = load_cache()
        _cache.update(filtered_hashes)
        save_cache(_cache)
        print(f"  📝 {len(filtered_hashes)} 篇内容过短文章已记入缓存（跳过）")

    if not parsed_articles:
        print("无有效文章可摄入")
        return

    success = 0
    failed = 0
    total_batches = (len(parsed_articles) + batch_size - 1) // batch_size
    t_total_start = time.time()

    _periodic_state_file = config.WIKI_DIR / ".periodic-state.json"
    _periodic_state = {}
    if _periodic_state_file.exists():
        try:
            _periodic_state = json.loads(_periodic_state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    _last_periodic_at = _periodic_state.get("last_periodic_at", 0)
    _current_total = len(cache)  # cache 已在上面加载过
    articles_since_last_periodic = _current_total - _last_periodic_at
    if articles_since_last_periodic < 0:
        articles_since_last_periodic = 0
    if articles_since_last_periodic > 0:
        print(f"  📌 断点续传：距上次定期维护已累计 {articles_since_last_periodic} 篇"
              f"（每 {config.INGEST_PERIODIC_EVERY} 篇触发一次）")

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        end = min(start + batch_size, len(parsed_articles))
        batch = parsed_articles[start:end]

        print(f"\n{'='*50}")
        print(f"批次 {batch_idx+1}/{total_batches}: {len(batch)} 篇文章")
        for _, art in batch:
            print(f"  - {art['title'][:40]}")

        t_batch_start = time.time()
        try:
            result = _ingest_batch_one(batch, force=force)
            if result:
                success += result["success"]
                failed += result["failed"]
                skipped += result.get("skipped", 0)
                articles_since_last_periodic += result["success"]
            else:
                failed += len(batch)
        except Exception as e:
            import traceback
            print(f"  ❌ 批次 {batch_idx+1} 失败: {e}")
            print(f"     Traceback:\n{traceback.format_exc()}")
            failed += len(batch)
        t_batch = time.time() - t_batch_start
        t_total = time.time() - t_total_start
        print(f"  ⏱️ 本批耗时 {t_batch:.1f}s | 累计 {t_total:.1f}s")

        try:
            from lint import _rebuild_knowledge_index
            idx_added = _rebuild_knowledge_index()
            if idx_added > 0:
                print(f"  ✅ 知识页索引补全: 补入 {idx_added} 个缺失页面")
        except Exception as e:
            print(f"  ⚠️ 知识页索引补全失败: {e}")

        if articles_since_last_periodic >= config.INGEST_PERIODIC_EVERY:
            _check_periodic_tasks()
            articles_since_last_periodic = 0

    if articles_since_last_periodic > 0:
        _check_periodic_tasks()

    MAX_FINALIZE_ROUNDS = 3  # 最多跑 3 轮完整闭环
    for finalize_round in range(1, MAX_FINALIZE_ROUNDS + 1):
        print(f"\n{'─'*50}")
        print(f"  🔄 收尾修复 第 {finalize_round}/{MAX_FINALIZE_ROUNDS} 轮")
        print(f"{'─'*50}")

        code_fixed = 0
        try:
            from lint import quick_lint, auto_fix_all, print_quick_lint
            issues = quick_lint()
            if not issues:
                print(f"  ✅ 代码检查：无格式错误")
            else:
                print(f"  📋 代码检查：发现 {sum(len(v) for v in issues.values())} 个问题")
                fixes = auto_fix_all()
                code_fixed = sum(fixes.values())
                if code_fixed > 0:
                    parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
                    print(f"  🔧 代码修复：修复 {code_fixed} 个 ({', '.join(parts)})")
                else:
                    print(f"  ⏹️ 代码修复：无法自动修复的问题:")
                    print_quick_lint(issues)
        except Exception as e:
            print(f"  ⚠️ 代码修复失败: {e}")

        model_fixed = False
        try:
            from error_book import has_unfixed_samples
            has_work = (has_unfixed_samples("broken_link")
                        or has_unfixed_samples("digest_incomplete")
                        or has_unfixed_samples("missing_summary")
                        or has_unfixed_samples("missing_source_article")
                        or has_unfixed_samples("pending_entries")
                        or has_unfixed_samples("index_error")
                        or has_unfixed_samples("missing_sections"))
            if has_work:
                print(f"  🤖 模型修复...")
                _check_periodic_tasks()
                model_fixed = True
            else:
                print(f"  ✅ 模型修复：无待修复项")
        except Exception as e:
            print(f"  ⚠️ 模型修复失败: {e}")

        if code_fixed == 0 and not model_fixed:
            print(f"  ✅ 第 {finalize_round} 轮无新修复，收尾完成")
            break

    print(f"\n  🔄 收尾整理（合并/拆分、别名检测、目录优化、知识概览）...")
    try:
        fix_related_source_format()
    except Exception as e:
        print(f"  ⚠️ 相关来源修复失败: {e}")
    try:
        merge_duplicate_pages()
    except Exception as e:
        print(f"  ⚠️ 知识页合并检查失败: {e}")
    try:
        split_overloaded_pages()
    except Exception as e:
        print(f"  ⚠️ 知识页拆分检查失败: {e}")
    try:
        from lint import detect_alias_overlaps
        detect_alias_overlaps()
    except Exception as e:
        print(f"  ⚠️ 别名重复检测失败: {e}")
    try:
        from lint import consolidate_wiki
        result = consolidate_wiki(dry_run=False, total_ingested=len(load_cache()))
        status = result.get("status", "")
        if status == "executed":
            suggestions = result.get("suggestions", [])
            print(f"  ✅ 目录优化完成，执行了 {len(suggestions)} 项变更")
        elif status == "no_changes":
            print(f"  ✅ 目录结构无需优化")
        elif status == "skipped":
            print(f"  ⏭️ 跳过: {result.get('reason', '')}")
    except Exception as e:
        print(f"  ⚠️ 目录优化失败: {e}")
    try:
        overview_ok = _rebuild_global_index(update_overview=True)
        if overview_ok:
            print(f"  ✅ 知识概览已更新")
    except Exception as e:
        print(f"  ⚠️ 知识概览更新失败: {e}")

    try:
        from lint import quick_lint, print_quick_lint, auto_fix_all
        final_issues = quick_lint()
        if not final_issues:
            print(f"\n  ✅ 收尾后零格式错误 🎉")
        else:
            total_remaining = sum(len(v) for v in final_issues.values())
            print(f"\n  ⚠️ 收尾整理后发现 {total_remaining} 个问题，尝试代码修复...")
            fixes = auto_fix_all()
            code_fixed = sum(fixes.values())
            if code_fixed > 0:
                parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
                print(f"  🔧 代码修复: {code_fixed} 个 ({', '.join(parts)})")
            final_issues = quick_lint()
            if not final_issues:
                print(f"\n  ✅ 收尾后零格式错误 🎉")
            else:
                total_remaining = sum(len(v) for v in final_issues.values())
                print(f"\n  ⚠️ 代码修复后仍有 {total_remaining} 个问题，尝试模型修复...")
                try:
                    from error_book import has_unfixed_samples, record_lint_issues
                    record_lint_issues(final_issues)
                    has_work = (has_unfixed_samples("broken_link")
                                or has_unfixed_samples("digest_incomplete")
                                or has_unfixed_samples("missing_summary")
                                or has_unfixed_samples("missing_source_article")
                                or has_unfixed_samples("pending_entries")
                                or has_unfixed_samples("index_error")
                                or has_unfixed_samples("missing_sections"))
                    if has_work:
                        _check_periodic_tasks()
                        print(f"  🤖 模型修复完成")
                    else:
                        print(f"  ✅ 模型修复：无待修复项")
                except Exception as e:
                    print(f"  ⚠️ 模型修复失败: {e}")
                final_issues = quick_lint()
                if final_issues:
                    total_remaining = sum(len(v) for v in final_issues.values())
                    print(f"\n  ⚠️ 收尾后仍有 {total_remaining} 个问题（可能需要人工介入或主观判断）:")
                    print_quick_lint(final_issues)
                else:
                    print(f"\n  ✅ 收尾后零格式错误 🎉")
    except Exception as e:
        print(f"  ⚠️ 最终检查失败: {e}")

    t_total = time.time() - t_total_start
    print(f"\n{'='*50}")
    print(f"批量摄入完成: 成功 {success} / 跳过 {skipped} / 失败 {failed} / 总计 {total} | 总耗时 {t_total:.1f}s")


def _run_quick_lint(article_stems: list[str] | None = None):
    """摄入后自动运行快速代码检查。

    参数：
      article_stems  当批文章的 stem 列表（如 ["2024-01-01-xxx", "2024-01-02-yyy"]），
                     用于在错题本中记录断链时关联原始文章，供后续 LLM 定期修复使用。
    """
    try:
        from lint import quick_lint, print_quick_lint, auto_fix_all
        issues = quick_lint()
        print_quick_lint(issues)

        if issues:
            try:
                from error_book import record_lint_issues
                record_lint_issues(issues, batch_article_stems=article_stems)
            except ImportError:
                pass

            fixes = auto_fix_all()
            total = sum(fixes.values())
            if total > 0:
                parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
                print(f"  ✅ 已自动修复 {total} 个问题 ({', '.join(parts)})")
    except Exception as e:
        print(f"  ⚠️ 快速检查失败: {e}")


def _clean_noise(text: str, source_type: str = "source corpus") -> str:
    """清洗文章噪声：关注引导、署名行、页脚广告等。

    source_type: "source corpus" 或 "video source"，video transcript 文本有不同的噪声模式。
    """
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.search(r'(长按|扫码|二维码|点击.*关注|号|source corpus.*ID|关注我们?$|关注本号|关注source corpus|扫码关注|长按关注)', stripped) and len(stripped) < 80:
            continue
        if re.search(r'(版权归.*所有|转载.*请联系|本文来源[:：]|图片来源[:：])', stripped) and len(stripped) < 80:
            continue
        if re.search(r'^[-\s*#]*(?:编辑|排版|责编|美编)[:：]\s*\S{2,6}\s*$', stripped) and len(stripped) < 80:
            continue
        if re.search(r'(限时优惠|团购价|团购链接|点击报名|免费领|立即购买|戳.*原文|抽奖活动)', stripped) and len(stripped) < 80:
            continue
        if stripped in ('—', '——', '***', '---', '===', '- - -'):
            continue
        cleaned.append(line)

    result = "\n".join(cleaned)

    if source_type == "video source":
        result = re.sub(
            r'(?:那就)?赶紧拍下[^。？！\n]*[。？！]?',
            '', result
        )
        result = re.sub(
            r'(?:所以)?感兴趣的(?:朋友)?赶紧[^。？！\n]*[。？！]?',
            '', result
        )
        result = re.sub(
            r'我(?:都)?把这些内容[^。？！\n]*小灶[^。？！\n]*[。？！]?',
            '', result
        )
        result = re.sub(
            r'放在了?我们的(?:加更|嘉庚|嘉更|小灶)[^。？！\n]*[。？！]?',
            '', result
        )

    return result


def _parse_article(article_path: Path) -> dict | None:
    """解析单篇文章，返回 dict 或 None（跳过）。"""
    if not article_path.exists():
        return None

    try:
        text = article_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    title = article_path.stem
    source_time = ""
    source_type = "source corpus"  # 默认source corpus
    body = text

    if text.startswith("---"):
        parts = _robust_fm_split(text)
        if len(parts) >= 3:
            try:
                import yaml
                fm = yaml.safe_load(parts[1])
                if isinstance(fm, dict):
                    title = fm.get("title", title)
                    if isinstance(title, str):
                        title = title.strip('"')
                    source_time = fm.get("date", "")
                    if isinstance(source_time, date_type):
                        source_time = source_time.isoformat()
                    st = fm.get("source_type", "")
                    if st == "video":
                        source_type = "video source"
                    body = parts[2].strip()
            except Exception:
                body = text

    body = _clean_noise(body, source_type=source_type)

    if len(body) < config.INGEST_MIN_CONTENT_LEN:
        return None

    return {
        "title": title,
        "source_time": source_time,
        "source_type": source_type,
        "content": body,
        "content_hash": sha256_of(text),
    }


def _ingest_batch_one(batch: list[tuple[Path, dict]], force: bool = False) -> dict | None:
    """处理一批文章：一次选页 + 一次生成 + 一次写入。"""
    paths = [p for p, _ in batch]
    articles = [a for _, a in batch]

    page_count = _count_knowledge_pages()
    print(f"  📦 两步模式（当前 {page_count} 个知识页）")

    t1 = time.time()
    combined_titles = ", ".join(a["title"][:20] for a in articles)
    combined_content = "\n\n".join(
        f"【{a['title']}】\n{a['content'][:config.INGEST_MAX_CONTENT_LEN]}"
        for a in articles
    )
    sel_sys, sel_user = build_select_pages_for_ingest_prompt(combined_titles, combined_content)
    sel_result = call_llm_json(sel_sys, sel_user, model=config.LLM_STEP1_MODEL, temperature=0.1)

    skipped_articles = set()
    if sel_result.get("skip", False):
        skip_reason = sel_result.get("skip_reason", "LLM 判断无知识价值")
        t_step1 = time.time() - t1
        print(f"  🚫 整批跳过（{skip_reason}）({t_step1:.1f}s)")
        cache = load_cache()
        for art in articles:
            cache[art["content_hash"]] = {
                "title": art["title"],
                "ingested_at": datetime.now().isoformat(),
                "relevance": "SKIP",
                "skip_reason": skip_reason,
            }
        save_cache(cache)
        return {"success": 0, "failed": 0, "skipped": len(articles)}

    skip_articles_list = sel_result.get("skip_articles", [])
    if skip_articles_list:
        for skip_info in skip_articles_list:
            if isinstance(skip_info, dict):
                skip_title = skip_info.get("title", "")
                skip_reason = skip_info.get("reason", "无知识价值")
            elif isinstance(skip_info, str):
                skip_title = skip_info
                skip_reason = "无知识价值"
            else:
                continue
            skipped_articles.add(skip_title)
            print(f"  🚫 跳过文章: {skip_title[:30]}（{skip_reason}）")

    if skipped_articles:
        cache = load_cache()
        new_paths = []
        new_articles = []
        for path, art in zip(paths, articles):
            if art["title"] in skipped_articles:
                cache[art["content_hash"]] = {
                    "title": art["title"],
                    "ingested_at": datetime.now().isoformat(),
                    "relevance": "SKIP",
                    "skip_reason": "LLM 判断无知识价值",
                }
            else:
                new_paths.append(path)
                new_articles.append(art)
        save_cache(cache)
        n_skipped_in_filter = len(articles) - len(new_articles)
        paths = new_paths
        articles = new_articles

        if not articles:
            t_step1 = time.time() - t1
            print(f"  🚫 批次中所有文章均被跳过 ({t_step1:.1f}s)")
            return {"success": 0, "failed": 0, "skipped": n_skipped_in_filter}

    selected_pages = sel_result.get("pages_to_view", [])
    reasoning = sel_result.get("reasoning", "")
    MAX_SELECTED_PAGES = 15
    if len(selected_pages) > MAX_SELECTED_PAGES:
        print(f"  ⚠️ LLM 选了 {len(selected_pages)} 个页面，截断到 {MAX_SELECTED_PAGES} 个")
        selected_pages = selected_pages[:MAX_SELECTED_PAGES]
    t_step1 = time.time() - t1
    print(f"  🔍 LLM 选中 {len(selected_pages)} 个页面: {selected_pages[:8]}{'...' if len(selected_pages) > 8 else ''} ({t_step1:.1f}s)")
    if reasoning:
        print(f"     理由: {reasoning[:100]}")

    t2 = time.time()
    if _is_pruning_disabled():
        pages_content = _get_all_pages_content()
    else:
        pages_content = _get_selected_pages_content(selected_pages) if selected_pages else "(LLM 未选择任何已有页面，可能是全新主题)"
    sys_prompt, user_prompt = build_ingest_prompt_batch(articles, existing_pages_content=pages_content,
                                                         selected_pages=selected_pages)

    result_text = call_llm(sys_prompt, user_prompt, max_tokens=16384, timeout=900,
                           model=config.LLM_STEP2_MODEL, temperature=config.LLM_STEP2_TEMPERATURE)
    t_step2 = time.time() - t2

    file_outputs = parse_file_outputs(result_text)
    if not file_outputs:
        print(f"  ⚠️ 未输出有效文件块，可能需要检查 LLM 输出格式")
        debug_path = config.BASE_DIR / f"debug_ingest_batch_{datetime.now().strftime('%H%M%S')}.txt"
        debug_path.write_text(result_text, encoding="utf-8")
        print(f"     原始输出已保存到 {debug_path}")
        return None

    file_list = list(file_outputs.keys())
    print(f"  📦 LLM 输出 {len(file_outputs)} 个文件: {', '.join(f.split('/')[-1] for f in file_list[:10])}")

    article_stems = []
    for path, art in zip(paths, articles):
        stem = _save_article_original(path, art["source_time"], art["title"], art.get("source_type", "source corpus"))
        if stem:
            article_stems.append(stem)

    write_wiki_files(file_outputs, selected_pages=selected_pages)
    invalidate_graph_cache()

    if article_stems:
        _inject_article_link_to_digests(article_stems)

    _fix_digest_article_links()

    _assert_digest_source_article(file_outputs, article_stems)

    _run_quick_lint(article_stems=article_stems if article_stems else None)

    dir_changes = parse_dir_changes(result_text)
    _apply_ingest_dir_changes(dir_changes)

    cache = load_cache()
    success = 0
    for art in articles:
        cache[art["content_hash"]] = {
            "title": art["title"],
            "ingested_at": datetime.now().isoformat(),
            "relevance": "MEDIUM",
        }
        append_log(art["title"][:30], "MEDIUM")
        success += 1
    save_cache(cache)

    print(f"  ✅ 批次完成: 写入 {len(file_outputs)} 个文件，摄入 {success} 篇文章 (选页 {t_step1:.1f}s + 生成 {t_step2:.1f}s)")
    return {"success": success, "failed": 0, "skipped": len(skipped_articles)}


def select_seed_articles(n: int = 50) -> list[Path]:
    """从 raw/articles/ 中智能选取种子文章。"""
    articles = sorted(config.RAW_DIR.glob("*.md"))

    low_priority_keywords = ["祝", "快乐", "节日", "通知", "报名", "活动", "团购", "优惠",
                             "限时", "福利", "抽奖", "免费领"]

    scored = []
    for art in articles:
        text = art.read_text(encoding="utf-8")
        title = art.stem
        for line in text.split("\n"):
            if line.startswith("title:"):
                title = line.replace("title:", "").strip().strip('"')
                break
            if line.startswith("# "):
                title = line[2:].strip()
                break

        score = 0
        for kw in low_priority_keywords:
            if kw in title:
                score -= 3

        if len(text) > 3000:
            score += 2
        if len(text) > 6000:
            score += 2
        if len(text) > 10000:
            score += 1

        scored.append((score, art, title))

    scored.sort(key=lambda x: -x[0])
    selected = [(s[1], s[2], s[0]) for s in scored[:n]]

    print(f"选取 {len(selected)} 篇种子文章（按优先级排序）:")
    for path, title, score in selected[:10]:
        print(f"  [{score:+d}] {title[:40]}")
    if len(selected) > 10:
        print(f"  ... 还有 {len(selected) - 10} 篇")

    return [s[0] for s in selected]


def _auto_consolidate(ingested_count: int):
    """摄入足够多文章后，自动触发目录结构优化。（保留兼容）"""
    _check_periodic_tasks()


def _check_periodic_tasks():
    """检查并执行定期任务：错题本修复、知识概览更新、目录结构优化、矛盾检测。

    在批量摄入过程中，每累计摄入 INGEST_PERIODIC_EVERY 篇文章后自动调用一次。
    这样能保证错题量始终可控，不会无限累积。

    用 .wiki-periodic-state.json 记录上次触发时的篇数，
    避免取模跳过（比如一批 3 篇从 14→17 会跳过 15）。
    - 每次触发：修复错题本中的断链和不完整摘要
    - 每累计 INGEST_OVERVIEW_EVERY 篇：更新知识概览
    - 每累计 INGEST_CONSOLIDATE_EVERY 篇：目录结构优化 + 更新知识概览
    - 每累计 INGEST_CONTRADICTION_EVERY 篇：LLM 矛盾检测
    """
    cache = load_cache()
    total_ingested = len(cache)

    state_file = config.WIKI_DIR / ".periodic-state.json"
    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    last_overview = state.get("last_overview_at", 0)
    last_consolidate = state.get("last_consolidate_at", 0)
    last_contradiction = state.get("last_contradiction_at", 0)

    need_overview = (total_ingested - last_overview) >= config.INGEST_OVERVIEW_EVERY
    need_consolidate = (total_ingested - last_consolidate) >= config.INGEST_CONSOLIDATE_EVERY
    need_contradiction = (total_ingested - last_contradiction) >= config.INGEST_CONTRADICTION_EVERY

    has_active_error_fix = False
    try:
        from error_book import has_unfixed_samples
        has_active_error_fix = (has_unfixed_samples("broken_link")
                                or has_unfixed_samples("digest_incomplete")
                                or has_unfixed_samples("missing_summary")
                                or has_unfixed_samples("missing_source_article")
                                or has_unfixed_samples("pending_entries")
                                or has_unfixed_samples("index_error")
                                or has_unfixed_samples("missing_sections"))
    except Exception:
        pass

    if not need_overview and not need_consolidate and not need_contradiction and not has_active_error_fix:
        state["last_periodic_at"] = total_ingested
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return

    print(f"\n{'='*50}")
    print(f"🔄 定期维护（已累计摄入 {total_ingested} 篇）")

    try:
        from error_book import load_error_book, save_error_book, _normalize_sample
        _eb = load_error_book()
        _eb_changed = False
        for _e in _eb:
            if _e.get("category") != "unseen_page_overwrite":
                continue
            if _e.get("status") == "closed":
                continue
            for _s in _e.get("samples", []):
                _s_n = _normalize_sample(_s)
                if not _s_n.get("fixed"):
                    _s_n["fixed"] = True
                    _eb_changed = True
            _e["status"] = "closed"
            _e["closed_at"] = datetime.now().strftime("%Y-%m-%d")
            _eb_changed = True
            print(f"  📔 自动关闭 {_e.get('id', '?')} (unseen_page_overwrite): 代码已拦截，无需修复")
        if _eb_changed:
            save_error_book(_eb)
    except Exception as _ex:
        print(f"  ⚠️ 关闭 unseen_page_overwrite 失败: {_ex}")

    try:
        _fix_digest_article_links()
    except Exception as e:
        print(f"  ⚠️ digest article 链接修正失败: {e}")

    try:
        fix_incomplete_digests()
    except Exception as e:
        print(f"  ⚠️ 摘要补全失败: {e}")

    try:
        fix_missing_source_article_from_error_book()
    except Exception as e:
        print(f"  ⚠️ source_article 回填失败: {e}")

    try:
        from lint import relocate_pending_entries
        relocate_pending_entries(dry_run=False)
    except Exception as e:
        print(f"  ⚠️ 待整理归位失败: {e}")

    try:
        from lint import merge_small_sections
        merge_result = merge_small_sections(dry_run=False)
        if merge_result.get("sections_merged", 0) > 0:
            print(f"  🔀 分区合并: 合并 {merge_result['sections_merged']} 个小分区，移动 {merge_result['entries_moved']} 条")
    except Exception as e:
        print(f"  ⚠️ 分区合并失败: {e}")

    try:
        fix_missing_summary()
    except Exception as e:
        print(f"  ⚠️ 一句话概括修复失败: {e}")

    try:
        fix_missing_sections()
    except Exception as e:
        print(f"  ⚠️ 知识页章节补全失败: {e}")

    try:
        fix_broken_links_from_error_book()
    except Exception as e:
        print(f"  ⚠️ 断链修复失败: {e}")


    if need_consolidate:
        try:
            fix_related_source_format()
        except Exception as e:
            print(f"  ⚠️ 相关来源修复失败: {e}")

    if need_consolidate:
        try:
            merge_duplicate_pages()
        except Exception as e:
            print(f"  ⚠️ 知识页合并检查失败: {e}")

    if need_consolidate:
        try:
            split_overloaded_pages()
        except Exception as e:
            print(f"  ⚠️ 知识页拆分检查失败: {e}")

    if need_consolidate:
        try:
            from lint import detect_alias_overlaps
            detect_alias_overlaps()
        except Exception as e:
            print(f"  ⚠️ 别名重复检测失败: {e}")

    if need_overview:
        print(f"  📝 更新知识概览（上次: {last_overview} 篇时）...")
        try:
            overview_ok = _rebuild_global_index(update_overview=True)
            if overview_ok:
                state["last_overview_at"] = total_ingested
                print(f"  ✅ 知识概览已更新")
            else:
                print(f"  ⚠️ 知识概览为空，下次将重试")
        except Exception as e:
            print(f"  ⚠️ 知识概览更新失败: {e}")

    if need_consolidate:
        print(f"  📂 检查目录结构（上次: {last_consolidate} 篇时）...")
        try:
            from lint import consolidate_wiki
            result = consolidate_wiki(dry_run=False, total_ingested=total_ingested)
            status = result.get("status", "")
            if status == "executed":
                suggestions = result.get("suggestions", [])
                print(f"  ✅ 目录优化完成，执行了 {len(suggestions)} 项变更")
                overview_ok = _rebuild_global_index(update_overview=True)
                if overview_ok:
                    state["last_overview_at"] = total_ingested
            elif status == "no_changes":
                print(f"  ✅ 目录结构无需优化")
            elif status == "skipped":
                print(f"  ⏭️ 跳过: {result.get('reason', '')}")
            state["last_consolidate_at"] = total_ingested
        except Exception as e:
            print(f"  ⚠️ 目录优化失败: {e}")

    if need_contradiction:
        print(f"  🔍 LLM 矛盾检测（上次: {last_contradiction} 篇时）...")
        try:
            from lint import _detect_contradictions_with_llm
            from wiki_page import WikiGraph
            graph = WikiGraph()
            graph.load_all()
            if len(graph.pages) >= 5:
                contradictions = _detect_contradictions_with_llm(graph)
                if contradictions:
                    print(f"  ⚠️ 发现 {len(contradictions)} 处矛盾:")
                    for c in contradictions:
                        print(f"    {c.get('page_a', '?')} ↔ {c.get('page_b', '?')}: {c.get('conflict', '')}")
                    try:
                        from error_book import record_lint_issues
                        record_lint_issues({"contradictions": contradictions})
                    except Exception:
                        pass
                else:
                    print(f"  ✅ 未发现矛盾")
            state["last_contradiction_at"] = total_ingested
        except Exception as e:
            print(f"  ⚠️ 矛盾检测失败: {e}")

    state["last_periodic_at"] = total_ingested
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _fix_alias_broken_links(alias_matches: dict[str, str], original_names: dict[str, str], graph):
    """修正别名匹配的断链：将引用错误名称的页面中的 [[broken_name]] 替换为 [[correct_name]]。

    参数：
      alias_matches   {断链短名: 正确页面短名}，如 {"帕格尼尼": "尼科洛·帕格尼尼"}
      original_names  {断链短名: 断链全名（含目录前缀）}，如 {"帕格尼尼": "composers/帕格尼尼"}
      graph           WikiGraph 实例，用于搜索哪些页面引用了断链
    """
    import re
    from config import get_page_dirs
    dirs = get_page_dirs()

    fixed_count = 0
    for broken_short, correct_short in alias_matches.items():
        broken_full = original_names.get(broken_short, broken_short)
        if "/" in broken_full:
            dir_prefix = broken_full.rsplit("/", 1)[0]
            correct_full = f"{dir_prefix}/{correct_short}"
        else:
            correct_full = correct_short

        pages_to_fix = []
        for page in graph.pages.values():
            if any(broken_full in link or broken_short in link for link in page.outgoing_links):
                pages_to_fix.append(page)

        for page in pages_to_fix:
            page_dir = dirs.get(page.page_type)
            if page_dir is None:
                continue
            page_path = page_dir / f"{page.name}.md"
            if not page_path.exists():
                continue

            try:
                text = page_path.read_text(encoding="utf-8")
                original_text = text

                text = text.replace(f"[[{broken_full}]]", f"[[{correct_full}]]")
                text = text.replace(f"[[{broken_short}]]", f"[[{correct_short}]]")
                text = re.sub(
                    rf'\[\[{re.escape(broken_full)}\|([^\]]+)\]\]',
                    rf'[[{correct_full}|\1]]',
                    text
                )
                text = re.sub(
                    rf'\[\[{re.escape(broken_short)}\|([^\]]+)\]\]',
                    rf'[[{correct_short}|\1]]',
                    text
                )

                if text != original_text:
                    page_path.write_text(text, encoding="utf-8")
                    _wfs_mirror_write(page_path, text)
                    fixed_count += 1
                    print(f"    ✏️ 修正链接: {page.page_type}/{page.name} 中 [[{broken_short}]] → [[{correct_short}]]")
            except Exception as e:
                print(f"    ⚠️ 修正链接失败 {page.page_type}/{page.name}: {e}")

    if fixed_count:
        invalidate_graph_cache()
        print(f"  ✅ 修正了 {fixed_count} 个文件中的别名断链")

    for broken_short, correct_short in alias_matches.items():
        broken_full = original_names.get(broken_short, broken_short)
        if "/" in broken_full:
            dir_name = broken_full.rsplit("/", 1)[0]
            idx_path = dirs.get(dir_name, config.WIKI_DIR / dir_name) / "_index.md"
            if idx_path.exists():
                try:
                    text = idx_path.read_text(encoding="utf-8")
                    new_text = text.replace(f"[[{broken_short}]]", f"[[{correct_short}]]")
                    if new_text != text:
                        idx_path.write_text(new_text, encoding="utf-8")
                        _wfs_mirror_write(idx_path, new_text)
                        print(f"    ✏️ 修正 _index.md: {dir_name}/ 中 [[{broken_short}]] → [[{correct_short}]]")
                except Exception:
                    pass


def _fix_source_broken_links(source_broken_links: list, mark_samples_fixed):
    """修复 sources/digests/ 和 sources/articles/ 类型的断链。

    这类断链的根因是文件名中的特殊字符（逗号、冒号、问号等）被替换成了短横线，
    导致链接找不到文件。用模糊匹配找到正确文件名后，把正确链接写回知识页。
    纯代码操作，不需要 LLM。
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    articles_dir = config.WIKI_DIR / "sources" / "articles"

    def _build_index(directory):
        index = {}  # date_prefix → {stem: path}
        if not directory.exists():
            return index
        for md in directory.glob("*.md"):
            if md.name == "_index.md":
                continue
            stem = md.stem
            if len(stem) >= 10 and stem[4] == '-' and stem[7] == '-':
                date_prefix = stem[:10]
                index.setdefault(date_prefix, {})[stem] = md
        return index

    digest_index = _build_index(digests_dir)
    article_index = _build_index(articles_dir)

    graph = _get_graph()

    fixed_samples = []
    for source_page, broken_target, sample in source_broken_links:
        if broken_target.startswith("sources/digests/"):
            sub_path = broken_target[len("sources/digests/"):]
            file_index = digest_index
            prefix = "sources/digests/"
        elif broken_target.startswith("sources/articles/"):
            sub_path = broken_target[len("sources/articles/"):]
            file_index = article_index
            prefix = "sources/articles/"
        else:
            continue

        if len(sub_path) < 10 or sub_path[4] != '-' or sub_path[7] != '-':
            continue
        date_prefix = sub_path[:10]
        broken_title = sub_path[11:]  # 去掉日期和连接符

        candidates = file_index.get(date_prefix, {})
        if not candidates:
            continue

        def _normalize_for_match(s):
            """将特殊字符统一化，用于模糊比较"""
            return re.sub(r'[^\w\u4e00-\u9fff]', '', s)

        broken_norm = _normalize_for_match(broken_title)
        best_match = None
        best_score = 0
        for stem in candidates:
            cand_title = stem[11:] if len(stem) > 11 else stem
            cand_norm = _normalize_for_match(cand_title)
            if broken_norm == cand_norm:
                best_match = stem
                best_score = 1.0
                break
            lcs = _lcs_len(broken_norm, cand_norm)
            score = lcs / max(len(broken_norm), len(cand_norm), 1)
            if score > best_score:
                best_score = score
                best_match = stem

        if not best_match or best_score < 0.5:
            continue

        correct_link = f"{prefix}{best_match}"
        if correct_link == broken_target:
            continue  # 没有差异，不需要修复

        if source_page:
            page = graph.get_page(source_page)
            if page and page.path.exists():
                text = page.path.read_text(encoding="utf-8")
                if f"[[{correct_link}]]" not in text:
                    if "## 相关来源" in text:
                        sec_start = text.find("## 相关来源")
                        next_sec = text.find("\n## ", sec_start + 1)
                        if next_sec == -1:
                            new_text = text.rstrip() + f"\n- [[{correct_link}]]\n"
                        else:
                            new_text = text[:next_sec].rstrip() + f"\n- [[{correct_link}]]\n" + text[next_sec:]
                        if new_text != text:
                            page.path.write_text(new_text, encoding="utf-8")
                            _wfs_mirror_write(page.path, new_text)
                            print(f"  🔗 修复 sources 断链: {source_page} → [[{correct_link}]]（原: {broken_target}）")
                    else:
                        text = text.rstrip() + f"\n\n## 相关来源\n- [[{correct_link}]]\n"
                        page.path.write_text(text, encoding="utf-8")
                        _wfs_mirror_write(page.path, text)
                        print(f"  🔗 修复 sources 断链: {source_page} → [[{correct_link}]]（原: {broken_target}，新建相关来源章节）")
                else:
                    print(f"  ✅ sources 断链已修复: {source_page} 已有 [[{correct_link}]]")

        from error_book import _sample_name
        fixed_samples.append(_sample_name(sample))

    if fixed_samples:
        mark_samples_fixed("broken_link", fixed_samples)
        try:
            from error_book import append_ledger
            append_ledger(issue_type="broken_link", auto_fixed=True,
                          fix_method="fuzzy_match_source_file",
                          note=f"模糊匹配修复 {len(fixed_samples)} 个 sources 断链",
                          count=len(fixed_samples))
        except Exception:
            pass


def fix_broken_links_from_error_book():
    """从错题本读取历史断链记录，用 LLM 批量创建缺失页面。

    仅处理 broken_link 类型的活跃错题中 fixed=False 的 samples。
    修复成功后标记 sample 为 fixed=True，下次不再修复。

    核心改进：优先从错题本中记录的 context.batch_articles 读取当初产生断链时
    的那批原始文章全文，作为 LLM 输入。这样 LLM 生成的页面有真实来源依据，
    而不是凭空编造。只有当 context 缺失时才 fallback 到旧的"引用上下文"方式。
    """
    from error_book import (load_error_book, save_error_book,
                            get_unfixed_samples, get_unfixed_samples_full,
                            mark_samples_fixed, _normalize_sample, _sample_name)
    errors = load_error_book()

    broken_errors = [e for e in errors
                     if e.get("category") == "broken_link" and e.get("status") != "closed"]
    if not broken_errors:
        return

    unfixed_samples = get_unfixed_samples_full("broken_link")
    if not unfixed_samples:
        return

    missing_info = {}  # page_name → sample_dict
    source_broken_links = []  # sources/ 类型的断链，单独用模糊匹配修复
    for s in unfixed_samples:
        name = _sample_name(s)
        if " → " in name:
            source_page = name.split(" → ")[0].strip()
            target = name.split(" → ")[-1].strip("[]")
        else:
            source_page = None
            target = name.strip("[]")
        if target.startswith("sources/articles/") or target.startswith("sources/digests/"):
            source_broken_links.append((source_page, target, s))
            continue
        missing_info[target] = s

    if source_broken_links:
        _fix_source_broken_links(source_broken_links, mark_samples_fixed)

    if not missing_info:
        return

    print(f"  🔍 正在检查 {len(missing_info)} 个非 sources 断链...", flush=True)
    graph = _get_graph()
    still_missing = {}
    already_exist = []
    for name, sample in missing_info.items():
        page = graph.get_page(name)
        if page is None and "/" in name:
            short_name = name.rsplit("/", 1)[-1]
            page = graph.get_page(short_name)
        if page is None:
            still_missing[name] = sample
        else:
            already_exist.append(name)

    if already_exist:
        mark_samples_fixed("broken_link", already_exist)
        try:
            from error_book import append_ledger
            append_ledger(issue_type="broken_link", auto_fixed=False,
                          fix_method="page_already_exists", note=f"页面已存在，标记已修复: {', '.join(already_exist[:5])}",
                          count=len(already_exist))
        except Exception:
            pass

    if not still_missing:
        print("  ✅ 所有未修复的断链目标页面均已存在")
        return

    batch_info = {}  # clean_name → {"original": full_name, "sample": sample_dict}
    for full_name, sample in still_missing.items():
        clean = full_name.rsplit("/", 1)[-1] if "/" in full_name else full_name
        batch_info[clean] = {"original": full_name, "sample": sample}

    pipe_fixed = {}  # broken_clean_name → correct_link（需要修正的 | 格式断链）
    pipe_fixed_names = []
    for clean_name, info in list(batch_info.items()):
        if " | " not in clean_name:
            continue
        target_name = clean_name.split(" | ", 1)[0].strip()
        full_name = info["original"]
        if "/" in full_name:
            dir_prefix = full_name.split("/", 1)[0]
            pipe_target_full = full_name.split(" | ", 1)[0].strip()
            target_page = graph.get_page(pipe_target_full)
        else:
            target_page = graph.get_page(target_name)
        if target_page is not None:
            try:
                correct_link = str(target_page.path.relative_to(config.WIKI_DIR))[:-3]
            except ValueError:
                correct_link = target_name
            pipe_fixed[clean_name] = correct_link
            pipe_fixed_names.append(clean_name)
            broken_full = info["original"]
            import re as _re
            for page in graph.all_pages + graph.index_pages:
                if not page.path.exists():
                    continue
                if broken_full not in str(page.outgoing_links) and clean_name not in str(page.outgoing_links):
                    continue
                try:
                    text = page.path.read_text(encoding="utf-8")
                    original_text = text
                    text = text.replace(f"[[{broken_full}]]", f"[[{correct_link}]]")
                    text = text.replace(f"[[{clean_name}]]", f"[[{correct_link}]]")
                    if text != original_text:
                        page.path.write_text(text, encoding="utf-8")
                        _wfs_mirror_write(page.path, text)
                        print(f"    ✏️ 修正管道符链接: {page.name} [[{clean_name}]] → [[{correct_link}]]")
                except Exception:
                    pass

    if pipe_fixed_names:
        for name in pipe_fixed_names:
            batch_info.pop(name, None)
        mark_samples_fixed("broken_link", pipe_fixed_names)
        invalidate_graph_cache()
        print(f"  ✅ 修正了 {len(pipe_fixed_names)} 个管道符格式断链")
        if not batch_info:
            print("  ✅ 所有断链均已通过管道符修正解决")
            return

    batch = list(batch_info.keys())
    print(f"  🔗 发现 {len(batch)} 个缺失页面待处理: {', '.join(batch[:5])}{'...' if len(batch) > 5 else ''}")

    from config import get_page_dirs
    dirs = get_page_dirs()
    original_names = {clean: info["original"] for clean, info in batch_info.items()
                      if "/" in info["original"]}
    dir_index_cache = {}  # dir_name → _index.md 内容
    for clean_name in batch:
        full_name = original_names.get(clean_name, "")
        if "/" in full_name:
            dir_name = full_name.rsplit("/", 1)[0]
        else:
            dir_name = None
        if dir_name and dir_name not in dir_index_cache:
            idx_path = dirs.get(dir_name, config.WIKI_DIR / dir_name) / "_index.md"
            if idx_path.exists():
                try:
                    dir_index_cache[dir_name] = idx_path.read_text(encoding="utf-8")
                except Exception:
                    pass

    alias_matches = {}  # broken_name → correct_page_name（LLM 判断为别名匹配的）
    truly_missing = list(batch)  # 真正需要创建的页面

    if dir_index_cache:
        index_text_parts = []
        for dir_name, content in dir_index_cache.items():
            index_text_parts.append(f"### {dir_name}/_index.md\n{content}")
        index_text_for_alias = "\n\n".join(index_text_parts)

        missing_with_dir = []
        for clean_name in batch:
            full_name = original_names.get(clean_name, clean_name)
            dir_name = full_name.rsplit("/", 1)[0] if "/" in full_name else "未知"
            missing_with_dir.append(f"- 断链名: {clean_name}，所在目录: {dir_name}/")

        alias_sys = """你是知识库维护专家。你的任务是判断一批"断链"（wiki 中引用了但找不到对应页面的链接）是否只是名字不匹配——实际上目录中已有对应的页面（可能用了不同的名字、别名、全名/简称等）。

请仔细对比每个断链名和对应目录 _index.md 中的已有页面列表（注意页面名后括号中的别名/外文名也要对比）。"""

        alias_user = f"""## 目录索引内容
{index_text_for_alias}

{chr(10).join(missing_with_dir)}

对每个断链名，判断是以下哪种情况：
1. **别名匹配**：目录 _index.md 中已有对应页面，只是名字不同（别名、全名/简称、外文名/中文名、不同译名等）
2. **真正缺失**：目录中确实没有对应的页面

请严格按以下 JSON 格式输出（不要输出其他内容）：
```json
[
  {{"broken_name": "断链名", "status": "alias_match", "correct_name": "目录中已有的正确页面名"}},
  {{"broken_name": "断链名", "status": "truly_missing", "correct_name": null}}
]
```

注意：
- correct_name 必须是 _index.md 中 [[...]] 内的精确页面名（不带目录前缀）
- 只有当你很确定是同一个实体/概念时才判断为 alias_match，不确定则判断为 truly_missing
- 例如"帕格尼尼"和"尼科洛·帕格尼尼"是同一人，"肖邦"和"弗雷德里克·肖邦"是同一人，"月光"和"月光奏鸣曲"可能是同一作品"""

        try:
            print(f"    🤖 正在调用 LLM 判断 {len(batch)} 个断链的别名匹配...", flush=True)
            alias_result = call_llm(alias_sys, alias_user, max_tokens=len(batch) * 100,
                                    model=config.LLM_FAST_MODEL, temperature=0.1)
            if alias_result.strip():
                import json as _json
                import re
                json_match = re.search(r'\[.*\]', alias_result, re.DOTALL)
                if json_match:
                    alias_decisions = _json.loads(json_match.group())
                    truly_missing = []
                    for item in alias_decisions:
                        broken = item.get("broken_name", "")
                        status = item.get("status", "")
                        correct = item.get("correct_name")
                        if status == "alias_match" and correct and broken in batch_info:
                            alias_matches[broken] = correct
                            print(f"    🔄 别名匹配: {broken} → 已有页面 {correct}")
                        elif broken in batch_info:
                            truly_missing.append(broken)
                    mentioned = {item.get("broken_name") for item in alias_decisions}
                    for name in batch:
                        if name not in mentioned and name not in alias_matches:
                            truly_missing.append(name)
        except Exception as e:
            print(f"    ⚠️ 别名判断 LLM 调用失败: {e}，全部视为真正缺失")
            truly_missing = list(batch)

    if alias_matches:
        _fix_alias_broken_links(alias_matches, original_names, graph)
        mark_samples_fixed("broken_link", list(alias_matches.keys()))
        try:
            from error_book import append_ledger
            matches_desc = ", ".join(f"{k}→{v}" for k, v in list(alias_matches.items())[:5])
            append_ledger(issue_type="broken_link", auto_fixed=False,
                          fix_method="alias_link_fix",
                          note=f"别名匹配修正链接: {matches_desc}",
                          count=len(alias_matches))
        except Exception:
            pass

    if not truly_missing:
        print("  ✅ 所有断链均为别名匹配，已修正链接")
        return

    print(f"  🔗 其中 {len(truly_missing)} 个确认为真正缺失，需要创建页面")

    fs_verified_missing = []
    for clean_name in truly_missing:
        full_name = original_names.get(clean_name, clean_name)
        if "/" in full_name:
            prefix = full_name.rsplit("/", 1)[0]
            candidate_dir = dirs.get(prefix, config.WIKI_DIR / prefix)
        else:
            candidate_dir = None
        found_on_disk = False
        if candidate_dir and candidate_dir.exists():
            if (candidate_dir / f"{clean_name}.md").exists():
                found_on_disk = True
        if not found_on_disk:
            for d_path in dirs.values():
                if d_path.exists() and (d_path / f"{clean_name}.md").exists():
                    found_on_disk = True
                    break
        if found_on_disk:
            print(f"    ⏭️ 页面已存在: {full_name}（文件系统预检查）")
        else:
            fs_verified_missing.append(clean_name)

    if not fs_verified_missing:
        print("  ✅ 所有缺失页面实际已存在（文件系统预检查）")
        return
    if len(fs_verified_missing) < len(truly_missing):
        print(f"  📋 文件系统预检查过滤: {len(truly_missing)} → {len(fs_verified_missing)} 个真正缺失")
    truly_missing = fs_verified_missing

    articles_dir = config.WIKI_DIR / "sources" / "articles"
    article_texts = {}  # article_stem → 全文内容
    for clean_name in truly_missing:
        info = batch_info.get(clean_name)
        if not info:
            continue
        ctx = info["sample"].get("context", {})
        batch_articles = ctx.get("batch_articles", [])
        for stem in batch_articles:
            if stem in article_texts:
                continue  # 已读取
            art_path = articles_dir / f"{stem}.md"
            if art_path.exists():
                try:
                    content = art_path.read_text(encoding="utf-8")
                    if content.startswith("---"):
                        parts = _robust_fm_split(content)
                        if len(parts) >= 3:
                            content = parts[2].strip()
                    article_texts[stem] = content[:3000]
                except Exception:
                    pass

    has_article_context = bool(article_texts)

    if has_article_context:
        article_context_parts = []
        for stem, text in article_texts.items():
            article_context_parts.append(f"### 原始文章: {stem}\n{text}")
        article_context_text = "\n\n".join(article_context_parts)
        print(f"    📰 从 {len(article_texts)} 篇原始文章中读取上下文")
    else:
        article_context_text = ""
        print(f"    ⚠️ 无原始文章上下文，fallback 到已有页面引用上下文")

    schema_content = _read_file_safe(config.SCHEMA_FILE, 5000)
    from config import get_dir_catalog_text
    dir_catalog = get_dir_catalog_text()

    fallback_context_text = ""
    if not has_article_context:
        context_parts = []
        for name in truly_missing:
            full_name = original_names.get(name, name)
            for page in graph.pages.values():
                if any(full_name in link or name in link for link in page.outgoing_links):
                    snippet = page.content[:500]
                    context_parts.append(f"引用了 [[{full_name}]] 的页面 {page.page_type}/{page.name}:\n{snippet}")
                    break
        fallback_context_text = "\n\n".join(context_parts[:5]) if context_parts else "(无上下文)"

    type_hints = []
    for name in truly_missing:
        full_name = original_names.get(name, "")
        if full_name and "/" in full_name:
            prefix = full_name.rsplit("/", 1)[0]
            type_hints.append(f"- {name} → 应放 {prefix}/ 目录（frontmatter type: {prefix}）")
    type_hint_text = "\n".join(type_hints) if type_hints else ""

    sys_prompt = f"""你是知识库维护专家。此知识库供 AI 检索使用，不是给人阅读的文档。

核心原则：
- 信息密度高、关键词突出、格式统一、便于语义匹配
- 用结构化的事实列表，不要散文段落
- 每条信息一个要点，避免长段落
- 关键词、人名、作品名用完整表述（不要用代词"他""其"）
- frontmatter 的 aliases 字段填写常见别名/外文名/不同译名，方便检索匹配

你的任务：根据提供的原始文章内容，为以下缺失页面生成 Wiki 知识页。
{"这些页面是之前摄入文章时遗漏创建的，现在根据原始文章补建。请严格基于原始文章中的信息生成内容，不要编造文章中没有的信息。" if has_article_context else "为以下缺失页面生成内容。每个页面根据上下文和已有知识填写。"}"""

    if has_article_context:
        user_prompt = f"""## Schema 规范
{schema_content}

{dir_catalog}

{', '.join(truly_missing)}

{type_hint_text if type_hint_text else "(无特定类型提示，请根据上下文判断)"}

{article_context_text}

**重要**：
1. 请严格基于上方原始文章中的信息生成页面内容，不要编造原文中没有的内容。
2. 如果原始文章中关于某个缺失页面的信息很少，就只写文章中提到的那些事实，宁缺毋滥。
3. 你可以适当补充该实体的基本定位信息（如生卒年、国籍等公认事实），但核心内容必须来自原文。

请为每个缺失页面生成完整的 Wiki 页面内容。每个页面包含：
1. frontmatter（type 必须与知识库目录类型一致，如 composers/works/topics 等；tags, aliases）
2. `# 页面名` 标题，标题下方紧跟一行 `> 一句话概括`（blockquote 格式，简明描述该实体/概念的核心定位）
3. 正文（用结构化事实列表格式，简明扼要，基于原文中的信息）
4. `## 相关页面`（每条必须带目录路径：`[[目录名/页面名]]`，如 `[[composers/贝多芬]]`；只能链接已有页面或本次新创建的页面；**禁止链接本页面自身**，例如为"帕格尼尼"生成页面时不能出现 `[[composers/帕格尼尼]]`）

按以下格式输出，每个页面用 === 页面名 === 标记（只写页面名，不要带目录前缀）：

=== 页面名 ===
frontmatter + 正文

=== 页面名 ===
frontmatter + 正文"""
    else:
        user_prompt = f"""## Schema 规范
{schema_content}

{dir_catalog}

{', '.join(truly_missing)}

{type_hint_text if type_hint_text else "(无特定类型提示，请根据上下文判断)"}

{fallback_context_text}

**重要**：请仔细阅读上下文再生成内容！这些页面是被已有知识库页面引用的，必须与上下文中的信息一致。不要生成与上下文矛盾的内容（例如上下文提到"张弦是华裔指挥家"，就不要把张弦写成作家）。不要编造上下文中没有提到的信息，如果上下文信息不足，只写上下文中明确提到的事实，宁缺毋滥。

请为每个缺失页面生成完整的 Wiki 页面内容。每个页面包含：
1. frontmatter（type 必须与知识库目录类型一致，如 composers/works/topics 等；tags, aliases）
2. `# 页面名` 标题，标题下方紧跟一行 `> 一句话概括`（blockquote 格式，简明描述该实体/概念的核心定位）
3. 正文（用结构化事实列表格式，简明扼要，5-10 个要点即可）
4. `## 相关页面`（每条必须带目录路径：`[[目录名/页面名]]`，如 `[[composers/贝多芬]]`；只能链接已有页面或本次新创建的页面；**禁止链接本页面自身**，例如为"帕格尼尼"生成页面时不能出现 `[[composers/帕格尼尼]]`）

按以下格式输出，每个页面用 === 页面名 === 标记（只写页面名，不要带目录前缀）：

=== 页面名 ===
frontmatter + 正文

=== 页面名 ===
frontmatter + 正文"""

    try:
        print(f"    🤖 正在调用 LLM 为 {len(truly_missing)} 个缺失页面生成内容（可能需要 1-3 分钟）...", flush=True)
        result = call_llm(sys_prompt, user_prompt, max_tokens=len(truly_missing) * 800,
                        model=config.LLM_PREMIUM_MODEL, temperature=0.3)

        if not result.strip():
            print("    ⚠️ LLM 返回空内容，跳过断链修复")
            return

        import re
        result_clean = re.sub(r'^```\w*\n?', '', result)
        result_clean = re.sub(r'\n?```\s*$', '', result_clean)
        result_clean = re.sub(r'\n```\w*\n', '\n', result_clean)
        pages_created = []
        pages_already_exist = []
        page_blocks = re.split(r'===\s*(.+?)\s*===', result_clean)
        for i in range(1, len(page_blocks) - 1, 2):
            page_name = page_blocks[i].strip()
            page_content = page_blocks[i + 1].strip()

            if not page_content:
                continue

            if "/" in page_name:
                page_name = page_name.rsplit("/", 1)[-1]

            fm_match = re.match(r'^---\n(.*?)\n---', page_content, re.DOTALL)
            page_type = None
            if fm_match:
                try:
                    import yaml
                    fm = yaml.safe_load(fm_match.group(1))
                    if isinstance(fm, dict) and fm.get("type"):
                        t = fm["type"]
                        from config import get_page_types
                        pt = get_page_types()
                        if t in pt:
                            page_type = t
                except Exception:
                    pass

            save_dir = dirs.get(page_type) if page_type else None
            if save_dir is None:
                original_full = original_names.get(page_name, "")
                if original_full and "/" in original_full:
                    prefix = original_full.rsplit("/", 1)[0]
                    save_dir = dirs.get(prefix)
                    if save_dir:
                        page_type = prefix
            if save_dir is None:
                for fallback_type, fallback_dir in dirs.items():
                    if not fallback_type.startswith("sources") and not fallback_type.startswith("syntheses"):
                        save_dir = fallback_dir
                        page_type = fallback_type
                        break
            if save_dir:
                save_dir.mkdir(parents=True, exist_ok=True)
                save_path = save_dir / f"{page_name}.md"
                if not save_path.exists():
                    page_content = _sanitize_frontmatter(page_content)
                    save_path.write_text(page_content, encoding="utf-8")
                    _wfs_mirror_write(save_path, page_content)
                    pages_created.append(page_name)
                    print(f"    📝 创建页面: {page_type}/{page_name}")
                else:
                    pages_already_exist.append(page_name)
                    print(f"    ⏭️ 页面已存在: {page_type}/{page_name}")

        if pages_created:
            invalidate_graph_cache()
            print(f"  ✅ 创建了 {len(pages_created)} 个缺失页面")

            mark_samples_fixed("broken_link", pages_created)
            try:
                from error_book import append_ledger
                fix_method = "llm_create_from_articles" if has_article_context else "llm_create_page"
                append_ledger(issue_type="broken_link", auto_fixed=False,
                              fix_method=fix_method,
                              note=f"LLM 创建缺失页面{'（基于原文）' if has_article_context else ''}: {', '.join(pages_created[:5])}",
                              count=len(pages_created))
            except Exception:
                pass

        if pages_already_exist:
            mark_samples_fixed("broken_link", pages_already_exist)
            try:
                from error_book import append_ledger
                append_ledger(issue_type="broken_link", auto_fixed=False,
                              fix_method="page_already_exists",
                              note=f"页面已存在，标记已修复: {', '.join(pages_already_exist[:5])}",
                              count=len(pages_already_exist))
            except Exception:
                pass

        if not pages_created and not pages_already_exist:
            print(f"  ⚠️ 未创建任何页面")
    except Exception as e:
        print(f"    ⚠️ 断链修复 LLM 调用失败: {e}")


def _robust_parse_json_array(text: str) -> list:
    """从 LLM 返回文本中健壮地提取 JSON 数组。

    处理三类常见 LLM 输出问题：
    1. Extra data：JSON 数组后有多余内容（解释文字、第二个 JSON 块等）
    2. Unterminated string：输出被截断，字符串未闭合
    3. Invalid control character：JSON 值中包含换行符等控制字符
    """
    import json as _json
    import re

    if not text or not text.strip():
        return []

    first_bracket = text.find('[')
    if first_bracket == -1:
        return []

    depth = 0
    in_string = False
    escape_next = False
    end_pos = -1

    for i in range(first_bracket, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end_pos = i
                break

    if end_pos == -1:
        candidate = text[first_bracket:]
        last_brace = candidate.rfind('}')
        if last_brace > 0:
            candidate = candidate[:last_brace + 1] + ']'
        else:
            return []
    else:
        candidate = text[first_bracket:end_pos + 1]

    def _clean_control_chars(s):
        """清理 JSON 字符串中的非法控制字符"""
        result = []
        in_str = False
        esc = False
        for c in s:
            if esc:
                result.append(c)
                esc = False
                continue
            if c == '\\' and in_str:
                result.append(c)
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                result.append(c)
                continue
            if in_str and ord(c) < 32:
                if c == '\n' or c == '\r':
                    result.append(' ')
                elif c == '\t':
                    result.append(' ')
                else:
                    pass  # 跳过其他控制字符
            else:
                result.append(c)
        return ''.join(result)

    try:
        parsed = _json.loads(candidate)
        if isinstance(parsed, list):
            return parsed
    except _json.JSONDecodeError:
        pass

    try:
        cleaned = _clean_control_chars(candidate)
        parsed = _json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
    except _json.JSONDecodeError:
        pass

    try:
        objects = []
        for m in re.finditer(r'\{[^{}]*\}', candidate):
            try:
                obj_text = _clean_control_chars(m.group())
                obj = _json.loads(obj_text)
                if isinstance(obj, dict) and 'keep' in obj and 'merge' in obj:
                    objects.append(obj)
            except _json.JSONDecodeError:
                continue
        return objects
    except Exception:
        return []


def merge_duplicate_pages():
    """扫描各目录 _index.md，用 LLM 判断是否有可合并的重复页面。

    流程：
    1. 读取每个知识页目录的 _index.md
    2. 用快速模型让 LLM 判断是否存在重复/可合并的页面（同一实体不同命名）
    3. 对每组重复：读取两个页面 → LLM 合并内容 → 保存 → 全局替换链接 → 删除冗余页面
    """
    from config import get_page_dirs, get_page_types
    from llm_client import call_llm
    import re
    import json as _json

    dirs = get_page_dirs()
    page_types = get_page_types()

    print("🔍 知识页合并检查...")

    dir_indices = {}  # dir_name → _index.md 内容
    for dir_name in page_types:
        idx_path = dirs.get(dir_name, config.WIKI_DIR / dir_name) / "_index.md"
        if idx_path.exists():
            try:
                content = idx_path.read_text(encoding="utf-8")
                if content.strip():
                    dir_indices[dir_name] = content
            except Exception:
                pass

    if not dir_indices:
        print("  ⏭️ 无 _index.md 可检查")
        return

    index_text_parts = []
    for dir_name, content in dir_indices.items():
        index_text_parts.append(f"### {dir_name}/_index.md\n{content}")
    all_index_text = "\n\n".join(index_text_parts)

    MAX_INDEX_CHARS = 12000
    if len(all_index_text) > MAX_INDEX_CHARS:
        batches = []
        for dir_name, content in dir_indices.items():
            batches.append((dir_name, f"### {dir_name}/_index.md\n{content}"))
    else:
        batches = [("all", all_index_text)]

    all_merge_groups = []  # [{keep, merge, dir, reason}, ...]

    for batch_label, batch_text in batches:
        detect_sys = """你是知识库去重专家。你的任务是检查知识库的目录索引（_index.md），找出可能重复的页面——即同一个实体/概念/作品被创建了多个不同名字的页面。

判断标准：
1. **同一人不同名**：如「帕格尼尼」和「尼科洛·帕格尼尼」、「肖邦」和「弗雷德里克·肖邦」
2. **同一作品不同名**：如「梁祝」和「梁祝小提琴协奏曲」、「拉德斯基进行曲」和「拉德茨基进行曲」
3. **翻译差异**：如同一外文名的不同中文翻译
4. **注意**：括号内的别名信息是该条目的别名，不是独立页面。只关注 [[...]] 中的页面名本身是否有重复

重要：只标注你非常确定是同一个实体的情况。如果只是相关但不是同一实体（如「第一大提琴协奏曲」和「第二大提琴协奏曲」），不要标为重复。"""

        valid_dir_names = ", ".join(sorted(dirs.keys()))

        detect_user = f"""## 目录索引内容
{batch_text}

{valid_dir_names}

找出上述索引中可能是同一实体/作品但名字不同的重复页面。

请严格按以下 JSON 格式输出（不要输出其他内容）：
```json
[
  {{"keep": "更规范/更完整的页面名", "merge": "应被合并的页面名", "dir": "所在目录名", "reason": "为什么认为是同一实体"}},
  ...
]
```

如果没有发现重复，输出空数组：`[]`

注意：
- keep 和 merge 都**必须**是 _index.md 中 `[[...]]` 内实际出现的页面名，不要编造或推测不存在的页面名
- 只写页面名，不带目录前缀，不带 .md 后缀
- keep 应选择名字更完整/更规范的那个（如全名优于简称、有准确描述的优于模糊的）
- dir 必须是上面"可用的目录名列表"中的某一个（如 `{list(dirs.keys())[:3]}`），不要使用语义分类名或自创目录名
- 每组的 dir 就是 _index.md 标题 `### XXX/_index.md` 中 XXX 的部分
- **严禁**输出 _index.md 中不存在的页面名，否则合并会失败"""

        try:
            detect_result = call_llm(detect_sys, detect_user,
                                     max_tokens=2000,
                                     model=config.LLM_FAST_MODEL,
                                     temperature=0.1)
            if detect_result.strip():
                groups = _robust_parse_json_array(detect_result)
                if groups:
                    all_merge_groups.extend(groups)
        except Exception as e:
            print(f"  ⚠️ 重复检测 LLM 调用失败 ({batch_label}): {e}")

    if not all_merge_groups:
        print("  ✅ 未发现可合并的重复页面")
        return

    print(f"  🔄 发现 {len(all_merge_groups)} 组疑似重复页面")

    valid_merge_groups = []
    skipped_count = 0
    for group in all_merge_groups:
        keep_name = group.get("keep", "")
        merge_name = group.get("merge", "")
        dir_name = group.get("dir", "")
        if not keep_name or not merge_name or not dir_name:
            skipped_count += 1
            continue
        if keep_name == merge_name:
            skipped_count += 1
            continue
        dir_path = dirs.get(dir_name)
        if dir_path is None or not dir_path.exists():
            found = False
            for d_name, d_path in dirs.items():
                if not d_path.exists():
                    continue
                if (d_path / f"{keep_name}.md").exists() or (d_path / f"{merge_name}.md").exists():
                    group["dir"] = d_name
                    found = True
                    break
            if not found:
                skipped_count += 1
                continue
            dir_path = dirs.get(group["dir"])
        keep_path = dir_path / f"{keep_name}.md"
        merge_path = dir_path / f"{merge_name}.md"
        if not keep_path.exists() or not merge_path.exists():
            skipped_count += 1
            continue
        valid_merge_groups.append(group)

    if skipped_count > 0:
        print(f"  ⏭️ 预过滤: {skipped_count} 组因文件不存在被跳过")
    if not valid_merge_groups:
        print("  ⚠️ 未执行任何合并（文件可能不存在或 LLM 返回为空）")
        return

    merged_count = 0
    for group in valid_merge_groups:
        keep_name = group.get("keep", "")
        merge_name = group.get("merge", "")
        dir_name = group.get("dir", "")
        reason = group.get("reason", "")

        dir_path = dirs.get(dir_name)
        keep_path = dir_path / f"{keep_name}.md"
        merge_path = dir_path / f"{merge_name}.md"

        if not keep_path.exists():
            print(f"    ⏭️ 保留页面不存在: {dir_name}/{keep_name}")
            continue
        if not merge_path.exists():
            print(f"    ⏭️ 待合并页面不存在: {dir_name}/{merge_name}")
            continue

        try:
            keep_content = keep_path.read_text(encoding="utf-8")
            merge_content = merge_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"    ⚠️ 读取页面失败: {e}")
            continue

        merge_sys = """你是知识库维护专家。现在要将两个重复的知识页合并为一个。

合并原则：
- 保留所有不重复的信息，合并到同一个页面
- frontmatter 的 aliases 字段要包含被合并页面的名字（作为别名）
- tags 合并去重
- 正文合并去重，保持结构化事实列表格式
- 相关页面合并去重
- 如果两个页面有矛盾信息，保留信息更详细/更准确的那个版本"""

        merge_user = f"""## 保留页面: {keep_name}
```
{keep_content}
```

```
{merge_content}
```

{reason}

请输出合并后的完整页面内容（包含 frontmatter 和正文）。
注意：
1. aliases 中必须包含 "{merge_name}" 作为别名
2. 标题用保留页面的名字: `# {keep_name}`
3. 直接输出页面内容，不要用 === 标记或其他包装"""

        try:
            merged_result = call_llm(merge_sys, merge_user,
                                     max_tokens=3000,
                                     model=config.LLM_FAST_MODEL,
                                     temperature=0.2)
            if not merged_result.strip():
                print(f"    ⚠️ 合并 LLM 返回空: {keep_name} + {merge_name}")
                continue

            merged_text = re.sub(r'^```\w*\n?', '', merged_result.strip())
            merged_text = re.sub(r'\n?```\s*$', '', merged_text)

            merged_text = _sanitize_frontmatter(merged_text)

            keep_path.write_text(merged_text, encoding="utf-8")
            _wfs_mirror_write(keep_path, merged_text)
            print(f"    📝 合并内容 → {dir_name}/{keep_name}")

            merge_full = f"{dir_name}/{merge_name}"
            keep_full = f"{dir_name}/{keep_name}"
            replaced_files = 0

            for d_name, d_path in dirs.items():
                if not d_path.exists():
                    continue
                for md_file in d_path.glob("*.md"):
                    try:
                        text = md_file.read_text(encoding="utf-8")
                        original_text = text

                        text = text.replace(f"[[{merge_full}]]", f"[[{keep_full}]]")
                        text = text.replace(f"[[{merge_name}]]", f"[[{keep_name}]]")
                        text = re.sub(
                            rf'\[\[{re.escape(merge_full)}\|([^\]]+)\]\]',
                            rf'[[{keep_full}|\1]]',
                            text
                        )
                        text = re.sub(
                            rf'\[\[{re.escape(merge_name)}\|([^\]]+)\]\]',
                            rf'[[{keep_name}|\1]]',
                            text
                        )

                        if text != original_text:
                            md_file.write_text(text, encoding="utf-8")
                            _wfs_mirror_write(md_file, text)
                            replaced_files += 1
                    except Exception:
                        pass

            if replaced_files:
                print(f"    ✏️ 更新了 {replaced_files} 个文件中的链接引用")

            idx_path = dir_path / "_index.md"
            if idx_path.exists():
                try:
                    idx_text = idx_path.read_text(encoding="utf-8")
                    lines = idx_text.split("\n")
                    new_lines = []
                    removed = False
                    for line in lines:
                        if f"[[{merge_name}]]" in line:
                            removed = True
                            continue  # 跳过被合并页面的条目
                        new_lines.append(line)
                    if removed:
                        idx_text_new = "\n".join(new_lines)
                        idx_path.write_text(idx_text_new, encoding="utf-8")
                        _wfs_mirror_write(idx_path, idx_text_new)
                        print(f"    ✏️ 从 {dir_name}/_index.md 移除 [[{merge_name}]] 条目")

                except Exception:
                    pass

            try:
                merge_path.unlink()
                _wfs_mirror_delete(merge_path)
                print(f"    🗑️ 删除重复页面: {dir_name}/{merge_name}")
            except Exception as e:
                print(f"    ⚠️ 删除失败: {e}")

            merged_count += 1

        except Exception as e:
            print(f"    ⚠️ 合并 {keep_name} + {merge_name} 失败: {e}")

    if merged_count:
        invalidate_graph_cache()
        print(f"  ✅ 完成 {merged_count} 组页面合并")

        try:
            from error_book import append_ledger
            descs = [f"{g['merge']}→{g['keep']}" for g in all_merge_groups[:5]]
            append_ledger(issue_type="duplicate_page", auto_fixed=False,
                          fix_method="llm_merge_pages",
                          note=f"合并重复页面: {', '.join(descs)}",
                          count=merged_count)
        except Exception:
            pass
    else:
        print("  ⚠️ 未执行任何合并（文件可能不存在或 LLM 返回为空）")


def split_overloaded_pages():
    """扫描知识页，识别“单页混多个主题”的页面并拆分。

    目标：与 merge_duplicate_pages() 同级的低频维护能力。
    策略：
    1. 先用快速模型保守筛选“疑似需要拆分”的页面
    2. 对每个候选页面，让模型输出 FILE 块（更新原页 + 新增子页）
    3. 复用 write_wiki_files 落盘并维护索引
    """
    from config import get_page_dirs, get_page_types
    from llm_client import call_llm, call_llm_json

    print("🔍 知识页拆分检查...")

    dirs = get_page_dirs()
    page_types = get_page_types()

    candidates: list[dict] = []
    for dir_name in page_types:
        dir_path = dirs.get(dir_name, config.WIKI_DIR / dir_name)
        if not dir_path.exists():
            continue
        for md_file in sorted(dir_path.glob("*.md")):
            if md_file.name == "_index.md":
                continue
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            if len(text) < 2200:
                continue

            body = text
            if text.startswith("---"):
                parts = _robust_fm_split(text)
                if len(parts) >= 3:
                    body = parts[2]
            body_preview = re.sub(r"\s+", " ", body).strip()[:320]
            if not body_preview:
                continue

            candidates.append({
                "dir": dir_name,
                "name": md_file.stem,
                "path": md_file,
                "chars": len(text),
                "preview": body_preview,
            })

    if not candidates:
        print("  ⏭️ 无可审计的候选页面")
        return

    MAX_CAND_PER_BATCH = 20
    batches = [candidates[i:i + MAX_CAND_PER_BATCH] for i in range(0, len(candidates), MAX_CAND_PER_BATCH)]

    split_targets: list[dict] = []

    for bi, batch in enumerate(batches, 1):
        batch_text = "\n".join(
            f"- dir={c['dir']}, page={c['name']}, chars={c['chars']}, preview={c['preview']}"
            for c in batch
        )

        detect_sys = "你是知识库结构优化专家。请识别是否存在‘单页混多个相对独立主题’，需要拆分为多个页面的情况。偏向保守，不拆分优先。"
        detect_user = f"""## 候选页面列表（第 {bi}/{len(batches)} 批）
{batch_text}

1. 仅当一个页面明显混入 2 个以上相对独立的实体/主题，且拆分后检索会显著更清晰，才建议拆分
2. 若只是同一主题下的多个子点，通常不拆分
3. 每次最多返回 3 个拆分建议

{{
  "splits": [
    {{"dir": "目录名", "page": "原页面名", "reason": "为何需要拆分"}}
  ]
}}

如果无需拆分，输出：{{"splits": []}}"""

        try:
            result = call_llm_json(detect_sys, detect_user, model=config.LLM_FAST_MODEL, temperature=0.1)
            proposals = result.get("splits", []) if isinstance(result, dict) else []
            if isinstance(proposals, list):
                split_targets.extend([p for p in proposals if isinstance(p, dict)])
        except Exception as e:
            print(f"  ⚠️ 拆分检测失败（batch {bi}）: {e}")

    if not split_targets:
        print("  ✅ 未发现需要拆分的页面")
        return

    dedup = {}
    for t in split_targets:
        d = str(t.get("dir", "")).strip()
        p = str(t.get("page", "")).strip()
        if not d or not p:
            continue
        key = (d, p)
        dedup[key] = t

    valid_targets = []
    for (d, p), t in dedup.items():
        dir_path = dirs.get(d)
        if not dir_path:
            continue
        src = dir_path / f"{p}.md"
        if src.exists():
            valid_targets.append({"dir": d, "page": p, "path": src, "reason": str(t.get("reason", ""))})

    if not valid_targets:
        print("  ⚠️ 拆分建议均无效（页面不存在或目录不匹配）")
        return

    split_done = 0
    for t in valid_targets:
        dir_name = t["dir"]
        page_name = t["page"]
        src_path: Path = t["path"]
        reason = t["reason"]

        try:
            source_text = src_path.read_text(encoding="utf-8")
        except Exception as e:
            print(f"    ⚠️ 读取失败 {dir_name}/{page_name}: {e}")
            continue

        source_for_llm = source_text[:7000]

        split_sys = "你是知识库维护专家。请将一个混合主题页面拆分为多个更聚焦页面，同时保持原页面为总览页。"
        split_user = f"""## 原页面
路径：wiki/{dir_name}/{page_name}.md
拆分原因：{reason if reason else '主题混杂，检索可分离'}

内容：
```md
{source_for_llm}
```

请输出 FILE 块（`---FILE: path---`），并满足：
1. 必须包含更新后的原页面：`wiki/{dir_name}/{page_name}.md`（改为总览页，保留核心定义与导航）
2. 再新增 1~2 个同目录子页面：`wiki/{dir_name}/xxx.md`
3. 所有页面 frontmatter 的 `type` 必须是 `{dir_name}`
4. 各页面使用结构化事实列表，避免长散文
5. 原页面的“相关页面”中加入新建子页面链接；新页面也要回链原页面
6. 不要输出 `_index.md`、`wiki/index.md`、`log.md`

只输出 FILE 块，不要解释。"""

        try:
            result_text = call_llm(
                split_sys,
                split_user,
                max_tokens=5000,
                model=config.LLM_FAST_MODEL,
                temperature=0.2,
            )
        except Exception as e:
            print(f"    ⚠️ 拆分生成失败 {dir_name}/{page_name}: {e}")
            continue

        file_outputs = parse_file_outputs(result_text)
        if not file_outputs:
            print(f"    ⚠️ 拆分输出为空 {dir_name}/{page_name}")
            continue

        src_key = f"wiki/{dir_name}/{page_name}.md"
        if src_key not in file_outputs:
            print(f"    ⚠️ 拆分输出缺少原页面更新 {src_key}")
            continue

        safe_outputs: dict[str, str] = {}
        for rel_path, content in file_outputs.items():
            if not rel_path.startswith(f"wiki/{dir_name}/"):
                continue
            if rel_path.endswith("_index.md"):
                continue
            if "/sources/" in rel_path or rel_path == "wiki/index.md" or rel_path.endswith("/log.md"):
                continue
            safe_outputs[rel_path] = _sanitize_frontmatter(content)

        new_pages = [p for p in safe_outputs.keys() if p != src_key]
        if src_key not in safe_outputs or not new_pages:
            print(f"    ⚠️ 拆分结果不完整（需原页+新页）: {dir_name}/{page_name}")
            continue

        try:
            selected_pages = [page_name] + [Path(p).stem for p in new_pages]
            write_wiki_files(safe_outputs, selected_pages=selected_pages)
            split_done += 1
            print(f"    ✅ 已拆分: {dir_name}/{page_name}（新增 {len(new_pages)} 页）")
        except Exception as e:
            print(f"    ⚠️ 落盘失败 {dir_name}/{page_name}: {e}")

    if split_done:
        invalidate_graph_cache()
        print(f"  ✅ 完成 {split_done} 组页面拆分")
        try:
            from error_book import append_ledger
            append_ledger(issue_type="overloaded_page", auto_fixed=False,
                          fix_method="llm_split_page",
                          note=f"拆分混合主题页面 {split_done} 组",
                          count=split_done)
        except Exception:
            pass
    else:
        print("  ⚠️ 未执行任何页面拆分")


def _lcs_len_local(a: str, b: str) -> int:
    """本地 LCS 长度计算（避免引用外部脚本）。"""
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0
    prev = [0] * (n + 1)
    curr = [0] * (n + 1)
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev, curr = curr, [0] * (n + 1)
    return prev[n]


def _similarity_local(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return _lcs_len_local(a, b) / max(len(a), len(b))


def _write_digest_source_article(md: Path, art_stem: str) -> bool:
    """给 digest 文件注入 source_article 字段并补 ## 原文 章节；已有则跳过。返回是否修改。"""
    text = md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    parts = _robust_fm_split(text)
    if len(parts) < 3:
        return False
    fm_text = parts[1]
    for line in fm_text.split("\n"):
        if line.strip().startswith("source_article:"):
            v = line.split(":", 1)[1].strip().strip('"').strip("'")
            if v:
                return False
    new_fm = fm_text.rstrip() + f"\nsource_article: {_yaml_quote_value(art_stem)}"
    new_text = f"---{new_fm}\n---{parts[2]}"
    if "## 原文" not in new_text:
        new_text = new_text.rstrip() + f"\n\n## 原文\n- [[sources/articles/{art_stem}]]\n"
    md.write_text(new_text, encoding="utf-8")
    _wfs_mirror_write(md, new_text)
    return True


def fix_missing_source_article_from_error_book():
    """从错题本读取 missing_source_article 的未修复 sample，回填 frontmatter.source_article。

    sample.context.candidates 是摄入时记录的当批候选 article（通常 ≤ 3 篇）。
    修复策略（按可靠度从高到低）：
      1) candidates 只有 1 个：直接采用
      2) candidates 中有 stem 与 digest 完全相同：直接采用
      3) 候选里按 LCS 相似度 top1 ≥ 0.75 且 margin ≥ 0.15：采用
      4) 以上都不决定 → 用 LLM 看 digest 摘要 + 各候选原文前 1500 字，返回 JSON
      5) 若候选 article 全部不存在（文件被删/改名）→ 退化到全局 LCS（repair_digests 策略）
    """
    try:
        from error_book import get_unfixed_samples_full, mark_samples_fixed
    except Exception as e:
        print(f"  ⚠️ 无法加载错题本: {e}")
        return 0

    samples = get_unfixed_samples_full("missing_source_article")
    if not samples:
        return 0

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    articles_dir = config.WIKI_DIR / "sources" / "articles"
    if not digests_dir.exists() or not articles_dir.exists():
        return 0

    all_art_stems = [p.stem for p in articles_dir.glob("*.md") if p.name != "_index.md"]
    all_art_set = set(all_art_stems)

    print(f"  🔧 待修复 source_article 缺失: {len(samples)} 条")

    fixed_names: list[str] = []
    unresolved = 0

    for s in samples:
        dig_stem = s.get("name", "")
        if not dig_stem:
            continue
        md = digests_dir / f"{dig_stem}.md"
        if not md.exists():
            fixed_names.append(dig_stem)
            continue

        ctx = s.get("context", {}) or {}
        raw_cands = ctx.get("candidates", []) or []
        cands = [c for c in raw_cands if c in all_art_set]

        match: str | None = None
        rule = ""

        if len(cands) == 1:
            match = cands[0]
            rule = "single_candidate"
        elif dig_stem in cands:
            match = dig_stem
            rule = "same_name"

        if match is None and len(cands) >= 2:
            scored = sorted(
                ((c, _similarity_local(dig_stem, c)) for c in cands),
                key=lambda x: -x[1]
            )
            best, bs = scored[0]
            second_s = scored[1][1] if len(scored) > 1 else 0.0
            if bs >= 0.75 and (bs - second_s) >= 0.15:
                match = best
                rule = "candidate_lcs"

        if match is None and len(cands) >= 2:
            try:
                match = _llm_pick_source_article(md, dig_stem, cands, articles_dir)
                if match:
                    rule = "llm_pick"
            except Exception as e:
                print(f"    ⚠️ {dig_stem}: LLM 决策失败 {e}")

        if match is None and not cands:
            scored = sorted(
                ((a, _similarity_local(dig_stem, a)) for a in all_art_stems),
                key=lambda x: -x[1]
            )
            if scored:
                best, bs = scored[0]
                second_s = scored[1][1] if len(scored) > 1 else 0.0
                if bs >= 0.75 and (bs - second_s) >= 0.15:
                    match = best
                    rule = "global_lcs"

        if match is None:
            unresolved += 1
            continue

        if _write_digest_source_article(md, match):
            print(f"    ✅ {dig_stem} ← {match} ({rule})")
        fixed_names.append(dig_stem)

    if fixed_names:
        mark_samples_fixed("missing_source_article", fixed_names)
        try:
            from error_book import append_ledger
            append_ledger(issue_type="missing_source_article", auto_fixed=True,
                          fix_method="lcs_match", note=f"回填 {len(fixed_names)} 条 source_article",
                          count=len(fixed_names))
        except Exception:
            pass
    if unresolved:
        print(f"  ⚠️ {unresolved} 条仍无法自动决策，保留在错题本待下次")

    return len(fixed_names)


def _llm_pick_source_article(md: Path, dig_stem: str,
                              candidates: list[str],
                              articles_dir: Path) -> str | None:
    """给 LLM：digest 摘要 + 各候选原文前 1500 字，返回 JSON {"match": "<stem>" | null}。"""
    dig_text = md.read_text(encoding="utf-8")
    dig_parts = _robust_fm_split(dig_text)
    dig_body = dig_parts[2] if len(dig_parts) >= 3 else dig_text
    dig_body = dig_body.strip()[:1500]

    cand_blocks = []
    for i, stem in enumerate(candidates, start=1):
        ap = articles_dir / f"{stem}.md"
        if not ap.exists():
            continue
        at = ap.read_text(encoding="utf-8")
        at = re.sub(r'^---\n.*?\n---\s*\n', '', at, flags=re.DOTALL)
        at = at.strip()[:1500]
        cand_blocks.append(f"### 候选 {i}: stem=`{stem}`\n{at}")

    if not cand_blocks:
        return None

    sys_prompt = ("你是知识库维护专家。下面给你一段 digest（摘要页）的正文，和若干个候选原文。"
                  "请判断这个 digest 最可能是从哪个候选原文总结而来的，只输出 JSON。")
    user_prompt = f"""## Digest 文件名
{dig_stem}

{dig_body}

{chr(10).join(cand_blocks)}

只输出一行 JSON，格式：
{{"match": "<选中的 stem，必须是上面某个候选的 stem 原样>", "confidence": "high|medium|low"}}

如果没有任何一个候选匹配（可能 digest 是无关内容），输出：
{{"match": null, "confidence": "low"}}
"""
    try:
        result = call_llm_json(sys_prompt, user_prompt, model=config.LLM_PREMIUM_MODEL, temperature=0.0)
    except Exception:
        return None
    m = result.get("match")
    if not m or not isinstance(m, str):
        return None
    if m not in candidates:
        return None
    conf = result.get("confidence", "low")
    if conf == "low":
        return None  # 低置信度不采用
    return m


def fix_incomplete_digests():
    """用 LLM 补全不完整的 digest 页面。

    扫描 sources/digests/ 下缺少必要章节的页面，
    从对应的原文中用 LLM 生成完整摘要内容。
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    articles_dir = config.WIKI_DIR / "sources" / "articles"
    if not digests_dir.exists():
        return 0

    required_sections = ["摘要", "核心观点", "关键信息", "提及实体"]
    incomplete = []

    for md in sorted(digests_dir.glob("*.md")):
        if md.name == "_index.md":
            continue
        text = md.read_text(encoding="utf-8")
        parts = _robust_fm_split(text)
        if len(parts) < 3:
            continue
        body = parts[2]
        missing = [s for s in required_sections if f"## {s}" not in body]
        for s in required_sections:
            if f"## {s}" in body and s not in missing:
                pattern = rf'## {re.escape(s)}\s*\n(.*?)(?=\n## |\Z)'
                m = re.search(pattern, body, re.DOTALL)
                if m:
                    content = m.group(1).strip()
                    if not content or re.match(r'^[-•\s]*（待补充）\s*$', content):
                        missing.append(s)
        if missing:
            article_path = None
            fm_text = parts[1]
            for line in fm_text.split("\n"):
                if line.strip().startswith("source_article:"):
                    src_stem = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if src_stem:
                        candidate = articles_dir / f"{src_stem}.md"
                        if candidate.exists():
                            article_path = candidate
                    break
            if article_path is None:
                filename = md.name
                candidate = articles_dir / filename
                if candidate.exists():
                    article_path = candidate
            if article_path is None and len(md.name) >= 10 and md.name[4] == '-':
                date_prefix = md.name[:10]
                matches = list(articles_dir.glob(f"{date_prefix}-*.md"))
                article_path = matches[0] if matches else None
            incomplete.append((md, missing, article_path))

    if not incomplete:
        print("  ✅ 所有摘要页完整")
        return 0

    print(f"  📄 发现 {len(incomplete)} 个不完整摘要页，开始补全...")

    fixed = 0
    fixed_names = []  # 收集修好的文件路径前缀，用于标记 error_book
    for md, missing, article_path in incomplete:
        article_text = ""
        article_stem = ""
        if article_path and article_path.exists():
            article_text = article_path.read_text(encoding="utf-8")
            article_text = re.sub(r'\r\n', '\n', article_text)
            article_text = re.sub(r'^---\n.*?\n---\s*\n', '', article_text, flags=re.DOTALL)
            if len(article_text) > 3000:
                article_text = article_text[:3000] + "\n...(内容已截断)"
            article_stem = article_path.stem
        else:
            rescuable = set(missing).issubset({"提及实体"})
            if not rescuable:
                print(f"    ⏭️ {md.name} — 原文不存在且缺关键章节 {missing}，跳过")
                continue
            article_stem = "(原文缺失)"

        digest_text = md.read_text(encoding="utf-8")

        sys_prompt = "你是知识库维护专家。根据原文补全摘要页的缺失章节。只输出需要补全的章节内容，不要重复已有内容。"
        if article_text:
            article_block = f"## 原文（文件名：{article_stem}）\n{article_text}"
        else:
            article_block = "## 原文\n(原文缺失，请根据摘要页已有内容提取信息)"
        user_prompt = f"""## 当前摘要页
{digest_text}

{', '.join(missing)}

{article_block}

请补全上述缺失章节的内容。格式要求：
- ## 摘要：不超过200字的结构化摘要
- ## 核心观点：author的个人观点、判断、态度（不是百科知识）
- ## 关键引用：原文中可直接引用的精彩原话（加引号标注）
- ## 关键信息：事实性信息
- ## 提及实体：直接写实体名称，不加 [[]] 链接（如 `- 贝多芬`、`- 月光奏鸣曲`）

只输出需要补全的章节（## 开头），不要输出已有且完整的章节。"""

        try:
            result = call_llm(sys_prompt, user_prompt, max_tokens=2048,
                            model=config.LLM_PREMIUM_MODEL, temperature=0.2)

            if not result.strip():
                print(f"    ⚠️ {md.name} — LLM 返回空内容")
                continue

            parts = _robust_fm_split(digest_text)
            if len(parts) < 3:
                continue
            body = parts[2]

            for section in missing:
                section_header = f"## {section}"
                if section_header in body:
                    pattern = rf'## {re.escape(section)}\s*\n(.*?)(?=\n## |\Z)'
                    llm_pattern = rf'## {re.escape(section)}\s*\n(.*?)(?=\n## |\Z)'
                    llm_match = re.search(llm_pattern, result, re.DOTALL)
                    if llm_match:
                        new_content = llm_match.group(0).rstrip()
                        body = re.sub(pattern, new_content + "\n", body, flags=re.DOTALL)

            for line in result.strip().split("\n"):
                if line.startswith("## "):
                    section_name = line[3:].strip()
                    if f"## {section_name}" not in body:
                        section_pattern = rf'## {re.escape(section_name)}\s*\n(.*?)(?=\n## |\Z)'
                        section_match = re.search(section_pattern, result, re.DOTALL)
                        if section_match:
                            body = body.rstrip() + "\n\n" + section_match.group(0).rstrip() + "\n"

            new_text = f"---{parts[1]}---{body}"
            md.write_text(new_text, encoding="utf-8")
            _wfs_mirror_write(md, new_text)
            fixed += 1
            fixed_names.append(f"sources/digests/{md.name}")
            print(f"    ✅ {md.name} — 补全 [{', '.join(missing)}]")

            if "提及实体" in missing:
                new_text = md.read_text(encoding="utf-8")
                parts_body = _robust_fm_split(new_text)
                if len(parts_body) >= 3:
                    body_text = parts_body[2]
                    entity_match = re.search(r'(## 提及实体\s*\n.*?)(?=\n## |\Z)', body_text, re.DOTALL)
                    if entity_match:
                        entity_section = entity_match.group(1)
                        def _clean_link(m):
                            link = m.group(1)
                            page_name = link.split("|")[0].strip()
                            if page_name.startswith("sources/"):
                                return m.group(0)
                            return page_name  # 去掉 [[]]，只保留名称
                        cleaned_section = re.sub(r'\[\[([^\]]+)\]\]', _clean_link, entity_section)
                        if cleaned_section != entity_section:
                            body_text = body_text.replace(entity_section, cleaned_section)
                            new_text = f"---{parts_body[1]}---{body_text}"
                            md.write_text(new_text, encoding="utf-8")
                            _wfs_mirror_write(md, new_text)
        except Exception as e:
            print(f"    ⚠️ {md.name} — 补全失败: {e}")

    if fixed_names:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("digest_incomplete", fixed_names)
        except Exception as e:
            print(f"  ⚠️ 更新错题本修复状态失败: {e}")
        try:
            from error_book import append_ledger
            append_ledger(issue_type="digest_incomplete", auto_fixed=False,
                          fix_method="llm_supplement", note=f"补全 {len(fixed_names)} 个 digest 章节",
                          count=len(fixed_names))
        except Exception:
            pass

    return fixed


def fix_missing_summary():
    """修复知识页缺少 blockquote 一句话概括的问题。

    规则：知识页的 # 标题后应紧跟 > 一句话概括（blockquote 格式）。
    如果标题后是 - 列表项格式的一句话概括，自动转为 blockquote。
    如果标题后没有任何概括行，用 LLM 生成。
    """
    from wiki_page import WikiGraph
    graph = WikiGraph()
    graph.load_all()

    missing_pages = []
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        body = page.content
        if body.startswith("---"):
            parts = _robust_fm_split(body)
            body = parts[2] if len(parts) >= 3 else ""
        lines = body.strip().split("\n")
        found_title = False
        has_blockquote = False
        first_line_after_title = None
        first_line_idx = -1
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                found_title = True
                continue
            if found_title:
                if stripped == "":
                    continue
                if stripped.startswith("> "):
                    has_blockquote = True
                else:
                    first_line_after_title = stripped
                    first_line_idx = i
                break
        if found_title and not has_blockquote:
            missing_pages.append((page, first_line_after_title, first_line_idx))

    if not missing_pages:
        print("  ✅ 所有知识页均有一句话概括")
        return 0

    print(f"  📝 发现 {len(missing_pages)} 个知识页缺少 blockquote 概括，开始修复...")
    fixed = 0
    fixed_names = []

    for page, first_line, line_idx in missing_pages:
        text = page.path.read_text(encoding="utf-8")
        parts = _robust_fm_split(text)
        if len(parts) < 3:
            continue
        fm = parts[1]
        body = parts[2]

        if first_line and first_line.startswith("- "):
            summary_text = first_line[2:]  # 去掉 "- "
            old_line = first_line
            new_line = f"> {summary_text}"
            body = body.replace(f"\n{old_line}\n", f"\n{new_line}\n", 1)
            new_text = f"---{fm}---{body}"
            page.path.write_text(new_text, encoding="utf-8")
            _wfs_mirror_write(page.path, new_text)
            fixed += 1
            fixed_names.append(f"{page.page_type}/{page.name}")
            print(f"    ✅ {page.page_type}/{page.name} — 列表项转 blockquote")
        else:
            full_body = body.strip()
            sys_prompt = "你是知识库维护专家。为给定的知识页生成一句话概括，用 blockquote 格式。"
            user_prompt = f"""## 知识页内容
{full_body}

请为这个知识页生成一行一句话概括（blockquote 格式），简明描述该实体/概念的核心定位。
只输出一行，格式：> 一句话概括内容
例如：> 巴洛克时期德国作曲家，西方近代音乐之父"""

            try:
                result = call_llm(sys_prompt, user_prompt, max_tokens=256,
                                  model=config.LLM_PREMIUM_MODEL, temperature=0.2)
                summary_line = result.strip()
                if not summary_line.startswith("> "):
                    summary_line = f"> {summary_line.lstrip('> ')}"

                body_lines = body.split("\n")
                for i, line in enumerate(body_lines):
                    if line.strip().startswith("# ") and not line.strip().startswith("## "):
                        j = i + 1
                        while j < len(body_lines) and body_lines[j].strip() == "":
                            j += 1
                        body_lines.insert(j, summary_line)
                        break

                body = "\n".join(body_lines)
                new_text = f"---{fm}---{body}"
                page.path.write_text(new_text, encoding="utf-8")
                _wfs_mirror_write(page.path, new_text)
                fixed += 1
                fixed_names.append(f"{page.page_type}/{page.name}")
                print(f"    ✅ {page.page_type}/{page.name} — LLM 生成 blockquote")
            except Exception as e:
                print(f"    ⚠️ {page.page_type}/{page.name} — 生成失败: {e}")

    if fixed_names:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("missing_summary", fixed_names)
        except Exception as e:
            print(f"  ⚠️ 更新错题本修复状态失败: {e}")
        try:
            from error_book import append_ledger
            append_ledger(issue_type="missing_summary", auto_fixed=False,
                          fix_method="llm_generate_blockquote", note=f"补全 {len(fixed_names)} 个一句话概括",
                          count=len(fixed_names))
        except Exception:
            pass

    return fixed


def fix_related_source_format():
    """修复知识页「相关来源」章节：为缺少相关来源的页面补回正确的 digest 链接。

    场景：auto_fix_related_source_format 删除了非 digest 链接/纯文本/空 section 后，
    页面可能丢失了本该有的相关来源。本函数用 LLM 根据页面内容匹配对应的 digest。
    """
    from wiki_page import WikiGraph
    graph = WikiGraph()
    graph.load_all()

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    digest_info: dict[str, str] = {}  # stem → 简短描述
    if digests_dir.exists():
        for md in digests_dir.glob("*.md"):
            if md.name == "_index.md":
                continue
            text = md.read_text(encoding="utf-8")
            body = text
            if text.startswith("---"):
                parts = _robust_fm_split(text)
                if len(parts) >= 3:
                    body = parts[2]
            desc = md.stem
            for line in body.split("\n"):
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("## "):
                    desc = stripped[2:].strip()
                    break
                if stripped.startswith("> "):
                    desc = stripped[2:].strip()
                    break
            digest_info[md.stem] = desc

    try:
        from error_book import load_error_book, get_unfixed_samples
        unfixed = []
        for e in load_error_book():
            if e.get("category") == "related_source_format" and e.get("status") != "closed":
                unfixed = get_unfixed_samples(e)
                break
    except Exception:
        unfixed = []

    if not unfixed:
        print("  ✅ 相关来源无需 LLM 修复")
        return 0

    page_names_to_fix = set()
    for sample in unfixed:
        name = sample.split(":")[0].strip() if ":" in sample else sample.strip()
        page_names_to_fix.add(name)

    pages_to_fix = []
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        page_id = f"{page.page_type}/{page.name}"
        if page_id not in page_names_to_fix:
            continue

        body = page.content
        if body.startswith("---"):
            parts = _robust_fm_split(body)
            body = parts[2] if len(parts) >= 3 else ""

        has_related_source = False
        has_digest_links = False
        in_section = False
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                in_section = (stripped == "## 相关来源")
                if in_section:
                    has_related_source = True
                continue
            if in_section:
                if stripped.startswith("## "):
                    break
                if not stripped:
                    continue
                links = re.findall(r'\[\[sources/digests/([^\]]+)\]\]', stripped)
                if links:
                    has_digest_links = True

        if not has_related_source or not has_digest_links:
            pages_to_fix.append(page)

    if not pages_to_fix:
        print("  ✅ 相关来源无需 LLM 修复（所有页面已有正确 digest 链接）")
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("related_source_format", list(page_names_to_fix))
            mark_samples_fixed("missing_sections", list(page_names_to_fix))
        except Exception:
            pass
        try:
            from error_book import append_ledger
            append_ledger(issue_type="related_source_format", auto_fixed=True,
                          fix_method="code_supplement", note=f"代码补回 {len(page_names_to_fix)} 个页面相关来源",
                          count=len(page_names_to_fix))
        except Exception:
            pass
        return 0

    print(f"  📝 发现 {len(pages_to_fix)} 个页面需补回相关来源，开始 LLM 修复...")
    fixed = 0
    fixed_names = []

    digest_list_lines = []
    for stem, desc in sorted(digest_info.items()):
        digest_list_lines.append(f"- [[sources/digests/{stem}]] — {desc}")
    digest_list_text = "\n".join(digest_list_lines[:100])  # 限制 token

    for page in pages_to_fix:
        text = page.path.read_text(encoding="utf-8")
        parts = _robust_fm_split(text)
        if len(parts) < 3:
            continue
        fm = parts[1]
        body = parts[2]

        full_body = body.strip()

        sys_prompt = "你是知识库维护专家。为知识页的「相关来源」章节匹配最相关的来源摘要(digest)链接。"
        user_prompt = f"""## 知识页内容
{full_body}

{digest_list_text}

请从上面的来源摘要列表中，选出与该知识页最相关的 digest 链接（1-3个）。
只输出链接行，每行格式：`- [[sources/digests/日期-标题]] — 一句话说明`
不要输出其他内容。如果没有相关的 digest，输出：无"""

        try:
            result = call_llm(sys_prompt, user_prompt, max_tokens=512,
                            model=config.LLM_PREMIUM_MODEL, temperature=0.2)
            result = result.strip()

            if not result or result == "无":
                print(f"    ⏭️ {page.page_type}/{page.name} — 无相关 digest，跳过")
                fixed_names.append(f"{page.page_type}/{page.name}")
                continue

            new_links = []
            for line in result.split("\n"):
                stripped = line.strip()
                if not stripped:
                    continue
                links = re.findall(r'\[\[sources/digests/([^\]]+)\]\]', stripped)
                if links:
                    for link_stem in links:
                        if link_stem in digest_info or (digests_dir / f"{link_stem}.md").exists():
                            desc_match = re.search(r'—\s*(.+)$', stripped)
                            desc = desc_match.group(1).strip() if desc_match else ""
                            if desc:
                                new_links.append(f"- [[sources/digests/{link_stem}]] — {desc}")
                            else:
                                new_links.append(f"- [[sources/digests/{link_stem}]]")

            if not new_links:
                print(f"    ⏭️ {page.page_type}/{page.name} — LLM 未返回有效链接")
                continue

            body_lines = body.split("\n")
            new_body_lines = []
            in_source = False
            source_replaced = False

            for line in body_lines:
                stripped = line.strip()
                if stripped == "## 相关来源":
                    in_source = True
                    new_body_lines.append("## 相关来源")
                    for link_line in new_links:
                        new_body_lines.append(link_line)
                    source_replaced = True
                    continue
                if in_source:
                    if stripped.startswith("## "):
                        in_source = False
                        new_body_lines.append(line)
                    continue
                new_body_lines.append(line)

            if not source_replaced:
                last_section_idx = -1
                for i, line in enumerate(new_body_lines):
                    if line.strip().startswith("## "):
                        last_section_idx = i
                if last_section_idx >= 0:
                    insert_idx = last_section_idx + 1
                    while insert_idx < len(new_body_lines) and new_body_lines[insert_idx].strip():
                        insert_idx += 1
                    while insert_idx < len(new_body_lines) and not new_body_lines[insert_idx].strip():
                        insert_idx += 1
                    new_body_lines.insert(insert_idx, "")
                    new_body_lines.insert(insert_idx + 1, "## 相关来源")
                    for j, link_line in enumerate(new_links):
                        new_body_lines.insert(insert_idx + 2 + j, link_line)
                else:
                    new_body_lines.append("")
                    new_body_lines.append("## 相关来源")
                    for link_line in new_links:
                        new_body_lines.append(link_line)

            new_body = "\n".join(new_body_lines)
            new_body = re.sub(r'\n{3,}', '\n\n', new_body)
            new_text = f"---{fm}---{new_body}"
            page.path.write_text(new_text, encoding="utf-8")
            _wfs_mirror_write(page.path, new_text)

            fixed += 1
            fixed_names.append(f"{page.page_type}/{page.name}")
            print(f"    ✅ {page.page_type}/{page.name} — 补回 {len(new_links)} 个 digest 链接")

        except Exception as e:
            print(f"    ⚠️ {page.page_type}/{page.name} — 修复失败: {e}")

    if fixed_names:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("related_source_format", fixed_names)
            mark_samples_fixed("missing_sections", fixed_names)
        except Exception as e:
            print(f"  ⚠️ 更新错题本修复状态失败: {e}")
        try:
            from error_book import append_ledger
            append_ledger(issue_type="related_source_format", auto_fixed=False,
                          fix_method="llm_supplement", note=f"LLM 补回 {len(fixed_names)} 个页面相关来源",
                          count=len(fixed_names))
        except Exception:
            pass

    return fixed


def fix_missing_sections():
    """用 LLM 补全知识页缺少的必要章节（核心事实、相关页面、相关来源）。

    扫描所有知识页，对缺少 核心事实/相关页面/相关来源 的页面：
    - 核心事实：用 LLM 根据已有内容生成
    - 相关页面：根据已有内容提取相关实体，匹配 wiki 中已有页面
    - 相关来源：根据已有内容匹配 digests 目录中的摘要
    """
    from wiki_page import WikiGraph
    graph = WikiGraph()
    graph.load_all()

    _KNOWLEDGE_REQUIRED_SECTIONS = ["核心事实", "相关页面", "相关来源"]
    missing_pages = []  # [(page, missing_sections)]

    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        body = page.content
        if body.startswith("---"):
            parts = _robust_fm_split(body)
            body = parts[2] if len(parts) >= 3 else ""
        missing = [s for s in _KNOWLEDGE_REQUIRED_SECTIONS if f"## {s}" not in body]
        if missing:
            missing_pages.append((page, missing))

    if not missing_pages:
        print("  ✅ 所有知识页章节完整")
        return 0

    print(f"  📑 发现 {len(missing_pages)} 个知识页缺少必要章节，开始修复...")

    all_page_names = sorted(set(
        f"{p.page_type}/{p.name}" for p in graph.pages.values()
        if p.page_type and p.page_type not in ("source", "sources", "synthesis", "syntheses")
    ))
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    digest_info: dict[str, str] = {}
    if digests_dir.exists():
        for md in digests_dir.glob("*.md"):
            if md.name == "_index.md":
                continue
            text = md.read_text(encoding="utf-8")
            body_d = text
            if text.startswith("---"):
                dparts = _robust_fm_split(text)
                if len(dparts) >= 3:
                    body_d = dparts[2]
            desc = md.stem
            for line in body_d.split("\n"):
                stripped = line.strip()
                if stripped.startswith("# ") and not stripped.startswith("## "):
                    desc = stripped[2:].strip()
                    break
                if stripped.startswith("> "):
                    desc = stripped[2:].strip()
                    break
            digest_info[md.stem] = desc

    fixed = 0
    fixed_names = []

    for page, missing in missing_pages:
        text = page.path.read_text(encoding="utf-8")
        parts = _robust_fm_split(text)
        if len(parts) < 3:
            continue
        fm = parts[1]
        body = parts[2]

        full_body = body.strip()

        context_parts = []
        if "相关页面" in missing:
            same_dir = [n for n in all_page_names if n.startswith(f"{page.page_type}/")]
            other_dir = [n for n in all_page_names if not n.startswith(f"{page.page_type}/")]
            ref_pages = same_dir + other_dir
            context_parts.append(f"可选的相关页面（格式 [[类型/名]]）：{', '.join(ref_pages)}")
        if "相关来源" in missing and digest_info:
            digest_list_lines = [f"- [[sources/digests/{stem}]] — {desc}"
                                 for stem, desc in sorted(digest_info.items())]
            context_parts.append("可选的相关来源：\n" + "\n".join(digest_list_lines))

        context_hint = "\n".join(context_parts)

        sys_prompt = "你是知识库维护专家。为知识页补全缺失的必要章节。只输出需要补全的章节内容，不要重复已有内容。"
        user_prompt = f"""## 当前知识页内容
{full_body}

{', '.join(missing)}

{context_hint}

请补全上述缺失章节的内容。格式要求：
- ## 核心事实：至少 2 条事实，每条以 - 开头，如 `- 事实描述`
- ## 相关页面：列出相关的知识页链接，每行 `- [[类型/名]] — 一句话说明`，如果不确定可以写 `- （待补充）`
- ## 相关来源：列出相关的 digest 链接，每行 `- [[sources/digests/文件名]] — 一句话说明`，如果不确定可以写 `- （待补充）`

只输出需要补全的章节（## 开头），不要输出已有且完整的章节。"""

        try:
            result = call_llm(sys_prompt, user_prompt, max_tokens=1024,
                              model=config.LLM_PREMIUM_MODEL, temperature=0.2)

            if not result.strip():
                print(f"    ⚠️ {page.page_type}/{page.name} — LLM 返回空内容")
                continue

            llm_sections = {}  # section_name -> full text (含 ## 标题行)
            for line in result.strip().split("\n"):
                if line.startswith("## "):
                    section_name = line[3:].strip()
                    if f"## {section_name}" not in body:
                        section_pattern = rf'## {re.escape(section_name)}\s*\n(.*?)(?=\n## |\Z)'
                        section_match = re.search(section_pattern, result, re.DOTALL)
                        if section_match:
                            llm_sections[section_name] = section_match.group(0).rstrip()

            if llm_sections:
                _SECTION_ORDER = ["核心事实", "相关页面", "相关来源"]
                body_lines = body.rstrip().split("\n")

                for sec_name in _SECTION_ORDER:
                    if sec_name not in llm_sections:
                        continue
                    sec_text = llm_sections[sec_name]
                    sec_order_idx = _SECTION_ORDER.index(sec_name)
                    insert_after_sections = _SECTION_ORDER[:sec_order_idx]
                    insert_before_sections = _SECTION_ORDER[sec_order_idx + 1:]

                    insert_line_idx = len(body_lines)  # 默认追加到末尾
                    for before_sec in insert_before_sections:
                        for i, bl in enumerate(body_lines):
                            if bl.strip() == f"## {before_sec}":
                                insert_line_idx = i
                                break
                        if insert_line_idx < len(body_lines):
                            break

                    sec_lines = [""] + sec_text.split("\n")
                    for j, sl in enumerate(sec_lines):
                        body_lines.insert(insert_line_idx + j, sl)

                new_body = "\n".join(body_lines)
            else:
                new_body = body.rstrip()

            if new_body != body.rstrip():
                new_text = f"---{fm}---{new_body}\n"
                page.path.write_text(new_text, encoding="utf-8")
                _wfs_mirror_write(page.path, new_text)
                fixed += 1
                fixed_names.append(f"{page.page_type}/{page.name}")
                print(f"    ✅ {page.page_type}/{page.name} — 补全 [{', '.join(missing)}]")
            else:
                print(f"    ⚠️ {page.page_type}/{page.name} — 未能插入新章节")

        except Exception as e:
            print(f"    ⚠️ {page.page_type}/{page.name} — 修复失败: {e}")

    if fixed_names:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("missing_sections", fixed_names)
            mark_samples_fixed("related_source_format", fixed_names)
        except Exception as e:
            print(f"  ⚠️ 更新错题本修复状态失败: {e}")
        try:
            from error_book import append_ledger
            append_ledger(issue_type="missing_sections", auto_fixed=False,
                          fix_method="llm_supplement", note=f"补全 {len(fixed_names)} 个知识页缺失章节",
                          count=len(fixed_names))
        except Exception:
            pass

    return fixed
