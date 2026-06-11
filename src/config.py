"""全局配置"""

import os
import re
import yaml
from pathlib import Path

_FM_SPLIT_RE = re.compile(r'^---\s*$', re.MULTILINE)


def split_frontmatter(text: str) -> tuple[str, str, str] | None:
    """健壮地拆分 YAML frontmatter 和正文 body。

    与 text.split("---", 2) 不同，本函数只在「行首的 ---」处分割，
    不会被 frontmatter 内容中的 --- 截断（如书名中的破折号）。

    返回:
        (before, fm_text, body) — before 通常为空字符串，fm_text 是 YAML 文本，body 是正文
        如果文件没有合法的 frontmatter 格式，返回 None
    """
    if not text.startswith("---"):
        return None
    matches = list(_FM_SPLIT_RE.finditer(text))
    if len(matches) < 2:
        return None
    start = matches[0].end()   # 第一个 --- 之后
    end = matches[1].start()   # 第二个 --- 之前
    fm_text = text[start:end]
    body = text[matches[1].end():]
    before = text[:matches[0].start()]
    return (before, fm_text, body)


USER_MAP = {
    "demo":       {"uin": "1000000001", "name": "Demo Corpus"},
    "luxun":      {"uin": "1111122222", "name": "Lu Xun"},
    "zhouzuoren": {"uin": "2222233333", "name": "Zhou Zuoren"},
    "zhuziqing":  {"uin": "3333344444", "name": "Zhu Ziqing"},
    "xiaohong":   {"uin": "4444455555", "name": "Xiao Hong"},
    "yudafu":     {"uin": "5555566666", "name": "Yu Dafu"},

    "luxun_fixed_init":         {"uin": "1111122223", "name": "Lu Xun",      "ablation": True, "ablation_group": "fixed_init",     "ablation_base": "luxun"},
    "luxun_no_dynamic_dir":     {"uin": "1111122224", "name": "Lu Xun",      "ablation": True, "ablation_group": "no_dynamic_dir", "ablation_base": "luxun"},
    "zhouzuoren_fixed_init":    {"uin": "2222233334", "name": "Zhou Zuoren", "ablation": True, "ablation_group": "fixed_init",     "ablation_base": "zhouzuoren"},
    "zhouzuoren_no_dynamic_dir":{"uin": "2222233335", "name": "Zhou Zuoren", "ablation": True, "ablation_group": "no_dynamic_dir", "ablation_base": "zhouzuoren"},
    "zhuziqing_fixed_init":     {"uin": "3333344445", "name": "Zhu Ziqing",  "ablation": True, "ablation_group": "fixed_init",     "ablation_base": "zhuziqing"},
    "zhuziqing_no_dynamic_dir": {"uin": "3333344446", "name": "Zhu Ziqing",  "ablation": True, "ablation_group": "no_dynamic_dir", "ablation_base": "zhuziqing"},
    "xiaohong_fixed_init":      {"uin": "4444455556", "name": "Xiao Hong",   "ablation": True, "ablation_group": "fixed_init",     "ablation_base": "xiaohong"},
    "xiaohong_no_dynamic_dir":  {"uin": "4444455557", "name": "Xiao Hong",   "ablation": True, "ablation_group": "no_dynamic_dir", "ablation_base": "xiaohong"},
    "yudafu_fixed_init":        {"uin": "5555566667", "name": "Yu Dafu",     "ablation": True, "ablation_group": "fixed_init",     "ablation_base": "yudafu"},
    "yudafu_no_dynamic_dir":    {"uin": "5555566668", "name": "Yu Dafu",     "ablation": True, "ablation_group": "no_dynamic_dir", "ablation_base": "yudafu"},
    "zhuziqing_no_pruning":     {"uin": "3333344447", "name": "Zhu Ziqing",  "ablation": True, "ablation_group": "no_pruning",     "ablation_base": "zhuziqing"},
    "luxun_no_pruning":         {"uin": "1111122225", "name": "Lu Xun",      "ablation": True, "ablation_group": "no_pruning",     "ablation_base": "luxun"},

    "luxun_s500":      {"uin": "6111122222", "name": "Lu Xun"},
    "zhouzuoren_s500": {"uin": "6222233333", "name": "Zhou Zuoren"},
    "zhuziqing_s500":  {"uin": "6333344444", "name": "Zhu Ziqing"},
    "xiaohong_s500":   {"uin": "6444455555", "name": "Xiao Hong"},
    "yudafu_s500":     {"uin": "6555566666", "name": "Yu Dafu"},
    "luxun_s1000":      {"uin": "7111122222", "name": "Lu Xun"},
    "zhouzuoren_s1000": {"uin": "7222233333", "name": "Zhou Zuoren"},
    "zhuziqing_s1000":  {"uin": "7333344444", "name": "Zhu Ziqing"},
    "xiaohong_s1000":   {"uin": "7444455555", "name": "Xiao Hong"},
    "yudafu_s1000":     {"uin": "7555566666", "name": "Yu Dafu"},
}

def get_ablation_group(user_key: str | None = None) -> str | None:
    """返回 user_key 对应的消融实验分组（normal / fixed_init / no_dynamic_dir / None）。

    None 表示该用户不是消融实验用户。
    """
    if user_key is None:
        user_key = _current_user
    if user_key is None:
        return None
    info = USER_MAP.get(user_key)
    if not info or not info.get("ablation"):
        return None
    return info.get("ablation_group")

def is_ablation_user(user_key: str | None = None) -> bool:
    """判断当前/指定用户是否为消融实验用户。"""
    if user_key is None:
        user_key = _current_user
    if user_key is None:
        return False
    return bool(USER_MAP.get(user_key, {}).get("ablation"))

FINDERUIN_MAP = {v["finderuin"]: k for k, v in USER_MAP.items() if "finderuin" in v}

BASE_DIR = Path(__file__).parent
RAW_DIR = BASE_DIR / "raw" / "articles"
DATA_DIR = BASE_DIR / "data"
SCHEMA_FILE = BASE_DIR / "wiki-schema.md"

DEFAULT_PAGE_TYPES = {
    "people": {"description": "人物 — 相关人物介绍", "auto_created": True},
    "works": {"description": "作品 — 代表性作品", "auto_created": True},
    "topics": {"description": "主题 — 综合性主题分析", "auto_created": True},
}

