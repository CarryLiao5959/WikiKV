"""Lint 引擎 — Wiki 健康检查 + 目录结构优化。

quick_lint() — 每次 ingest 后自动运行的完整代码检查（秒级完成，不含 LLM）：
1. 断裂链接（指向不存在的页面）
2. index 一致性（含跨目录引用检测）
3. sources .md 后缀检测
4. 重复检测（标题精确匹配）
5. 完整性检查（frontmatter 必填字段）
6. digest 章节完整性检查
7. 孤立页面（无入站链接）
8. 缺失内容（被≥2页面引用但无独立页面）
9. 过时页面（超过90天未更新）
10. 噪声检测（关注引导、署名行等）
11. 实体异常频率检测
12. 内容空洞检测（知识页正文太短）
13. 无效文件检测（文件过小）
14. 相关页面格式检测（列表项缺少 [[...]] 链接）
15. 零宽字符检测（文件名或内容含 U+200B 等不可见字符）

lint_wiki() — 手动运行的完整检查：
- 复用 quick_lint() 所有检查项
- 额外：LLM 矛盾检测（可选，需 --llm）

自动修复：
- auto_fix_broken_links() — 删除指向不存在页面的 [[wikilink]]
- auto_fix_index_inconsistencies() — 修复 _index.md 中的不一致
- auto_fix_md_suffix() — 修复 sources 字段中的 .md 后缀
- auto_fix_completeness() — 补全缺少 type/tags 的页面
- auto_fix_noise() — 清理 digest 中的噪声内容
- auto_fix_invalid_files() — 删除过小的无效文件
- auto_fix_related_page_format() — 删除相关页面中缺少链接的行

目录结构优化（consolidate）：
- LLM 审视目录结构，提议拆分/合并/迁移 → 自动执行
"""

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import config

def _robust_fm_split(text):
    """兼容 text.split('---', 2) 的返回格式，但使用行首 --- 匹配。
    
    返回 [before, fm_text, body] 列表（与 split 返回格式一致），
    如果没有合法 frontmatter 则返回 [text]（与 split 行为一致）。
    """
    result = config.split_frontmatter(text)
    if result is None:
        return [text]
    return list(result)


from config import get_page_dirs, get_page_types
from wiki_page import WikiGraph, WikiPage, _extract_page_name
from llm_client import call_llm_json


def lint_wiki(use_llm: bool = False) -> dict:
    """运行 Wiki 完整健康检查。复用 quick_lint() 的所有检查项，再加上 LLM 矛盾检测。

    use_llm=True 时用 LLM 检测矛盾。
    """
    graph = WikiGraph()
    graph.load_all()

    issues = quick_lint()

    report = {
        "timestamp": datetime.now().isoformat(),
        "stats": graph.stats,
        "orphan_pages": issues.get("orphan_pages", []),
        "broken_links": issues.get("broken_links", []),
        "missing_content": issues.get("missing_content", []),
        "index_inconsistencies": issues.get("index_inconsistencies", []),
        "stale_pages": issues.get("stale_pages", []),
        "noise_issues": issues.get("noise_issues", []),
        "entity_anomalies": issues.get("entity_anomalies", []),
        "completeness_issues": issues.get("completeness", []),
        "duplicates": issues.get("duplicates", []),
        "digest_incomplete": issues.get("digest_incomplete", []),
        "broken_frontmatter": issues.get("broken_frontmatter", []),
        "hollow_pages": issues.get("hollow_pages", []),
        "invalid_files": issues.get("invalid_files", []),
        "related_page_format": issues.get("related_page_format", []),
        "type_path_mismatch": issues.get("type_path_mismatch", []),
        "contradictions": [],
        "suggestions": [],
    }

    if not graph.pages:
        report["suggestions"].append("Wiki 为空，请先运行 ingest 命令")
        return report

    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue
        try:
            idx_text = idx_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        idx_links = set(re.findall(r'\[\[([^\]]+)\]\]', idx_text))
        for name, page in graph.pages.items():
            if page.page_type == dir_name and name not in idx_links:
                path_link = f"{dir_name}/{name}"
                if path_link not in idx_links:
                    report["index_inconsistencies"].append(f"{dir_name}/ 页面 {name} 未在 _index.md 中列出")

    if use_llm and len(graph.pages) >= 5:
        report["contradictions"] = _detect_contradictions_with_llm(graph)

    stats = graph.stats
    if stats["orphans"] > stats["total_pages"] * 0.15:
        report["suggestions"].append(f"孤立率过高 ({stats['orphans']}/{stats['total_pages']})，建议增加交叉引用")
    if stats["avg_links"] < 3:
        report["suggestions"].append(f"平均链接数过低 ({stats['avg_links']:.1f})，建议增加 [[wikilink]]")
    if stats["broken_links"] > 0:
        report["suggestions"].append(f"有 {stats['broken_links']} 个断裂链接需要修复")
    if report["missing_content"]:
        top = report["missing_content"][:3]
        names = ", ".join(m["name"] if isinstance(m, dict) else str(m) for m in top)
        report["suggestions"].append(f"建议为高引用内容创建独立页面: {names}")
    if report["noise_issues"]:
        report["suggestions"].append(f"发现 {len(report['noise_issues'])} 处噪声内容，建议清理")
    if report["entity_anomalies"]:
        report["suggestions"].append(f"实体频率异常，可能是实体名不统一")
    if report["completeness_issues"]:
        report["suggestions"].append(f"有 {len(report['completeness_issues'])} 个页面缺少必填字段")
    if report["duplicates"]:
        report["suggestions"].append(f"发现 {len(report['duplicates'])} 组重复页面")
    if report["hollow_pages"]:
        report["suggestions"].append(f"有 {len(report['hollow_pages'])} 个内容空洞的知识页")
    if report["invalid_files"]:
        report["suggestions"].append(f"有 {len(report['invalid_files'])} 个无效文件待清理")

    return report


def _detect_contradictions_with_llm(graph: WikiGraph) -> list[dict]:
    """用 LLM 检测页面间的矛盾（抽样检查）。"""
    import random

    candidates = list(graph.pages.values())
    sample = random.sample(candidates, min(5, len(candidates)))

    sample_text = ""
    for page in sample:
        sample_text += f"\n### {page.name} ({page.page_type})\n{page.content[:1500]}\n"

    try:
        result = call_llm_json(
            "你是知识库审核专家。检查以下 Wiki 页面之间是否存在矛盾信息。",
            f"""## Wiki 页面
{sample_text}


{{
  "contradictions": [
    {{"page_a": "页面A", "page_b": "页面B", "conflict": "矛盾描述"}}
  ]
}}

如果没有发现矛盾，返回空列表。""",
            model=config.LLM_PREMIUM_MODEL,
        )
        return result.get("contradictions", [])
    except Exception as e:
        print(f"  ⚠️ LLM 矛盾检测失败: {e}")
        return []


def print_lint_report(report: dict):
    """打印 Lint 报告。"""
    stats = report["stats"]
    print("=" * 60)
    print("  Wiki 健康检查报告")
    print("=" * 60)

    print(f"\n📊 基础统计")
    print(f"  总页面数: {stats['total_pages']}")
    for ptype, count in sorted(stats["by_type"].items()):
        print(f"    {ptype}: {count}")
    print(f"  总链接数: {stats['total_links']}")
    print(f"  平均链接: {stats['avg_links']:.1f}")

    orphans = report["orphan_pages"]
    broken = report["broken_links"]
    missing = report["missing_content"]
    index_issues = report["index_inconsistencies"]
    stale = report["stale_pages"]
    noise = report.get("noise_issues", [])
    entity_anomalies = report.get("entity_anomalies", [])
    completeness = report.get("completeness_issues", [])
    duplicates = report.get("duplicates", [])
    contradictions = report.get("contradictions", [])
    hollow = report.get("hollow_pages", [])
    invalid = report.get("invalid_files", [])
    digest_incomplete = report.get("digest_incomplete", [])
    related_format = report.get("related_page_format", [])

    if orphans:
        print(f"\n🔗 孤立页面 ({len(orphans)})")
        for name in orphans[:10]:
            print(f"    {name}")
        if len(orphans) > 10:
            print(f"    ... 还有 {len(orphans)-10} 个")

    if broken:
        print(f"\n💔 断裂链接 ({len(broken)})")
        for item in broken[:10]:
            if isinstance(item, dict):
                print(f"    {item['from']} → [[{item['to']}]]")
            else:
                print(f"    {item}")

    if missing:
        print(f"\n❓ 缺失内容 (被引用但无独立页面)")
        for item in missing[:10]:
            if isinstance(item, dict):
                print(f"    [[{item['name']}]] — 被 {item['referenced_by']} 个页面引用")
            else:
                print(f"    {item}")

    if index_issues:
        print(f"\n📋 index.md 一致性问题 ({len(index_issues)})")
        for issue in index_issues[:10]:
            print(f"    {issue}")

    if stale:
        print(f"\n⏰ 过时页面 ({len(stale)})")
        for item in stale[:10]:
            if isinstance(item, dict):
                print(f"    {item['name']} — 最后更新: {item['last_updated']} ({item['days_stale']}天前)")
            else:
                print(f"    {item}")

    if noise:
        print(f"\n🗑️ 噪声内容 ({len(noise)})")
        for item in noise[:10]:
            if isinstance(item, dict):
                print(f"    {item['file']} — {item['type']}: {', '.join(item['matches'][:2])}")
            else:
                print(f"    {item}")
        if len(noise) > 10:
            print(f"    ... 还有 {len(noise)-10} 处")

    if entity_anomalies:
        print(f"\n📈 实体频率异常 ({len(entity_anomalies)})")
        for item in entity_anomalies[:10]:
            if isinstance(item, dict):
                print(f"    [[{item['entity']}]] 出现 {item['count']} 次（均值 {item['avg']}，阈值 {item['threshold']}）")
            else:
                print(f"    {item}")

    if completeness:
        print(f"\n📝 完整性问题 ({len(completeness)})")
        for item in completeness[:10]:
            if isinstance(item, dict):
                print(f"    {item['type']}/{item['page']} — 缺少: {', '.join(item['missing_fields'])}")
            else:
                print(f"    {item}")
        if len(completeness) > 10:
            print(f"    ... 还有 {len(completeness)-10} 个")

    if duplicates:
        print(f"\n🔄 重复页面 ({len(duplicates)})")
        for item in duplicates:
            if isinstance(item, dict):
                print(f"    {item['title']} → {', '.join(item['locations'])}")
            else:
                print(f"    {item}")

    if digest_incomplete:
        print(f"\n📄 摘要页不完整 ({len(digest_incomplete)})")
        for item in digest_incomplete[:10]:
            print(f"    {item}")
        if len(digest_incomplete) > 10:
            print(f"    ... 还有 {len(digest_incomplete)-10} 个")

    if hollow:
        print(f"\n📭 内容空洞 ({len(hollow)})")
        for item in hollow[:10]:
            print(f"    {item}")
        if len(hollow) > 10:
            print(f"    ... 还有 {len(hollow)-10} 个")

    if invalid:
        print(f"\n📁 无效文件 ({len(invalid)})")
        for item in invalid[:10]:
            print(f"    {item}")

    if related_format:
        print(f"\n🔗 相关页面格式问题 ({len(related_format)})")
        for item in related_format[:10]:
            print(f"    {item}")
        if len(related_format) > 10:
            print(f"    ... 还有 {len(related_format) - 10} 个")

    type_path = report.get("type_path_mismatch", [])
    if type_path:
        print(f"\n🏷️ type与目录不一致 ({len(type_path)})")
        for item in type_path[:10]:
            print(f"    {item}")
        if len(type_path) > 10:
            print(f"    ... 还有 {len(type_path) - 10} 个")

    if contradictions:
        print(f"\n⚠️ 矛盾检测 ({len(contradictions)})")
        for item in contradictions:
            if isinstance(item, dict):
                print(f"    {item.get('page_a', '?')} ↔ {item.get('page_b', '?')}: {item.get('conflict', '')}")
            else:
                print(f"    {item}")

    suggestions = report.get("suggestions", [])
    if suggestions:
        print(f"\n💡 建议")
        for s in suggestions:
            print(f"    • {s}")

    print(f"\n{'='*60}")


def save_lint_report(report: dict):
    """将 Lint 报告保存为 Wiki 页面。"""
    import json
    report_path = config.WIKI_DIR / "lint-report.md"
    today = datetime.now().strftime("%Y-%m-%d")

    stats = report["stats"]
    content = f"""---
type: lint-report
created: {today}
---



| 指标 | 值 |
|------|---|
| 总页面数 | {stats['total_pages']} |
| 总链接数 | {stats['total_links']} |
| 平均链接 | {stats['avg_links']:.1f} |
| 孤立页面 | {len(report['orphan_pages'])} |
| 断裂链接 | {len(report['broken_links'])} |

"""
    if report["suggestions"]:
        content += "\n## 建议\n\n"
        for s in report["suggestions"]:
            content += f"- {s}\n"

    if report["missing_content"]:
        content += "\n## 缺失内容\n\n"
        for item in report["missing_content"][:20]:
            content += f"- [[{item['name']}]] — 被 {item['referenced_by']} 个页面引用\n"

    report_path.write_text(content, encoding="utf-8")
    print(f"\n📄 Lint 报告已保存: {report_path}")




