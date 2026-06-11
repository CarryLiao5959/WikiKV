#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py — 一键处理流程

给定用户的 user_key、uin、name，自动完成：
1. 注册用户到 config.py / preprocess.py / fetch_articles.py
2. 拉取文章（fetch_articles.py）
3. 预处理（preprocess.py）
4. 摄入构建 Wiki（main.py ingest-all）
5. 同步到 KV 存储（wiki_sync_kv.py）

用法：
    python pipeline.py --add "xuexishi,3282315433,学习型生活实验室"

    python pipeline.py --add "xuexishi,3282315433,学习型生活实验室" \
                       --add "duojin,3213052039,多金童鞋扯扯淡"

    python pipeline.py --users xuexishi,duojin

    python pipeline.py --all-users

    python pipeline.py --users xuexishi --limit 500 --concurrency 6 --model-profile auto

    python pipeline.py --users xuexishi --skip-fetch --skip-sync

    python pipeline.py --add "test,123456,测试号" --register-only

    python pipeline.py --add "test,123456,测试号" --dry-run

    python pipeline.py --users xuexishi --fetch-workers 4
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent



def parse_user_spec(spec: str) -> dict:
    """解析用户规格字符串 'user_key,uin,name'。

    返回 {"user_key": str, "uin": str, "name": str}
    """
    parts = [p.strip() for p in spec.split(",", 2)]
    if len(parts) != 3:
        raise ValueError(f"用户规格格式错误: '{spec}'，应为 'user_key,uin,name'")
    user_key, uin, name = parts
    if not user_key or not uin or not name:
        raise ValueError(f"用户规格字段不能为空: '{spec}'")
    if not re.match(r'^[a-z][a-z0-9]*$', user_key):
        raise ValueError(f"user_key 必须是小写字母开头的英文标识: '{user_key}'")
    if not uin.isdigit():
        raise ValueError(f"uin 必须是纯数字: '{uin}'")
    return {"user_key": user_key, "uin": uin, "name": name}



def _inject_to_config_py(user_key: str, uin: str, name: str, dry_run: bool = False) -> bool:
    """将用户注入 config.py 的 USER_MAP。"""
    config_path = BASE_DIR / "config.py"
    text = config_path.read_text(encoding="utf-8")

    if f'"{user_key}"' in text and f'"uin": "{uin}"' in text:
        print(f"    ✅ config.py: {user_key} 已存在，跳过")
        return False

    pattern = r'(USER_MAP\s*=\s*\{.*?)(^\})'
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    if not match:
        print(f"    ❌ config.py: 无法定位 USER_MAP，请手动添加")
        return False

    new_entry = f'    "{user_key}": {{\n        "uin": "{uin}",\n        "name": "{name}",\n    }},\n'

    if dry_run:
        print(f"    [DRY-RUN] config.py: 将添加 {user_key}")
        return True

    insert_pos = match.start(2)
    new_text = text[:insert_pos] + new_entry + text[insert_pos:]
    config_path.write_text(new_text, encoding="utf-8")
    print(f"    ✅ config.py: 已添加 {user_key}")
    return True


def _inject_to_preprocess_py(user_key: str, uin: str, dry_run: bool = False) -> bool:
    """将用户注入 preprocess.py 的 BIZUIN_MAP。"""
    pp_path = BASE_DIR / "preprocess.py"
    text = pp_path.read_text(encoding="utf-8")

    if f'"{uin}"' in text and f'"{user_key}"' in text:
        print(f"    ✅ preprocess.py: {user_key} 已存在，跳过")
        return False

    pattern = r'(BIZUIN_MAP\s*=\s*\{)(.*?)(^\})'
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    if not match:
        print(f"    ❌ preprocess.py: 无法定位 BIZUIN_MAP，请手动添加")
        return False

    new_entry = f'    "{uin}": "{user_key}",\n'

    if dry_run:
        print(f"    [DRY-RUN] preprocess.py: 将添加 {uin} → {user_key}")
        return True

    insert_pos = match.start(3)
    new_text = text[:insert_pos] + new_entry + text[insert_pos:]
    pp_path.write_text(new_text, encoding="utf-8")
    print(f"    ✅ preprocess.py: 已添加 {uin} → {user_key}")
    return True