_ABLATION_FIXED_PAGE_TYPES = {
    "people":   {"description": "人物 — 与作者相关的人物（家人、朋友、师友、同时代文人等）", "auto_created": True},
    "works":    {"description": "作品 — 散文、小说、杂文、诗歌等代表作及作品分析",         "auto_created": True},
    "themes":   {"description": "主题 — 反复出现的核心思想、文学主张、社会议题",           "auto_created": True},
    "events":   {"description": "事件 — 重要历史事件、人生经历、文学活动",                 "auto_created": True},
    "language": {"description": "语言风格 — 修辞、意象、文体特征、语言习惯",               "auto_created": True},
}

USER_PAGE_TYPES = {
    "luxun_fixed_init":          _ABLATION_FIXED_PAGE_TYPES,
    "luxun_no_dynamic_dir":      _ABLATION_FIXED_PAGE_TYPES,
    "zhouzuoren_fixed_init":     _ABLATION_FIXED_PAGE_TYPES,
    "zhouzuoren_no_dynamic_dir": _ABLATION_FIXED_PAGE_TYPES,
    "zhuziqing_fixed_init":      _ABLATION_FIXED_PAGE_TYPES,
    "zhuziqing_no_dynamic_dir":  _ABLATION_FIXED_PAGE_TYPES,
    "xiaohong_fixed_init":       _ABLATION_FIXED_PAGE_TYPES,
    "xiaohong_no_dynamic_dir":   _ABLATION_FIXED_PAGE_TYPES,
    "yudafu_fixed_init":         _ABLATION_FIXED_PAGE_TYPES,
    "yudafu_no_dynamic_dir":     _ABLATION_FIXED_PAGE_TYPES,
}


FIXED_DIRS = {
    "sources": {"description": "来源页总目录"},
    "sources/digests": {"description": "摘要页 — LLM 生成的每篇文章结构化摘要"},
    "sources/articles": {"description": "原文页 — source article/video transcript 原文存档"},
    "sources/instructions": {"description": "author instructions — author的个人风格、写作习惯、核心观点"},
    "syntheses": {"description": "查询沉淀页 — 有价值的查询结果存档"},
}

ALIAS_MAP = {}

WFS_BASE = os.environ.get("WIKI_WFS_BASE", str(BASE_DIR / "wfs_mirror"))

_current_user = None


def set_user(user_key: str):
    """切换当前用户，更新所有 wiki 相关路径。

    注意：此函数不会自动启用 WFS 镜像写入。
    如需 WFS 镜像，请在 set_user 之后显式调用 enable_wfs_mirror()。
    """
    global _current_user, WIKI_DIR, CACHE_FILE, WIKI_INDEX, WIKI_OVERVIEW, WIKI_LOG, RAW_DIR, INGEST_LOG_DIR, WFS_WIKI_DIR

    if user_key not in USER_MAP:
        raise ValueError(f"未知用户: {user_key}，可选: {list(USER_MAP.keys())}")

    _current_user = user_key
    WIKI_DIR = BASE_DIR / f"{user_key}_wiki" / "wiki"
    CACHE_FILE = BASE_DIR / f".wiki-cache-{user_key}.json"
    WIKI_INDEX = WIKI_DIR / "index.md"
    WIKI_OVERVIEW = WIKI_DIR / "overview.md"
    WIKI_LOG = WIKI_DIR / "log.md"
    INGEST_LOG_DIR = BASE_DIR / f"{user_key}_wiki" / "logs"
    WFS_WIKI_DIR = None
    user_raw = BASE_DIR / "raw" / user_key / "articles"
    RAW_DIR = user_raw if user_raw.exists() else BASE_DIR / "raw" / "articles"


def enable_wfs_mirror():
    """为当前用户启用 WFS 镜像写入。

    必须在 set_user() 之后调用。pipeline.py 专用，main.py 不调用此函数。
    """
    global WFS_WIKI_DIR
    if _current_user is None:
        raise RuntimeError("请先调用 set_user() 设置当前用户")
    WFS_WIKI_DIR = Path(WFS_BASE) / f"{_current_user}_wiki" / "wiki"


def get_user() -> str | None:
    """获取当前用户 key。"""
    return _current_user


def get_current_user() -> str | None:
    """获取当前用户 key（别名，与 get_user 一致）。"""
    return _current_user


