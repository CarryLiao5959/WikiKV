#!/usr/bin/env python3
"""
重新分类脚本 — 对已构建author wiki 的每个子目录 _index.md 进行重新分区归类。

用法:
  python reclassify_index.py                         # 扫描所有用户所有目录，只重跑不满足"适中"的
  python reclassify_index.py --all                   # 同上（显式）
  python reclassify_index.py --user caobian          # 只处理指定author（所有目录）
  python reclassify_index.py --user caobian --dir emotion  # 只处理指定author的指定目录
  python reclassify_index.py --dry-run               # 只打印不写入
  python reclassify_index.py --concurrency 4         # 并行数（默认 4）
  python reclassify_index.py --force                 # 强制重跑所有目录（不过滤适中）

分区状态说明：
  适中 (ok)     — 分区数在建议范围内，跳过（除非 --force）
  不足 (under)  — 分区数低于建议下限，重跑
  过多 (over)   — 分区数超过建议上限，重跑
"""

import argparse
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import config
from llm_client import call_llm_json

EXCLUDE_USERS = {"yusi_v1", "tym_v1"}

SKIP_DIRS = {"sources", "syntheses"}

BATCH_SIZE = 40

BASE_DIR = Path(__file__).parent

_print_lock = threading.Lock()


def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)


def find_wiki_dirs() -> list[tuple[str, Path]]:
    """找到所有已构建author的 wiki 子目录。"""
    results = []
    for d in sorted(BASE_DIR.iterdir()):
        if not d.is_dir() or not d.name.endswith("_wiki"):
            continue
        user = d.name.removesuffix("_wiki")
        if user in EXCLUDE_USERS:
            continue
        wiki_dir = d / "wiki"
        if not wiki_dir.is_dir():
            continue
        for sub in sorted(wiki_dir.iterdir()):
            if not sub.is_dir() or sub.name in SKIP_DIRS or sub.name.startswith("."):
                continue
            index_file = sub / "_index.md"
            if index_file.is_file():
                results.append((user, index_file))
    return results


def parse_index_md(path: Path) -> dict:
    """
    解析 _index.md，返回:
    {
        "header_lines": ["# 标题", "> 描述"],
        "sections": {"分区名": ["- [[条目]] ...", ...], ...},
        "section_order": ["分区名1", "分区名2", ...],
    }
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")

    header_lines = []
    sections: dict[str, list[str]] = {}
    section_order: list[str] = []
    current_section = None

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            header_lines.append(line)
            continue
        if line.startswith("> ") and not sections:
            header_lines.append(line)
            continue
        if line.startswith("## "):
            section_name = line[3:].strip()
            if section_name not in sections:
                sections[section_name] = []
                section_order.append(section_name)
            current_section = section_name
            continue
        if line.startswith("- [[") and current_section:
            sections[current_section].append(line)
            continue

    return {
        "header_lines": header_lines,
        "sections": sections,
        "section_order": section_order,
    }


def collect_all_entries(parsed: dict) -> list[str]:
    """收集所有条目行（排除「待整理」分区）。"""
    entries = []
    for sec in parsed["section_order"]:
        if sec == "待整理":
            continue
        entries.extend(parsed["sections"][sec])
    return entries


def get_suggested_range(n: int) -> str:
    """根据条目总数返回建议分区范围字符串。"""
    if n <= 10:
        return "2-3"
    elif n <= 25:
        return "3-5"
    elif n <= 50:
        return "4-6"
    elif n <= 100:
        return "5-8"
    elif n <= 200:
        return "6-10"
    else:
        return "8-12"


def check_section_stance(n_entries: int, n_sections: int) -> tuple[str, str]:
    """
    判断当前分区状态。
    返回 (stance, suggested_range)
      stance: "ok" | "under" | "over" | "too_few"
    """
    suggested_range = get_suggested_range(n_entries)
    suggested_min = int(suggested_range.split("-")[0])
    suggested_max = int(suggested_range.split("-")[-1])

    if n_entries <= 3:
        return "too_few", suggested_range

    if n_sections < suggested_min:
        return "under", suggested_range
    elif n_sections > suggested_max:
        return "over", suggested_range
    else:
        return "ok", suggested_range


def reclassify_entries(dir_name: str, entries: list[str], dry_run: bool = False) -> dict[str, list[str]]:
    """
    调用 LLM 对所有条目重新分类。
    返回 {"分区名": ["条目行1", "条目行2", ...], ...}
    """
    if not entries:
        return {}

    name_to_line: dict[str, str] = {}
    for entry in entries:
        m = re.match(r"- \[\[(.+?)\]\]", entry)
        if m:
            name_to_line[m.group(1).strip()] = entry

    all_decisions: dict[str, str] = {}  # name -> section

    for start in range(0, len(entries), BATCH_SIZE):
        batch = entries[start:start + BATCH_SIZE]
        prompt_entries = "\n".join(batch)

        existing_sections = sorted(set(all_decisions.values()))

        n = len(entries)
        suggested_range = get_suggested_range(n)

        if existing_sections:
            suggested_min = int(suggested_range.split("-")[0])
            suggested_max = int(suggested_range.split("-")[-1])
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
                "你是知识库编辑。为以下条目决定归入哪个分区。"
                f"{new_section_sys} "
                "分区名应该宽泛概括（2-6字中文短名），能容纳多个相关条目。"
                "**分区不宜过多过细**：相近、相似、有交叉的主题应合并为同一个分区，"
                "分区名要有足够的包容性和概括性。"
            )
            sections_text = "\n".join(f"- {s}" for s in existing_sections)
            user_prompt = f"""目录: {dir_name}/