def _inject_to_fetch_articles_py(user_key: str, uin: str, dry_run: bool = False) -> bool:
    """将用户注入 fetch_articles.py 的 BIZUIN_MAP。"""
    fa_path = BASE_DIR / "fetch_articles.py"
    text = fa_path.read_text(encoding="utf-8")

    if f'"{uin}"' in text and f'"{user_key}"' in text:
        print(f"    ✅ fetch_articles.py: {user_key} 已存在，跳过")
        return False

    pattern = r'(BIZUIN_MAP\s*=\s*\{)(.*?)(^\})'
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    if not match:
        print(f"    ❌ fetch_articles.py: 无法定位 BIZUIN_MAP，请手动添加")
        return False

    new_entry = f'    "{uin}": "{user_key}",\n'

    if dry_run:
        print(f"    [DRY-RUN] fetch_articles.py: 将添加 {uin} → {user_key}")
        return True

    insert_pos = match.start(3)
    new_text = text[:insert_pos] + new_entry + text[insert_pos:]
    fa_path.write_text(new_text, encoding="utf-8")
    print(f"    ✅ fetch_articles.py: 已添加 {uin} → {user_key}")
    return True


def register_users(users: list[dict], dry_run: bool = False) -> list[str]:
    """注册用户到所有配置文件。返回成功注册的 user_key 列表。"""
    registered = []
    for user in users:
        uk, uin, name = user["user_key"], user["uin"], user["name"]
        print(f"\n  📝 注册用户: {uk} ({name}, uin={uin})")
        _inject_to_config_py(uk, uin, name, dry_run=dry_run)
        _inject_to_preprocess_py(uk, uin, dry_run=dry_run)
        _inject_to_fetch_articles_py(uk, uin, dry_run=dry_run)
        registered.append(uk)
    return registered