def consolidate_wiki(dry_run: bool = False, total_ingested: int = 0) -> dict:
    """让 LLM 审视目录结构，提议拆分/合并/迁移，然后执行。

    适用时机：
    - 每摄入 N 篇文章后自动触发
    - 手动执行 `python main.py --user tym consolidate`
    - 目录数过多（>10）或某些目录页数过少（<3）

    dry_run=True: 只输出 LLM 建议，不执行。
    total_ingested: 已摄入的源文档总数，帮助 LLM 判断"目录页少是因为没用还是文章还不够多"。
    """
    import config as _cfg
    if _cfg.get_ablation_group() == "no_dynamic_dir":
        print("  🚫 [消融实验 no_dynamic_dir] 跳过 consolidate_wiki（目录结构冻结）")
        return {"status": "skipped", "reason": "ablation:no_dynamic_dir"}

    graph = WikiGraph()
    graph.load_all()

    if len(graph.pages) < 10:
        return {"status": "skipped", "reason": "页面太少，无需优化"}

    dir_stats = _collect_dir_stats(graph)

    suggestions = _llm_audit_structure(dir_stats, graph, total_ingested=total_ingested)

    if not suggestions:
        return {"status": "no_changes", "suggestions": []}

    if dry_run:
        print("\n📋 目录结构优化建议（dry run，未执行）：")
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. [{s['action']}] {s.get('from', '')} → {s.get('to', '')} — {s.get('reason', '')}")
        return {"status": "dry_run", "suggestions": suggestions}

    from config import apply_dir_changes
    apply_dir_changes(suggestions)

    from ingest import _rebuild_global_index
    _rebuild_global_index(update_overview=True)

    return {"status": "executed", "suggestions": suggestions}


def _collect_dir_stats(graph: WikiGraph) -> dict:
    """收集各目录的统计信息，供 LLM 判断是否需要拆分/合并。"""
    from config import get_page_dirs, get_page_types

    dir_stats = {}
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name in ("sources", "syntheses") or dir_name.startswith("sources/"):
            continue  # 固定目录不参与优化

        pages = [p for p in graph.pages.values() if p.page_type == dir_name]
        if not pages:
            dir_stats[dir_name] = {"count": 0, "pages": [], "description": ""}
            continue

        page_summaries = []
        for p in sorted(pages, key=lambda x: x.name):
            facts = []
            for line in p.content.split("\n"):
                line = line.strip()
                if line.startswith("- ") and not line.startswith("- [["):
                    facts.append(line[2:])
                    if len(facts) >= 3:
                        break
            page_summaries.append({
                "name": p.name,
                "aliases": p.frontmatter.get("aliases", []),
                "tags": p.frontmatter.get("tags", []),
                "facts_preview": facts,
                "outgoing_links": list(p.outgoing_links)[:10],
            })

        pt = get_page_types()
        desc = pt.get(dir_name, {}).get("description", "")

        dir_stats[dir_name] = {
            "count": len(pages),
            "pages": page_summaries,
            "description": desc,
        }

    return dir_stats


def _llm_audit_structure(dir_stats: dict, graph: WikiGraph, total_ingested: int = 0) -> list[dict]:
    """让 LLM 审视目录结构，输出拆分/合并/迁移建议。"""
    dir_text = ""
    for dir_name, stats in sorted(dir_stats.items()):
        dir_text += f"\n### {dir_name}/ ({stats['count']} 页) — {stats['description']}\n"
        for p in stats["pages"][:20]:  # 每个目录最多展示20页
            alias_str = f" (别名: {', '.join(str(a) for a in p['aliases'][:2])})" if p.get("aliases") else ""
            tags_str = f" #{' #'.join(str(t) for t in p['tags'][:3])}" if p.get("tags") else ""
            facts_str = "; ".join(p.get("facts_preview", []))[:100]
            dir_text += f"  - {p['name']}{alias_str}{tags_str}: {facts_str}\n"

    purpose = ""
    purpose_file = config.get_purpose_file()
    if purpose_file.exists():
        purpose = purpose_file.read_text(encoding="utf-8")[:2000]

    prompt = f"""你是知识库架构师。请审视以下知识库的目录结构，判断是否**确实**需要优化。

**重要原则：偏向保守，不变更优先。**
目录结构的稳定性非常重要，频繁变更会造成索引混乱。只有明确的结构性问题才需要调整。

{purpose}

{dir_text}

- 已摄入源文档总数：{total_ingested} 篇
- 知识页总数：{len(graph.pages)} 页

⚠️ **进度感知**：如果已摄入文章较少（<100篇），某些目录只有1-2页很正常——后续文章会持续补充。不要因为"页数少"就合并一个主题明确的目录。只有在文章量已经足够多（>100篇）时，某目录仍然只有1-2页，才说明该目录可能没有存在的必要。


1. **拆分**：某目录页数超过 40 页，且内容明显包含 2 个以上互不相关的子主题
2. **合并**：已摄入 >100 篇文章后，某目录仍然只有 1-2 页，且这些页面与另一个目录的主题高度重合
3. **迁移**：某个页面被放在了明显错误的目录（与目录描述完全不符）
4. **不变更**：如果目录结构基本合理、各目录都有 3 页以上且没有明显的归类错误，**请返回空列表**

{{
  "changes": [
    {{
      "action": "split",
      "from": "原目录名",
      "to": "新目录名",
      "description": "中文类型名 — 一句话描述该目录的内容范围",
      "move_pages": ["页面名1", "页面名2"],
      "reason": "拆分原因"
    }},
    {{
      "action": "merge",
      "from": "被合并目录名",
      "to": "目标目录名",
      "reason": "合并原因"
    }},
    {{
      "action": "move_page",
      "from": "原目录名",
      "to": "目标目录名",
      "description": "中文类型名 — 一句话描述该目录的内容范围",
      "move_pages": ["页面名1"],
      "reason": "迁移原因"
    }}
  ],
  "reasoning": "整体判断理由（如果不需要变更，说明为什么当前结构合理）"
}}

注意：
- **大多数情况下应该返回空列表**——目录结构通常不需要频繁调整
- 目录名必须是单个英文单词
- **description 格式必须是「中文类型名 — 一句话描述」**（例如：「驾驶辅助 — 涵盖车道保持、自适应巡航等驾驶辅助与主动安全系统」），不要只写目录名或简单的迁移说明
- split 操作只移动 move_pages 中列出的页面，不是全部移动
- merge 会删除 from 目录（页面全部移到 to 目录），慎用"""

    try:
        result = call_llm_json(
            "你是知识库架构师。请审视目录结构并输出优化建议。",
            prompt,
            model=config.LLM_LINT_MODEL,
            temperature=0.2,
        )
        changes = result.get("changes", [])
        reasoning = result.get("reasoning", "")
        if reasoning:
            print(f"  💡 LLM 审计理由: {reasoning[:200]}")
        return changes
    except Exception as e:
        print(f"  ⚠️ 目录结构审计失败: {e}")
        return []




def quick_lint() -> dict:
    """快速代码 lint（不含 LLM 调用），适合每次 ingest 后自动运行。

    只运行成本低的检查项，秒级完成。
    返回 report dict，仅包含发现的问题。
    """
    graph = WikiGraph()
    graph.load_all()

    issues = {}

    if not graph.pages:
        return issues

    broken = graph.get_broken_links()
    if broken:
        issues["broken_links"] = [{"from": f, "to": t} for f, t in broken[:10]]

    index_issues = []
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue
        try:
            idx_text = idx_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        idx_links = set(re.findall(r'\[\[([^\]]+)\]\]', idx_text))
        for link in idx_links:
            page_name = _extract_page_name(link)
            if page_name not in graph.pages and not link.startswith("sources/"):
                index_issues.append(f"{dir_name}/_index.md 引用了不存在的页面: [[{link}]]")
            elif page_name in graph.pages:
                page = graph.pages[page_name]
                if page.page_type != dir_name:
                    index_issues.append(
                        f"{dir_name}/_index.md 跨目录引用: [[{link}]] 实际在 {page.page_type}/ 目录下"
                    )
    if index_issues:
        issues["index_inconsistencies"] = index_issues[:10]

    type_path_issues = []
    valid_types = set(get_page_dirs().keys())
    for page in graph.pages.values():
        if page.page_type in ("source", "sources"):
            continue
        if not page.path:
            continue
        parent_dir = page.path.parent.name
        if parent_dir in ("wiki",):
            continue
        if "sources" in str(page.path.relative_to(config.WIKI_DIR)):
            continue
        if page.page_type != parent_dir:
            type_path_issues.append(
                f"{page.page_type}/{page.name}: type='{page.page_type}' 但文件在 {parent_dir}/ 目录下"
            )
    if type_path_issues:
        issues["type_path_mismatch"] = type_path_issues[:10]

    md_suffix_issues = []
    for page in graph.pages.values():
        sources_field = page.frontmatter.get("sources", [])
        if isinstance(sources_field, list):
            for s in sources_field:
                if isinstance(s, str) and s.endswith(".md"):
                    md_suffix_issues.append(f"{page.page_type}/{page.name}: sources 值 '{s}' 带 .md 后缀")
    if md_suffix_issues:
        issues["md_suffix"] = md_suffix_issues[:10]

    title_map = {}
    for name, page in graph.pages.items():
        title_map.setdefault(name.strip(), []).append(f"{page.page_type}/{name}")
    duplicates = [{
        "title": title,
        "locations": pages,
    } for title, pages in title_map.items() if len(pages) > 1]
    if duplicates:
        issues["duplicates"] = duplicates[:5]

    completeness = []
    required_fields_knowledge = ["type", "tags"]
    required_fields_source = ["type", "source_date", "tags"]
    for page in graph.pages.values():
        if "sources/articles" in str(page.path):
            continue
        fm = page.frontmatter
        if page.page_type in ("source", "sources"):
            missing = [f for f in required_fields_source if not fm.get(f)]
            if "source_date" in missing and fm.get("source_date") == "unknown":
                missing.remove("source_date")
        else:
            missing = [f for f in required_fields_knowledge if not fm.get(f)]
        if missing:
            completeness.append(f"{page.page_type}/{page.name}: 缺少 {', '.join(missing)}")
    if completeness:
        issues["completeness"] = completeness[:10]

    digest_issues = []
    broken_fm: list[str] = []  # frontmatter 损坏（缺开头/结尾 ---）的 digest
    required_sections = ["摘要", "核心观点", "关键信息", "提及实体"]
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if digests_dir.exists():
        for md in sorted(digests_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            parts = _robust_fm_split(text)
            if len(parts) < 3:
                broken_fm.append(f"sources/digests/{md.name}: frontmatter 缺开头或结尾 ---")
                continue
            body = parts[2]
            missing_sections = [s for s in required_sections if f"## {s}" not in body]
            stub_sections = []
            for s in required_sections:
                if f"## {s}" in body:
                    pattern = rf'## {re.escape(s)}\s*\n(.*?)(?=\n## |\Z)'
                    m = re.search(pattern, body, re.DOTALL)
                    if m:
                        content = m.group(1).strip()
                        if not content or re.match(r'^[-•\s]*（待补充）\s*$', content):
                            stub_sections.append(s)
            all_bad = missing_sections + stub_sections
            if all_bad:
                digest_issues.append(f"sources/digests/{md.name}: 缺少/待补充 [{', '.join(all_bad)}]")
    if digest_issues:
        issues["digest_incomplete"] = digest_issues
    if broken_fm:
        issues["broken_frontmatter"] = broken_fm

    missing_src_art: list[str] = []
    if digests_dir.exists():
        for md in sorted(digests_dir.glob("*.md")):
            if md.name == "_index.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not text.startswith("---"):
                continue
            parts = _robust_fm_split(text)
            if len(parts) < 3:
                continue
            has_src = False
            for line in parts[1].split("\n"):
                if line.strip().startswith("source_article:"):
                    v = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if v:
                        has_src = True
                        break
            if not has_src:
                missing_src_art.append(md.stem)
    if missing_src_art:
        issues["missing_source_article"] = missing_src_art

    pending_buildup: list[str] = []
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue
        try:
            idx_text = idx_path.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r'## 待整理\s*\n(.*?)(?=\n## |\Z)', idx_text, re.DOTALL)
        if not m:
            continue
        body = m.group(1)
        count = sum(1 for ln in body.split("\n") if ln.lstrip().startswith("- "))
        if count >= 3:
            pending_buildup.append(f"{dir_name}/_index.md: 待整理 {count} 条")
    if pending_buildup:
        issues["pending_entries"] = pending_buildup


    orphans = graph.get_orphan_pages()
    if orphans:
        issues["orphan_pages"] = orphans

    link_counts: dict[str, int] = {}
    for page in graph.pages.values():
        for link in page.outgoing_links:
            if graph.get_page(link) is None:
                key = _extract_page_name(link)
                link_counts[key] = link_counts.get(key, 0) + 1
    missing_content = [
        {"name": name, "referenced_by": count}
        for name, count in sorted(link_counts.items(), key=lambda x: -x[1])
        if count >= 2
    ]
    if missing_content:
        issues["missing_content"] = missing_content

    now = datetime.now()
    stale_threshold = timedelta(days=90)
    stale_pages = []
    for page in graph.pages.values():
        updated = page.frontmatter.get("updated", "")
        if updated:
            try:
                update_dt = datetime.strptime(str(updated), "%Y-%m-%d")
                if now - update_dt > stale_threshold:
                    stale_pages.append(f"{page.page_type}/{page.name}: {(now - update_dt).days}天未更新")
            except ValueError:
                pass
    if stale_pages:
        issues["stale_pages"] = stale_pages

    noise_patterns = [
        (r'(长按|扫码|二维码|点击.*关注|号|source corpus.*ID|关注我们?$|关注本号|关注source corpus|扫码关注|长按关注)', "关注引导"),
        (r'(版权归.*所有|转载.*请联系|本文来源[:：])', "版权声明"),
        (r'(限时优惠|团购价|团购链接|点击报名|免费领|立即购买|戳.*原文|抽奖活动)', "推广信息"),
        (r'^[-\s*#]*(?:编辑|排版|责编|美编)[:：]\s*\S{2,6}\s*$|图片来源[:：]', "署名行"),
    ]
    noise_issues = []
    digests_dir2 = config.WIKI_DIR / "sources" / "digests"
    if digests_dir2.exists():
        for md in digests_dir2.glob("*.md"):
            if md.name == "_index.md":
                continue
            try:
                text = md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits: list[tuple[str, str]] = []  # (type, matched_line)
            in_fm = False
            fm_cnt = 0
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped == '---':
                    fm_cnt += 1
                    if fm_cnt == 1:
                        in_fm = True
                    elif fm_cnt == 2:
                        in_fm = False
                    continue
                if in_fm:
                    continue
                if not stripped or len(stripped) >= 80:
                    continue
                for pattern, noise_type in noise_patterns:
                    if re.search(pattern, stripped):
                        hits.append((noise_type, stripped[:60]))
                        break
            if hits:
                types_hit = sorted({t for t, _ in hits})
                noise_issues.append({
                    "file": f"sources/digests/{md.name}",
                    "type": "、".join(types_hit),
                    "matches": [m for _, m in hits[:3]],
                })
    if noise_issues:
        issues["noise_issues"] = noise_issues

    entity_counter = Counter()
    for page in graph.pages.values():
        if page.page_type in ("source", "sources"):
            for link in page.outgoing_links:
                entity_counter[link] += 1
    if entity_counter:
        avg = sum(entity_counter.values()) / len(entity_counter)
        std = (sum((c - avg) ** 2 for c in entity_counter.values()) / len(entity_counter)) ** 0.5
        threshold = avg + 2 * std
        entity_anomalies = []
        for entity, count in entity_counter.most_common():
            if count > threshold and count > 5:
                entity_anomalies.append(f"[[{entity}]] 出现 {count} 次（均值 {round(avg, 1)}，阈值 {round(threshold, 1)}）")
        if entity_anomalies:
            issues["entity_anomalies"] = entity_anomalies

    hollow_pages = []
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if "sources/articles" in str(page.path):
            continue
        if not page.page_type:
            continue
        body = page.content
        if body.startswith("---"):
            parts = _robust_fm_split(body)
            body = parts[2] if len(parts) >= 3 else ""
        body_lines = [l for l in body.strip().split("\n")
                      if l.strip() and not l.strip().startswith("#")]
        if len(body_lines) < 3:
            hollow_pages.append(f"{page.page_type}/{page.name}: 仅 {len(body_lines)} 行有效内容")
    if hollow_pages:
        issues["hollow_pages"] = hollow_pages

    missing_summary = []
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
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                found_title = True
                continue
            if found_title:
                if stripped == "":
                    continue  # 空行跳过
                if stripped.startswith("> "):
                    has_blockquote = True
                break  # 标题后第一个非空行判断完毕
        if found_title and not has_blockquote:
            missing_summary.append(f"{page.page_type}/{page.name}")
    if missing_summary:
        issues["missing_summary"] = missing_summary

    invalid_files = []
    digests_dir3 = config.WIKI_DIR / "sources" / "digests"
    if digests_dir3.exists():
        for md in digests_dir3.glob("*.md"):
            if md.name == "_index.md":
                continue
            try:
                size = md.stat().st_size
            except OSError:
                continue
            if size < 100:
                invalid_files.append(f"sources/digests/{md.name}: 文件过小（{size} 字节）")
    if invalid_files:
        issues["invalid_files"] = invalid_files

    related_page_issues = []
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
        in_section = False
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                in_section = (stripped == "## 相关页面" or stripped == "## 相关链接" or stripped == "## 参见")
                continue
            if in_section:
                if not stripped:
                    continue
                if stripped.startswith("## "):
                    break
                if stripped.startswith("- "):
                    content = stripped[2:].strip()
                    if not re.search(r'\[\[', content):
                        related_page_issues.append(f"{page.page_type}/{page.name}: 相关页面项缺少链接 — `{stripped}`")
                    else:
                        for m in re.finditer(r'\[\[([^\]]+)\]\]', content):
                            link = m.group(1)
                            if '/' in link:
                                continue  # 已有路径前缀
                            if link.startswith('_'):
                                continue
                            target = graph.get_page(link)
                            if target and target.page_type and target.page_type not in ("source", "sources"):
                                related_page_issues.append(
                                    f"{page.page_type}/{page.name}: 链接缺少目录路径 — `[[{link}]]` → 应为 `[[{target.page_type}/{link}]]`")
                elif stripped.startswith("—") or stripped.startswith("–"):
                    related_page_issues.append(f"{page.page_type}/{page.name}: 相关页面项缺少链接 — `{stripped}`")
    if related_page_issues:
        issues["related_page_format"] = related_page_issues

    related_source_issues = []
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
        in_section = False
        for line in body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("## "):
                in_section = (stripped == "## 相关来源")
                continue
            if in_section:
                if not stripped:
                    continue
                if stripped.startswith("## "):
                    break
                all_links = re.findall(r'\[\[([^\]]+)\]\]', stripped)
                non_digest = [l for l in all_links if not l.startswith("sources/digests/")]
                if non_digest:
                    related_source_issues.append(
                        f"{page.page_type}/{page.name}: 相关来源含非digest链接 — `{stripped[:80]}`")
                elif not all_links:
                    related_source_issues.append(
                        f"{page.page_type}/{page.name}: 相关来源缺少链接 — `{stripped[:80]}`")
    if related_source_issues:
        issues["related_source_format"] = related_source_issues

    _KNOWLEDGE_REQUIRED_SECTIONS = ["核心事实", "相关页面", "相关来源"]
    missing_sections = []
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
            missing_sections.append(
                f"{page.page_type}/{page.name}: 缺少 [{', '.join(missing)}]")
    if missing_sections:
        issues["missing_sections"] = missing_sections

    type_path_issues = []
    valid_types = set(get_page_types().keys())
    for page in graph.pages.values():
        if page.page_type in ("source", "sources"):
            continue  # sources 是特殊目录，不检查
        if page.path and "sources" in str(page.path.relative_to(config.WIKI_DIR)):
            continue
        if page.page_type not in valid_types:
            type_path_issues.append(
                f"{page.page_type}/{page.name}: type '{page.page_type}' 不在 page_types.yaml 注册表中")
        if page.path:
            parent_dir = page.path.parent.name
            if parent_dir != page.page_type and parent_dir not in ("wiki",):
                type_path_issues.append(
                    f"{page.page_type}/{page.name}: type='{page.page_type}' 但文件在 {parent_dir}/ 目录下")
    if type_path_issues:
        issues["type_path_mismatch"] = type_path_issues[:10]

    _ZERO_WIDTH_RE = re.compile(r'[\u200b\u200c\u200d\ufeff\u00ad]')
    zero_width_issues = []
    wiki_dir = config.WIKI_DIR
    if wiki_dir.exists():
        for md in sorted(wiki_dir.rglob("*.md")):
            if _ZERO_WIDTH_RE.search(md.name):
                zero_width_issues.append(f"{md.relative_to(wiki_dir)}: 文件名含零宽字符")
            try:
                head = md.read_text(encoding="utf-8")[:500]
                if _ZERO_WIDTH_RE.search(head):
                    zero_width_issues.append(f"{md.relative_to(wiki_dir)}: 内容含零宽字符")
            except OSError:
                pass
    if zero_width_issues:
        issues["zero_width_chars"] = zero_width_issues

    return issues