def auto_init_purpose() -> Path | None:
    """首次初始化：LLM 自动分析文章样本生成 purpose 文件。

    流程：
    1. 从 raw 目录采样文章（复用 _sample_articles_for_init）
    2. 调用强模型分析内容，生成 purpose 描述
    3. 保存到 purpose_{user_key}.md
    4. 如果 LLM 失败，生成一个基础模板

    返回生成的 purpose 文件路径，失败返回 None。
    """
    from llm_client import call_llm
    import json as _json

    if not _current_user:
        print("  ❌ 未设置当前用户，无法生成 purpose 文件")
        return None

    purpose_path = BASE_DIR / f"purpose_{_current_user}.md"
    if purpose_path.exists():
        return purpose_path

    if get_ablation_group() == "fixed_init":
        template_path = BASE_DIR / "purpose_ablation_fixed.md"
        if template_path.exists():
            user_info = USER_MAP.get(_current_user, {})
            user_name = user_info.get("name", _current_user)
            content = template_path.read_text(encoding="utf-8")
            content = content.replace("{user_name}", user_name)
            purpose_path.write_text(content, encoding="utf-8")
            print(f"  ✅ [消融实验 fixed_init] 从通用模板复制 purpose: {purpose_path.name}")
            return purpose_path
        else:
            print(f"  ⚠️ [消融实验 fixed_init] 未找到通用模板 {template_path.name}，fallback 到 LLM 生成")

    print(f"  📝 首次初始化，自动分析文章生成 purpose 文件...")

    user_info = USER_MAP.get(_current_user, {})
    user_name = user_info.get("name", _current_user)

    samples = _sample_articles_for_init(n=50)

    if not samples or len(samples) < 3:
        print(f"  ⚠️ 文章不足（{len(samples)} 篇），生成基础 purpose 模板")
        purpose_content = f"""# Wiki 研究方向

本 Wiki 是source corpus「{user_name}」AI 分身的专属知识库，服务对象是关注该author领域的用户。
知识来源是该author的source article和video source视频。


（待分析更多文章后自动补充）


- 优先提取：核心知识、人物关系、方法论、深度观点、author个人见解和精彩原话
- 适度提取：文化评论、审美观点、行业洞察
- 禁止提取：仅含"加/扫码关注"等纯引流话术且无任何具体信息的内容；与author领域完全无关的内容


关注该author领域的用户，希望通过 AI 分身了解author的专业知识和观点。
"""
        purpose_path.write_text(purpose_content, encoding="utf-8")
        print(f"  ✅ 已生成基础 purpose 模板: {purpose_path.name}")
        return purpose_path

    sample_lines = []
    for i, s in enumerate(samples, 1):
        sample_lines.append(f"### 文章 {i}: {s['title']}\n{s['excerpt']}\n")
    samples_text = "\n".join(sample_lines)

    system_prompt = """你是一个知识库架构师。你的任务是根据author的文章样本，分析author的内容定位和知识领域，生成一份 purpose 文件。

严格按以下 Markdown 格式输出，不要输出其他内容：

```markdown

本 Wiki 是source corpus「{author名}」AI 分身的专属知识库，服务对象是关注该author领域的用户。
知识来源是该author的source article和video source视频。


（用逗号分隔的 5~10 个关键领域，基于文章内容分析得出）


- 优先提取：（基于文章特点列出 3~5 个重点）
- 适度提取：（2~3 个次要方面）
- 禁止提取：（仅列出真正无信息价值的内容类型，见下方注意事项）


（一句话描述目标用户群体）
```

1. 仔细阅读文章样本，提炼author的**核心内容领域**
2. 重点知识领域要**具体**，不要泛泛而谈（如"古典音乐深度解读"比"音乐"好）
3. 摄入侧重要反映author的**写作特点**（如author擅长讲故事就强调叙事性内容）
4. 用户画像要**精准**，基于文章内容推断目标读者

"禁止提取"必须非常谨慎，只禁止**真正无任何信息价值**的内容。你需要先判断author的内容类型：
- 如果author的核心内容本身就以"推广文案""活动规则""价格套餐""招募信息"等商业化形式呈现（如电商导购、游戏代练、团购服务等），那么这些内容中包含的**具体规则、价格、条件、玩法说明**就是知识库需要的核心知识，绝不能因为文章形式像广告就排除。
- "禁止提取"应该只针对：纯引流话术（仅含"加/扫码关注"且无任何具体信息）、与author领域完全无关的内容。
- **绝对不要**笼统地写"推广信息、广告内容、活动通知"作为禁止项，这会导致大量有价值的内容被误杀。
- 如果author的文章几乎全是商业推广形式，应在"摄入侧重"中添加一段特别说明，告知后续摄入流程：本号内容天然以推广文案形式呈现，判断标准应以"是否包含具体可查询的事实信息"为准，而非文章的营销语气。

如果author的核心内容是**情感语录、社交文案、心灵鸡汤、句子合集、文案分享**等形式，这些内容本身就是author的核心产出和审美表达，是粉丝关注该author的根本原因。
- 每条语录/文案都是author精心筛选或原创的内容，具有知识沉淀价值，**绝不能**因为"看起来像社交内容"就判定为无知识价值。
- 应在"摄入侧重"中明确说明：本号内容以语录/文案合集形式呈现，每篇文章中的语录/文案都是author精选的核心内容，应完整提取并按主题归类。
- "禁止提取"中**绝对不要**写"纯社交文案""情绪语录""心灵鸡汤"等，这会导致author的全部核心内容被误杀。
- 同理，如果author的内容是**段子合集、笑话集锦、冷知识合集、每日一句**等合集形式，这些也是author的核心产出，不应跳过。"""

    user_prompt = f"""## author信息
- 名称: {user_name}
- 文章总数: {len(samples)} 篇（采样）

{samples_text}

请根据以上信息，分析author的内容定位，生成 purpose 文件。"""

    print(f"  🤖 调用强模型分析 {len(samples)} 篇文章样本，生成 purpose 文件...")

    try:
        response = call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=LLM_PREMIUM_MODEL,
            temperature=0.3,
            max_tokens=2048,
        )
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        response = None

    if response:
        import re as _re
        m = _re.search(r'```markdown\s*(.*?)\s*```', response, _re.DOTALL)
        if m:
            purpose_content = m.group(1).strip()
        else:
            purpose_content = response.strip()
            if not purpose_content.startswith("#"):
                lines = purpose_content.split("\n")
                start_idx = next((i for i, l in enumerate(lines) if l.startswith("#")), None)
                if start_idx is not None:
                    purpose_content = "\n".join(lines[start_idx:])
                else:
                    purpose_content = None

    if not response or not purpose_content:
        print(f"  ⚠️ LLM 生成 purpose 失败，使用基础模板")
        purpose_content = f"""# Wiki 研究方向

本 Wiki 是source corpus「{user_name}」AI 分身的专属知识库，服务对象是关注该author领域的用户。
知识来源是该author的source article和video source视频。


（待分析更多文章后自动补充）


- 优先提取：核心知识、人物关系、方法论、深度观点、author个人见解和精彩原话
- 适度提取：文化评论、审美观点、行业洞察
- 禁止提取：仅含"加/扫码关注"等纯引流话术且无任何具体信息的内容；与author领域完全无关的内容


关注该author领域的用户，希望通过 AI 分身了解author的专业知识和观点。
"""

    purpose_path.write_text(purpose_content, encoding="utf-8")
    print(f"  ✅ 已自动生成 purpose 文件: {purpose_path.name}")
    return purpose_path


def get_purpose_file() -> Path:
    """返回当前用户的 purpose 文件路径。如果不存在则自动生成。"""
    if not _current_user:
        raise RuntimeError("未设置当前用户，请先调用 set_user()")

    user_purpose = BASE_DIR / f"purpose_{_current_user}.md"
    if user_purpose.exists():
        return user_purpose

    result = auto_init_purpose()
    if result and result.exists():
        return result

    raise FileNotFoundError(f"用户 {_current_user} 的 purpose 文件不存在且自动生成失败: {user_purpose}")


def get_page_types() -> dict:
    """读取当前 wiki 的知识页类型注册表（page_types.yaml）。
    如果文件不存在则返回默认类型。
    兼容两种格式：标准嵌套 {"page_types": {...}} 和扁平 {key: value} 格式。
    """
    if WIKI_DIR.exists():
        yaml_path = WIKI_DIR / "page_types.yaml"
        if yaml_path.exists():
            try:
                data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            except (yaml.YAMLError, OSError, UnicodeDecodeError):
                data = None
            if data:
                if "page_types" in data:
                    return data["page_types"]
                if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
                    converted = {}
                    for name, desc in data.items():
                        if name in ("sources", "syntheses"):
                            continue
                        converted[name] = {"description": desc, "auto_created": True}
                    save_page_types(converted)
                    print(f"  🔧 已自动修复 page_types.yaml 格式")
                    return converted
    return dict(DEFAULT_PAGE_TYPES)


