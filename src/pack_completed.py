#!/usr/bin/env python3
"""
检测已构建完成的用户并打包为 LLM_WiKi.zip

完成标准：
  1. 已摄入数 > 0（至少有一篇已摄入）
  2. wiki 目录下有实际内容（排除全被 SKIP 的空壳）
"""

import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

USER_MAP = {
    "demo": "Demo Corpus",
    "luxun": "Lu Xun",
    "zhouzuoren": "Zhou Zuoren",
    "zhuziqing": "Zhu Ziqing",
    "xiaohong": "Xiao Hong",
    "yudafu": "Yu Dafu",
}


def get_raw_count(user_key: str) -> int:
    """获取 raw 目录下的文章总数"""
    raw_dir = BASE_DIR / "raw" / user_key / "articles"
    if not raw_dir.exists():
        return 0
    return len(list(raw_dir.glob("*.md")))


def get_ingested_count(user_key: str) -> int:
    """从缓存文件获取已摄入数"""
    cache_file = BASE_DIR / f".wiki-cache-{user_key}.json"
    if not cache_file.exists():
        return 0
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        return len(cache)
    except (json.JSONDecodeError, OSError):
        return 0


def has_real_content(user_key: str) -> bool:
    """检查 wiki 目录下是否有实际内容（排除全被 SKIP 的空壳）"""
    wiki_dir = BASE_DIR / f"{user_key}_wiki" / "wiki"
    if not wiki_dir.exists():
        return False
    sources_dir = wiki_dir / "sources"
    if sources_dir.exists():
        source_files = list(sources_dir.glob("*.md"))
        source_files = [f for f in source_files if f.name != "_index.md"]
        if len(source_files) > 0:
            return True
    for subdir in wiki_dir.iterdir():
        if subdir.is_dir() and subdir.name not in ("sources", "digests"):
            md_files = [f for f in subdir.glob("*.md") if f.name != "_index.md"]
            if len(md_files) > 0:
                return True
    return False


def is_completed(user_key: str) -> tuple:
    """
    判断用户是否构建完成
    返回: (是否完成, raw总数, 已摄入数, 原因)
    """
    raw_count = get_raw_count(user_key)
    ingested = get_ingested_count(user_key)

    if ingested == 0:
        return False, raw_count, ingested, "未开始"

    if not has_real_content(user_key):
        return False, raw_count, ingested, "全部被SKIP（空壳）"

    return True, raw_count, ingested, f"已摄入 {ingested} 篇"


def main():
    print("=" * 70)
    print("  📊 LLM_WiKi 构建完成度检测")
    print("=" * 70)

    completed = []
    incomplete = []
    skipped = []

    for user_key, name in sorted(USER_MAP.items()):
        done, raw_count, ingested, reason = is_completed(user_key)
        wiki_dir = BASE_DIR / f"{user_key}_wiki"

        if not wiki_dir.exists():
            skipped.append((user_key, name, raw_count, ingested, "wiki目录不存在"))
            continue

        if done:
            completed.append((user_key, name, raw_count, ingested, reason))
        else:
            incomplete.append((user_key, name, raw_count, ingested, reason))

    print(f"\n✅ 已完成（{len(completed)} 个）:")
    print("-" * 70)
    for user_key, name, raw_count, ingested, reason in completed:
        print(f"  {user_key:12s} ({name}): {raw_count} 篇 → 已摄入 {ingested} | {reason}")

    if incomplete:
        print(f"\n⏳ 未完成（{len(incomplete)} 个）:")
        print("-" * 70)
        for user_key, name, raw_count, ingested, reason in incomplete:
            print(f"  {user_key:12s} ({name}): {raw_count} 篇 → 已摄入 {ingested} | {reason}")

    if skipped:
        print(f"\n⏭️  跳过（{len(skipped)} 个）:")
        print("-" * 70)
        for user_key, name, raw_count, ingested, reason in skipped:
            print(f"  {user_key:12s} ({name}): {reason}")

    if not completed:
        print("\n❌ 没有已完成的用户，无需打包")
        return

    wiki_dirs = [f"{uk}_wiki" for uk, *_ in completed]
    mid = (len(wiki_dirs) + 1) // 2  # 向上取整，part1 多一个（奇数时）
    part1_dirs = wiki_dirs[:mid]
    part2_dirs = wiki_dirs[mid:]

    exclude_args = "-x '*/log/*' -x '*/log'"
    zip_cmd1 = f"zip -r LLM_WiKi_part1.zip {' '.join(part1_dirs)} {exclude_args}"
    zip_cmd2 = f"zip -r LLM_WiKi_part2.zip {' '.join(part2_dirs)} {exclude_args}" if part2_dirs else None

    print(f"\n{'=' * 70}")
    print(f"  📦 打包命令（共 {len(completed)} 个已完成用户，拆为两个压缩包）")
    print(f"{'=' * 70}")
    print(f"\n[Part 1 — {len(part1_dirs)} 个用户]")
    print(f"  {zip_cmd1}")
    if zip_cmd2:
        print(f"\n[Part 2 — {len(part2_dirs)} 个用户]")
        print(f"  {zip_cmd2}")
    print()

    if "--yes" in sys.argv or "-y" in sys.argv:
        do_zip = True
    else:
        answer = input("是否立即执行打包？(y/n) ").strip().lower()
        do_zip = answer == "y"

    if do_zip:
        for zip_name in ("LLM_WiKi_part1.zip", "LLM_WiKi_part2.zip"):
            old = BASE_DIR / zip_name
            if old.exists():
                old.unlink()
                print(f"🗑️  已删除旧的 {zip_name}")

        all_ok = True
        for label, cmd, dirs in [
            ("Part 1", zip_cmd1, part1_dirs),
            ("Part 2", zip_cmd2, part2_dirs),
        ]:
            if not cmd:
                continue
            print(f"\n⏳ 正在打包 {label}（{len(dirs)} 个用户）...")
            result = subprocess.run(
                cmd, shell=True, cwd=str(BASE_DIR),
                capture_output=True, text=True,
            )
            zip_name = f"LLM_WiKi_{label.lower().replace(' ', '')}.zip"
            zip_path = BASE_DIR / zip_name
            if result.returncode == 0 and zip_path.exists():
                size_mb = zip_path.stat().st_size / (1024 * 1024)
                print(f"✅ {label} 打包完成: {zip_path}  ({size_mb:.1f} MB)")
            else:
                print(f"❌ {label} 打包失败:")
                print(result.stderr)
                all_ok = False

        if all_ok:
            print("\n🎉 全部打包完成！")
    else:
        print("已取消打包，你可以复制上面的命令手动执行。")


if __name__ == "__main__":
    main()