def auto_fix_broken_links(graph: 'WikiGraph' = None) -> int:
    """自动修复断裂链接：修正全角/半角差异的链接，删除指向不存在页面的 [[wikilink]]。

    返回修复数量。
    """
    import unicodedata

    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    broken = graph.get_broken_links()
    if not broken:
        return 0

    from collections import defaultdict

    _index_by_path: dict[str, 'WikiPage'] = {}
    for idx_page in graph.index_pages:
        try:
            rel = str(idx_page.path.relative_to(config.WIKI_DIR))[:-3]  # 去 .md
            _index_by_path[rel] = idx_page
        except ValueError:
            pass

    by_page: dict[str, tuple['WikiPage', list]] = {}  # key → (page, [dead_links])
    for src_name, tgt_link in broken:
        page = graph.pages.get(src_name)
        if page and "sources/articles" in str(page.path):
            continue
        if page is None and src_name == "_index":
            for rel_path, idx_page in _index_by_path.items():
                if tgt_link in idx_page.outgoing_links:
                    key = rel_path
                    if key not in by_page:
                        by_page[key] = (idx_page, [])
                    by_page[key][1].append(tgt_link)
            continue
        key = src_name
        if key not in by_page:
            by_page[key] = (page, [])
        by_page[key][1].append(tgt_link)

    fixed = 0
    for src_key, (page, dead_links) in by_page.items():
        if not page or not page.path.exists():
            continue

        text = page.path.read_text(encoding="utf-8")
        original = text
        truly_dead = []  # 归一化后仍不存在的链接，才真正删除

        for dead_link in dead_links:
            norm_link = unicodedata.normalize('NFKC', dead_link)
            norm_link = re.sub(r' +', ' ', norm_link)  # 多空格→单空格
            if norm_link != dead_link:
                target = graph.get_page(norm_link)
                if target is not None:
                    pattern_fix = re.compile(r'\[\[' + re.escape(dead_link) + r'\]\]')
                    text, n = pattern_fix.subn(f'[[{norm_link}]]', text)
                    if n > 0:
                        fixed += n
                        print(f"  🔧 修正全角链接: {src_key} [[{dead_link}]] → [[{norm_link}]]")
                        continue
                    pattern_fix_line = re.compile(
                        r'^([ \t]*-[ \t]+)\[\[' + re.escape(dead_link) + r'\]\]',
                        re.MULTILINE
                    )
                    text, n = pattern_fix_line.subn(r'\1[[' + norm_link + ']]', text)
                    if n > 0:
                        fixed += n
                        print(f"  🔧 修正全角链接: {src_key} [[{dead_link}]] → [[{norm_link}]]")
                        continue

            if ' | ' in dead_link:
                pipe_target = dead_link.split(' | ', 1)[0].strip()
                target = graph.get_page(pipe_target)
                if target is not None:
                    try:
                        rel = str(target.path.relative_to(config.WIKI_DIR))[:-3]  # 去 .md
                        correct_link = rel
                    except ValueError:
                        correct_link = pipe_target
                    pattern_fix = re.compile(r'\[\[' + re.escape(dead_link) + r'\]\]')
                    text, n = pattern_fix.subn(f'[[{correct_link}]]', text)
                    if n > 0:
                        fixed += n
                        print(f"  🔧 修正别名链接: {src_key} [[{dead_link}]] → [[{correct_link}]]")
                        continue

            truly_dead.append(dead_link)

        for dead_link in truly_dead:
            pattern_line = re.compile(
                r'^[ \t]*-[ \t]+\[\[' + re.escape(dead_link) + r'\]\][ \t]*\n?',
                re.MULTILINE
            )
            text, n = pattern_line.subn('', text)
            if n > 0:
                fixed += n
                continue
            pattern_inline = re.compile(r'\[\[' + re.escape(dead_link) + r'\]\]')
            text, n = pattern_inline.subn('', text)
            if n > 0:
                fixed += n

        if text != original:
            LINK_ONLY_SECTIONS = {"## 提及实体", "## 相关页面", "## 相关链接", "## 参见", "## 反向链接"}

            def _clean_empty_sections(t):
                sections = re.split(r'^(## .+)$', t, flags=re.MULTILINE)
                result = sections[0]  # 标题前的内容
                i = 1
                while i < len(sections):
                    heading = sections[i]
                    body = sections[i + 1] if i + 1 < len(sections) else ""
                    heading_stripped = heading.strip()

                    if heading_stripped in LINK_ONLY_SECTIONS:
                        if body.strip() and re.search(r'\[\[', body):
                            result += heading + body  # 还有有效链接，保留
                        elif not body.strip():
                            result += "\n"  # 内容为空，删掉章节
                        else:
                            result += "\n"
                    else:
                        result += heading + body
                    i += 2
                return result

            text = _clean_empty_sections(text)
            text = re.sub(r'\n{3,}', '\n\n', text)
            page.path.write_text(text, encoding="utf-8")
            print(f"  🔧 修复断裂链接: {src_key} (删除 {len(dead_links)} 个无效链接)")

    if fixed > 0:
        try:
            from error_book import mark_samples_fixed
            fixed_descs = []
            for src_key, (_, dead_links) in by_page.items():
                for dead_link in dead_links:
                    fixed_descs.append(f"{src_key} → [[{dead_link}]]")
                    fixed_descs.append(dead_link)
            if fixed_descs:
                mark_samples_fixed("broken_link", fixed_descs)
        except Exception:
            pass  # 错题本标记失败不影响主流程

    return fixed