{sections_text}

{prompt_entries}

{{
  "decisions": [
    {{"name": "条目的页面名（从 [[]] 中提取）", "section": "分区名", "is_new": false}},
    // section 为已有分区名 → is_new=false
    // section 为你建议新建的分区名 → is_new=true
  ]
}}

规则：
- 只输出 JSON，不要多余解释
- section 用中文短名（2-6 字），风格与现有分区一致
- {new_section_rule}
- **分区不宜过多过细**：相近或有交叉的主题应合并为同一个宽泛分区，不要为每个细微角度都单独建分区
- **禁止**使用目录英文名「{dir_name}」作为分区名——分区名必须是该目录下更细粒度的子主题
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名，每个条目都应归入有明确语义的分区
- 判断依据：条目的 tag 与 summary"""
        else:
            sys_prompt = (
                "你是知识库编辑。为以下条目建立分区体系。"
                "你需要根据条目的内容主题，将它们分成多个有意义的子分区。"
                "分区名应该是宽泛的子主题概括（2-6字中文短名），有足够的包容性。"
                "**分区不宜过多过细**：相近、相似、有交叉的主题应合并为同一个分区。"
                f"**本批次最多只能建 {suggested_max} 个分区，至少 2 个，严格遵守上限。**"
            )
            user_prompt = f"""目录: {dir_name}/

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
- **禁止**使用目录英文名「{dir_name}」作为分区名——分区名必须是该目录下更细粒度的子主题
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名，每个条目都应归入有明确语义的分区
- 判断依据：条目的 tag 与 summary"""

        if dry_run:
            safe_print(f"    [DRY-RUN] 第 {start//BATCH_SIZE+1} 批: {len(batch)} 条目, {len(existing_sections)} 个已有分区")
            continue

        try:
            result = call_llm_json(sys_prompt, user_prompt, model=config.LLM_ECONOMY_MODEL)
            for dec in result.get("decisions", []) or []:
                name = (dec.get("name") or "").strip()
                section = (dec.get("section") or "").strip()
                if name and section:
                    all_decisions[name] = section
            cur_sec_count = len(set(all_decisions.values()))
            safe_print(f"    ✅ 第 {start//BATCH_SIZE+1} 批: {len(batch)} 条目 → {cur_sec_count} 个分区")

            if cur_sec_count > suggested_max:
                safe_print(f"    ⚠️ 分区数 {cur_sec_count} 超出上限 {suggested_max}，实时合并中...")
                cur_secs = sorted(set(all_decisions.values()))
                sec_summary = "\n".join(f"- {s}" for s in cur_secs)
                merge_sys = (
                    "你是知识库编辑。以下分区数量过多，需要合并到合理数量。"
                    f"目标：将分区总数控制在 {suggested_range} 个（最多不超过 {suggested_max} 个）。"
                    "合并时优先把主题相近、有交叉的分区合并为一个更宽泛的分区名。"
                )
                merge_user = f"""目录: {dir_name}/