def save_page_types(page_types: dict):
    """保存知识页类型注册表。"""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)
    yaml_path = WIKI_DIR / "page_types.yaml"
    content = yaml.dump(
        {"page_types": page_types},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    yaml_path.write_text(content, encoding="utf-8")


def get_page_dirs() -> dict[str, Path]:
    """返回所有知识页类型对应的目录（动态类型 + 固定基础设施目录）。"""
    dirs = {}
    for name in get_page_types():
        dirs[name] = WIKI_DIR / name
    for name in FIXED_DIRS:
        dirs[name] = WIKI_DIR / name
    return dirs


def get_all_dir_info() -> dict[str, dict]:
    """返回所有目录的名称、描述、路径，用于 LLM 选择目录。"""
    result = {}
    for name, info in get_page_types().items():
        result[name] = {
            "description": info.get("description", ""),
            "path": str(WIKI_DIR / name),
        }
    for name, info in FIXED_DIRS.items():
        result[name] = {
            "description": info.get("description", ""),
            "path": str(WIKI_DIR / name),
        }
    return result


def get_dir_catalog_text() -> str:
    """生成目录概览文本，供 LLM 第一步选择目录时使用。格式类似 skill list。"""
    lines = []
    for name, info in get_all_dir_info().items():
        if name.startswith("sources/"):
            continue
        desc = info["description"]
        dir_path = WIKI_DIR / name
        if name == "sources":
            total = 0
            for sub in ("digests", "articles", "instructions"):
                sub_dir = WIKI_DIR / "sources" / sub
                if sub_dir.exists():
                    total += len([f for f in sub_dir.glob("*.md") if f.name != "_index.md"])
            count = total
            desc = "来源页 — 文章摘要、原文存档、author风格"
        else:
            count = len([f for f in dir_path.glob("*.md") if f.name != "_index.md"]) if dir_path.exists() else 0
        lines.append(f"- **{name}/** ({count} 页) — {desc}")
    return "\n".join(lines)


def ensure_wiki_dirs():
    """确保所有注册的知识页类型目录都存在，并初始化 page_types.yaml 和 purpose 文件。"""
    WIKI_DIR.mkdir(parents=True, exist_ok=True)

    if _current_user:
        purpose_path = BASE_DIR / f"purpose_{_current_user}.md"
        if not purpose_path.exists():
            auto_init_purpose()

    yaml_path = WIKI_DIR / "page_types.yaml"
    if not yaml_path.exists():
        auto_init_page_types()

    for name, dir_path in get_page_dirs().items():
        dir_path.mkdir(parents=True, exist_ok=True)


def _sample_articles_for_init(n: int = 50) -> list[dict]:
    """从 raw 目录均匀采样文章，返回 [{title, excerpt}]。

    采样策略：按文件名排序后均匀间隔抽取，确保覆盖不同时期的内容。
    每篇只读取标题 + 前 1200 字正文（初始化只调用一次，多给样本让分类更准确）。
    """
    import re as _re

    raw_dir = RAW_DIR
    if not raw_dir.exists():
        print(f"  ⚠️ raw 目录不存在: {raw_dir}")
        return []

    all_files = sorted(raw_dir.glob("*.md"))
    if not all_files:
        return []

    total = len(all_files)
    if total <= n:
        selected = all_files
    else:
        step = total / n
        indices = [int(i * step) for i in range(n)]
        if indices[-1] != total - 1:
            indices[-1] = total - 1
        selected = [all_files[i] for i in indices]

    samples = []
    for f in selected:
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue

        title = f.stem  # 默认用文件名
        body = text

        result = split_frontmatter(text)
        if result is not None:
            _, fm, body = result
            tm = _re.search(r'title:\s*["\']?(.+?)["\']?\s*$', fm, _re.MULTILINE)
            if tm:
                title = tm.group(1).strip()

        excerpt = body.strip()[:1200]
        if excerpt:
            samples.append({"title": title, "excerpt": excerpt})

    print(f"  📊 从 {total} 篇文章中采样 {len(samples)} 篇")
    return samples


def _llm_generate_page_types(samples: list[dict]) -> dict | None:
    """调用强模型 LLM 分析文章样本，自动生成合理的目录结构。

    返回 page_types dict，格式与 save_page_types 兼容。
    失败时返回 None。
    """
    from llm_client import call_llm
    import json as _json
    import re as _re

    purpose_text = ""
    try:
        purpose_file = get_purpose_file()
        purpose_text = purpose_file.read_text(encoding="utf-8")[:2000]
    except (FileNotFoundError, RuntimeError):
        pass

    user_info = USER_MAP.get(_current_user, {})
    user_name = user_info.get("name", _current_user or "未知")

    sample_lines = []
    for i, s in enumerate(samples, 1):
        sample_lines.append(f"### 文章 {i}: {s['title']}\n{s['excerpt']}\n")
    samples_text = "\n".join(sample_lines)

    system_prompt = """你是一个知识库架构师。你的任务是根据author的文章样本，设计一套合理的知识页分类目录结构。

在设计目录时，请**完全忽略**以下类型的文章，不要为它们设计专门的目录：
- 节日祝福/问候（节日快乐、新年祝福、生日祝贺等纯社交内容）
- 纯转载/导流（内容极少，主要是引导关注、转发、点击外链，author没有表达自己的观点）
- 招聘/求职信息
- 纯图片/视频合集（正文几乎没有文字知识内容）
- 纯硬广（只有产品参数、价格、购买链接，author没有个人观点）
- 纯活动通知（只有时间地点报名方式，author没有阐述理念或思考）
- 平台互动/抽奖/投票等运营性内容

⚠️ 注意区分：如果author在广告/活动中表达了个人观点、审美判断或使用体验，这些文章**不会被过滤**，仍然会进入知识库。设计目录时应考虑这类有观点的内容归入哪个主题目录（而非单独设一个"广告"或"通知"目录）。

1. 目录应该反映author**有知识价值的内容**的实际主题分布，而非所有文章的分布
2. 每个目录应该能容纳至少 5% 的**有价值文章**（避免过于细碎的分类）
3. 目录之间应该**互斥**（一篇文章主要归属一个目录）且**完备**（所有有价值的文章都能归入某个目录）
4. **禁止创建 misc、other、general、uncategorized 等兜底/杂项目录**。每个目录都必须有明确的主题含义。如果某些文章难以归类，说明你的目录设计不够完备——请扩大某个相近目录的覆盖范围，或增设一个有明确主题名的目录来容纳它们
5. 目录数量控制在 5~8 个，不宜过多也不宜过少
6. **目录名必须是单个小写英文单词**（如 philosophy、society、culture、life），禁止使用下划线或多词组合（如 culture_and_art、daily_life 都是错误的）
7. 不要创建 sources、syntheses（这些是系统保留目录）
8. 描述格式必须是「中文类型名 — 一句话描述」
9. **禁止创建以下类型的目录**：专门用于存放通知/公告、广告/推广、互动/运营、杂务/日常碎念的目录，以及 misc/other/general 等无主题兜底目录

严格输出 JSON，不要输出其他内容：
```json
{
  "page_types": {
    "目录名": {
      "description": "中文类型名 — 一句话描述",
      "reason": "为什么需要这个目录（简要说明）"
    }
  },
  "analysis": "对author内容领域的简要分析（1-2句话）"
}
```"""

    user_prompt = f"""## author信息
- 名称: {user_name}
- 文章总数: {len(samples)} 篇（采样）
"""
    if purpose_text:
        user_prompt += f"""
{purpose_text}
"""
    user_prompt += f"""
{samples_text}

请根据以上信息，设计最适合该author的知识页分类目录结构。"""

    print(f"  🤖 调用强模型分析 {len(samples)} 篇文章样本，生成目录结构...")
    print(f"     提示词长度: system={len(system_prompt)} chars, user={len(user_prompt)} chars")

    try:
        response = call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=LLM_PREMIUM_MODEL,
            temperature=0.3,
            max_tokens=4096,
        )
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        return None

    try:
        data = _json.loads(response)
    except _json.JSONDecodeError:
        m = _re.search(r'```json\s*(.*?)\s*```', response, _re.DOTALL)
        if m:
            try:
                data = _json.loads(m.group(1))
            except _json.JSONDecodeError:
                pass
            else:
                data = data  # 成功
        else:
            start = response.find('{')
            end = response.rfind('}')
            if start != -1 and end != -1 and end > start:
                try:
                    data = _json.loads(response[start:end + 1])
                except _json.JSONDecodeError:
                    print(f"  ❌ 无法解析 LLM 输出的 JSON:\n{response[:500]}")
                    return None
            else:
                print(f"  ❌ LLM 输出不包含有效 JSON:\n{response[:500]}")
                return None

    raw_types = data.get("page_types", data)
    analysis = data.get("analysis", "")
    if analysis:
        print(f"  📝 LLM 分析: {analysis}")

    page_types = {}
    reserved = {"sources", "syntheses", "sources/digests", "sources/articles", "sources/instructions"}
    banned_catchall = {"misc", "other", "others", "general", "uncategorized", "miscellaneous", "catchall", "default"}
    for name, info in raw_types.items():
        if name in reserved:
            print(f"  ⚠️ 跳过保留目录名: {name}")
            continue
        if name in banned_catchall:
            print(f"  ⚠️ 跳过兜底目录名: {name}（不允许创建无主题兜底目录）")
            continue
        if not _re.match(r'^[a-z][a-z0-9]*$', name):
            print(f"  ⚠️ 跳过非法目录名: {name}")
            continue
        if isinstance(info, dict):
            desc = info.get("description", f"{name} — 自动生成")
        elif isinstance(info, str):
            desc = info
        else:
            desc = f"{name} — 自动生成"
        page_types[name] = {"description": desc, "auto_created": True}

    if len(page_types) < 3:
        print(f"  ⚠️ LLM 生成的目录数量过少（{len(page_types)}），补充通用目录")
        if "topics" not in page_types:
            page_types["topics"] = {"description": "主题 — 综合性主题分析", "auto_created": True}
        if "people" not in page_types:
            page_types["people"] = {"description": "人物 — 相关人物介绍", "auto_created": True}
        if "works" not in page_types:
            page_types["works"] = {"description": "作品 — 代表性作品", "auto_created": True}

    if len(page_types) > 10:
        print(f"  ⚠️ LLM 生成的目录数量过多（{len(page_types)}），截取前 8 个")
        items = list(page_types.items())[:8]
        page_types = dict(items)

    return page_types