def auto_fix_index_inconsistencies(graph: 'WikiGraph' = None) -> int:
    """自动修复 _index.md 中的不一致：删除引用不存在页面的链接，修正跨目录引用。

    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    fixed = 0
    fixed_descriptions = []  # 收集修复描述，用于同步标记 error_book
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue

        try:
            text = idx_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        original = text

        links = re.findall(r'\[\[([^\]]+)\]\]', text)
        for link in links:
            page_name = _extract_page_name(link)
            if page_name not in graph.pages and not link.startswith("sources/"):
                pattern = re.compile(
                    r'^[ \t]*-[ \t]+\[\[' + re.escape(link) + r'\]\][^\n]*\n?',
                    re.MULTILINE
                )
                text, n = pattern.subn('', text)
                if n > 0:
                    fixed += n
                    print(f"  🔧 索引修复: {dir_name}/_index.md 删除不存在页面 [[{link}]]")
                    fixed_descriptions.append(f"{dir_name}/_index.md")
            elif page_name in graph.pages:
                page = graph.pages[page_name]
                if page.page_type != dir_name:
                    pattern = re.compile(
                        r'^[ \t]*-[ \t]+\[\[' + re.escape(link) + r'\]\][^\n]*\n?',
                        re.MULTILINE
                    )
                    text, n = pattern.subn('', text)
                    if n > 0:
                        fixed += n
                        desc = f"{dir_name}/_index.md 跨目录引用: [[{link}]] 实际在 {page.page_type}/ 目录下"
                        print(f"  🔧 索引修复: {dir_name}/_index.md 删除跨目录引用 [[{link}]] (实际在 {page.page_type}/)")
                        fixed_descriptions.append(desc)

        if text != original:
            text = re.sub(r'\n{3,}', '\n\n', text)
            idx_path.write_text(text, encoding="utf-8")

    if fixed_descriptions:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("index_error", fixed_descriptions)
        except Exception:
            pass

    return fixed


def _rebuild_knowledge_index(graph: 'WikiGraph' = None) -> int:
    """重建知识页目录的 _index.md，将缺失页面补入末尾「## 待整理」分区。

    对比目录下实际存在的页面与 _index.md 中已收录的页面，
    把缺失页面追加到 _index.md 的「## 待整理」分区（没有则新建）。
    条目格式与现有格式一致：- [[页面名]] (别名) — 一句话概括 #标签

    返回补充的条目数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    added = 0
    page_types_info = get_page_types()
    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue

        try:
            text = idx_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        type_info = page_types_info.get(dir_name, {})
        description = type_info.get("description", dir_name)
        cn_name = description.split("—")[0].strip() if "—" in description else dir_name
        cn_desc = description.strip()
        std_title = f"# {cn_name}"
        std_summary = f"> {cn_desc}"
        header_changed = False
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                text = parts[2].lstrip("\n")
                header_changed = True
        old_text = text
        text = re.sub(r'^# .+', std_title, text, count=1)
        if text != old_text:
            header_changed = True
        old_text = text
        text = re.sub(r'^> .+', std_summary, text, count=1, flags=re.MULTILINE)
        if text != old_text:
            header_changed = True

        idx_links = set(re.findall(r'\[\[([^\]]+)\]\]', text))

        ghost_removed = 0
        existing_files = {f.stem for f in dir_path.glob("*.md") if f.name != "_index.md"}
        dir_page_names = set()
        for name, page in graph.pages.items():
            if page.page_type == dir_name:
                dir_page_names.add(name)
        ghost_names = idx_links - existing_files - dir_page_names
        ghost_names_full = set()
        for g in ghost_names:
            ghost_names_full.add(g)
        for g in list(ghost_names):
            if "/" in g:
                short = g.split("/", 1)[1]
                if short in existing_files or short in dir_page_names:
                    ghost_names.discard(g)
        if ghost_names:
            lines = text.split("\n")
            new_lines = []
            for line in lines:
                m = re.match(r'^\s*-\s*\[\[([^\]]+)\]\]', line)
                if m:
                    link_name = m.group(1).split('|')[0].strip()
                    short_name = link_name.split('/')[-1] if '/' in link_name else link_name
                    if link_name in ghost_names or short_name in ghost_names:
                        ghost_removed += 1
                        continue
                new_lines.append(line)
            if ghost_removed > 0:
                text = "\n".join(new_lines)
                text = re.sub(r'\n{3,}', '\n\n', text)

        idx_links = set(re.findall(r'\[\[([^\]]+)\]\]', text))

        missing_pages = []
        for name, page in graph.pages.items():
            if page.page_type != dir_name:
                continue
            if name in idx_links or f"{dir_name}/{name}" in idx_links:
                continue
            missing_pages.append(page)

        if ghost_removed > 0 or header_changed:
            idx_path.write_text(text, encoding="utf-8")
            if ghost_removed > 0:
                print(f"  🧹 索引清理: {dir_name}/_index.md 移除 {ghost_removed} 个幽灵条目")
            if header_changed:
                print(f"  📝 索引标题统一: {dir_name}/_index.md")

        if not missing_pages:
            continue

        new_entries = []
        for page in missing_pages:
            entry = _build_index_entry(page)
            new_entries.append(entry)

        section_header = "## 待整理"
        if section_header in text:
            parts = text.split(section_header, 1)
            after_header = parts[1]
            next_section_pos = after_header.find("\n## ")
            if next_section_pos >= 0:
                before = after_header[:next_section_pos].rstrip()
                after = after_header[next_section_pos:]
                insert_block = "\n" + "\n".join(new_entries) + "\n"
                text = parts[0] + section_header + before + insert_block + after
            else:
                text = text.rstrip() + "\n" + "\n".join(new_entries) + "\n"
        else:
            text = text.rstrip() + f"\n\n{section_header}\n" + "\n".join(new_entries) + "\n"

        idx_path.write_text(text, encoding="utf-8")
        added += len(new_entries)
        print(f"  🔧 索引补全: {dir_name}/_index.md 补入 {len(new_entries)} 个缺失页面")

    return added


def _build_index_entry(page: 'WikiPage') -> str:
    """根据页面的 frontmatter 和内容，生成 _index.md 条目。

    格式：- [[页面名]] (别名1, 别名2) — 一句话概括 #标签1 #标签2
    """
    name = page.name
    fm = page.frontmatter

    aliases = fm.get("aliases", [])
    if isinstance(aliases, list) and aliases:
        alias_str = ", ".join(str(a) for a in aliases[:3])
        alias_part = f" ({alias_str})"
    else:
        alias_part = ""

    summary = ""
    content = page.content
    lines = content.split("\n")
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

    summary_part = f" — {summary}" if summary else ""

    tags = fm.get("tags", [])
    if isinstance(tags, list) and tags:
        tag_str = " ".join(f"#{t}" for t in tags[:4])
        tag_part = f" {tag_str}"
    else:
        tag_part = ""

    return f"- [[{name}]]{alias_part}{summary_part}{tag_part}"




def _parse_index_sections(text: str) -> list[tuple[str, list[str]]]:
    """把 _index.md 解析为 [(section_header_line, [item_line, ...]), ...]。
    非 `## ` 章节（比如文件顶部的 `# xxx 目录索引` + `> 描述`）存到 key="__preamble__"。
    """
    sections: list[tuple[str, list[str]]] = []
    current_header = "__preamble__"
    current_lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if stripped.startswith("## "):
            sections.append((current_header, current_lines))
            current_header = stripped
            current_lines = []
        else:
            current_lines.append(line)
    sections.append((current_header, current_lines))
    return sections


def _assemble_sections(sections: list[tuple[str, list[str]]]) -> str:
    parts: list[str] = []
    for header, lines in sections:
        if header == "__preamble__":
            parts.append("\n".join(lines))
        else:
            body = "\n".join(lines).rstrip()
            if body:
                parts.append(f"{header}\n{body}")
            else:
                parts.append(header)
    return "\n".join(parts).rstrip() + "\n"


def _extract_entry_name(entry_line: str) -> str:
    """从 `- [[页面名]] ...` 中提取页面名。"""
    m = re.match(r'\s*-\s*\[\[([^\]|#]+)', entry_line)
    if not m:
        return ""
    name = m.group(1).strip()
    return name.rsplit("/", 1)[-1]


def relocate_pending_entries(dry_run: bool = False, batch_size: int = 40) -> dict:
    """把各目录 `_index.md` 的「## 待整理」条目归入已有分区（或建议新分区）。

    算法：
    1. 对每个知识页目录（非 sources/syntheses），提取现有所有 `## ...` 分区名与
       「## 待整理」下的全部条目。
    2. 让 LLM 针对每条条目决定：归入某现有分区、建议新建某分区、或保留在待整理。
    3. 按 LLM 决策重写 `_index.md`：条目移到对应分区末尾；新建分区插在「## 待整理」之前；
       无法决定的仍留在「## 待整理」。
    """
    from wiki_page import WikiGraph
    from config import get_page_dirs
    from llm_client import call_llm_json

    graph = WikiGraph()
    graph.load_all()

    summary = {"dirs_processed": 0, "moved": 0, "new_sections": 0, "left_pending": 0, "details": []}

    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue

        text = idx_path.read_text(encoding="utf-8")
        sections = _parse_index_sections(text)

        pending_idx = None
        existing_sections: list[str] = []  # 不含 "待整理" 和 preamble
        for i, (header, _) in enumerate(sections):
            if header == "__preamble__":
                continue
            name = header[3:].strip()
            if name == "待整理":
                pending_idx = i
            else:
                existing_sections.append(name)

        if pending_idx is None:
            continue

        pending_lines = sections[pending_idx][1]
        entries: list[str] = [l for l in pending_lines if l.lstrip().startswith("- [[")]
        other_lines: list[str] = [l for l in pending_lines if not l.lstrip().startswith("- [[")]

        if not entries:
            continue

        summary["dirs_processed"] += 1
        print(f"\n📂 {dir_name}/: 「## 待整理」{len(entries)} 条，现有分区 {len(existing_sections)} 个")

        dir_cn_name = dir_name
        for header, lines in sections:
            if header == "__preamble__":
                for l in lines:
                    if l.startswith("# "):
                        dir_cn_name = l[2:].strip()
                        break
                break

        existing_entry_count = sum(
            sum(1 for l in lines if l.lstrip().startswith("- [["))
            for header, lines in sections
            if header not in ("__preamble__",) and header[3:].strip() != "待整理"
        )
        total_entries = existing_entry_count + len(entries)

        if total_entries <= 10:
            suggested_range = "2-3"
        elif total_entries <= 25:
            suggested_range = "3-5"
        elif total_entries <= 50:
            suggested_range = "4-6"
        elif total_entries <= 100:
            suggested_range = "5-8"
        elif total_entries <= 200:
            suggested_range = "6-10"
        else:
            suggested_range = "8-12"

        suggested_min = int(suggested_range.split("-")[0])
        suggested_max = int(suggested_range.split("-")[-1])

        if total_entries <= 3:
            if existing_sections:
                target_sec = existing_sections[0]
                print(f"  ⏭️  条目过少（共 {total_entries} 条），直接归入「{target_sec}」，跳过 LLM")
                for entry in entries:
                    name = _extract_entry_name(entry)
                    if name:
                        summary["entries_moved"] = summary.get("entries_moved", 0) + 1
                new_sections_list = []
                for header, sec_lines in sections:
                    if header == "__preamble__":
                        continue
                    sec_name = header[3:].strip()
                    if sec_name == "待整理":
                        continue
                    if sec_name == target_sec:
                        new_sections_list.append((header, sec_lines + entries))
                    else:
                        new_sections_list.append((header, sec_lines))
                preamble_lines = next((lines for h, lines in sections if h == "__preamble__"), [])
                new_content = "\n".join(preamble_lines)
                if new_content and not new_content.endswith("\n"):
                    new_content += "\n"
                for header, sec_lines in new_sections_list:
                    new_content += f"\n{header}\n" + "\n".join(sec_lines) + "\n"
                idx_path.write_text(new_content, encoding="utf-8")
                summary["moved"] += len(entries)
            else:
                print(f"  ⏭️  条目过少（共 {total_entries} 条）且无已有分区，保留待整理，跳过 LLM")
                summary["left_pending"] += len(entries)
            continue

        decisions: dict[str, dict] = {}  # name → {"section": str, "is_new": bool}
        for start in range(0, len(entries), batch_size):
            batch = entries[start:start + batch_size]
            prompt_entries = "\n".join(batch)

            if existing_sections:
                cur_count = len(existing_sections)

                if cur_count >= suggested_max:
                    section_stance = "at_limit"
                elif cur_count < suggested_min:
                    section_stance = "under"
                else:
                    section_stance = "ok"

                if section_stance == "at_limit":
                    new_section_rule = (
                        f"当前已有 {cur_count} 个分区，已达到建议上限（{suggested_range} 个）。"
                        "**禁止新建任何分区**，所有条目必须归入已有分区之一，"
                        "请尽量宽松匹配——只要条目内容与某个已有分区的主题沾边或相关，就归入该分区。"
                    )
                    new_section_sys = (
                        f"当前分区数已达建议上限（{cur_count}/{suggested_max}），禁止新建分区，"
                        "所有条目必须归入已有分区。"
                    )
                elif section_stance == "under":
                    new_section_rule = (
                        f"当前只有 {cur_count} 个分区，建议范围是 {suggested_range} 个，分区可能偏少。"
                        "请根据条目内容本身来判断：**如果条目之间主题确实有明显差异，建议积极新建分区**，"
                        f"让分区总数向 {suggested_range} 个靠拢；"
                        "但如果条目内容高度集中、差异不大，归入已有分区也完全合理，不必强行凑数量。"
                        "总之，以内容是否真的需要细分为准，不要为了达到数量而建出没有实际意义的分区。"
                    )
                    new_section_sys = (
                        f"当前分区数（{cur_count}）低于建议下限（{suggested_min}），"
                        f"如果条目主题差异明显，建议适当新建分区，目标 {suggested_range} 个左右；"
                        "但以内容实际需求为准，不强制凑数。"
                    )
                else:
                    remaining = suggested_max - cur_count
                    new_section_rule = (
                        f"当前已有 {cur_count} 个分区，在建议范围（{suggested_range} 个）之内，"
                        f"还可新建最多 {remaining} 个分区。"
                        "**优先归入已有分区**——只要条目内容与某个已有分区的主题沾边或相关，就归入该分区。"
                        "只有当条目与所有已有分区都明显不同、且新建后分区总数不超过上限时，才新建分区。"
                    )
                    new_section_sys = (
                        f"当前分区数（{cur_count}）在建议范围（{suggested_range}）内，"
                        f"还可新建最多 {remaining} 个分区，优先归入已有分区。"
                    )

                sys_prompt = (
                    "你是知识库编辑。为以下『待整理』条目决定归入哪个分区。"
                    f"{new_section_sys} "
                    "分区名应该宽泛概括（2-6字中文短名），能容纳多个相关条目。"
                    "**分区不宜过多过细**：相近、相似、有交叉的主题应合并为同一个分区。"
                    "不要将任何条目保留在『待整理』，每条都必须归入某个分区。"
                )
                sections_text = "\n".join(f"- {s}" for s in existing_sections)
                user_prompt = f"""目录: {dir_name}/ — 存放 {dir_name} 主题的知识页

{sections_text}

{prompt_entries}

{{
  "decisions": [
    {{"name": "条目的页面名（从 [[]] 中提取）", "section": "分区名", "is_new": false}},
    // section 为已有分区名 → is_new=false
    // section 为你建议新建的分区名 → is_new=true
    // 每条都必须归入某个分区，不要输出 "待整理"
  ]
}}

规则：
- 只输出 JSON，不要多余解释
- section 用中文短名（2-6 字），风格与现有分区一致
- {new_section_rule}
- **禁止**使用目录英文名「{dir_name}」或目录中文名「{dir_cn_name}」作为分区名——分区名必须是该目录下更细粒度的子主题
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名，每个条目都应归入有明确语义的分区
- 判断依据：条目的 tag 与 summary"""
            else:
                n_entries = len(entries)
                sys_prompt = (
                    "你是知识库编辑。为以下条目建立分区体系。"
                    "你需要根据条目的内容主题，将它们分成多个有意义的子分区。"
                    "分区名应该是宽泛的子主题概括（2-6字中文短名），有足够的包容性。"
                    "**分区不宜过多过细**：相近、相似、有交叉的主题应合并为同一个分区。"
                    f"**分区总数严格限制在 {suggested_min}–{suggested_max} 个之间，绝对不能超过 {suggested_max} 个。**"
                    "不要将任何条目保留在『待整理』，每条都必须归入某个分区。"
                )
                user_prompt = f"""目录: {dir_name}/ — 存放 {dir_name} 主题的知识页

{prompt_entries}

{{
  "decisions": [
    {{"name": "条目的页面名（从 [[]] 中提取）", "section": "分区名", "is_new": true}}
  ]
}}

规则：
- 只输出 JSON，不要多余解释
- section 用中文短名（2-6 字），风格统一
- 分区名要**宽泛且有概括性**，能容纳多个相关条目（如"人生感悟"而非"中年困境"，"影视"而非"纪录片审美"，"实战技巧"而非"某某打法"）
- **必须建立至少 2 个不同的分区**，不要把所有条目都放进同一个分区
- **分区总数严格限制在 {suggested_min}–{suggested_max} 个之间，绝对不能超过 {suggested_max} 个**
- **分区不宜过多过细**：相近或有交叉的主题必须合并为同一个宽泛分区
- **禁止**使用目录英文名「{dir_name}」或目录中文名「{dir_cn_name}」作为分区名——分区名必须是该目录下更细粒度的子主题
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名，每个条目都应归入有明确语义的分区
- 判断依据：条目的 tag 与 summary"""

            try:
                result = call_llm_json(sys_prompt, user_prompt, model=config.LLM_ECONOMY_MODEL)
            except Exception as e:
                print(f"  ⚠️ LLM 归位失败（跳过该批）: {e}")
                continue

            for dec in result.get("decisions", []) or []:
                name = (dec.get("name") or "").strip()
                section = (dec.get("section") or "").strip()
                is_new = bool(dec.get("is_new"))
                if not name or not section:
                    continue
                decisions[name] = {"section": section, "is_new": is_new}

        move_to: dict[str, list[str]] = {}
        new_sections_requested: dict[str, list[str]] = {}
        left_behind: list[str] = []

        for entry in entries:
            name = _extract_entry_name(entry)
            dec = decisions.get(name)
            if not dec:
                left_behind.append(entry)
                continue
            if dec["section"] == "待整理":
                left_behind.append(entry)
                continue
            if dec["section"] in ("其他", "未分类", "杂项", dir_name, dir_cn_name):
                left_behind.append(entry)
                continue
            target = dec["section"]
            if dec["is_new"] and target not in existing_sections:
                new_sections_requested.setdefault(target, []).append(entry)
            else:
                if target in existing_sections:
                    move_to.setdefault(target, []).append(entry)
                else:
                    new_sections_requested.setdefault(target, []).append(entry)

        final_new_sections: dict[str, list[str]] = {}
        for sec, items in new_sections_requested.items():
            final_new_sections[sec] = items

        moved_count = sum(len(v) for v in move_to.values()) + sum(len(v) for v in final_new_sections.values())
        summary["moved"] += moved_count
        summary["new_sections"] += len(final_new_sections)
        summary["left_pending"] += len(left_behind)
        detail = {
            "dir": dir_name,
            "moved_to_existing": {k: len(v) for k, v in move_to.items()},
            "new_sections": {k: len(v) for k, v in final_new_sections.items()},
            "left_pending": len(left_behind),
        }
        summary["details"].append(detail)
        print(
            f"  → 归入已有分区 {sum(len(v) for v in move_to.values())} 条，"
            f"新建 {len(final_new_sections)} 个分区（共 {sum(len(v) for v in final_new_sections.values())} 条）"
        )
        if move_to:
            distrib = ", ".join(f"{k}(+{len(v)})" for k, v in sorted(move_to.items(), key=lambda x: -len(x[1])))
            print(f"     归入已有: {distrib}")
        if final_new_sections:
            new_s = ", ".join(f"{k}({len(v)})" for k, v in sorted(final_new_sections.items(), key=lambda x: -len(x[1])))
            print(f"     新建分区: {new_s}")

        if dry_run or moved_count == 0:
            continue

        for i, (header, lines) in enumerate(sections):
            if not header.startswith("## "):
                continue
            sec_name = header[3:].strip()
            if sec_name in move_to:
                new_lines = list(lines)
                while new_lines and not new_lines[-1].strip():
                    new_lines.pop()
                new_lines.extend(move_to[sec_name])
                new_lines.append("")  # 保留一个空行分隔
                sections[i] = (header, new_lines)

        insert_before = pending_idx  # 插入位置
        for sec, items in final_new_sections.items():
            block = items + [""]
            sections.insert(insert_before, (f"## {sec}", block))
            insert_before += 1
            pending_idx += 1  # 插入后 pending_idx 后移

        if left_behind:
            new_pending_body = other_lines + left_behind
            sections[pending_idx] = ("## 待整理", new_pending_body)
        else:
            sections.pop(pending_idx)

        new_text = _assemble_sections(sections)
        idx_path.write_text(new_text, encoding="utf-8")
        print(f"  ✅ 已更新 {idx_path.relative_to(config.WIKI_DIR)}")

    return summary