当前有 {cur_sec_count} 个分区（建议 {suggested_range} 个），需要合并：

{sec_summary}

请输出合并方案 JSON：
{{
  "merges": [
    {{"new_name": "合并后的分区名", "from": ["原分区名1", "原分区名2", ...]}}
  ]
}}

规则：
- 只输出 JSON，不要多余解释
- 合并后分区总数必须 ≤ {suggested_max} 个，且 ≥ {suggested_min} 个
- 未被合并的分区保持原名（不需要出现在 merges 里）
- new_name 用中文短名（2-6 字），宽泛概括
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名"""
                try:
                    merge_result = call_llm_json(merge_sys, merge_user, model=config.LLM_ECONOMY_MODEL)
                    rename_map: dict[str, str] = {}
                    for merge in merge_result.get("merges", []) or []:
                        new_name = (merge.get("new_name") or "").strip()
                        from_secs = [s.strip() for s in (merge.get("from") or []) if s.strip()]
                        for old_sec in from_secs:
                            if old_sec and new_name:
                                rename_map[old_sec] = new_name
                    for name_key, sec in all_decisions.items():
                        if sec in rename_map:
                            all_decisions[name_key] = rename_map[sec]
                    after_count = len(set(all_decisions.values()))
                    safe_print(f"    ✅ 实时合并后: {after_count} 个分区")
                except Exception as me:
                    safe_print(f"    ❌ 实时合并失败: {me}")
        except Exception as e:
            safe_print(f"    ❌ 第 {start//BATCH_SIZE+1} 批 LLM 调用失败: {e}")

    if dry_run:
        return {}

    forbidden_names = {"其他", "未分类", "杂项", dir_name}

    result_sections: dict[str, list[str]] = {}
    unmatched = []

    for entry in entries:
        m = re.match(r"- \[\[(.+?)\]\]", entry)
        if not m:
            continue
        name = m.group(1).strip()
        section = all_decisions.get(name)
        if section and section not in forbidden_names:
            result_sections.setdefault(section, []).append(entry)
        else:
            unmatched.append(entry)

    if unmatched and result_sections:
        sorted_secs = sorted(result_sections.keys(), key=lambda s: -len(result_sections[s]))
        for i, entry in enumerate(unmatched):
            target = sorted_secs[i % len(sorted_secs)]
            result_sections[target].append(entry)
        safe_print(f"    ⚠️ {len(unmatched)} 条目未被 LLM 分类，已分散到已有分区")
    elif unmatched:
        result_sections["待整理"] = unmatched
        safe_print(f"    ⚠️ {len(unmatched)} 条目全部未被 LLM 分类，归入「待整理」")

    n_total = len(entries)
    suggested_range = get_suggested_range(n_total)
    suggested_max = int(suggested_range.split("-")[-1])
    suggested_min = int(suggested_range.split("-")[0])
    real_sections = {k: v for k, v in result_sections.items() if k != "待整理"}

    if len(real_sections) > suggested_max:
        safe_print(f"    ⚠️ 分区数 {len(real_sections)} 超出上限 {suggested_max}，调用 LLM 合并...")
        sec_list = sorted(real_sections.keys(), key=lambda s: -len(real_sections[s]))
        sec_summary = "\n".join(f"- {s}（{len(real_sections[s])} 条）" for s in sec_list)
        merge_sys = (
            "你是知识库编辑。以下分区数量过多，需要合并到合理数量。"
            f"目标：将分区总数控制在 {suggested_range} 个（最多不超过 {suggested_max} 个）。"
            "合并时优先把主题相近、有交叉的分区合并为一个更宽泛的分区名。"
        )
        merge_user = f"""目录: {dir_name}/
