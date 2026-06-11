#!/usr/bin/env python3
"""初始化 HDFS 增量状态文件。

从现有的 raw/{user}/articles/ 目录中扫描已处理过的文章，
生成 incr_state.json 并上传到 HDFS，防止增量脚本首次运行时
把所有已处理文章当成"新增"重新处理。

用法：
    python init_hdfs_state.py --preview

    python init_hdfs_state.py --local-only

    python init_hdfs_state.py --commit

    python init_hdfs_state.py --users tym,yusi --commit

    python init_hdfs_state.py --hdfs-state hdfs://xxx/incr_state.json --commit

    python init_hdfs_state.py --local-only
    python init_hdfs_state.py --upload-only
    python init_hdfs_state.py --upload-only --state-file /path/to/state.json
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import config as cfg

HADOOP_BIN = shutil.which("hadoop") or os.environ.get("HADOOP_BIN", "hadoop")
HDFS_BASE_PATH = os.environ.get("WIKI_HDFS_BASE", "hdfs:///llm_wiki")
DEFAULT_HDFS_STATE_PATH = f"{HDFS_BASE_PATH}/llm_wiki/incr_state.json"
LOCAL_STATE_FILE = BASE_DIR / ".incr-pipeline-state.json"


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _hdfs_put(local_path: str, hdfs_path: str) -> bool:
    """上传本地文件到 HDFS（覆盖）。"""
    if not os.path.exists(HADOOP_BIN):
        log(f"  ⚠️ hadoop 二进制不存在: {HADOOP_BIN}")
        return False
    tq_user = os.environ.get("TQ_USER_NAME", "")
    tq_token = os.environ.get("TQ_USER_TOKEN", "")
    if not tq_user or not tq_token:
        log(f"  ⚠️ Hadoop auth env (TQ_USER_NAME / TQ_USER_TOKEN) not set")
        return False
    try:
        hdfs_dir = hdfs_path.rsplit("/", 1)[0]
        subprocess.run([HADOOP_BIN, "fs", "-mkdir", "-p", hdfs_dir],
                       capture_output=True, text=True, timeout=60)
        subprocess.run([HADOOP_BIN, "fs", "-rm", "-f", hdfs_path],
                       capture_output=True, text=True, timeout=60)
        result = subprocess.run([HADOOP_BIN, "fs", "-put", local_path, hdfs_path],
                                capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True
        log(f"  HDFS put 失败: {result.stderr.strip()}")
    except Exception as e:
        log(f"  HDFS put 异常: {e}")
    return False


def _hdfs_get(hdfs_path: str, local_path: str) -> bool:
    """从 HDFS 下载文件到本地。"""
    if not os.path.exists(HADOOP_BIN):
        return False
    try:
        if os.path.exists(local_path):
            os.remove(local_path)
        result = subprocess.run([HADOOP_BIN, "fs", "-get", hdfs_path, local_path],
                                capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except Exception:
        return False


def scan_user_articles(user_key: str) -> list[str]:
    """扫描 raw/{user_key}/articles/ 目录，返回所有已有文章的 source_id 列表。"""
    raw_dir = BASE_DIR / "raw" / user_key / "articles"
    if not raw_dir.exists():
        return []
    return sorted(f.stem for f in raw_dir.glob("*.md"))


def build_init_state(user_keys: list[str], mark_all_done: bool = False) -> dict:
    """从 raw 目录构建初始状态。

    Args:
        user_keys: 用户 key 列表
        mark_all_done: 如果为 True，所有 raw 文章都标记为已处理完成
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    state = {
        "updated_at": now,
        "initialized_at": now,
        "init_method": "init_hdfs_state.py",
        "users": {},
    }

    for uk in user_keys:
        source_ids = scan_user_articles(uk)
        if not source_ids:
            continue

        cache_file = BASE_DIR / f".wiki-cache-{uk}.json"
        cache_count = 0
        if cache_file.exists():
            try:
                cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
                cache_count = len(cache_data)
            except (json.JSONDecodeError, IOError):
                pass

        if mark_all_done:
            ingested = len(source_ids)
            cache_count = len(source_ids)
        else:
            ingested = cache_count if cache_count > 0 else len(source_ids)

        state["users"][uk] = {
            "processed_source_ids": source_ids,
            "last_run": now,
            "total_ingested": ingested,
            "init_from": "raw_directory",
            "raw_article_count": len(source_ids),
            "cache_article_count": cache_count,
        }

    return state