def merge_small_sections(dry_run: bool = False, min_items: int = 2, max_sections: int = 15) -> dict:
    """合并 _index.md 中过小的分区，降低分类碎片化。

    算法：
    1. 对每个知识页目录，统计各分区的条目数
    2. 找出条目数 < min_items 的"小分区"
    3. 如果总分区数 > max_sections 或小分区占比 > 50%，用 LLM 批量决定合并方案
    4. 将小分区条目合并到 LLM 建议的目标分区

    返回合并统计。
    """
    from config import get_page_dirs
    from llm_client import call_llm_json

    summary = {"dirs_processed": 0, "sections_merged": 0, "entries_moved": 0}

    for dir_name, dir_path in get_page_dirs().items():
        if dir_name.startswith("sources") or dir_name == "syntheses":
            continue
        idx_path = dir_path / "_index.md"
        if not idx_path.exists():
            continue

        text = idx_path.read_text(encoding="utf-8")
        sections = _parse_index_sections(text)

        section_stats: list[tuple[int, str, int]] = []  # (idx, name, count)
        for i, (header, lines) in enumerate(sections):
            if header == "__preamble__" or header.strip() == "## 待整理":
                continue
            count = sum(1 for l in lines if l.lstrip().startswith("- [["))
            sec_name = header[3:].strip()
            section_stats.append((i, sec_name, count))

        if not section_stats:
            continue

        total_sections = len(section_stats)
        small_sections = [(i, name, cnt) for i, name, cnt in section_stats if cnt < min_items]

        if total_sections <= max_sections and len(small_sections) <= total_sections * 0.3:
            continue  # 分区数量合理且小分区不多，跳过

        min_sections_keep = 2
        big_sections_count = total_sections - len(small_sections)
        if big_sections_count < min_sections_keep:
            need_keep = min_sections_keep - big_sections_count
            small_sections_sorted = sorted(small_sections, key=lambda x: -x[2])
            small_sections = small_sections_sorted[need_keep:]  # 只合并剩余的
            if not small_sections:
                continue  # 没有需要合并的了

        summary["dirs_processed"] += 1
        print(f"\n🔀 {dir_name}/: 共 {total_sections} 个分区，其中 {len(small_sections)} 个少于 {min_items} 条")

        big_sections = [name for _, name, cnt in section_stats if cnt >= min_items]
        small_names = [name for _, name, _ in small_sections]

        if not big_sections or not small_names:
            continue

        sys_prompt = (
            "你是知识库编辑。以下分区条目太少，需要合并到更大的分区中。"
            "为每个小分区选择一个最合适的目标大分区进行合并。"
        )
        user_prompt = f"""目录: {dir_name}/

{chr(10).join(f'- {s}' for s in big_sections)}

{chr(10).join(f'- {s}' for s in small_names)}

{{
  "merges": [
    {{"from": "小分区名", "to": "大分区名"}}
  ]
}}

规则：
- 只输出 JSON
- 每个小分区必须合并到某个大分区
- 选择主题最接近的大分区"""

        try:
            result = call_llm_json(sys_prompt, user_prompt, model=config.LLM_ECONOMY_MODEL)
        except Exception as e:
            print(f"  ⚠️ LLM 合并决策失败: {e}")
            continue

        merge_map: dict[str, str] = {}  # from → to
        for m in result.get("merges", []) or []:
            frm = (m.get("from") or "").strip()
            to = (m.get("to") or "").strip()
            if frm and to and to in big_sections:
                merge_map[frm] = to

        if not merge_map:
            continue

        if dry_run:
            for frm, to in merge_map.items():
                print(f"  📋 [dry run] {frm} → {to}")
            continue

        merged_count = 0
        entries_moved = 0
        sections_to_remove: set[int] = set()

        for i, (header, lines) in enumerate(sections):
            if not header.startswith("## "):
                continue
            sec_name = header[3:].strip()
            if sec_name not in merge_map:
                continue

            target_name = merge_map[sec_name]
            target_idx = None
            for j, (th, _) in enumerate(sections):
                if th.startswith("## ") and th[3:].strip() == target_name:
                    target_idx = j
                    break

            if target_idx is None:
                continue

            entry_lines = [l for l in lines if l.lstrip().startswith("- [[")]
            if not entry_lines:
                sections_to_remove.add(i)
                merged_count += 1
                continue

            target_lines = list(sections[target_idx][1])
            while target_lines and not target_lines[-1].strip():
                target_lines.pop()
            target_lines.extend(entry_lines)
            target_lines.append("")
            sections[target_idx] = (sections[target_idx][0], target_lines)

            sections_to_remove.add(i)
            merged_count += 1
            entries_moved += len(entry_lines)
            print(f"  🔀 {sec_name}({len(entry_lines)}条) → {target_name}")

        for idx in sorted(sections_to_remove, reverse=True):
            sections.pop(idx)

        if merged_count > 0:
            new_text = _assemble_sections(sections)
            idx_path.write_text(new_text, encoding="utf-8")
            summary["sections_merged"] += merged_count
            summary["entries_moved"] += entries_moved
            print(f"  ✅ 合并 {merged_count} 个分区，移动 {entries_moved} 条")

    return summary


def auto_fix_md_suffix(graph: 'WikiGraph' = None) -> int:
    """自动修复 sources 字段中带 .md 后缀的值。

    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    fixed = 0
    for page in graph.pages.values():
        sources_field = page.frontmatter.get("sources", [])
        if not isinstance(sources_field, list) or not sources_field:
            continue

        needs_fix = False
        new_sources = []
        for s in sources_field:
            if isinstance(s, str) and s.endswith(".md"):
                new_sources.append(s[:-3])
                needs_fix = True
                fixed += 1
            else:
                new_sources.append(s)

        if needs_fix and page.path.exists():
            text = page.path.read_text(encoding="utf-8")
            import yaml
            fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
            if fm_match:
                try:
                    fm = yaml.safe_load(fm_match.group(1))
                except yaml.YAMLError:
                    continue
                if isinstance(fm, dict):
                    fm["sources"] = new_sources
                    new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
                    new_text = f"---\n{new_fm}\n---{text[fm_match.end():]}"
                    page.path.write_text(new_text, encoding="utf-8")
                    print(f"  🔧 修复 .md 后缀: {page.page_type}/{page.name}")

    return fixed


def auto_fix_frontmatter_format() -> int:
    """自动修复 frontmatter 中的格式错误（纯正则，不依赖 LLM）。

    已知问题模式：
    1. `type: source_date: 2026-01-13` — LLM 把两个字段写到同一行
       修复为两行：`type: source` + `source_date: 2026-01-13`

    返回修复数量。
    """
    fixed = 0
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    for md in digests_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        if not text.startswith("---"):
            continue

        new_text = text
        new_text = re.sub(
            r'^(type:\s*)source_date:\s*(\d{4}-\d{2}-\d{2})\s*$',
            r'type: source\nsource_date: \2',
            new_text,
            flags=re.MULTILINE
        )

        if new_text != text:
            md.write_text(new_text, encoding="utf-8")
            fixed += 1
            print(f"  🔧 修复 frontmatter 格式: {md.name}")

    if fixed > 0:
        try:
            from error_book import mark_samples_fixed
            mark_samples_fixed("incomplete", ["缺少 type, tags"])
        except Exception:
            pass

    return fixed


def auto_fix_completeness(graph: 'WikiGraph' = None) -> int:
    """自动修复缺少 type/tags 的页面。

    规则：
    - sources/ 下的页面补 type: source
    - 从页面目录推导 tags
    - 从标题推导 source_date（sources/ 下的日期前缀页面）

    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    fixed = 0
    import yaml

    for page in graph.pages.values():
        if "sources/articles" in str(page.path):
            continue
        fm = page.frontmatter
        needs_fix = False
        updates = {}

        if page.page_type in ("source", "sources"):
            if not fm.get("type"):
                updates["type"] = "source"
                needs_fix = True
            if not fm.get("source_date") and not fm.get("date"):
                date_match = re.match(r'(\d{4}-\d{2}-\d{2})', page.name)
                if date_match:
                    updates["source_date"] = date_match.group(1)
                    needs_fix = True
                elif page.name.startswith("unknown-"):
                    updates["source_date"] = "unknown"
                    needs_fix = True
            if not fm.get("tags"):
                updates["tags"] = [page.path.parent.name]
                needs_fix = True
        else:
            if not fm.get("type"):
                updates["type"] = page.page_type
                needs_fix = True
            if not fm.get("tags"):
                updates["tags"] = [page.page_type]
                needs_fix = True

        if needs_fix and page.path.exists():
            text = page.path.read_text(encoding="utf-8")
            fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
            if fm_match:
                fm_raw = fm_match.group(1)
                try:
                    fm_data = yaml.safe_load(fm_raw)
                except yaml.YAMLError:
                    fixed_fm_lines = []
                    for line in fm_raw.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("source_article:"):
                            val = stripped.split(":", 1)[1].strip()
                            if val and not (val.startswith('"') and val.endswith('"')):
                                escaped = val.replace('\\', '\\\\').replace('"', '\\"')
                                line = f'source_article: "{escaped}"'
                        fixed_fm_lines.append(line)
                    fixed_fm_raw = "\n".join(fixed_fm_lines)
                    try:
                        fm_data = yaml.safe_load(fixed_fm_raw)
                        text = f"---\n{fixed_fm_raw}\n---{text[fm_match.end():]}"
                    except yaml.YAMLError:
                        continue
                if isinstance(fm_data, dict):
                    fm_data.update(updates)
                    new_fm = yaml.dump(fm_data, allow_unicode=True, default_flow_style=False).strip()
                    new_text = f"---\n{new_fm}\n---{text[fm_match.end():]}"
                    page.path.write_text(new_text, encoding="utf-8")
                    fixed += 1
                    fields = ", ".join(updates.keys())
                    print(f"  🔧 补全字段: {page.page_type}/{page.name} (+{fields})")

    return fixed