def fetch_articles(user_keys: list[str], fetch_workers: int = 7,
                   dry_run: bool = False) -> bool:
    """调用 fetch_articles.py 拉取指定用户的文章。"""
    users_str = ",".join(user_keys)
    cmd = [
        sys.executable, str(BASE_DIR / "fetch_articles.py"),
        "--users", users_str,
        "--workers", str(fetch_workers),
    ]

    if dry_run:
        print(f"  [DRY-RUN] 将执行: {' '.join(cmd)}")
        return True

    print(f"  🚀 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print(f"  ❌ 文章拉取失败 (exit code {result.returncode})")
        return False
    print(f"  ✅ 文章拉取完成")
    return True



def preprocess(user_keys: list[str], dry_run: bool = False) -> bool:
    """调用 preprocess.py 预处理指定用户的文章。"""
    users_str = ",".join(user_keys)
    cmd = [
        sys.executable, str(BASE_DIR / "preprocess.py"),
        "--users", users_str,
    ]

    if dry_run:
        print(f"  [DRY-RUN] 将执行: {' '.join(cmd)}")
        return True

    print(f"  🚀 执行: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        print(f"  ❌ 预处理失败 (exit code {result.returncode})")
        return False
    print(f"  ✅ 预处理完成")
    return True



def ingest_wiki(user_keys: list[str], limit: int = 500, concurrency: int = 6,
                model_profile: str = "auto", dry_run: bool = False) -> bool:
    """调用 main.py ingest-all 摄入构建 Wiki。

    通过环境变量 WIKI_ENABLE_WFS=1 启用 WFS 镜像写入。
    """
    users_str = ",".join(user_keys)
    cmd = [
        sys.executable, str(BASE_DIR / "main.py"),
        "--user", users_str,
        "--model-profile", model_profile,
        "ingest-all",
        "--limit", str(limit),
        "--concurrency", str(concurrency),
        "-y",
    ]

    if dry_run:
        print(f"  [DRY-RUN] 将执行: {' '.join(cmd)}")
        return True

    print(f"  🚀 执行: {' '.join(cmd)}")
    env = os.environ.copy()
    env["WIKI_ENABLE_WFS"] = "1"
    result = subprocess.run(cmd, cwd=str(BASE_DIR), env=env)
    if result.returncode != 0:
        print(f"  ❌ Wiki 摄入失败 (exit code {result.returncode})")
        return False
    print(f"  ✅ Wiki 摄入完成")
    return True



def sync_to_kv(user_keys: list[str], kv_concurrency: int = 5,
               dry_run: bool = False) -> bool:
    """调用 wiki_sync_kv.py 将 Wiki 同步到 KV 存储。"""
    users_str = ",".join(user_keys)

    all_ok = True
    for uk in user_keys:
        wiki_dir = BASE_DIR / f"{uk}_wiki" / "wiki"
        if not wiki_dir.exists():
            print(f"  ⚠️ {uk} 的 wiki 目录不存在: {wiki_dir}，跳过同步")
            all_ok = False
            continue

        cmd = [
            sys.executable, str(BASE_DIR / "wiki_sync_kv.py"),
            "--user", uk,
            "--wiki-dir", str(wiki_dir),
            "--concurrency", str(kv_concurrency),
            "--clean",  # 全量重建，确保一致性
        ]

        if dry_run:
            print(f"  [DRY-RUN] 将执行: {' '.join(cmd)}")
            continue

        print(f"\n  🚀 同步 {uk}: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(BASE_DIR))
        if result.returncode != 0:
            print(f"  ❌ {uk} KV 同步失败 (exit code {result.returncode})")
            all_ok = False
        else:
            print(f"  ✅ {uk} KV 同步完成")

    return all_ok



def main():
    parser = argparse.ArgumentParser(
        description="一键处理流程：注册用户 → 拉取文章 → 预处理 → 摄入 Wiki → 同步 KV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    user_group = parser.add_mutually_exclusive_group(required=True)
    user_group.add_argument("--add", action="append", metavar="KEY,UIN,NAME",
                            help="添加新用户并处理，格式: user_key,uin,name（可多次指定）")
    user_group.add_argument("--users", "-u", type=str,
                            help="处理已注册的用户（逗号分隔，如 xuexishi,duojin）")
    user_group.add_argument("--all-users", action="store_true",
                            help="处理所有已注册用户")

    parser.add_argument("--register-only", action="store_true",
                        help="只注册用户到配置文件，不执行后续流程")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="跳过文章拉取步骤（已有 CSV 数据时使用）")
    parser.add_argument("--skip-preprocess", action="store_true",
                        help="跳过预处理步骤（已有 raw/ 数据时使用）")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="跳过 Wiki 摄入步骤")
    sync_group = parser.add_mutually_exclusive_group()
    sync_group.add_argument("--sync", dest="sync_kv", action="store_true", default=True,
                            help="执行 KV 同步步骤（默认开启）")
    sync_group.add_argument("--no-sync", dest="sync_kv", action="store_false",
                            help="跳过 KV 同步步骤")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，只打印将要执行的操作")

    parser.add_argument("--limit", "-l", type=int, default=500,
                        help="摄入文章数量限制（默认: 500）")
    parser.add_argument("--concurrency", "-c", type=int, default=6,
                        help="摄入并发数（默认: 6）")
    parser.add_argument("--model-profile", "-m", default="auto",
                        choices=["model1", "model2", "auto"],
                        help="模型 profile（默认: auto，自动轮询分配）")

    parser.add_argument("--fetch-workers", type=int, default=7,
                        help="文章拉取并行进程数（默认: 7）")

    parser.add_argument("--kv-concurrency", type=int, default=5,
                        help="KV 同步并发数（默认: 5）")

    args = parser.parse_args()

    new_users = []  # 需要注册的新用户
    user_keys = []  # 最终要处理的 user_key 列表

    if args.add:
        for spec in args.add:
            try:
                user = parse_user_spec(spec)
                new_users.append(user)
                user_keys.append(user["user_key"])
            except ValueError as e:
                print(f"❌ {e}")
                sys.exit(1)
    elif args.users:
        user_keys = [u.strip() for u in args.users.split(",") if u.strip()]
    elif args.all_users:
        sys.path.insert(0, str(BASE_DIR))
        import config
        user_keys = list(config.USER_MAP.keys())

    if not user_keys:
        print("❌ 未指定任何用户")
        sys.exit(1)

    print("=" * 60)
    print("  🚀 LLM Wiki 一键处理流程")
    print("=" * 60)
    print(f"  用户: {', '.join(user_keys)} ({len(user_keys)} 个)")
    if new_users:
        print(f"  新注册: {len(new_users)} 个")
    print(f"  步骤:")
    steps = []
    if new_users:
        steps.append("① 注册用户")
    if not args.register_only:
        if not args.skip_fetch:
            steps.append("② 拉取文章")
        if not args.skip_preprocess:
            steps.append("③ 预处理")
        if not args.skip_ingest:
            steps.append(f"④ 摄入 Wiki (limit={args.limit}, concurrency={args.concurrency}, model={args.model_profile})")
        if args.sync_kv:
            steps.append("⑤ 同步 KV")
    for s in steps:
        print(f"    {s}")
    if args.dry_run:
        print(f"  ⚠️ 模拟运行模式")
    print()

    t_total_start = time.time()
    results = {}

    if new_users:
        print("=" * 60)
        print("  [Step 1] 注册用户到配置文件")
        print("=" * 60)
        register_users(new_users, dry_run=args.dry_run)
        results["注册"] = "✅"

        if args.register_only:
            print("\n✅ 用户注册完成（--register-only 模式，跳过后续步骤）")
            return

    if not args.skip_fetch and not args.register_only:
        print("\n" + "=" * 60)
        print("  [Step 2] 拉取文章")
        print("=" * 60)
        ok = fetch_articles(user_keys, fetch_workers=args.fetch_workers,
                           dry_run=args.dry_run)
        results["拉取"] = "✅" if ok else "❌"
        if not ok and not args.dry_run:
            print("\n⚠️ 文章拉取失败，但继续后续步骤（可能已有部分数据）")

    if not args.skip_preprocess and not args.register_only:
        print("\n" + "=" * 60)
        print("  [Step 3] 预处理文章")
        print("=" * 60)
        ok = preprocess(user_keys, dry_run=args.dry_run)
        results["预处理"] = "✅" if ok else "❌"
        if not ok and not args.dry_run:
            print("\n⚠️ 预处理失败，但继续后续步骤")

    if not args.skip_ingest and not args.register_only:
        print("\n" + "=" * 60)
        print("  [Step 4] 摄入构建 Wiki")
        print("=" * 60)
        ok = ingest_wiki(
            user_keys,
            limit=args.limit,
            concurrency=args.concurrency,
            model_profile=args.model_profile,
            dry_run=args.dry_run,
        )
        results["摄入"] = "✅" if ok else "❌"
        if not ok and not args.dry_run:
            print("\n⚠️ Wiki 摄入失败，但继续后续步骤")

    if args.sync_kv and not args.register_only:
        print("\n" + "=" * 60)
        print("  [Step 5] 同步到 KV 存储")
        print("=" * 60)
        ok = sync_to_kv(user_keys, kv_concurrency=args.kv_concurrency,
                        dry_run=args.dry_run)
        results["同步"] = "✅" if ok else "❌"

    t_total = time.time() - t_total_start
    print("\n" + "=" * 60)
    print(f"  📊 一键处理完成 | 总耗时: {t_total:.1f}s")
    print("=" * 60)
    for step, status in results.items():
        print(f"    {status} {step}")
    print()

    if any("❌" in v for v in results.values()):
        print("⚠️ 部分步骤失败，请检查上方日志")
        sys.exit(1)
    else:
        print("✅ 全部完成！")


if __name__ == "__main__":
    main()