def auto_init_page_types():
    """首次初始化：LLM 自动分析文章样本生成目录结构。

    流程：
    1. 从 raw 目录采样 50 篇文章（标题 + 前 1200 字）
    2. 调用强模型（premium model）分析内容，生成 5~8 个分类目录
    3. 如果 LLM 失败，fallback 到 USER_PAGE_TYPES 或 DEFAULT_PAGE_TYPES

    消融实验特殊分支：当当前用户属于 fixed_init 组时，跳过 LLM，直接使用预设目录。
    """
    if get_ablation_group() == "fixed_init":
        page_types = USER_PAGE_TYPES.get(_current_user, _ABLATION_FIXED_PAGE_TYPES)
        save_page_types(page_types)
        print(f"  📂 [消融实验 fixed_init] 跳过 LLM，直接使用预设目录结构（{len(page_types)} 个目录）：")
        for name, info in page_types.items():
            print(f"     📁 {name}/ — {info['description']}")
        return

    print("  📂 首次初始化，自动分析文章生成目录结构...")

    samples = _sample_articles_for_init(n=50)

    if samples and len(samples) >= 5:
        page_types = _llm_generate_page_types(samples)
        if page_types:
            save_page_types(page_types)
            print(f"  ✅ LLM 自动生成 {len(page_types)} 个目录:")
            for name, info in page_types.items():
                print(f"     📁 {name}/ — {info['description']}")
            return

    print("  ⚠️ LLM 自动生成失败或文章不足，使用预设目录结构...")
    if _current_user and _current_user in USER_PAGE_TYPES:
        page_types = USER_PAGE_TYPES[_current_user]
        print(f"  📂 使用 {_current_user} 专属目录结构（{len(page_types)} 个目录）")
    else:
        page_types = DEFAULT_PAGE_TYPES
        print(f"  📂 使用默认目录结构（{len(page_types)} 个目录）")

    save_page_types(page_types)
    print(f"  ✅ 已创建 {len(page_types)} 个目录")