def _update_index_on_move(old_path: Path, new_path: Path, page_name: str,
                           from_dir: str, to_dir: str):
    """页面文件移动后，同步更新源目录和目标目录的 _index.md。"""
    from config import get_page_dirs
    page_dirs = get_page_dirs()

    from_idx = page_dirs.get(from_dir, Path()) / "_index.md"
    if from_idx.exists():
        _remove_index_entry(from_idx, page_name)

    to_idx = page_dirs.get(to_dir, Path()) / "_index.md"
    if to_idx.exists() and new_path.exists():
        _add_index_entry(to_idx, page_name, new_path)


def _remove_index_entry(index_path: Path, page_name: str):
    """从 _index.md 中移除指定页面的条目行。"""
    if not index_path.exists():
        return
    text = index_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    new_lines = []
    for line in lines:
        m = re.match(r'^(\s*-\s*)\[\[([^\]]+)\]\]', line)
        if m and m.group(2).split('|')[0].strip() == page_name:
            continue
        new_lines.append(line)
    result = "\n".join(new_lines)
    result = re.sub(r'\n{3,}', '\n\n', result)
    index_path.write_text(result, encoding="utf-8")


def _add_index_entry(index_path: Path, page_name: str, md_path: Path):
    """向 _index.md 追加页面条目（从知识页 frontmatter 提取信息）。"""
    import yaml as _yaml
    aliases = ""
    summary = ""
    tags = ""
    text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    parts = []
    if text.startswith("---"):
        parts = _robust_fm_split(text)
    if len(parts) >= 3:
        fm = parts[1]
        am = re.search(r'aliases:\s*\[(.+?)\]', fm)
        if am:
            aliases = am.group(1).strip()
        tm = re.search(r'tags:\s*\[(.+?)\]', fm)
        if tm:
            tag_list = [t.strip().strip("'\"") for t in tm.group(1).split(",")]
            tags = " ".join(f"#{t}" for t in tag_list if t)
    body = parts[2] if len(parts) >= 3 else text
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
    else:
        idx_text = index_path.read_text(encoding="utf-8")
        if "## 待整理" in idx_text:
            idx_text = idx_text.replace("## 待整理", f"## 待整理\n{entry}")
        else:
            idx_text = idx_text.rstrip() + f"\n\n## 待整理\n{entry}"
        index_path.write_text(idx_text, encoding="utf-8")


def auto_fix_type_path_mismatch(graph: 'WikiGraph' = None) -> int:
    """自动修复 frontmatter type 与文件所在目录不一致的问题。

    规则（与 ingest.py write_wiki_files 一致：以 type 为准）：
    - type 在 page_types.yaml 注册表中 → 以 type 为准，移动文件到对应目录
    - type 不在注册表中 → 以目录为准，修正 frontmatter type 为目录名

    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    valid_types = set(get_page_types().keys())
    fixed = 0
    import yaml
    import shutil

    for page in graph.pages.values():
        if page.page_type in ("source", "sources"):
            continue  # sources 特殊目录，不检查
        if page.path and "sources" in str(page.path.relative_to(config.WIKI_DIR)):
            continue
        if not page.path:
            continue

        parent_dir = page.path.parent.name
        if parent_dir in ("wiki",):
            continue

        needs_fix = False
        move_to_dir = None

        if page.page_type not in valid_types:
            needs_fix = True
        elif page.page_type != parent_dir:
            needs_fix = True
            move_to_dir = page.page_type

        if not needs_fix:
            continue

        text = page.path.read_text(encoding="utf-8")
        fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
        if not fm_match:
            if parent_dir in valid_types:
                from datetime import date
                today = date.today().isoformat()
                new_fm = f"---\ntype: {parent_dir}\ncreated: {today}\nupdated: {today}\naliases: []\ntags: []\n---\n"
                new_text = new_fm + text
                page.path.write_text(new_text, encoding="utf-8")
                fixed += 1
                print(f"  🔧 补全 frontmatter: {page.name} (type='{parent_dir}')")
            continue
        fm_raw = fm_match.group(1)
        try:
            fm_data = yaml.safe_load(fm_raw)
        except Exception:
            fm_data = None
            fixed_fm_lines = []
            for line in fm_raw.split("\n"):
                stripped = line.strip()
                if stripped.startswith("source_article:"):
                    val = stripped.split(":", 1)[1].strip()
                    if val and not (val.startswith('"') and val.endswith('"')):
                        escaped = val.replace('\\', '\\\\').replace('"', '\\"')
                        line = f'source_article: "{escaped}"'
                fixed_fm_lines.append(line)
            fixed_fm_raw = "\n".join(fixed_fm_lines)
            try:
                fm_data = yaml.safe_load(fixed_fm_raw)
                text = f"---\n{fixed_fm_raw}\n---{text[fm_match.end():]}"
                fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
            except Exception:
                pass
            if fm_data is None:
                def _quote_flow(m):
                    items = []
                    for item in m.group(1).split(','):
                        item = item.strip()
                        if item and not (item.startswith('"') and item.endswith('"')):
                            item = '"' + item.replace('\\', '\\\\').replace('"', '\\"') + '"'
                        items.append(item)
                    return '[' + ', '.join(items) + ']'
                quoted_fm = re.sub(r'\[([^\[\]]+)\]', _quote_flow, fm_raw)
                try:
                    fm_data = yaml.safe_load(quoted_fm)
                    text = f"---\n{quoted_fm}\n---{text[fm_match.end():]}"
                    fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
                except Exception:
                    pass
            if fm_data is None:
                continue
        if not isinstance(fm_data, dict):
            continue

        old_type = fm_data.get("type", "")

        if move_to_dir:
            new_dir = page.path.parent.parent / move_to_dir
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / page.path.name
            if new_path.exists():
                continue
            shutil.move(str(page.path), str(new_path))
            _update_index_on_move(page.path, new_path, page.name, parent_dir, move_to_dir)
            fixed += 1
            print(f"  🔧 type 纠正: {page.name} 从 {parent_dir}/ 移到 {move_to_dir}/ (type='{old_type}')")
        else:
            correct_type = parent_dir
            fm_data["type"] = correct_type
            new_fm = yaml.dump(fm_data, allow_unicode=True, default_flow_style=False).strip()
            new_text = f"---\n{new_fm}\n---{text[fm_match.end():]}"
            page.path.write_text(new_text, encoding="utf-8")
            fixed += 1
            print(f"  🔧 type 纠正: {page.name} type '{old_type}' → '{correct_type}' (无效type，以目录为准)")

    return fixed


def detect_alias_overlaps(graph: 'WikiGraph' = None):
    """检测同目录下别名交集非空的页面对，并自动修复。

    修复策略：
    1. 如果冲突别名等于某个页面的主名，保留在该页面，从另一个页面移除
    2. 如果两个页面的主名都不是冲突别名，用启发式规则判断：
       - 页面标题中包含该别名关键词的优先保留
       - 否则保留在内容更长/更详细的页面中
    3. 修改 frontmatter 中的 aliases 字段并写回文件
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    from config import get_page_types
    valid_types = set(get_page_types().keys())

    dir_pages: dict[str, list['WikiPage']] = {}
    for page in graph.pages.values():
        if page.page_type in ("source", "sources"):
            continue
        if page.page_type not in valid_types:
            continue
        dir_pages.setdefault(page.page_type, []).append(page)

    overlaps = []
    for dir_name, pages in dir_pages.items():
        for i in range(len(pages)):
            aliases_i = set(pages[i].frontmatter.get("aliases", []))
            aliases_i.add(pages[i].name)  # 主名也算
            for j in range(i + 1, len(pages)):
                aliases_j = set(pages[j].frontmatter.get("aliases", []))
                aliases_j.add(pages[j].name)
                common = aliases_i & aliases_j
                if common:
                    overlaps.append((dir_name, pages[i], pages[j], common))

    if not overlaps:
        print("  ✅ 无同目录别名重复")
        return

    print(f"  ⚠️ 发现 {len(overlaps)} 对同目录别名重复:")
    for dir_name, page_a, page_b, common in overlaps[:20]:
        print(f"    {dir_name}/{page_a.name} ↔ {dir_name}/{page_b.name}: 共有别名 {common}")
    if len(overlaps) > 20:
        print(f"    ... 还有 {len(overlaps) - 20} 对")

    import yaml
    fixed_count = 0
    pending_removals: dict[str, tuple['WikiPage', set]] = {}  # page_path_str → (page, aliases_to_remove)

    def _mark_removal(target_page, alias):
        """标记从 target_page 移除 alias。"""
        pk = str(target_page.path)
        if pk not in pending_removals:
            pending_removals[pk] = (target_page, set())
        pending_removals[pk][1].add(alias)

    for dir_name, page_a, page_b, common in overlaps:
        for alias in common:
            if alias == page_a.name:
                _mark_removal(page_b, alias)
                continue
            if alias == page_b.name:
                _mark_removal(page_a, alias)
                continue

            a_title_match = alias.lower() in page_a.name.lower()
            b_title_match = alias.lower() in page_b.name.lower()
            if a_title_match and not b_title_match:
                _mark_removal(page_b, alias)
                continue
            if b_title_match and not a_title_match:
                _mark_removal(page_a, alias)
                continue

            len_a = len(page_a.path.read_text(encoding="utf-8")) if page_a.path.exists() else 0
            len_b = len(page_b.path.read_text(encoding="utf-8")) if page_b.path.exists() else 0
            if len_a >= len_b:
                _mark_removal(page_b, alias)
            else:
                _mark_removal(page_a, alias)

    for page_path_str, (page, aliases_to_remove) in pending_removals.items():
        if not page.path.exists():
            continue
        page_key = f"{page.page_type}/{page.name}"

        text = page.path.read_text(encoding="utf-8")
        fm_match = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
        if not fm_match:
            continue

        try:
            fm = yaml.safe_load(fm_match.group(1))
        except yaml.YAMLError:
            continue

        if not isinstance(fm, dict):
            continue

        current_aliases = fm.get("aliases", [])
        if not isinstance(current_aliases, list):
            continue

        new_aliases = [a for a in current_aliases if str(a) not in aliases_to_remove]

        if len(new_aliases) == len(current_aliases):
            continue  # 没有变化

        removed = set(str(a) for a in current_aliases) - set(str(a) for a in new_aliases)
        fm["aliases"] = new_aliases
        new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False).strip()
        new_text = f"---\n{new_fm}\n---{text[fm_match.end():]}"
        page.path.write_text(new_text, encoding="utf-8")
        fixed_count += 1
        print(f"    🔧 从 {page_key} 移除重复别名: {removed}")

        dir_name = page.page_type
        idx_path = page.path.parent / "_index.md"
        if idx_path.exists():
            try:
                idx_text = idx_path.read_text(encoding="utf-8")
                page_name = page.name
                pattern = re.compile(
                    r'^(\s*-\s*\[\[' + re.escape(page_name) + r'\]\])\s*\(([^)]*)\)',
                    re.MULTILINE
                )
                m_idx = pattern.search(idx_text)
                if m_idx:
                    old_alias_str = m_idx.group(2)
                    old_idx_aliases = [a.strip() for a in old_alias_str.split(",") if a.strip()]
                    new_idx_aliases = [a for a in old_idx_aliases if a not in aliases_to_remove]
                    if len(new_idx_aliases) != len(old_idx_aliases):
                        if new_idx_aliases:
                            new_alias_part = f" ({', '.join(new_idx_aliases)})"
                        else:
                            new_alias_part = ""
                        new_idx_text = idx_text[:m_idx.start()] + m_idx.group(1) + new_alias_part + idx_text[m_idx.end():]
                        idx_path.write_text(new_idx_text, encoding="utf-8")
                        idx_removed = set(old_idx_aliases) - set(new_idx_aliases)
                        print(f"    ✏️ 同步更新 {dir_name}/_index.md: [[{page_name}]] 移除别名 {idx_removed}")
            except Exception as e:
                print(f"    ⚠️ 更新 {dir_name}/_index.md 失败: {e}")

    if fixed_count > 0:
        print(f"  ✅ 已自动修复 {fixed_count} 个页面的别名重复")
    else:
        print(f"  💡 别名重复均无法自动修复，需人工确认")


