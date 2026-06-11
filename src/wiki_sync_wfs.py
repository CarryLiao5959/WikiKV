"""将镜像内置 wiki zip 包同步写入 WFS 文件系统。

用法（容器内）:
    python wiki_sync_wfs.py --user tym,yusi

    python wiki_sync_wfs.py --all

    python wiki_sync_wfs.py --list-bundles

    python wiki_sync_wfs.py --user tym --clean

    python wiki_sync_wfs.py --user tym --dry-run

    python wiki_sync_wfs.py --user demo --wfs-base /path/to/wfs/wiki
"""

import argparse
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config

BUNDLE_DIR = Path(__file__).parent / "wiki_bundles"


def list_bundles() -> list[str]:
    """列出内置的 wiki zip 包。"""
    if not BUNDLE_DIR.exists():
        return []
    return sorted(p.stem for p in BUNDLE_DIR.glob("*.zip"))


def extract_bundle(user_key: str, target_dir: Path) -> Path:
    """解压内置 wiki zip 包到目标目录，返回 wiki 目录路径。"""
    zip_path = BUNDLE_DIR / f"{user_key}.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"未找到内置 wiki 包: {zip_path}")

    print(f"  📦 解压 {zip_path.name} → {target_dir}/")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    wiki_dir = target_dir / "wiki"
    if not wiki_dir.exists():
        raise FileNotFoundError(f"解压后未找到 wiki 目录: {wiki_dir}")

    md_count = sum(1 for _ in wiki_dir.rglob("*.md"))
    print(f"  ✅ 解压完成: {md_count} 个 .md 文件")
    return wiki_dir