当前有 {len(sec_list)} 个分区（建议 {suggested_range} 个），需要合并：

{sec_summary}

请输出合并方案 JSON：
{{
  "merges": [
    {{"new_name": "合并后的分区名", "from": ["原分区名1", "原分区名2", ...]}}
  ]
}}

规则：
- 只输出 JSON，不要多余解释
- 合并后分区总数必须 ≤ {suggested_max} 个，且 ≥ {suggested_min} 个
- 未被合并的分区保持原名（不需要出现在 merges 里）
- new_name 用中文短名（2-6 字），宽泛概括
- **禁止**使用「其他」「未分类」「杂项」等兜底分区名"""
        try:
            merge_result = call_llm_json(merge_sys, merge_user, model=config.LLM_ECONOMY_MODEL)
            for merge in merge_result.get("merges", []) or []:
                new_name = (merge.get("new_name") or "").strip()
                from_secs = [s.strip() for s in (merge.get("from") or []) if s.strip()]
                if not new_name or not from_secs:
                    continue
                merged_entries = []
                for old_sec in from_secs:
                    if old_sec in result_sections:
                        merged_entries.extend(result_sections.pop(old_sec))
                if merged_entries:
                    result_sections.setdefault(new_name, []).extend(merged_entries)
            after_count = len([k for k in result_sections if k != "待整理"])
            safe_print(f"    ✅ 合并后: {after_count} 个分区")
        except Exception as e:
            safe_print(f"    ❌ 合并 LLM 调用失败: {e}")

    return result_sections


def write_index_md(path: Path, header_lines: list[str], sections: dict[str, list[str]],
                   pending_lines: list[str] | None = None):
    """重写 _index.md，保留「待整理」分区（如有）。"""
    lines = []
    for h in header_lines:
        lines.append(h)
    lines.append("")  # 空行

    sorted_sections = sorted(
        [(k, v) for k, v in sections.items() if k != "待整理"],
        key=lambda x: -len(x[1])
    )

    for sec_name, entries in sorted_sections:
        if not entries:
            continue
        lines.append(f"## {sec_name}")
        for entry in entries:
            lines.append(entry)

    if pending_lines:
        lines.append("## 待整理")
        for l in pending_lines:
            lines.append(l)

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"

    path.write_text(content, encoding="utf-8")


def process_one_dir(user: str, index_path: Path, dry_run: bool, force: bool) -> dict:
    """
    处理单个目录。返回结果字典：
    {
        "user": str, "dir": str, "status": "skipped"|"ok"|"failed",
        "reason": str,  # skipped 时说明原因
        "before": int, "after": int,
    }
    """
    dir_name = index_path.parent.name
    rel_path = f"{user}_wiki/wiki/{dir_name}"

    parsed = parse_index_md(index_path)
    entries = collect_all_entries(parsed)

    if not entries:
        return {"user": user, "dir": dir_name, "status": "skipped", "reason": "无条目", "before": 0, "after": 0}

    n_entries = len(entries)
    n_sections = len([s for s in parsed["section_order"] if s != "待整理" and parsed["sections"][s]])

    stance, suggested_range = check_section_stance(n_entries, n_sections)

    if stance == "ok" and not force:
        return {
            "user": user, "dir": dir_name, "status": "skipped",
            "reason": f"适中（{n_sections} 个分区，建议 {suggested_range}，条目 {n_entries}）",
            "before": n_sections, "after": n_sections,
        }

    if stance == "too_few" and not force:
        return {
            "user": user, "dir": dir_name, "status": "skipped",
            "reason": f"条目过少（{n_entries} 条，无需细分分区）",
            "before": n_sections, "after": n_sections,
        }

    stance_label = {"ok": "适中(强制)", "under": "不足", "over": "过多", "too_few": "条目过少(强制)"}.get(stance, stance)
    safe_print(f"🔄 {rel_path}: {n_entries} 条目, {n_sections} 个分区 [{stance_label}，建议 {suggested_range}] → 重新分类...")

    pending_lines: list[str] = []
    if "待整理" in parsed["sections"]:
        pending_lines = parsed["sections"]["待整理"]

    new_sections = reclassify_entries(dir_name, entries, dry_run=dry_run)

    if dry_run:
        return {"user": user, "dir": dir_name, "status": "ok", "reason": "dry-run", "before": n_sections, "after": 0}

    if not new_sections:
        safe_print(f"  ⚠️ {rel_path}: 重新分类失败，保持原样")
        return {"user": user, "dir": dir_name, "status": "failed", "reason": "LLM 无返回", "before": n_sections, "after": n_sections}

    after_sections = len([k for k in new_sections if k != "待整理"])

    write_index_md(index_path, parsed["header_lines"], new_sections, pending_lines or None)

    delta = n_sections - after_sections
    arrow = f"↓{delta}" if delta > 0 else (f"↑{-delta}" if delta < 0 else "=")
    safe_print(f"  ✅ {rel_path}: {n_sections} → {after_sections} 个分区 ({arrow})")
    for sec, items in sorted(new_sections.items(), key=lambda x: -len(x[1])):
        if sec != "待整理":
            safe_print(f"    {sec}: {len(items)} 条")
    safe_print()

    return {"user": user, "dir": dir_name, "status": "ok", "reason": stance_label, "before": n_sections, "after": after_sections}


def main():
    parser = argparse.ArgumentParser(description="重新分类 wiki _index.md（只处理不满足适中的目录）")
    parser.add_argument("--user", type=str, help="只处理指定author")
    parser.add_argument("--dir", type=str, help="只处理指定子目录（需配合 --user）")
    parser.add_argument("--dry-run", action="store_true", help="只打印不写入")
    parser.add_argument("--force", action="store_true", help="强制重跑所有目录（包括已适中的）")
    parser.add_argument("--concurrency", type=int, default=4, help="并行处理的目录数（默认 4）")
    parser.add_argument("--all", dest="all_users", action="store_true", help="处理所有用户（默认行为，可省略）")
    args = parser.parse_args()

    all_dirs = find_wiki_dirs()

    if args.user:
        all_dirs = [(u, p) for u, p in all_dirs if u == args.user]
        if not all_dirs:
            print(f"❌ 未找到author {args.user} 的 wiki 目录")
            return

    if args.dir:
        all_dirs = [(u, p) for u, p in all_dirs if p.parent.name == args.dir]

    print(f"📋 共扫描到 {len(all_dirs)} 个目录")
    if not args.force:
        print(f"   （仅重跑分区数不满足建议范围的目录，--force 可强制全部重跑）")
    print(f"   并发数: {args.concurrency}，模型: {config.LLM_ECONOMY_MODEL}")
    print()

    results = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(process_one_dir, user, index_path, args.dry_run, args.force): (user, index_path)
            for user, index_path in all_dirs
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                user, index_path = futures[future]
                safe_print(f"❌ {user}/{index_path.parent.name} 处理异常: {e}")
                results.append({"user": user, "dir": index_path.parent.name, "status": "failed",
                                 "reason": str(e), "before": 0, "after": 0})

    processed = [r for r in results if r["status"] == "ok"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    total_before = sum(r["before"] for r in processed)
    total_after = sum(r["after"] for r in processed)

    print("=" * 60)
    print(f"📊 总计: {len(all_dirs)} 个目录")
    print(f"   ✅ 重新分类: {len(processed)} 个  |  ⏭️  跳过(适中): {len(skipped)} 个  |  ❌ 失败: {len(failed)} 个")
    if processed:
        print(f"   分区变化: {total_before} → {total_after} 个（重跑目录合计）")
    if failed:
        print(f"\n❌ 失败列表:")
        for r in failed:
            print(f"   {r['user']}/{r['dir']}: {r['reason']}")


if __name__ == "__main__":
    main()