def auto_fix_noise(graph: 'WikiGraph' = None) -> int:
    """自动清理 digest 摘要中的噪声内容（关注引导、署名行、推广信息等）。

    返回修复数量。
    """
    noise_patterns = [
        (r'(长按|扫码|二维码|点击.*关注|号|source corpus.*ID|关注我们?$|关注本号|关注source corpus|扫码关注|长按关注)', "关注引导"),
        (r'(版权归.*所有|转载.*请联系|本文来源[:：])', "版权声明"),
        (r'(限时优惠|团购价|团购链接|点击报名|免费领|立即购买|戳.*原文|抽奖活动)', "推广信息"),
        (r'^[-\s*#]*(?:编辑|排版|责编|美编)[:：]\s*\S{2,6}\s*$|图片来源[:：]', "署名行"),
    ]
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    fixed = 0
    for md in digests_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        original = text
        lines = text.split("\n")
        cleaned = []
        in_frontmatter = False
        fm_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped == '---':
                fm_count += 1
                if fm_count == 1:
                    in_frontmatter = True
                elif fm_count == 2:
                    in_frontmatter = False
                cleaned.append(line)
                continue
            if in_frontmatter:
                cleaned.append(line)
                continue
            is_noise = False
            for pattern, _ in noise_patterns:
                if re.search(pattern, stripped) and len(stripped) < 80:
                    is_noise = True
                    break
            if not is_noise:
                cleaned.append(line)
        text = "\n".join(cleaned)
        if text != original:
            text = re.sub(r'\n{3,}', '\n\n', text)
            md.write_text(text, encoding="utf-8")
            fixed += 1
            print(f"  🔧 清理噪声: sources/digests/{md.name}")
    return fixed


def auto_fix_invalid_files(graph: 'WikiGraph' = None) -> int:
    """自动删除过小的无效 digest 文件（< 100 字节，通常为空内容）。

    返回删除数量。
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    fixed = 0
    for md in digests_dir.glob("*.md"):
        if md.name == "_index.md":
            continue
        try:
            size = md.stat().st_size
        except OSError:
            continue
        if size < 100:
            md.unlink()
            fixed += 1
            print(f"  🔧 删除无效文件: sources/digests/{md.name} ({size} 字节)")
    return fixed


def auto_fix_related_page_format(graph: 'WikiGraph' = None) -> int:
    """自动修复'相关页面'章节中的格式问题。

    1. 删除没有链接的列表项（如 "— 大嗓门歌手代表"）
    2. 给缺少目录路径的链接补上前缀（如 [[贝多芬]] → [[composers/贝多芬]]）
    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    fixed = 0
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        if not page.path.exists():
            continue

        text = page.path.read_text(encoding="utf-8")
        original = text
        body = text
        header = ""
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                header = "---" + parts[1] + "---"
                body = parts[2]

        lines = body.split("\n")
        new_lines = []
        in_related = False
        changed = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                in_related = (stripped in ("## 相关页面", "## 相关链接", "## 参见"))
                new_lines.append(line)
                continue
            if in_related:
                if not stripped:
                    new_lines.append(line)
                    continue
                if stripped.startswith("## "):
                    in_related = False
                    new_lines.append(line)
                    continue
                if stripped.startswith("- ") and not re.search(r'\[\[', stripped):
                    changed = True
                    fixed += 1
                    continue
                if (stripped.startswith("—") or stripped.startswith("–")) and not re.search(r'\[\[', stripped):
                    changed = True
                    fixed += 1
                    continue
                if re.search(r'\[\[', stripped):
                    def _fix_link(m):
                        nonlocal changed, fixed
                        link = m.group(1)
                        if '/' in link or link.startswith('_'):
                            return m.group(0)  # 已有路径前缀或特殊页面
                        target = graph.get_page(link)
                        if target and target.page_type and target.page_type not in ("source", "sources"):
                            changed = True
                            fixed += 1
                            return f'[[{target.page_type}/{link}]]'
                        return m.group(0)
                    line = re.sub(r'\[\[([^\]]+)\]\]', _fix_link, line)
            new_lines.append(line)

        if changed:
            new_body = "\n".join(new_lines)
            new_text = header + new_body if header else new_body
            new_text = re.sub(r'\n{3,}', '\n\n', new_text)
            page.path.write_text(new_text, encoding="utf-8")
            print(f"  🔧 修复相关页面格式: {page.page_type}/{page.name}")

    return fixed


def auto_fix_related_source_format(graph: 'WikiGraph' = None) -> int:
    """自动修复'相关来源'章节中的格式问题。

    规则：'相关来源'只能包含 [[sources/digests/...]] 链接。
    1. 指向知识页的链接（如 [[composers/莫扎特]]）→ 移到'相关页面'章节，从'相关来源'删除
    2. 纯文本描述（如 '— 郎朗作为古典乐名牌被初学者盲目追随的现象'）→ 删除
    3. 保留合法的 [[sources/digests/...]] 链接
    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    digest_stems: set[str] = set()
    if digests_dir.exists():
        for md in digests_dir.glob("*.md"):
            if md.name != "_index.md":
                digest_stems.add(md.stem)

    fixed = 0
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        if not page.path.exists():
            continue

        text = page.path.read_text(encoding="utf-8")
        original = text
        header = ""
        body = text
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                header = "---" + parts[1] + "---"
                body = parts[2]

        lines = body.split("\n")
        new_lines = []
        links_to_move: list[str] = []
        in_source = False
        in_related = False
        changed = False

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if stripped.startswith("## "):
                in_source = (stripped == "## 相关来源")
                in_related = (stripped in ("## 相关页面", "## 相关链接", "## 参见"))
                new_lines.append(line)
                i += 1
                continue

            if in_source:
                if stripped.startswith("## "):
                    in_source = False
                    in_related = (stripped in ("## 相关页面", "## 相关链接", "## 参见"))
                    new_lines.append(line)
                    i += 1
                    continue

                if not stripped:
                    new_lines.append(line)
                    i += 1
                    continue

                all_links = re.findall(r'\[\[([^\]]+)\]\]', stripped)
                digest_links = [l for l in all_links if l.startswith("sources/digests/")]
                non_digest_links = [l for l in all_links if not l.startswith("sources/digests/")]

                if non_digest_links:
                    links_to_move.extend(non_digest_links)
                    changed = True
                    fixed += len(non_digest_links)

                if not all_links:
                    changed = True
                    fixed += 1
                    i += 1
                    continue

                if digest_links and non_digest_links:
                    desc_match = re.search(r'—\s*(.+)$', stripped)
                    desc = desc_match.group(1).strip() if desc_match else ""

                    new_items = []
                    for dl in digest_links:
                        dl_escaped = re.escape(dl)
                        m = re.search(r'\[\[' + dl_escaped + r'\]\]\s*—\s*([^\[]+?)(?:\s*[-—]\s*\[\[|$)', stripped)
                        if m:
                            new_items.append(f"- [[{dl}]] — {m.group(1).strip()}")
                        else:
                            new_items.append(f"- [[{dl}]]")
                    for item in new_items:
                        new_lines.append(item)
                elif digest_links:
                    new_lines.append(line)

                i += 1
                continue

            new_lines.append(line)
            i += 1

        if links_to_move:
            final_lines = []
            in_related = False
            related_inserted = False
            for line in new_lines:
                stripped = line.strip()
                if stripped.startswith("## "):
                    if in_related and not related_inserted:
                        for link in links_to_move:
                            final_lines.append(f"- [[{link}]]")
                        related_inserted = True
                    in_related = (stripped in ("## 相关页面", "## 相关链接", "## 参见"))
                    if stripped == "## 相关来源" and not any(
                        l.strip().startswith("## 相关页面") or l.strip().startswith("## 相关链接")
                        for l in new_lines
                    ):
                        final_lines.append("## 相关页面")
                        for link in links_to_move:
                            final_lines.append(f"- [[{link}]]")
                        final_lines.append("")
                        related_inserted = True
                    final_lines.append(line)
                    continue
                if in_related:
                    final_lines.append(line)
                    continue
                final_lines.append(line)

            if not related_inserted and links_to_move:
                final_lines.append("")
                final_lines.append("## 相关页面")
                for link in links_to_move:
                    final_lines.append(f"- [[{link}]]")

            new_lines = final_lines

        if changed:
            new_body = "\n".join(new_lines)
            new_text = header + new_body if header else new_body
            new_text = re.sub(r'\n{3,}', '\n\n', new_text)
            page.path.write_text(new_text, encoding="utf-8")
            print(f"  🔧 修复相关来源格式: {page.page_type}/{page.name}")
            try:
                from error_book import record_sample_with_context
                record_sample_with_context(
                    "related_source_format",
                    f"{page.page_type}/{page.name}",
                    context={"reason": "auto_fix 删除了非 digest 链接或纯文本"},
                )
            except Exception:
                pass

    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/articles" in str(page.path):
            continue
        if not page.path.exists():
            continue

        text = page.path.read_text(encoding="utf-8")
        if "## 相关来源" not in text:
            continue

        header = ""
        body = text
        if text.startswith("---"):
            parts = _robust_fm_split(text)
            if len(parts) >= 3:
                header = "---" + parts[1] + "---"
                body = parts[2]

        lines = body.split("\n")
        new_lines = []
        in_source = False
        source_has_content = False
        source_header_idx = -1
        source_start_idx = -1  # 包括标题前的空行

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "## 相关来源":
                in_source = True
                source_header_idx = i
                continue
            if in_source:
                if stripped.startswith("## "):
                    in_source = False
                    continue
                if stripped:
                    source_has_content = True
                    break

        if not source_has_content and source_header_idx >= 0:
            del_start = source_header_idx
            while del_start > 0 and lines[del_start - 1].strip() == "":
                del_start -= 1
            for i, line in enumerate(lines):
                if del_start <= i <= source_header_idx:
                    continue
                new_lines.append(line)

            new_body = "\n".join(new_lines)
            new_text = header + new_body if header else new_body
            new_text = re.sub(r'\n{3,}', '\n\n', new_text)
            page.path.write_text(new_text, encoding="utf-8")
            fixed += 1
            print(f"  🗑️ 删除空的相关来源: {page.page_type}/{page.name}")
            try:
                from error_book import record_sample_with_context
                record_sample_with_context(
                    "related_source_format",
                    f"{page.page_type}/{page.name}",
                    context={"reason": "auto_fix 删除了空的相关来源章节"},
                )
            except Exception:
                pass

    return fixed


def auto_fix_orphan_digests(graph: 'WikiGraph' = None) -> int:
    """修复孤立的 digest 页面：将其链接补到对应知识页的「相关来源」章节。

    孤立 digest 指没有被任何知识页的 ## 相关来源 引用的 sources/digests/*.md 文件。
    修复策略：
    1. 读取 digest 的「提及实体」章节，获取实体名列表
    2. 在知识页中查找这些实体对应的页面
    3. 把 digest 链接补到知识页的 ## 相关来源 中（选最相关的 1-2 个知识页）
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    referenced_digests: set[str] = set()
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if "sources/" in str(page.path.relative_to(config.WIKI_DIR)):
            continue
        for link in page.outgoing_links:
            if link.startswith("sources/digests/"):
                stem = link.split("sources/digests/", 1)[1]
                referenced_digests.add(stem)

    orphan_digests: list[Path] = []
    for md in sorted(digests_dir.glob("*.md")):
        if md.name == "_index.md":
            continue
        if md.stem not in referenced_digests:
            orphan_digests.append(md)

    if not orphan_digests:
        return 0

    print(f"  🔗 发现 {len(orphan_digests)} 个孤立 digest，尝试补回引用...")

    fixed = 0
    for digest_path in orphan_digests:
        try:
            text = digest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        entities: list[str] = []
        in_entities = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "## 提及实体":
                in_entities = True
                continue
            if in_entities:
                if stripped.startswith("## "):
                    break
                if stripped.startswith("- "):
                    entity = stripped[2:].strip()
                    if entity:
                        entities.append(entity)

        if not entities:
            continue

        digest_summary = ""
        in_summary = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "## 摘要":
                in_summary = True
                continue
            if in_summary:
                if stripped.startswith("## "):
                    break
                if stripped:
                    summary_text = stripped
                    for end_char in ["。", "？", "！", ".", "?", "!"]:
                        idx = summary_text.find(end_char)
                        if 0 < idx <= 40:
                            summary_text = summary_text[:idx + 1]
                            break
                    if len(summary_text) > 40:
                        summary_text = summary_text[:40] + "…"
                    digest_summary = summary_text
                    break

        target_pages: list['WikiPage'] = []
        for entity in entities:
            page = graph.get_page(entity)
            if page is None:
                page = graph.find_page_by_alias(entity)
            if page is not None:
                if page.page_type not in ("source", "sources", "synthesis", "syntheses"):
                    if page not in target_pages:
                        target_pages.append(page)

        if not target_pages:
            continue

        target_pages = target_pages[:3]

        digest_stem = digest_path.stem
        digest_link = f"sources/digests/{digest_stem}"

        for page in target_pages:
            try:
                page_text = page.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            if f"[[{digest_link}]]" in page_text:
                continue

            page_lines = page_text.split("\n")
            new_lines = []
            inserted = False
            in_source_section = False

            for i, line in enumerate(page_lines):
                stripped = line.strip()
                new_lines.append(line)

                if stripped == "## 相关来源":
                    in_source_section = True
                    continue

                if in_source_section:
                    next_stripped = page_lines[i + 1].strip() if i + 1 < len(page_lines) else ""
                    if (not stripped and not next_stripped) or \
                       (not stripped and next_stripped.startswith("## ")) or \
                       (stripped.startswith("## ")):
                        if not inserted:
                            link_line = f"- [[{digest_link}]] — {digest_summary}" if digest_summary else f"- [[{digest_link}]]"
                            new_lines.insert(len(new_lines) - (0 if stripped.startswith("## ") else 0),
                                           link_line)
                            inserted = True
                            in_source_section = False

            if not inserted and in_source_section:
                link_line = f"- [[{digest_link}]] — {digest_summary}" if digest_summary else f"- [[{digest_link}]]"
                new_lines.append(link_line)
                inserted = True

            if not inserted:
                link_line = f"- [[{digest_link}]] — {digest_summary}" if digest_summary else f"- [[{digest_link}]]"
                new_lines.append("")
                new_lines.append("## 相关来源")
                new_lines.append(link_line)
                inserted = True

            if inserted:
                new_text = "\n".join(new_lines)
                new_text = re.sub(r'\n{3,}', '\n\n', new_text)
                page.path.write_text(new_text, encoding="utf-8")
                fixed += 1
                print(f"    ✅ {page.page_type}/{page.name} ← [[{digest_link}]]")

    return fixed