def sync_to_wfs(user_key: str, wiki_dir: Path, wfs_wiki_dir: Path,
                clean: bool = False, dry_run: bool = False) -> dict:
    """将 wiki_dir 下的所有文件同步到 wfs_wiki_dir。

    返回统计信息 {"written": N, "skipped": N, "deleted": N, "failed": N}
    """
    stats = {"written": 0, "skipped": 0, "deleted": 0, "failed": 0}

    if clean:
        if dry_run:
            print(f"  [DRY-RUN] 清空 WFS 目录: {wfs_wiki_dir}")
        else:
            if wfs_wiki_dir.exists():
                print(f"  🗑️  清空 WFS 目录: {wfs_wiki_dir}")
                try:
                    shutil.rmtree(wfs_wiki_dir)
                    stats["deleted"] += 1
                except Exception as e:
                    print(f"  ❌ 清空失败: {e}")
                    return stats

    if not dry_run:
        try:
            wfs_wiki_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"  ❌ 创建 WFS 目录失败: {wfs_wiki_dir}")
            print(f"     {e}")
            print(f"     请确认 WFS 已挂载且有写入权限")
            return stats

    all_files = list(wiki_dir.rglob("*"))
    total = sum(1 for f in all_files if f.is_file())
    done = 0
    t_start = time.time()

    for src in sorted(all_files):
        rel = src.relative_to(wiki_dir)
        dst = wfs_wiki_dir / rel

        if src.is_dir():
            if not dry_run:
                try:
                    dst.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    print(f"  ⚠️ 创建目录失败 {rel}: {e}")
                    stats["failed"] += 1
            continue

        done += 1
        if dry_run:
            print(f"  [DRY-RUN] write: {rel}")
            stats["written"] += 1
            continue

        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            stats["written"] += 1
        except Exception as e:
            print(f"  ⚠️ 写入失败 {rel}: {e}")
            stats["failed"] += 1

        if done % 50 == 0 or done == total:
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  📊 进度: {done}/{total} ({done * 100 // total}%) | "
                  f"{rate:.1f} 个/秒 | 预计剩余 {eta:.0f}s")

    return stats


def sync_one_user(user_key: str, wfs_base: str, clean: bool, dry_run: bool) -> bool:
    """同步单个author的 wiki 到 WFS。返回是否成功。"""
    user_info = config.USER_MAP.get(user_key)
    if not user_info:
        print(f"  ❌ 未知用户: {user_key}")
        return False

    user_name = user_info["name"]
    wfs_wiki_dir = Path(wfs_base) / f"{user_key}_wiki" / "wiki"

    print(f"\n{'=' * 60}")
    print(f"🚀 同步: {user_key} ({user_name})")
    print(f"   WFS 目标: {wfs_wiki_dir}")

    zip_path = BUNDLE_DIR / f"{user_key}.zip"
    if not zip_path.exists():
        print(f"  ❌ 未找到内置 wiki 包: {zip_path}")
        return False

    tmp_dir = tempfile.mkdtemp(prefix=f"wiki_{user_key}_")
    try:
        wiki_dir = extract_bundle(user_key, Path(tmp_dir))

        md_count = sum(1 for _ in wiki_dir.rglob("*.md"))
        dir_count = sum(1 for _ in wiki_dir.rglob("*") if _.is_dir())
        print(f"  📁 目录: {dir_count} 个 | 📄 文件: {md_count} 个")

        t_start = time.time()
        stats = sync_to_wfs(user_key, wiki_dir, wfs_wiki_dir,
                            clean=clean, dry_run=dry_run)
        t_elapsed = time.time() - t_start

        print(f"\n  📊 同步结果 ({t_elapsed:.1f}s):")
        print(f"     ✅ 写入: {stats['written']} 个文件")
        if stats["deleted"]:
            print(f"     🗑️  清空: {stats['deleted']} 次")
        if stats["failed"]:
            print(f"     ❌ 失败: {stats['failed']} 个文件")
            return False

        return True

    except Exception as e:
        print(f"  ❌ 同步失败: {e}")
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="将镜像内置 wiki zip 包同步写入 WFS 文件系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument(
        "--user", "-u",
        help=f"author key，支持逗号分隔多个，如 tym,yusi。可选: {', '.join(config.USER_MAP.keys())}",
    )
    user_group.add_argument(
        "--all", action="store_true",
        help="同步所有内置 wiki 包的author",
    )
    user_group.add_argument(
        "--list-bundles", action="store_true",
        help="列出镜像中内置的 wiki 包",
    )

    parser.add_argument(
        "--wfs-base", default=config.WFS_BASE,
        help=f"WFS 根路径（默认: {config.WFS_BASE}）",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="写入前先清空 WFS 目标目录（全量覆盖）",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="模拟运行，不实际写入文件",
    )

    args = parser.parse_args()

    if args.list_bundles:
        bundles = list_bundles()
        if not bundles:
            print("📦 未找到内置 wiki 包")
            print(f"   查找目录: {BUNDLE_DIR}")
        else:
            print(f"📦 内置 wiki 包（{len(bundles)} 个）:")
            for b in bundles:
                zip_path = BUNDLE_DIR / f"{b}.zip"
                size = zip_path.stat().st_size
                in_config = "✅" if b in config.USER_MAP else "⚠️ 未在 config 中"
                print(f"   {b}: {size / 1024:.0f} KB {in_config}")
        return

    if args.all:
        bundles = list_bundles()
        if not bundles:
            print("❌ 未找到内置 wiki 包")
            sys.exit(1)
        users = [b for b in bundles if b in config.USER_MAP]
        if not users:
            print("❌ 内置 wiki 包中没有在 config.USER_MAP 中配置的author")
            sys.exit(1)
        print(f"🚀 批量同步 {len(users)} 个author到 WFS: {', '.join(users)}")
    elif args.user:
        raw_keys = [k.strip() for k in args.user.split(",") if k.strip()]
        bad_keys = [k for k in raw_keys if k not in config.USER_MAP]
        if bad_keys:
            print(f"❌ 未知用户: {', '.join(bad_keys)}")
            print(f"   可选: {', '.join(config.USER_MAP.keys())}")
            sys.exit(1)
        users = raw_keys
        if len(users) > 1:
            print(f"🚀 批量同步 {len(users)} 个author到 WFS: {', '.join(users)}")
    else:
        parser.print_help()
        sys.exit(1)

    print(f"   WFS 根路径: {args.wfs_base}")
    if args.clean:
        print(f"   ⚠️ 清空模式：写入前会清空目标目录")
    if args.dry_run:
        print(f"   ⚠️ 模拟运行模式，不实际写入")

    t_total_start = time.time()
    results = {}

    for user_key in users:
        success = sync_one_user(user_key, args.wfs_base, args.clean, args.dry_run)
        results[user_key] = success

    t_total = time.time() - t_total_start
    print(f"\n{'=' * 60}")
    print(f"📊 全部完成 | 总耗时: {t_total:.1f}s")
    print(f"{'=' * 60}")

    success_count = sum(1 for v in results.values() if v)
    fail_count = sum(1 for v in results.values() if not v)

    for user_key, success in results.items():
        icon = "✅" if success else "❌"
        print(f"   {icon} {user_key}")

    if fail_count:
        print(f"\n⚠️ {fail_count} 个author同步失败")
        sys.exit(1)
    else:
        print(f"\n✅ 全部 {success_count} 个author同步成功")


if __name__ == "__main__":
    main()