def _read_file_safe(path: Path, max_len: int = 5000) -> str:
    """安全读取文件，不存在则返回空。"""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "(读取失败)"
        return text[:max_len] if len(text) > max_len else text
    return "(空)"


def normalize_entity_name(name: str) -> str:
    """用别名映射表归一化实体名。"""
    return ALIAS_MAP.get(name, name)


def register_page_type(name: str, description: str, auto_created: bool = True):
    """注册新的知识页类型：更新 page_types.yaml + 创建目录。"""
    page_types = get_page_types()
    if name not in page_types:
        page_types[name] = {"description": description, "auto_created": auto_created}
        save_page_types(page_types)
        (WIKI_DIR / name).mkdir(parents=True, exist_ok=True)
        print(f"  📂 注册新知识页类型: {name} — {description}")


def _wfs_mirror_write_cfg(local_path: Path, content: str | None = None) -> None:
    """将本地 wiki 文件同步镜像写入 WFS 目录（config.py 内部使用）。"""
    if WFS_WIKI_DIR is None:
        return
    try:
        rel = local_path.relative_to(WIKI_DIR)
    except ValueError:
        return
    wfs_path = Path(WFS_WIKI_DIR) / rel
    try:
        wfs_path.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            wfs_path.write_text(content, encoding="utf-8")
        else:
            import shutil as _shutil
            _shutil.copy2(local_path, wfs_path)
    except Exception as e:
        print(f"  ⚠️ WFS 镜像写入失败 {rel}: {e}")


def _wfs_mirror_delete_cfg(local_path: Path) -> None:
    """将本地 wiki 文件/目录的删除操作同步到 WFS 镜像（config.py 内部使用）。"""
    if WFS_WIKI_DIR is None:
        return
    try:
        rel = local_path.relative_to(WIKI_DIR)
    except ValueError:
        return
    wfs_path = Path(WFS_WIKI_DIR) / rel
    try:
        if wfs_path.is_file():
            wfs_path.unlink()
        elif wfs_path.is_dir():
            import shutil as _shutil
            _shutil.rmtree(wfs_path, ignore_errors=True)
    except Exception as e:
        print(f"  ⚠️ WFS 镜像删除失败 {rel}: {e}")


def _wfs_mirror_move_cfg(src_local: Path, dst_local: Path) -> None:
    """将本地 wiki 文件的移动操作同步到 WFS 镜像（config.py 内部使用）。"""
    if WFS_WIKI_DIR is None:
        return
    try:
        src_rel = src_local.relative_to(WIKI_DIR)
        dst_rel = dst_local.relative_to(WIKI_DIR)
    except ValueError:
        return
    wfs_src = Path(WFS_WIKI_DIR) / src_rel
    wfs_dst = Path(WFS_WIKI_DIR) / dst_rel
    try:
        if wfs_src.exists():
            wfs_dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil as _shutil
            _shutil.move(str(wfs_src), str(wfs_dst))
    except Exception as e:
        print(f"  ⚠️ WFS 镜像移动失败 {src_rel} → {dst_rel}: {e}")