def auto_fix_missing_source_summary(graph: 'WikiGraph' = None) -> int:
    """补全「相关来源」章节中缺少一句话简介的 digest 链接。

    扫描所有知识页的「## 相关来源」章节，找到格式为
      - [[sources/digests/xxx]]
    （没有 — 简介）的行，从对应 digest 文件的「## 摘要」章节提取第一句话补全。
    返回修复数量。
    """
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    digest_summary_cache: dict[str, str] = {}

    def _get_digest_summary(stem: str) -> str:
        if stem in digest_summary_cache:
            return digest_summary_cache[stem]
        digest_path = digests_dir / f"{stem}.md"
        if not digest_path.exists():
            digest_summary_cache[stem] = ""
            return ""
        try:
            text = digest_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            digest_summary_cache[stem] = ""
            return ""
        in_summary = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "## 摘要":
                in_summary = True
                continue
            if in_summary:
                if stripped.startswith("## "):
                    break
                if stripped:
                    summary_text = stripped
                    for end_char in ["。", "？", "！", ".", "?", "!"]:
                        idx = summary_text.find(end_char)
                        if 0 < idx <= 40:
                            summary_text = summary_text[:idx + 1]
                            break
                    if len(summary_text) > 40:
                        summary_text = summary_text[:40] + "…"
                    digest_summary_cache[stem] = summary_text
                    return summary_text
        digest_summary_cache[stem] = ""
        return ""

    _no_summary_re = re.compile(
        r'^(\s*-\s*\[\[sources/digests/([^\]]+)\]\])\s*$'
    )

    fixed = 0
    for page in graph.pages.values():
        if page.page_type in ("source", "sources", "synthesis", "syntheses"):
            continue
        if not page.page_type:
            continue
        if "sources/" in str(page.path.relative_to(config.WIKI_DIR)):
            continue
        if not page.path.exists():
            continue

        text = page.path.read_text(encoding="utf-8")
        if "## 相关来源" not in text:
            continue

        lines = text.split("\n")
        new_lines = []
        in_source = False
        changed = False

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                in_source = (stripped == "## 相关来源")
                new_lines.append(line)
                continue

            if in_source:
                m = _no_summary_re.match(line)
                if m:
                    stem = m.group(2).strip()
                    summary = _get_digest_summary(stem)
                    if summary:
                        new_lines.append(f"- [[sources/digests/{stem}]] — {summary}")
                        changed = True
                        fixed += 1
                        continue

            new_lines.append(line)

        if changed:
            new_text = "\n".join(new_lines)
            page.path.write_text(new_text, encoding="utf-8")

    return fixed


def auto_fix_broken_frontmatter(graph: 'WikiGraph' = None) -> int:
    """修复 sources/digests 下 frontmatter 损坏的 digest（缺开头/结尾 ---）。

    判定：文件开头不是 `---`，但前若干行（≤30 行）出现行首 `---`，
    说明 frontmatter 的开头分隔符被吃掉了，只需在文件最前面补一行 `---`。

    保守起见：
    - 仅处理 sources/digests 下的 .md
    - 仅在前若干行能找到独立 `---` 闭合行，且这些行看起来像 YAML（key: value）时才补
    - 已经以 `---` 开头的文件原样不动
    """
    digests_dir = config.WIKI_DIR / "sources" / "digests"
    if not digests_dir.exists():
        return 0

    fixed = 0
    for md in sorted(digests_dir.glob("*.md")):
        if md.name == "_index.md":
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if text.startswith("---"):
            continue

        lines = text.split("\n")
        close_idx = -1
        for i, line in enumerate(lines[:30]):
            if line.strip() == "---":
                close_idx = i
                break
        if close_idx <= 0:
            continue
        head = lines[:close_idx]
        if not any(re.match(r'^\s*[A-Za-z_][\w\-]*\s*:', l) for l in head):
            continue

        new_text = "---\n" + text
        md.write_text(new_text, encoding="utf-8")
        fixed += 1
        print(f"  🩹 frontmatter 修复: {md.relative_to(config.WIKI_DIR)} 补回开头 ---")

    return fixed


def auto_fix_zero_width_chars(graph: 'WikiGraph' = None) -> int:
    """自动清除 wiki 目录下所有 .md 文件中的零宽字符（U+200B/200C/200D/FEFF/00AD）。

    使用 Unicode 字符串级别的正则替换，安全处理多字节 UTF-8 编码。
    同时处理文件名中的零宽字符（重命名文件）。
    """
    _ZW_RE = re.compile(r'[\u200b\u200c\u200d\ufeff\u00ad]')
    wiki_dir = config.WIKI_DIR
    if not wiki_dir.exists():
        return 0

    fixed = 0
    for md in sorted(wiki_dir.rglob("*.md")):
        try:
            content = md.read_text(encoding="utf-8")
            cleaned = _ZW_RE.sub('', content)
            if cleaned != content:
                md.write_text(cleaned, encoding="utf-8")
                fixed += 1
                print(f"  🧹 清除零宽字符: {md.relative_to(wiki_dir)}")

            if _ZW_RE.search(md.name):
                new_name = _ZW_RE.sub('', md.name)
                new_path = md.parent / new_name
                if not new_path.exists():
                    md.rename(new_path)
                    fixed += 1
                    print(f"  🧹 重命名(零宽字符): {md.name} → {new_name}")
        except (OSError, UnicodeDecodeError) as e:
            print(f"  ⚠️ 跳过 {md.name}: {e}")

    return fixed


def auto_fix_source_duplicates(graph: 'WikiGraph' = None) -> int:
    """自动修复 sources/ 目录下的重复文件。

    常见场景：
    1. 文件名末尾多余空格导致 articles/ 和 digests/ 下"同名"文件不匹配
    2. 同一目录下文件名仅差空格/标点的重复文件

    修复策略：
    - 文件名末尾空格：重命名去除末尾空格（如果去除后不冲突）
    - 同目录下 strip 后同名的文件：保留内容更长的，删除另一个
    """
    fixed = 0
    sources_dir = config.WIKI_DIR / "sources"
    if not sources_dir.exists():
        return 0

    for sub_dir in ("articles", "digests"):
        dir_path = sources_dir / sub_dir
        if not dir_path.exists():
            continue

        md_files = list(dir_path.glob("*.md"))

        for md in md_files:
            name = md.stem
            if name != name.rstrip():
                clean_name = name.rstrip() + ".md"
                clean_path = dir_path / clean_name
                if clean_path.exists():
                    len_old = len(md.read_text(encoding="utf-8"))
                    len_new = len(clean_path.read_text(encoding="utf-8"))
                    if len_old > len_new:
                        clean_path.unlink()
                        md.rename(clean_path)
                        print(f"  🧹 sources/{sub_dir}: 重命名(末尾空格) + 替换较短文件: {md.name}")
                    else:
                        md.unlink()
                        print(f"  🧹 sources/{sub_dir}: 删除末尾空格重复文件: {md.name}")
                else:
                    md.rename(clean_path)
                    print(f"  🧹 sources/{sub_dir}: 重命名(末尾空格): {md.name} → {clean_name}")
                fixed += 1

        md_files = list(dir_path.glob("*.md"))
        name_map: dict[str, list[Path]] = {}
        for md in md_files:
            key = md.stem.strip()
            name_map.setdefault(key, []).append(md)

        for key, paths in name_map.items():
            if len(paths) <= 1:
                continue
            paths.sort(key=lambda p: len(p.read_text(encoding="utf-8")) if p.exists() else 0, reverse=True)
            keep = paths[0]
            for dup in paths[1:]:
                if dup.exists():
                    print(f"  🧹 sources/{sub_dir}: 删除重复文件: {dup.name} (保留 {keep.name})")
                    dup.unlink()
                    fixed += 1

    return fixed


def auto_fix_all(graph: 'WikiGraph' = None) -> dict:
    """运行所有自动修复，返回各类修复数量。"""
    if graph is None:
        graph = WikiGraph()
        graph.load_all()

    ledger_type_map = {
        "broken_links": "broken_link",
        "index_inconsistencies": "index_error",
        "md_suffix": "md_suffix",
        "frontmatter_format": "incomplete",
        "broken_frontmatter": "broken_frontmatter",
        "completeness": "incomplete",
        "noise": "noise",
        "invalid_files": "invalid_file",
        "related_page_format": "related_page_format",
        "related_source_format": "related_source_format",
        "knowledge_index": "knowledge_index",
        "missing_sections": "missing_sections",
        "type_path_mismatch": "type_path_mismatch",
        "zero_width_chars": "zero_width_chars",
        "source_duplicates": "source_duplicates",
        "orphan_digests": "orphan_digests",
        "missing_source_summary": "related_source_format",
    }

    results = {}

    fix_tasks = [
        ("zero_width_chars", lambda: auto_fix_zero_width_chars(graph)),
        ("broken_frontmatter", lambda: auto_fix_broken_frontmatter(graph)),
        ("source_duplicates", lambda: auto_fix_source_duplicates(graph)),
        ("broken_links", lambda: auto_fix_broken_links(graph)),
        ("index_inconsistencies", lambda: auto_fix_index_inconsistencies(graph)),
        ("md_suffix", lambda: auto_fix_md_suffix(graph)),
        ("frontmatter_format", lambda: auto_fix_frontmatter_format()),
        ("completeness", lambda: auto_fix_completeness(graph)),
        ("type_path_mismatch", lambda: auto_fix_type_path_mismatch(graph)),
        ("noise", lambda: auto_fix_noise(graph)),
        ("invalid_files", lambda: auto_fix_invalid_files(graph)),
        ("related_page_format", lambda: auto_fix_related_page_format(graph)),
        ("related_source_format", lambda: auto_fix_related_source_format(graph)),
        ("orphan_digests", lambda: auto_fix_orphan_digests(graph)),
        ("missing_source_summary", lambda: auto_fix_missing_source_summary(graph)),
        ("knowledge_index", lambda: _rebuild_knowledge_index(graph)),
    ]
    for key, fn in fix_tasks:
        try:
            results[key] = fn()
        except Exception as e:
            results[key] = 0
            print(f"  ⚠️ auto_fix_{key} 失败: {e}")

    try:
        from ingest import _rebuild_global_index
        _rebuild_global_index(update_overview=False)
    except Exception as e:
        print(f"  ⚠️ 刷新 index.md 失败: {e}")

    try:
        from error_book import append_ledger
        for key, count in results.items():
            if count > 0:
                append_ledger(
                    issue_type=ledger_type_map.get(key, key),
                    auto_fixed=True,
                    fix_method=f"auto_fix_{key}",
                    count=count,
                )
    except Exception:
        pass

    return results


def print_quick_lint(issues: dict):
    """打印 quick_lint 的结果摘要。"""
    if not issues:
        print("  ✅ 快速检查通过，未发现问题")
        return

    total = sum(len(v) if isinstance(v, list) else 1 for v in issues.values())
    print(f"\n  ⚠️ 快速检查发现 {total} 个问题:")

    labels = {
        "broken_links": "💔 断裂链接",
        "index_inconsistencies": "📋 索引不一致",
        "md_suffix": "📎 .md 后缀",
        "duplicates": "🔄 重复页面",
        "completeness": "📝 完整性",
        "digest_incomplete": "📄 摘要页不完整",
        "broken_frontmatter": "🩹 frontmatter 损坏",
        "orphan_pages": "🔗 孤立页面",
        "missing_content": "❓ 缺失内容",
        "stale_pages": "⏰ 过时页面",
        "noise_issues": "🗑️ 噪声内容",
        "entity_anomalies": "📈 实体频率异常",
        "hollow_pages": "📭 内容空洞",
        "missing_summary": "📝 缺少一句话概括",
        "missing_source_article": "🔗 digest 缺 source_article",
        "pending_entries": "🗂️ 待整理堆积",
        "invalid_files": "📁 无效文件",
        "related_page_format": "🔗 相关页面格式",
        "related_source_format": "📚 相关来源格式",
        "missing_sections": "📑 知识页缺必要章节",
        "type_path_mismatch": "🏷️ type与目录不一致",
        "zero_width_chars": "🧹 零宽字符",
    }
    for key, items in issues.items():
        label = labels.get(key, key)
        if isinstance(items, list):
            print(f"    {label} ({len(items)}):")
            for item in items[:3]:
                if isinstance(item, dict):
                    if "from" in item and "to" in item:
                        print(f"      {item['from']} → [[{item['to']}]]")
                    elif "title" in item:
                        print(f"      {item['title']} → {', '.join(item.get('locations', []))}")
                    else:
                        print(f"      {item}")
                else:
                    print(f"      {item}")
            if len(items) > 3:
                print(f"      ... 还有 {len(items) - 3} 个")
