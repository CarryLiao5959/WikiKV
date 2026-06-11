"""错题本 — Wiki 质量螺旋上升闭环。

设计理念：
- 发现错误 → 记录错题 → 约束注入 prompt → 下次生成避免 → 验证消失 → 关闭
- 错题本存储为 YAML 文件（error_book.yaml），供代码和人工共同维护
- 每条错题包含：现象、根因、生成约束规则、检查方法、修复状态
- 修复日志存储为 JSONL 文件（lint_ledger.jsonl），追加式记录每次修复的时戳/方法/结果

两层状态（互不干扰）：
1. **错题状态**（status: open/closed）— 决定是否给 LLM prompt 注入约束
   - 关闭条件：pass_count >= 2（模型连续不犯错）或 closed 超 30 天物理删除
   - 与 samples 的修复状态无关
2. **修复状态**（samples 内每条的 fixed: true/false）— 决定 LLM 定期修复时修不修这条
   - LLM 修复成功后把 sample 标 fixed=True
   - 下次 LLM 修复只处理 fixed=False 的

生命周期：
1. 发现：lint 自动发现或人工发现
2. 记录：写入 error_book.yaml（samples 初始 fixed=False）
3. 生效：下次 ingest 时，活跃错题的约束规则自动注入 prompt
4. 修复：LLM 定期修复 fixed=False 的 samples，修好后标 fixed=True
5. 验证：lint 检查该类问题是否消失
6. 关闭：连续 2 次未再出现 → 标记关闭（约束保留）
7. 留痕：每次修复写入 lint_ledger.jsonl，可回溯修复历史
"""

import json
import yaml
from datetime import datetime
from pathlib import Path

import config


def _get_error_book_path() -> Path:
    """获取当前用户的错题本文件路径。"""
    return config.WIKI_DIR / "error_book.yaml"


