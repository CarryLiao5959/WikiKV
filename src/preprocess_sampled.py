#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preprocess_sampled.py — 规模化测试专用预处理

将晓晴提供的分层抽样语料子集
    sampled_subsets/sample_{500,1000}/corpus/{author}/*.txt
转成
    raw/{author}_s{N}/articles/*.md

输入文件名约定（由 sample_subsets.py 决定）：
  - luxun:   {卷号}_{篇号}_{篇名}.txt          单下划线 3 段
             例: 01_002_科学史教篇.txt          → 第一行=标题 + 正文
  - 其他4位: {原始序号}__[集名__]{篇名}.txt    双下划线 2 或 3 段
             例: 0003__自己的园地__三_国粹与欧化.txt
             例: 0001__故都的秋（代序）.txt
             → 全文都是正文，title 从文件名取

输出 frontmatter：
    ---
    source_id: {第一段}      # 与晓晴语境的"原始序号"对齐，方便对照 query gold doc
    source_type: article
    bizuin: {author}_s{N}    # 与 USER_MAP 中注册的 user_key 一致
    title: "{title}"
    collection: "{集名 或 ''}"
    date: unknown
    ---


    {正文}

用法：
    python preprocess_sampled.py --size 500

    python preprocess_sampled.py --size 1000

    python preprocess_sampled.py --size 500,1000

    python preprocess_sampled.py --size 500 --author luxun,xiaohong

    python preprocess_sampled.py --size 500,1000 --force
"""

import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
SAMPLED_DIR = BASE_DIR / "sampled_subsets"

SUPPORTED_AUTHORS = ["luxun", "zhouzuoren", "zhuziqing", "xiaohong", "yudafu"]
SUPPORTED_SIZES = [500, 1000]



def _strip_zero_width(text: str) -> str:
    return re.sub(r'[\u200b\u200c\u200d\ufeff\u00ad]', '', text)


def _fullwidth_alphanum_to_half(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFF10 <= cp <= 0xFF19:
            out.append(chr(cp - 0xFF10 + ord('0')))
        elif 0xFF21 <= cp <= 0xFF3A:
            out.append(chr(cp - 0xFF21 + ord('A')))
        elif 0xFF41 <= cp <= 0xFF5A:
            out.append(chr(cp - 0xFF41 + ord('a')))
        else:
            out.append(ch)
    return ''.join(out)


def normalize_title(title: str) -> str:
    if not title:
        return title
    title = _strip_zero_width(title)
    title = _fullwidth_alphanum_to_half(title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def normalize_content(content: str) -> str:
    if not content:
        return content
    content = _strip_zero_width(content)
    content = _fullwidth_alphanum_to_half(content)
    lines = [ln.rstrip() for ln in content.split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return '\n'.join(lines)


def sanitize_filename(name: str) -> str:
    name = normalize_title(name)
    name = re.sub(r'[<>:"/\\|?*\[\]]', '_', name)
    name = name.strip('. ')
    return name[:200]



def parse_luxun_stem(stem: str) -> tuple[str, str, str]:
    """luxun: {卷号}_{篇号}_{篇名}  → (source_id, title_from_name, collection='')

    title_from_name 仅作 fallback；luxun 实际 title 取 .txt 第一行。
    """
    parts = stem.split('_', 2)
    if len(parts) >= 2:
        source_id = f"{parts[0]}_{parts[1]}"
    else:
        source_id = stem
    title_from_name = parts[2] if len(parts) >= 3 else stem
    return source_id, title_from_name, ''


def parse_other_stem(stem: str) -> tuple[str, str, str]:
    """其他作者: {原始序号}__[集名__]{篇名}  → (source_id, title, collection)"""
    parts = stem.split('__')
    source_id = parts[0] if parts else stem
    if len(parts) >= 3:
        collection = parts[1]
        title = parts[2]
    elif len(parts) == 2:
        collection = ''
        title = parts[1]
    else:
        collection = ''
        title = stem
    return source_id, title, collection



def process_txt_luxun(txt_path: Path, out_dir: Path,
                     user_key: str, duplicates: set, force: bool,
                     stats: dict):
    raw = txt_path.read_text(encoding='utf-8')
    lines = raw.split('\n')

    source_id, title_from_name, _ = parse_luxun_stem(txt_path.stem)

    if title_from_name and title_from_name.strip():
        title = normalize_title(title_from_name.strip())
        content = '\n'.join(lines)
    else:
        title = normalize_title(lines[0].strip()) if lines else ''
        content = '\n'.join(lines[1:]) if len(lines) > 1 else ''

    if not title:
        stats['skipped'] += 1
        return

    content = normalize_content(content)
    if not content or len(content) < 10:
        stats['skipped'] += 1
        return

    if title in duplicates:
        title = f"{title}（{source_id}）"

    md_stem = sanitize_filename(txt_path.stem)
    out_path = out_dir / f"{md_stem}.md"

    if out_path.exists() and not force:
        stats['skipped'] += 1
        return
    if out_path.exists() and force:
        stats['overwritten'] += 1

    escaped_title = title.replace('"', '\\"')
    md = f"""---
source_id: {source_id}
source_type: article
bizuin: {user_key}
title: "{escaped_title}"
date: unknown
---


{content}
"""
    out_path.write_text(md, encoding='utf-8')
    stats['processed'] += 1


def process_txt_other(txt_path: Path, out_dir: Path,
                      user_key: str, duplicates: set, force: bool,
                      stats: dict):
    raw = txt_path.read_text(encoding='utf-8')
    content = normalize_content(raw)
    if not content or len(content) < 5:
        stats['skipped'] += 1
        return

    source_id, title_raw, collection_raw = parse_other_stem(txt_path.stem)
    title = normalize_title(title_raw)
    collection = normalize_title(collection_raw)

    if not title:
        stats['skipped'] += 1
        return

    if title in duplicates:
        title = f"{title}（{source_id}）"

    md_stem = sanitize_filename(txt_path.stem)
    out_path = out_dir / f"{md_stem}.md"

    if out_path.exists() and not force:
        stats['skipped'] += 1
        return
    if out_path.exists() and force:
        stats['overwritten'] += 1

    escaped_title = title.replace('"', '\\"')
    escaped_coll = collection.replace('"', '\\"')
    md = f"""---
source_id: {source_id}
source_type: article
bizuin: {user_key}
title: "{escaped_title}"
collection: "{escaped_coll}"
date: unknown
---


{content}
"""
    out_path.write_text(md, encoding='utf-8')
    stats['processed'] += 1



def process_one(author: str, size: int, force: bool):
    user_key = f"{author}_s{size}"
    src_dir = SAMPLED_DIR / f"sample_{size}" / "corpus" / author
    out_dir = BASE_DIR / "raw" / user_key / "articles"

    if not src_dir.is_dir():
        print(f"  ❌ 源目录不存在: {src_dir}")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    txt_files = sorted(p for p in src_dir.glob("*.txt") if not p.name.startswith("._"))
    if not txt_files:
        print(f"  ❌ {src_dir} 中无有效 txt 文件")
        return

    print(f"\n{'─' * 60}")
    print(f"  📂 {user_key}: {len(txt_files)} 个 txt → {out_dir}")
    print(f"{'─' * 60}")

    title_count: dict[str, int] = {}
    for p in txt_files:
        if author == "luxun":
            _, title_from_name, _ = parse_luxun_stem(p.stem)
            title = normalize_title(title_from_name.strip()) if title_from_name.strip() else ''
            if not title:
                first_line = p.read_text(encoding='utf-8').split('\n', 1)[0].strip()
                title = normalize_title(first_line)
        else:
            _, title_raw, _ = parse_other_stem(p.stem)
            title = normalize_title(title_raw)
        if title:
            title_count[title] = title_count.get(title, 0) + 1
    duplicates = {t for t, c in title_count.items() if c > 1}
    if duplicates:
        print(f"  ⚠️ 重复 title（将加 source_id 区分）: {sorted(duplicates)}")

    stats = {'processed': 0, 'skipped': 0, 'overwritten': 0}
    for p in txt_files:
        if author == "luxun":
            process_txt_luxun(p, out_dir, user_key, duplicates, force, stats)
        else:
            process_txt_other(p, out_dir, user_key, duplicates, force, stats)

    total_md = len(list(out_dir.glob("*.md")))
    print(f"  ✅ 新增={stats['processed']}, 跳过={stats['skipped']}, "
          f"覆盖={stats['overwritten']}, 当前 raw 共 {total_md} 个 md")


def main():
    parser = argparse.ArgumentParser(
        description="预处理 sampled_subsets 语料 → raw/{author}_s{N}/articles/*.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--size", required=True,
                        help="规模档：500 / 1000 / 500,1000")
    parser.add_argument("--author", default=",".join(SUPPORTED_AUTHORS),
                        help=f"作者（逗号分隔），默认全部: {','.join(SUPPORTED_AUTHORS)}")
    parser.add_argument("--force", action="store_true",
                        help="强制覆盖已存在的 .md 文件")
    args = parser.parse_args()

    sizes = []
    for s in args.size.split(','):
        s = s.strip()
        if not s:
            continue
        try:
            n = int(s)
        except ValueError:
            print(f"❌ 无效 size: {s}")
            sys.exit(1)
        if n not in SUPPORTED_SIZES:
            print(f"❌ 不支持的 size: {n}（支持 {SUPPORTED_SIZES}）")
            sys.exit(1)
        sizes.append(n)

    authors = []
    for a in args.author.split(','):
        a = a.strip()
        if not a:
            continue
        if a not in SUPPORTED_AUTHORS:
            print(f"❌ 不支持的作者: {a}（支持 {SUPPORTED_AUTHORS}）")
            sys.exit(1)
        authors.append(a)

    if not SAMPLED_DIR.is_dir():
        print(f"❌ 找不到 sampled_subsets 目录: {SAMPLED_DIR}")
        print(f"   请先解压: unzip sampled_subsets.zip")
        sys.exit(1)

    print(f"🚀 预处理规模档: {sizes}, 作者: {authors}, force={args.force}")

    for size in sizes:
        print(f"\n{'=' * 60}")
        print(f"  ▶ sample_{size}")
        print(f"{'=' * 60}")
        for author in authors:
            process_one(author, size, args.force)

    print(f"\n✅ 全部完成！")


if __name__ == "__main__":
    main()