def apply_dir_changes(changes: list[dict]):
    """执行 LLM 提议的目录调整（拆分/合并/迁移）。"""
    import shutil, re

    page_types = get_page_types()

    def _update_frontmatter_type(md_path: Path, new_type: str):
        """更新知识页 frontmatter 中的 type 字段，使其与新目录一致。"""
        if not md_path.exists():
            return
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        result = split_frontmatter(text)
        if result is None:
            return
        pre, fm_text, body = result
        if re.search(r'^type:\s*', fm_text, re.MULTILINE):
            fm_text = re.sub(r'^type:\s*.*$', f'type: {new_type}', fm_text, count=1, flags=re.MULTILINE)
        else:
            fm_text = fm_text.rstrip() + f'\ntype: {new_type}\n'
        new_text = f"---\n{fm_text}\n---\n{body}"
        md_path.write_text(new_text, encoding="utf-8")
        _wfs_mirror_write_cfg(md_path, new_text)

    def _remove_index_entry(index_path: Path, page_name: str):
        """从 _index.md 中移除指定页面的条目行。"""
        if not index_path.exists():
            return
        try:
            text = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return
        lines = text.split("\n")
        new_lines = []
        for line in lines:
            m = re.match(r'^(\s*-\s*)\[\[([^\]]+)\]\]', line)
            if m and m.group(2).split('|')[0].strip() == page_name:
                continue  # 跳过该条目
            new_lines.append(line)
        result = "\n".join(new_lines)
        result = re.sub(r'\n{3,}', '\n\n', result)
        index_path.write_text(result, encoding="utf-8")
        _wfs_mirror_write_cfg(index_path, result)

    def _add_index_entry(index_path: Path, page_name: str, md_path: Path):
        """向 _index.md 追加页面条目（从知识页 frontmatter 提取信息）。"""
        aliases = ""
        summary = ""
        tags = ""
        try:
            text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        except (OSError, UnicodeDecodeError):
            text = ""
        fm_result = split_frontmatter(text)
        if fm_result is not None:
            _, fm, body = fm_result
            am = re.search(r'aliases:\s*\[(.+?)\]', fm)
            if am:
                aliases = am.group(1).strip()
            tm = re.search(r'tags:\s*\[(.+?)\]', fm)
            if tm:
                tag_list = [t.strip().strip("'\"") for t in tm.group(1).split(",")]
                tags = " ".join(f"#{t}" for t in tag_list if t)
        else:
            body = text
        for line in body.strip().split("\n"):
            if line.startswith("> ") and not line.startswith("> 关键词") and not line.startswith("> 覆盖"):
                summary = line[2:].strip()
                break

        alias_part = f" ({aliases})" if aliases else ""
        summary_part = f" — {summary}" if summary else ""
        tags_part = f" {tags}" if tags else ""
        entry = f"- [[{page_name}]]{alias_part}{summary_part}{tags_part}"

        if not index_path.exists():
            dir_name = index_path.parent.name
            pt = get_page_types()
            type_info = pt.get(dir_name, {})
            description = type_info.get("description", dir_name)
            cn_name = description.split("—")[0].strip() if "—" in description else dir_name
            index_path.write_text(f"# {cn_name}\n> {description}\n\n## 待整理\n{entry}\n", encoding="utf-8")
            _wfs_mirror_write_cfg(index_path, f"# {cn_name}\n> {description}\n\n## 待整理\n{entry}\n")
        else:
            try:
                idx_text = index_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return
            if "## 待整理" in idx_text:
                idx_text = idx_text.replace("## 待整理", f"## 待整理\n{entry}")
            else:
                idx_text = idx_text.rstrip() + f"\n\n## 待整理\n{entry}"
            index_path.write_text(idx_text, encoding="utf-8")
            _wfs_mirror_write_cfg(index_path, idx_text)

    for change in changes:
        action = change.get("action", "")
        from_dir = change.get("from", "")
        to_dir = change.get("to", "")
        reason = change.get("reason", "")
        move_pages = change.get("move_pages", [])
        desc = change.get("description", "")

        if desc and "—" not in desc and " — " not in desc:
            if reason:
                desc = f"{desc} — {reason}"

        if action == "split":
            if to_dir and to_dir not in page_types:
                page_types[to_dir] = {"description": desc or f"{to_dir} — 从 {from_dir} 拆分", "auto_created": True}
                save_page_types(page_types)
                new_dir_path = WIKI_DIR / to_dir
                new_dir_path.mkdir(parents=True, exist_ok=True)
                print(f"  📂 拆分目录: {from_dir} → {to_dir} — {reason}")

                from_dir_path = WIKI_DIR / from_dir
                from_idx = from_dir_path / "_index.md"
                to_idx = new_dir_path / "_index.md"
                for page_name in move_pages:
                    src = from_dir_path / f"{page_name}.md"
                    if src.exists():
                        dst = new_dir_path / f"{page_name}.md"
                        shutil.move(str(src), str(dst))
                        _wfs_mirror_move_cfg(src, dst)
                        _update_frontmatter_type(dst, to_dir)
                        print(f"    📄 迁移: {page_name}.md → {to_dir}/")
                        _remove_index_entry(from_idx, page_name)
                        _add_index_entry(to_idx, page_name, dst)
                        print(f"    📝 索引更新: {page_name} 从 {from_dir}/_index.md 移至 {to_dir}/_index.md")

        elif action == "merge":
            if from_dir in page_types and to_dir:
                from_dir_path = WIKI_DIR / from_dir
                to_dir_path = WIKI_DIR / to_dir
                to_dir_path.mkdir(parents=True, exist_ok=True)

                to_idx = to_dir_path / "_index.md"
                if from_dir_path.exists():
                    moved_pages = []
                    for md_file in from_dir_path.glob("*.md"):
                        if md_file.name == "_index.md":
                            continue  # _index.md 单独处理
                        dst = to_dir_path / md_file.name
                        if not dst.exists():
                            shutil.move(str(md_file), str(dst))
                            _wfs_mirror_move_cfg(md_file, dst)
                            _update_frontmatter_type(dst, to_dir)
                            print(f"    📄 合并: {md_file.name} → {to_dir}/")
                            moved_pages.append(md_file.stem)

                    for page_name in moved_pages:
                        _add_index_entry(to_idx, page_name, to_dir_path / f"{page_name}.md")

                    from_idx = from_dir_path / "_index.md"
                    if from_idx.exists():
                        from_idx.unlink()
                        _wfs_mirror_delete_cfg(from_idx)

                if from_dir_path.exists():
                    for f in from_dir_path.iterdir():
                        f.unlink()
                        _wfs_mirror_delete_cfg(f)
                    from_dir_path.rmdir()
                    _wfs_mirror_delete_cfg(from_dir_path)
                del page_types[from_dir]
                save_page_types(page_types)
                print(f"  📂 合并目录: {from_dir} → {to_dir} — {reason}")

        elif action == "move_page":
            if from_dir and to_dir and move_pages:
                if to_dir not in page_types:
                    fallback_desc = f"{to_dir} — 从 {from_dir} 迁移"
                    final_desc = desc or fallback_desc
                    page_types[to_dir] = {"description": final_desc, "auto_created": True}
                    save_page_types(page_types)
                    print(f"  📂 注册新目录: {to_dir} — {final_desc}")

                from_dir_path = WIKI_DIR / from_dir
                to_dir_path = WIKI_DIR / to_dir
                to_dir_path.mkdir(parents=True, exist_ok=True)
                from_idx = from_dir_path / "_index.md"
                to_idx = to_dir_path / "_index.md"

                for page_name in move_pages:
                    src = from_dir_path / f"{page_name}.md"
                    if src.exists():
                        dst = to_dir_path / f"{page_name}.md"
                        if not dst.exists():
                            shutil.move(str(src), str(dst))
                            _wfs_mirror_move_cfg(src, dst)
                            _update_frontmatter_type(dst, to_dir)
                            print(f"    📄 迁移: {page_name}.md {from_dir}/ → {to_dir}/")
                            _remove_index_entry(from_idx, page_name)
                            _add_index_entry(to_idx, page_name, dst)
                            print(f"    📝 索引更新: {page_name} 从 {from_dir}/_index.md 移至 {to_dir}/_index.md")

                print(f"  📂 页面迁移: {move_pages} {from_dir} → {to_dir} — {reason}")

    _update_wiki_references_after_move(changes)


def _update_wiki_references_after_move(changes: list[dict]):
    """更新 wiki 中所有文件的 [[old_dir/page]] 引用为 [[new_dir/page]]。

    当页面被移动/拆分/合并后，其他文件中对该页面的引用路径需要同步更新。
    """
    import re as _re

    if not WIKI_DIR or not WIKI_DIR.exists():
        return

    ref_map: dict[str, str] = {}
    for change in changes:
        action = change.get("action", "")
        from_dir = change.get("from", "")
        to_dir = change.get("to", "")
        move_pages = change.get("move_pages", [])

        if action in ("split", "move_page") and from_dir and to_dir and move_pages:
            for page_name in move_pages:
                ref_map[f"{from_dir}/{page_name}"] = f"{to_dir}/{page_name}"
        elif action == "merge" and from_dir and to_dir:
            to_path = WIKI_DIR / to_dir
            if to_path.exists():
                for md_file in to_path.glob("*.md"):
                    if md_file.name == "_index.md":
                        continue
                    ref_map[f"{from_dir}/{md_file.stem}"] = f"{to_dir}/{md_file.stem}"

    if not ref_map:
        return

    updated_files = 0
    for md_file in sorted(WIKI_DIR.rglob("*.md")):
        try:
            text = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        new_text = text
        for old_ref, new_ref in ref_map.items():
            new_text = new_text.replace(f"[[{old_ref}]]", f"[[{new_ref}]]")

        if new_text != text:
            try:
                md_file.write_text(new_text, encoding="utf-8")
                _wfs_mirror_write_cfg(md_file, new_text)
                updated_files += 1
            except OSError:
                pass

    if updated_files > 0:
        print(f"  🔗 引用路径已更新: {updated_files} 个文件中的 {len(ref_map)} 条引用已重映射")