def load_error_book() -> list[dict]:
    """加载错题本。"""
    path = _get_error_book_path()
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data.get("errors", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_error_book(errors: list[dict]):
    """保存错题本。"""
    path = _get_error_book_path()
    content = yaml.dump(
        {"errors": errors},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    path.write_text(content, encoding="utf-8")


def get_active_constraints() -> str:
    """获取所有未关闭错题的生成约束规则，用于注入 prompt。

    返回格式化的约束文本，可直接拼入 system/user prompt。
    broken_link 类型只输出简短统计（"累计 N 处断链已删除"），不列详细样例。
    """
    errors = load_error_book()
    if not errors:
        return ""

    active = [e for e in errors if e.get("status") != "closed"]
    if not active:
        return ""

    lines = []
    for e in active:
        constraint = e.get("constraint", "")
        eid = e.get("id", "?")
        category = e.get("category", "")

        if e.get("brief") or category == "broken_link":
            count = e.get("count", 0)
            samples = e.get("samples", [])
            sample_names = []
            for s in samples[:3]:
                name = _sample_name(s)
                if " → " in name:
                    sample_names.append(name.split(" → ")[-1])
                else:
                    sample_names.append(name)
            sample_str = "、".join(sample_names) if sample_names else ""
            if count > 0:
                line = f"- [{eid}] 累计 {count} 处断链已自动删除"
                if sample_str:
                    line += f"（如 {sample_str}）"
                lines.append(line)
        elif constraint:
            lines.append(f"- [{eid}] {constraint}")

    if not lines:
        return ""

    return "## 已知问题（务必避免）\n\n" + "\n".join(lines) + "\n"


def record_lint_issues(issues: dict, batch_article_stems: list[str] | None = None):
    """将 lint 发现的问题自动记录到错题本。

    只记录新发现的问题（根据 category + 特征去重）。
    已存在的问题更新 last_seen 时间。

    参数：
      issues                lint 检测结果 dict
      batch_article_stems   当批文章的 stem 列表，用于给 broken_link 类型的 sample
                            附加 context.batch_articles，供后续 LLM 定期修复时读取原文。
    """
    errors = load_error_book()
    today = datetime.now().strftime("%Y-%m-%d")
    changed = False

    issue_map = {
        "broken_links": {
            "category": "broken_link",
            "description": "断裂链接：Wiki 链接指向不存在的页面（已自动删除，待定期修复时创建缺失页面）",
            "constraint": "避免创建指向不存在页面的链接，若需引用新实体请同时创建页面",  # 仅注入 ingest prompt 时的约束
            "brief": True,  # 标记：注入 prompt 时只输出简短统计
            "needs_llm_fix": True,  # 标记：需要 LLM 定期修复创建缺失页面，不靠 pass_count 关闭
        },
        "index_inconsistencies": {
            "category": "index_error",
            "description": "索引不一致：_index.md 未收录所有页面（LLM 创建页面时遗漏了索引更新）",
            "constraint": "每次创建新知识页时，必须在对应目录的 _index.md 中添加索引条目，格式为 `- [[页面名]] (别名) — 一句话概括 #标签`，追加到合适的分类分区，不要放入「## 待整理」分区",
            "needs_llm_fix": True,
        },
        "md_suffix": {
            "category": "md_suffix",
            "description": "frontmatter sources 字段值带多余 .md 后缀",
            "constraint": "frontmatter 中引用来源文件名时不要带 .md 后缀",
        },
        "duplicates": {
            "category": "duplicate",
            "description": "重复页面：同名页面出现在多个目录",
            "constraint": "创建新页面前检查已有页面名列表，同一实体必须更新已有页面而非创建新页面",
        },
        "completeness": {
            "category": "incomplete",
            "description": "页面不完整：缺少必填字段或内容空洞",
            "constraint": "每个页面必须包含 type、tags 字段，知识页至少 3 条核心事实",
        },
        "digest_incomplete": {
            "category": "digest_incomplete",
            "description": "摘要页不完整：缺少摘要/核心观点/关键信息等必要章节",
            "constraint": "每个来源摘要页（sources/digests/）必须包含完整章节：## 摘要、## 核心观点、## 关键引用、## 关键信息、## 提及实体。不可省略任何一个章节，每个章节必须有实质内容（不能只写'待补充'）。## 原文 章节由代码自动生成，无需 LLM 输出",
        },
        "missing_summary": {
            "category": "missing_summary",
            "description": "知识页缺少一句话概括：标题 # 后没有紧跟 > blockquote 格式的一句概括",
            "constraint": "每个知识页的 # 标题之后必须紧跟一行 > 一句话概括（blockquote 格式），简明描述该实体/概念的核心定位。例如：> 巴洛克时期德国作曲家，西方近代音乐之父",
            "needs_llm_fix": True,
        },
        "missing_source_article": {
            "category": "missing_source_article",
            "description": "digest 缺少 frontmatter.source_article 字段：无法精确定位其原文",
            "constraint": "生成 sources/digests/ 下摘要页时，frontmatter 必须包含 source_article 字段，值为对应原文 article 的文件名（不带 .md 后缀）。例如当文章文件名是 `2024-01-01-xxx.md` 时，digest 写 `source_article: 2024-01-01-xxx`",
            "needs_llm_fix": True,
        },
        "pending_entries": {
            "category": "pending_entries",
            "description": "_index.md「## 待整理」分区堆积未归位条目：LLM 在创建索引时未能决策分区",
            "constraint": "生成目录 _index.md 时，新增条目必须直接归入最合适的 `## 分类名` 分区（优先复用已有分区，避免只新建 1 条的孤立分区），不要留在「## 待整理」兜底分区",
            "needs_llm_fix": True,
        },
        "related_source_format": {
            "category": "related_source_format",
            "description": "相关来源章节格式错误：包含非 digest 链接或纯文本描述，代码已删除错误内容但缺少正确的 digest 链接，需 LLM 补回",
            "constraint": "知识页的「## 相关来源」章节只能包含 `[[sources/digests/日期-标题]]` 格式的链接，每行一个，如 `- [[sources/digests/2024-01-01-xxx]] — 一句话说明`。不要放入知识页链接（知识页链接属于「## 相关页面」），不要放纯文本描述",
            "needs_llm_fix": True,
        },
        "missing_sections": {
            "category": "missing_sections",
            "description": "知识页缺少必要章节：缺少 核心事实/相关页面/相关来源 中的一个或多个",
            "constraint": "每个知识页必须包含 ## 核心事实、## 相关页面、## 相关来源 三个章节。## 核心事实 至少有 2 条事实；## 相关页面 列出相关的知识页链接；## 相关来源 列出对应的 digest 链接。如果暂时没有内容，用占位符 `- （待补充）` 代替",
            "needs_llm_fix": True,
        },
        "unseen_page_overwrite": {
            "category": "unseen_page_overwrite",
            "description": "LLM 尝试覆盖未看过内容的已有页面：Step1 未选中该页面但 Step2 仍输出了更新，已被代码拦截跳过",
            "constraint": "只能修改「已有页面内容」中展示过完整内容的页面。如果某个已有页面你只在索引中看到了名字但没看到完整内容，不要尝试输出该页面的更新——否则会覆盖原有内容。对这些页面只能在「## 相关页面」中用 [[...]] 引用",
        },
    }

    for issue_key, items in issues.items():
        if issue_key not in issue_map:
            continue

        template = issue_map[issue_key]
        category = template["category"]

        existing = None
        for e in errors:
            if e.get("category") == category:
                existing = e
                break

        item_count = len(items) if isinstance(items, list) else 1
        samples = []
        if isinstance(items, list):
            for item in items[:3]:
                if isinstance(item, dict):
                    if "from" in item and "to" in item:
                        sample = {"name": f"{item['from']} → [[{item['to']}]]", "fixed": False}
                    elif "title" in item:
                        sample = {"name": item["title"], "fixed": False}
                    else:
                        continue
                elif isinstance(item, str):
                    sample = {"name": item[:80], "fixed": False}
                else:
                    continue
                if category == "broken_link" and batch_article_stems:
                    sample["context"] = {
                        "batch_articles": list(batch_article_stems),
                        "recorded_at": today,
                    }
                samples.append(sample)

        if existing:
            existing["last_seen"] = today
            if existing.get("needs_llm_fix") and category == "broken_link":
                existing_map = {}  # name → sample_dict
                for s in existing.get("samples", []):
                    s = _normalize_sample(s)
                    existing_map[_sample_name(s)] = s
                for s in samples:
                    s = _normalize_sample(s)
                    name = _sample_name(s)
                    if " → " in name:
                        target = name.split(" → ")[-1].strip("[]")
                    else:
                        target = name.strip("[]")
                    if target not in existing_map:
                        existing_map[target] = s  # 新发现的，保留完整 sample（含 context）
                existing["samples"] = [
                    v if isinstance(v, dict) else {"name": k, "fixed": False}
                    for k, v in sorted(existing_map.items(), key=lambda x: x[0])
                ]
                existing["count"] = len(existing_map)
            else:
                existing["count"] = item_count
                existing["samples"] = samples
            if existing.get("status") == "closed":
                existing["status"] = "open"
                existing["reopened_at"] = today
                existing["pass_count"] = 0
                print(f"  📔 错题 {existing['id']} 再次出现，已重新打开")
            changed = True
        else:
            max_id = 0
            for e in errors:
                eid = e.get("id", "")
                if eid.startswith("E") and eid[1:].isdigit():
                    max_id = max(max_id, int(eid[1:]))
            new_id = f"E{max_id + 1:02d}"

            new_error = {
                "id": new_id,
                "category": category,
                "description": template["description"],
                "constraint": template["constraint"],
                "status": "open",
                "count": item_count,
                "samples": samples,
                "discovered_at": today,
                "last_seen": today,
                "pass_count": 0,  # 连续检查未出现的次数
            }
            for flag_key in ("brief", "needs_llm_fix"):
                if template.get(flag_key):
                    new_error[flag_key] = True
            errors.append(new_error)
            changed = True
            print(f"  📔 新错题 {new_id}: {template['description']} ({item_count} 处)")

    seen_categories = set()
    for issue_key in issues:
        if issue_key in issue_map:
            seen_categories.add(issue_map[issue_key]["category"])

    for e in errors:
        if e.get("status") == "open" and e.get("category") not in seen_categories:
            if e.get("needs_llm_fix"):
                continue
            e["pass_count"] = e.get("pass_count", 0) + 1
            if e["pass_count"] >= 2:
                e["status"] = "closed"
                e["closed_at"] = today
                print(f"  📔 错题 {e['id']} 连续 2 次未出现，已关闭")
            changed = True

    _cleanup_old_closed(errors)

    if changed:
        save_error_book(errors)


def print_error_book():
    """打印错题本摘要。"""
    errors = load_error_book()
    if not errors:
        print("📔 错题本为空")
        return

    open_errors = [e for e in errors if e.get("status") != "closed"]
    closed_errors = [e for e in errors if e.get("status") == "closed"]

    print(f"\n{'='*60}")
    print(f"  📔 错题本（{len(open_errors)} 个活跃 / {len(closed_errors)} 个已关闭）")
    print(f"{'='*60}")

    if open_errors:
        print(f"\n🔴 活跃错题:")
        for e in open_errors:
            eid = e.get("id", "?")
            desc = e.get("description", "")
            count = e.get("count", 0)
            last_seen = e.get("last_seen", "")
            unfixed = get_unfixed_samples(e)
            fixed_count = count - len(unfixed) if count >= len(unfixed) else 0
            print(f"  [{eid}] {desc} ({count} 处, {fixed_count} 已修复, 最近 {last_seen})")
            samples = e.get("samples", [])
            for s in samples[:3]:
                name = _sample_name(s)
                tag = "✅" if _sample_is_fixed(s) else "❌"
                print(f"       {tag} {name}")

    if closed_errors:
        print(f"\n🟢 已关闭:")
        for e in closed_errors:
            eid = e.get("id", "?")
            desc = e.get("description", "")
            closed_at = e.get("closed_at", "")
            print(f"  [{eid}] {desc} (关闭于 {closed_at})")

    print(f"\n{'='*60}")


def add_error_manually(description: str, constraint: str, category: str = "manual"):
    """手动添加一条错题。"""
    errors = load_error_book()
    today = datetime.now().strftime("%Y-%m-%d")

    max_id = 0
    for e in errors:
        eid = e.get("id", "")
        if eid.startswith("E") and eid[1:].isdigit():
            max_id = max(max_id, int(eid[1:]))
    new_id = f"E{max_id + 1:02d}"

    new_error = {
        "id": new_id,
        "category": category,
        "description": description,
        "constraint": constraint,
        "status": "open",
        "count": 0,
        "samples": [],
        "discovered_at": today,
        "last_seen": today,
        "pass_count": 0,
    }
    errors.append(new_error)
    save_error_book(errors)
    print(f"  📔 已添加错题 {new_id}: {description}")
    return new_id


def _cleanup_old_closed(errors: list[dict], max_age_days: int = 30):
    """删除已关闭超过 max_age_days 天的错题条目。

    已关闭的条目不再注入 prompt，长时间保留无意义。
    """
    today = datetime.now()
    to_remove = []
    for e in errors:
        if e.get("status") != "closed":
            continue
        closed_at = e.get("closed_at", "")
        if not closed_at:
            continue
        try:
            closed_dt = datetime.strptime(closed_at, "%Y-%m-%d")
            age = (today - closed_dt).days
            if age > max_age_days:
                to_remove.append(e)
        except ValueError:
            pass

    for e in to_remove:
        errors.remove(e)
        print(f"  🗑️ 清理过期错题 {e.get('id', '?')} (已关闭 {(today - datetime.strptime(e['closed_at'], '%Y-%m-%d')).days} 天)")



def _normalize_sample(s) -> dict:
    """将旧格式的纯字符串 sample 转成带修复状态的 dict。

    兼容两种格式：
    - 旧格式："页面名" (纯字符串)
    - 新格式：{"name": "页面名", "fixed": False}
    """
    if isinstance(s, dict):
        return s
    return {"name": str(s), "fixed": False}


def _sample_name(s) -> str:
    """从 sample（dict 或 str）中取出名称。"""
    if isinstance(s, dict):
        return s.get("name", "")
    return str(s)


def _sample_is_fixed(s) -> bool:
    """判断 sample 是否已修复。"""
    if isinstance(s, dict):
        return bool(s.get("fixed", False))
    return False


def get_unfixed_samples(error_entry: dict) -> list[str]:
    """获取一条错题中所有未修复的 sample 名称列表。"""
    return [_sample_name(s) for s in error_entry.get("samples", [])
            if not _sample_is_fixed(s)]


def has_unfixed_samples(category: str = None) -> bool:
    """检查是否有指定类型（或全部类型）的未修复 samples。

    用于 _check_periodic_tasks() 判断是否需要触发 LLM 修复。
    """
    errors = load_error_book()
    for e in errors:
        if e.get("status") == "closed":
            continue
        if category and e.get("category") != category:
            continue
        if get_unfixed_samples(e):
            return True
    return False


def mark_samples_fixed(category: str, fixed_names: list[str]):
    """将指定 category 错题中匹配的 sample 标记为已修复。

    fixed_names：已修复的 sample 名称列表。
    匹配规则：精确匹配 或 sample name 以 fixed_name 开头（支持前缀匹配，
    如 digest_incomplete 的 sample 是 "sources/digests/xxx.md: 缺少/待补充 [...]"，
    传入 "sources/digests/xxx.md" 即可匹配）。
    """
    if not fixed_names:
        return
    errors = load_error_book()
    fixed_set = set(fixed_names)
    changed = False

    for e in errors:
        if e.get("category") != category:
            continue
        if e.get("status") == "closed":
            continue
        new_samples = []
        for s in e.get("samples", []):
            s = _normalize_sample(s)
            name = _sample_name(s)
            name_bare = name.rsplit("/", 1)[-1] if "/" in name else name
            name_target = ""
            name_target_bare = ""
            name_target_parts = []  # 管道符分隔的多个别名
            if " → " in name:
                name_target = name.split(" → ")[-1].strip("[]")
                name_target_bare = name_target.rsplit("/", 1)[-1] if "/" in name_target else name_target
                if " | " in name_target_bare:
                    main_part = name_target_bare.split(" | ")[0].strip()
                    alias_part = name_target_bare.split(" | ", 1)[1].strip()
                    name_target_parts = [main_part] + [a.strip() for a in alias_part.split(",")]
                elif ", " in name_target_bare:
                    name_target_parts = [a.strip() for a in name_target_bare.split(",")]
            matched = (name in fixed_set
                       or name_bare in fixed_set
                       or name_target in fixed_set
                       or name_target_bare in fixed_set
                       or any(p in fixed_set for p in name_target_parts)
                       or any(name.startswith(fn) for fn in fixed_set)
                       or any(name.endswith("/" + fn) for fn in fixed_set))
            if matched and not s.get("fixed"):
                s["fixed"] = True
                changed = True
            new_samples.append(s)
        e["samples"] = new_samples

    if changed:
        save_error_book(errors)
        print(f"  📔 已标记 {len(fixed_names)} 条 {category} sample 为已修复")


def record_sample_with_context(category: str, sample_name: str,
                                context: dict | None = None,
                                template: dict | None = None):
    """记录一条带"修复上下文"的错题 sample（不依赖 lint 扫描，由生成端主动写入）。

    用于 ingest 阶段就能发现、且后续 LLM 修复需要额外线索的问题。
    典型场景：digest 写盘后发现缺 source_article，但生成端已知道当批的 3 篇候选 article，
    把这 3 个候选写入 sample.context，下次 LLM 修复时直接 3 选 1，命中率极高。

    参数：
      category       错题类别（如 "missing_source_article"）
      sample_name    sample 的主键名（如 digest 的 stem）
      context        任意可序列化的 dict（如 {"candidates": [...], "batch_ingested_at": "..."}）
      template       新错题模板（description / constraint / needs_llm_fix），仅当该 category
                     还没有活跃错题时用于创建新条目；已有则忽略。
    """
    errors = load_error_book()
    today = datetime.now().strftime("%Y-%m-%d")

    existing = None
    for e in errors:
        if e.get("category") == category and e.get("status") != "closed":
            existing = e
            break

    if existing is None:
        if template is None:
            template = {
                "description": f"{category}（自动记录）",
                "constraint": "",
                "needs_llm_fix": True,
            }
        max_id = 0
        for e in errors:
            eid = e.get("id", "")
            if eid.startswith("E") and eid[1:].isdigit():
                max_id = max(max_id, int(eid[1:]))
        new_id = f"E{max_id + 1:02d}"
        existing = {
            "id": new_id,
            "category": category,
            "description": template.get("description", ""),
            "constraint": template.get("constraint", ""),
            "status": "open",
            "count": 0,
            "samples": [],
            "discovered_at": today,
            "last_seen": today,
            "pass_count": 0,
        }
        if template.get("needs_llm_fix"):
            existing["needs_llm_fix"] = True
        errors.append(existing)
        print(f"  📔 新错题 {new_id}: {existing['description']}")

    for s in existing.get("samples", []):
        s_n = _normalize_sample(s)
        if _sample_name(s_n) == sample_name:
            if _sample_is_fixed(s_n):
                return  # 已修复，无需再记
            if context and isinstance(s_n, dict):
                s_n.setdefault("context", {}).update(context)
            save_error_book(errors)
            return

    new_sample = {"name": sample_name, "fixed": False}
    if context:
        new_sample["context"] = context
        new_sample["recorded_at"] = today
    existing.setdefault("samples", []).append(new_sample)
    existing["count"] = len(existing["samples"])
    existing["last_seen"] = today
    save_error_book(errors)


def get_unfixed_samples_full(category: str) -> list[dict]:
    """返回某 category 下所有未修复 sample 的完整 dict（含 context），供修复器使用。"""
    errors = load_error_book()
    out: list[dict] = []
    for e in errors:
        if e.get("category") != category or e.get("status") == "closed":
            continue
        for s in e.get("samples", []):
            s_n = _normalize_sample(s)
            if not _sample_is_fixed(s_n):
                out.append(s_n)
    return out



def _get_ledger_path() -> Path:
    """获取当前用户的修复日志文件路径。"""
    return config.WIKI_DIR / "lint_ledger.jsonl"


def append_ledger(
    issue_type: str,
    file: str = "",
    auto_fixed: bool = True,
    fix_method: str = "",
    note: str = "",
    count: int = 1,
):
    """追加一条修复日志到 lint_ledger.jsonl。

    参数：
      issue_type   问题类别（如 broken_link, digest_incomplete, missing_source_article 等）
      file         涉及的文件路径或页面名
      auto_fixed   是否代码自动修复（True）还是 LLM 修复（False）
      fix_method   修复方法描述（如 delete_link, normalize_width, llm_create_page, lcs_match）
      note         补充说明
      count        本次修复的条数（默认1，批量修复时可>1）
    """
    path = _get_ledger_path()
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M"),
        "issue_type": issue_type,
        "file": file,
        "auto_fixed": auto_fixed,
        "fix_method": fix_method,
        "note": note,
        "count": count,
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志写入失败不应阻断主流程


def load_ledger() -> list[dict]:
    """加载全部修复日志。"""
    path = _get_ledger_path()
    if not path.exists():
        return []
    entries = []
    try:
        lines = path.read_text(encoding="utf-8").strip().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def print_ledger_report(days: int = 30):
    """打印修复日志聚合报告。

    按 issue_type 聚合，显示总次数、自动修/LLM修占比、最近活跃时间。
    days: 只统计最近 N 天的记录（默认30天）。
    """
    entries = load_ledger()
    if not entries:
        print("📋 修复日志为空（lint_ledger.jsonl 不存在或无记录）")
        return

    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")
    recent = [e for e in entries if e.get("ts", "") >= cutoff]

    if not recent:
        print(f"📋 最近 {days} 天无修复记录（总记录 {len(entries)} 条）")
        return

    from collections import defaultdict
    by_type: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "auto": 0, "llm": 0, "last_ts": "", "files": set()
    })
    for e in recent:
        t = e.get("issue_type", "unknown")
        bucket = by_type[t]
        cnt = e.get("count", 1)
        bucket["total"] += cnt
        if e.get("auto_fixed"):
            bucket["auto"] += cnt
        else:
            bucket["llm"] += cnt
        ts = e.get("ts", "")
        if ts > bucket["last_ts"]:
            bucket["last_ts"] = ts
        f = e.get("file", "")
        if f:
            bucket["files"].add(f)
            if len(bucket["files"]) > 5:
                bucket["files"] = set(list(bucket["files"])[-5:])

    print(f"\n{'='*70}")
    print(f"  📋 修复日志报告（最近 {days} 天，共 {len(recent)} 条记录 / {sum(b['total'] for b in by_type.values())} 次修复）")
    print(f"{'='*70}")
    print(f"\n{'issue_type':<25} {'总次数':>6} {'代码修':>6} {'LLM修':>6} {'最近活跃':<17} 涉及文件")
    print(f"{'-'*25} {'-'*6} {'-'*6} {'-'*6} {'-'*17} {'-'*20}")
    for t in sorted(by_type.keys(), key=lambda x: by_type[x]["total"], reverse=True):
        b = by_type[t]
        files_str = ", ".join(list(b["files"])[:3]) + ("..." if len(b["files"]) > 3 else "")
        print(f"{t:<25} {b['total']:>6} {b['auto']:>6} {b['llm']:>6} {b['last_ts']:<17} {files_str}")

    print(f"\n{'='*70}")
    print(f"💡 高频问题 = 下一步改生成逻辑的优先级")
    all_time = len(entries)
    print(f"📊 总历史记录: {all_time} 条（可用 --days 查看其他时间范围）")