def main():
    parser = argparse.ArgumentParser(
        description="初始化 HDFS 增量状态文件（防止已处理文章被重复摄入）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--users", type=str, default=None,
                        help="只初始化指定用户（逗号分隔），默认所有已注册用户")
    parser.add_argument("--hdfs-state", type=str, default=DEFAULT_HDFS_STATE_PATH,
                        help=f"HDFS 状态文件路径（默认: {DEFAULT_HDFS_STATE_PATH}）")
    parser.add_argument("--preview", action="store_true", default=True,
                        help="预览模式（默认），只显示不写入")
    parser.add_argument("--local-only", action="store_true",
                        help="只写入本地文件，不上传 HDFS")
    parser.add_argument("--commit", action="store_true",
                        help="写入本地 + 上传 HDFS")
    parser.add_argument("--merge", action="store_true",
                        help="合并模式：如果 HDFS 上已有状态，合并而非覆盖（只补充缺失的用户）")
    parser.add_argument("--upload-only", action="store_true",
                        help="仅上传模式：跳过扫描，直接将本地状态文件上传到 HDFS（适用于本地无 HDFS 环境）")
    parser.add_argument("--state-file", type=str, default=None,
                        help="指定要上传的本地状态文件路径（配合 --upload-only 使用，默认读取 .incr-pipeline-state.json）")
    parser.add_argument("--only-completed", action="store_true",
                        help="只处理所有文章都已摄入完成的用户（cache 数 == raw 数）")
    parser.add_argument("--mark-all-done", action="store_true",
                        help="将所有 raw 文章标记为已处理完成（忽略 cache 实际数量）")

    args = parser.parse_args()

    if args.upload_only:
        state_path = Path(args.state_file) if args.state_file else LOCAL_STATE_FILE
        if not state_path.exists():
            log(f"❌ 本地状态文件不存在: {state_path}")
            log(f"   请先在本地运行: python init_hdfs_state.py --local-only")
            sys.exit(1)

        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            user_count = len(state_data.get("users", {}))
            total_ids = sum(
                len(u.get("processed_source_ids", []))
                for u in state_data.get("users", {}).values()
            )
        except (json.JSONDecodeError, IOError) as e:
            log(f"❌ 状态文件格式错误: {e}")
            sys.exit(1)

        log(f"📄 本地状态文件: {state_path}")
        log(f"   文件大小: {state_path.stat().st_size / 1024:.1f} KB")
        log(f"   用户数: {user_count}")
        log(f"   总文章 ID 数: {total_ids}")
        log(f"   更新时间: {state_data.get('updated_at', '未知')}")
        log("")
        log(f"📤 上传到 HDFS: {args.hdfs_state}")

        if _hdfs_put(str(state_path), args.hdfs_state):
            log(f"✅ HDFS 上传成功!")
        else:
            log(f"❌ HDFS 上传失败")
            sys.exit(1)
        return

    if args.users:
        user_keys = [u.strip() for u in args.users.split(",")]
        for uk in user_keys:
            if uk not in cfg.USER_MAP:
                log(f"❌ 未知用户: {uk}")
                sys.exit(1)
    else:
        user_keys = list(cfg.USER_MAP.keys())

    if args.only_completed:
        filtered_user_keys = []
        for uk in user_keys:
            cache_file = BASE_DIR / f".wiki-cache-{uk}.json"
            cache_count = 0
            if cache_file.exists():
                try:
                    cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
                    cache_count = len(cache_data)
                except (json.JSONDecodeError, IOError):
                    pass
            raw_count = len(scan_user_articles(uk))
            if cache_count == raw_count and cache_count > 0:
                filtered_user_keys.append(uk)
        user_keys = filtered_user_keys

    log(f"📋 扫描 {len(user_keys)} 个用户的 raw 目录...")
    log("")

    state = build_init_state(user_keys, mark_all_done=args.mark_all_done)

    log(f"{'='*70}")
    log(f"  初始化状态预览")
    log(f"{'='*70}")
    log("")

    total_articles = 0
    total_cached = 0
    users_with_data = []
    users_without_data = []

    for uk in user_keys:
        name = cfg.USER_MAP[uk]["name"]
        if uk in state["users"]:
            u = state["users"][uk]
            raw_count = u["raw_article_count"]
            cache_count = u["cache_article_count"]
            total_articles += raw_count
            total_cached += cache_count
            users_with_data.append(uk)

            if cache_count > 0 and cache_count == raw_count:
                status = "✅ 全部已摄入"
            elif cache_count > 0:
                status = f"⚠️ 部分摄入 ({cache_count}/{raw_count})"
            else:
                status = "📦 仅有 raw（无 cache）"

            log(f"  {uk:15s} ({name:20s}): {raw_count:4d} 篇 raw | "
                f"{cache_count:4d} 篇 cache | {status}")
        else:
            users_without_data.append(uk)

    log("")
    log(f"  📊 汇总: {len(users_with_data)} 个用户有数据，"
        f"{len(users_without_data)} 个用户无数据")
    log(f"     总计 {total_articles} 篇 raw 文章，{total_cached} 篇已缓存摄入")

    if users_without_data:
        log(f"  ⏭️  无数据用户（将跳过）: {', '.join(users_without_data)}")

    log("")

    if args.merge and (args.local_only or args.commit):
        log("🔀 合并模式：尝试从 HDFS 读取已有状态...")
        tmp_path = str(LOCAL_STATE_FILE) + ".merge_tmp"
        if _hdfs_get(args.hdfs_state, tmp_path):
            try:
                existing = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
                existing_users = set(existing.get("users", {}).keys())
                new_users = set(state.get("users", {}).keys())
                overlap = existing_users & new_users
                only_new = new_users - existing_users

                log(f"  HDFS 已有 {len(existing_users)} 个用户")
                log(f"  本次新增 {len(only_new)} 个用户: {', '.join(sorted(only_new)) or '(无)'}")
                if overlap:
                    log(f"  ⏭️  已存在（不覆盖）: {', '.join(sorted(overlap))}")

                for uk in only_new:
                    existing.setdefault("users", {})[uk] = state["users"][uk]
                existing["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                state = existing
            except (json.JSONDecodeError, IOError) as e:
                log(f"  ⚠️ 解析 HDFS 状态失败: {e}，将使用全新状态")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        else:
            log("  HDFS 无已有状态，将创建全新状态")
        log("")

    if args.commit or args.local_only:
        LOCAL_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        log(f"✅ 已写入本地: {LOCAL_STATE_FILE}")
        log(f"   文件大小: {LOCAL_STATE_FILE.stat().st_size / 1024:.1f} KB")

        if args.commit:
            log(f"\n📤 上传到 HDFS: {args.hdfs_state}")
            if _hdfs_put(str(LOCAL_STATE_FILE), args.hdfs_state):
                log(f"✅ HDFS 上传成功!")
            else:
                log(f"❌ HDFS 上传失败，状态仅保存在本地")
        else:
            log(f"\n💡 本地模式，未上传 HDFS。如需上传，请加 --commit")
    else:
        log(f"👀 预览模式，未写入任何文件。")
        log(f"   如需写入本地:  python init_hdfs_state.py --local-only")
        log(f"   如需写入 HDFS: python init_hdfs_state.py --commit")
        log(f"   合并已有状态:  python init_hdfs_state.py --merge --commit")


if __name__ == "__main__":
    main()