WIKI_DIR = BASE_DIR / "wiki"
CACHE_FILE = BASE_DIR / ".wiki-cache.json"
WIKI_INDEX = WIKI_DIR / "index.md"
WIKI_OVERVIEW = WIKI_DIR / "overview.md"
WIKI_LOG = WIKI_DIR / "log.md"
INGEST_LOG_DIR = BASE_DIR / "logs"  # 每次运行详细日志目录，set_user 后覆盖
WFS_WIKI_DIR: Path | None = None   # WFS 镜像目录，set_user 后覆盖（未设置用户时为 None）

_OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

MODEL_PROFILES = {
    "default": {
        "premium_model": os.environ.get("LLM_PREMIUM_MODEL", "gpt-4o"),
        "premium_api_base": _OPENAI_BASE_URL,
        "description": "Default OpenAI-compatible profile (override via env vars).",
    },
}

_current_model_profile = None

LLM_PREMIUM_MODEL = os.environ.get("LLM_PREMIUM_MODEL", "gpt-4o")
LLM_PREMIUM_API_BASE = os.environ.get("LLM_PREMIUM_API_BASE", _OPENAI_BASE_URL)
LLM_FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "gpt-4o-mini")
LLM_FAST_API_BASE = os.environ.get("LLM_FAST_API_BASE", _OPENAI_BASE_URL)

LLM_ECONOMY_MODEL = LLM_FAST_MODEL
LLM_ECONOMY_API_BASE = LLM_FAST_API_BASE

LLM_MODEL = LLM_FAST_MODEL
LLM_API_BASE = LLM_FAST_API_BASE
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "16384"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

LLM_STEP1_MODEL = LLM_FAST_MODEL
LLM_STEP1_API_BASE = LLM_FAST_API_BASE
LLM_STEP2_MODEL = LLM_PREMIUM_MODEL
LLM_STEP2_API_BASE = LLM_PREMIUM_API_BASE
LLM_STEP1_TEMPERATURE = LLM_TEMPERATURE
LLM_STEP2_TEMPERATURE = LLM_TEMPERATURE
LLM_QUERY_MODEL = LLM_PREMIUM_MODEL
LLM_QUERY_API_BASE = LLM_PREMIUM_API_BASE
LLM_LINT_MODEL = LLM_PREMIUM_MODEL
LLM_LINT_API_BASE = LLM_PREMIUM_API_BASE


def set_model_profile(profile_name: str):
    """切换模型 profile，更新所有强模型相关配置。

    Args:
        profile_name: 'model1'~'model5' 之一，或 'auto'
    """
    global _current_model_profile
    global LLM_PREMIUM_MODEL, LLM_PREMIUM_API_BASE
    global LLM_STEP2_MODEL, LLM_STEP2_API_BASE
    global LLM_QUERY_MODEL, LLM_QUERY_API_BASE
    global LLM_LINT_MODEL, LLM_LINT_API_BASE

    if profile_name not in MODEL_PROFILES:
        raise ValueError(f"未知模型 profile: {profile_name}，可选: {list(MODEL_PROFILES.keys())}")

    profile = MODEL_PROFILES[profile_name]
    _current_model_profile = profile_name

    LLM_PREMIUM_MODEL = profile["premium_model"]
    LLM_PREMIUM_API_BASE = profile["premium_api_base"]

    LLM_STEP2_MODEL = LLM_PREMIUM_MODEL
    LLM_STEP2_API_BASE = LLM_PREMIUM_API_BASE
    LLM_QUERY_MODEL = LLM_PREMIUM_MODEL
    LLM_QUERY_API_BASE = LLM_PREMIUM_API_BASE
    LLM_LINT_MODEL = LLM_PREMIUM_MODEL
    LLM_LINT_API_BASE = LLM_PREMIUM_API_BASE

    print(f"🔧 模型 profile: {profile_name} — {profile['description']}")
    print(f"   强模型: {LLM_PREMIUM_MODEL}")
    print(f"   API: {LLM_PREMIUM_API_BASE}")


def get_model_profile() -> str | None:
    """获取当前模型 profile 名称。"""
    return _current_model_profile


LLM_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}",
}


def get_llm_headers(uin: str | None = None) -> dict:
    """返回带动态 Trace-ID 和用户 UIN 的请求 header。

    Args:
        uin: 当前用户的 UIN（用于负载均衡/KV cache 复用）。
             如果不传则从 _current_user 自动获取；如果也没有则用 token 中的 RTX。
    """
    import uuid
    headers = dict(LLM_HEADERS)
    headers["X-Request-Id"] = uuid.uuid4().hex
    return headers

INGEST_BATCH_SIZE = 10          # 每批摄入文章数
INGEST_MAX_CONTENT_LEN = 15000  # 单篇文章最大字符数（超长截断）
INGEST_MIN_CONTENT_LEN = 50    # 低于此长度跳过
INGEST_CONSOLIDATE_EVERY = 30  # 每累计摄入 N 篇文章后自动触发一次目录优化+知识概览更新（目录结构变更需保守，不宜过于频繁）
INGEST_OVERVIEW_EVERY = 30     # 每累计摄入 N 篇文章后更新知识概览（与 consolidate 同频，consolidate 后会顺带更新概览）
INGEST_PERIODIC_EVERY = 15      # 每摄入 N 篇文章后触发定期维护（错题修复等），建议设为 batch_size 的倍数
INGEST_CONTRADICTION_EVERY = 30  # 每累计摄入 N 篇文章后触发一次 LLM 矛盾检测（与 consolidate 同频）

QUERY_TOP_K = 5                 # 返回 Top-K 相关页面

WEIGHT_DIRECT_LINK = 3.0
WEIGHT_SOURCE_OVERLAP = 4.0
WEIGHT_ADAMIC_ADAR = 1.5
WEIGHT_TYPE_AFFINITY = 1.0

TYPE_BONUS = {
    "source": 0.1,
    "synthesis": 0.1,
    "_default": 0.3,  # 动态知识页目录的默认加分
}