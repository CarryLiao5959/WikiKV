#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
incr_pipeline.py — 增量运行脚本（定期执行）

逻辑：
1. 从 HDFS 读取上次处理状态（每个用户已处理过哪些文章的 source_id 集合）
2. 拉取所有已注册用户的文章列表
3. 对比找出新增文章
4. 对新增文章逐篇进行预处理 + 摄入
5. 对有实际摄入的用户执行收尾修复（finalize）
6. 对有实际摄入的用户同步到 KV
7. 将更新后的处理状态写回 HDFS

与 pipeline.py 的区别：
- pipeline.py 是全量构建（从零开始）
- incr_pipeline.py 是增量更新（只处理新文章）
- 不需要每15篇/30篇的定期维护（因为每次增量文章少，最后统一 finalize）
- 每次运行结束后对有摄入的用户执行一次 finalize 收尾

用法：
    python incr_pipeline.py

    python incr_pipeline.py --users tym,yusi

    python incr_pipeline.py --concurrency 10

    python incr_pipeline.py --dry-run

    python incr_pipeline.py --no-ingest

    python incr_pipeline.py --no-ingest

    python incr_pipeline.py --hdfs-state hdfs://path/to/incr_state.json

    python incr_pipeline.py --no-sync

    python incr_pipeline.py --model-profile auto
    python incr_pipeline.py --model-profile model1

    python incr_pipeline.py --fetch-workers 4

    python incr_pipeline.py --only-in-state

    python incr_pipeline.py --users tym --force-full

    python incr_pipeline.py --skip-fetch
"""

import argparse
import concurrent.futures
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

HADOOP_BIN = shutil.which("hadoop") or os.environ.get("HADOOP_BIN", "hadoop")
HDFS_BASE_PATH = os.environ.get("WIKI_HDFS_BASE", "hdfs:///llm_wiki")
DEFAULT_HDFS_STATE_PATH = f"{HDFS_BASE_PATH}/llm_wiki/incr_state.json"
LOCAL_STATE_FILE = BASE_DIR / ".incr-pipeline-state.json"


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)



def _check_hdfs_env() -> bool:
    """检查 HDFS 访问所需的环境变量和二进制是否就绪。"""
    if not os.path.exists(HADOOP_BIN):
        log(f"  ⚠️ hadoop 二进制不存在: {HADOOP_BIN}")
        return False
    tq_user = os.environ.get("TQ_USER_NAME", "")
    tq_token = os.environ.get("TQ_USER_TOKEN", "")
    if not tq_user or not tq_token:
        log(f"  ⚠️ Hadoop auth env not set (TQ_USER_NAME={tq_user or '(empty)'}, "
            f"TQ_USER_TOKEN={'set' if tq_token else '(empty)'})")
        return False
    return True


def _hdfs_get(hdfs_path: str, local_path: str) -> bool:
    """从 HDFS 下载文件到本地，返回是否成功。"""
    if not _check_hdfs_env():
        return False
    try:
        if os.path.exists(local_path):
            os.remove(local_path)
        result = subprocess.run(
            [HADOOP_BIN, "fs", "-get", hdfs_path, local_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True
        log(f"  HDFS get 失败: {result.stderr.strip()}")
    except Exception as e:
        log(f"  HDFS get 异常: {e}")
    return False


def _hdfs_put(local_path: str, hdfs_path: str) -> bool:
    """上传本地文件到 HDFS（覆盖），返回是否成功。"""
    if not _check_hdfs_env():
        return False
    try:
        hdfs_dir = hdfs_path.rsplit("/", 1)[0]
        subprocess.run(
            [HADOOP_BIN, "fs", "-mkdir", "-p", hdfs_dir],
            capture_output=True, text=True, timeout=60
        )
        subprocess.run(
            [HADOOP_BIN, "fs", "-rm", "-f", hdfs_path],
            capture_output=True, text=True, timeout=60
        )
        result = subprocess.run(
            [HADOOP_BIN, "fs", "-put", local_path, hdfs_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return True
        log(f"  HDFS put 失败: {result.stderr.strip()}")
    except Exception as e:
        log(f"  HDFS put 异常: {e}")
    return False



def load_incr_state(hdfs_path: str) -> dict:
    """加载增量处理状态。

    状态结构：
    {
        "updated_at": "2025-04-24 18:00:00",
        "users": {
            "tym": {
                "processed_source_ids": ["2024-01-01-xxx", ...],
                "last_run": "2025-04-24 18:00:00",
                "total_ingested": 500
            },
            ...
        }
    }

    优先从 HDFS 读取，失败则从本地文件读取。
    """
    if hdfs_path:
        local_tmp = str(LOCAL_STATE_FILE) + ".hdfs_tmp"
        if _hdfs_get(hdfs_path, local_tmp):
            try:
                with open(local_tmp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                log(f"  从 HDFS 加载增量状态: {len(data.get('users', {}))} 个用户")
                try:
                    os.rename(local_tmp, str(LOCAL_STATE_FILE))
                except OSError:
                    pass
                return data
            except (json.JSONDecodeError, IOError) as e:
                log(f"  HDFS 状态文件解析失败: {e}")
            finally:
                if os.path.exists(local_tmp):
                    try:
                        os.remove(local_tmp)
                    except OSError:
                        pass
        else:
            log(f"  HDFS 状态文件不存在或不可读: {hdfs_path}，尝试本地文件")

    if LOCAL_STATE_FILE.exists():
        try:
            data = json.loads(LOCAL_STATE_FILE.read_text(encoding="utf-8"))
            log(f"  从本地文件加载增量状态: {len(data.get('users', {}))} 个用户")
            return data
        except (json.JSONDecodeError, IOError):
            pass

    log(f"  无历史状态，将从现有 raw 目录初始化")
    return {"updated_at": "", "users": {}}


def save_incr_state(state: dict, hdfs_path: str):
    """保存增量处理状态到本地 + HDFS。"""
    state["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        LOCAL_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except IOError as e:
        log(f"  保存状态到本地失败: {e}")
        return

    if hdfs_path:
        if _hdfs_put(str(LOCAL_STATE_FILE), hdfs_path):
            log(f"  增量状态已同步到 HDFS: {hdfs_path}")
        else:
            log(f"  ⚠️ 增量状态同步到 HDFS 失败，仅保存在本地: {LOCAL_STATE_FILE}")


def init_state_from_existing(state: dict, user_keys: list[str]) -> dict:
    """从现有的 raw 目录初始化增量状态。

    对于已经全量构建过的用户，将其 raw 目录中所有文章的 source_id 标记为已处理，
    这样增量运行时只会处理新增文章。
    """
    for uk in user_keys:
        if uk in state.get("users", {}):
            continue  # 已有状态，不覆盖

        raw_dir = BASE_DIR / "raw" / uk / "articles"
        if not raw_dir.exists():
            continue

        existing_files = sorted(f.stem for f in raw_dir.glob("*.md"))
        if not existing_files:
            continue

        state.setdefault("users", {})[uk] = {
            "processed_source_ids": existing_files,
            "last_run": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "total_ingested": len(existing_files),
            "init_from": "existing_raw",
        }
        log(f"  📦 {uk}: 从现有 raw 目录初始化，{len(existing_files)} 篇已标记为已处理")

    return state



def fetch_and_detect_new(user_keys: list[str], state: dict,
                         fetch_workers: int = 7, force_full: bool = False) -> dict:
    """拉取文章并检测新增。

    流程：
    1. 从本地数据目录读取各用户的文章 CSV（由用户自行准备，见 README）
    2. 调用 preprocess.py 预处理 CSV → Markdown
    3. 对比 state 中的已处理列表，找出新增文章

    返回 {user_key: [new_article_paths]} — 每个用户的新增文章 md 文件路径列表。
    """
    import config as cfg

    user_uins = {}
    for uk in user_keys:
        if uk in cfg.USER_MAP:
            user_uins[uk] = cfg.USER_MAP[uk]["uin"]
        else:
            log(f"  ⚠️ 未知用户: {uk}，跳过")

    if not user_uins:
        return {}

    log(f"\n{'='*60}")
    log(f"  [Step 1] 读取 {len(user_uins)} 个用户的文章 CSV")
    log(f"{'='*60}")

    import shutil as _shutil
    wfs_csv_dir = Path(os.environ.get("WIKI_CSV_DIR", str(BASE_DIR / "data" / "csv")))
    local_data_dir = BASE_DIR / "data"
    local_data_dir.mkdir(parents=True, exist_ok=True)
    if wfs_csv_dir.exists():
        copied = 0
        for csv_file in wfs_csv_dir.glob("*.csv"):
            dst = local_data_dir / csv_file.name
            _shutil.copy2(csv_file, dst)
            copied += 1
        if copied:
            log(f"  📋 已从 WFS 复制 {copied} 个 CSV 文件到 {local_data_dir}")
        else:
            log(f"  ⚠️ WFS CSV 目录为空: {wfs_csv_dir}")
    else:
        log(f"  ⚠️ WFS CSV 目录不存在: {wfs_csv_dir}，preprocess 可能失败")

    log(f"\n{'='*60}")
    log(f"  [Step 2] 预处理文章")
    log(f"{'='*60}")

    preprocess_cmd = [
        sys.executable, str(BASE_DIR / "preprocess.py"),
        "--users", users_str,
    ]
    log(f"  🚀 执行: {' '.join(preprocess_cmd)}")
    result = subprocess.run(preprocess_cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        log(f"  ⚠️ 预处理失败 (exit code {result.returncode})，但继续检测新增")

    log(f"\n{'='*60}")
    log(f"  [Step 3] 检测新增文章")
    log(f"{'='*60}")

    new_articles = {}  # {user_key: [Path, ...]}

    for uk in user_uins:
        raw_dir = BASE_DIR / "raw" / uk / "articles"
        if not raw_dir.exists():
            log(f"  ⚠️ {uk}: raw 目录不存在: {raw_dir}")
            continue

        user_state = state.get("users", {}).get(uk, {})
        processed_ids = set(user_state.get("processed_source_ids", []))

        if force_full:
            processed_ids = set()  # 强制全量，忽略已处理记录

        all_files = sorted(raw_dir.glob("*.md"))
        new_for_user = []
        for f in all_files:
            if f.stem not in processed_ids:
                new_for_user.append(f)

        if new_for_user:
            new_articles[uk] = new_for_user
            log(f"  📰 {uk}: {len(new_for_user)} 篇新增 / {len(all_files)} 篇总计")
        else:
            log(f"  ✅ {uk}: 无新增文章 ({len(all_files)} 篇全部已处理)")

    return new_articles


def detect_new_from_existing(user_keys: list[str], state: dict,
                              force_full: bool = False) -> dict:
    """不拉取文章，直接从现有 raw 目录检测新增（--skip-fetch 模式）。

    返回 {user_key: [new_article_paths]}
    """
    new_articles = {}

    for uk in user_keys:
        raw_dir = BASE_DIR / "raw" / uk / "articles"
        if not raw_dir.exists():
            continue

        user_state = state.get("users", {}).get(uk, {})
        processed_ids = set(user_state.get("processed_source_ids", []))

        if force_full:
            processed_ids = set()

        all_files = sorted(raw_dir.glob("*.md"))
        new_for_user = [f for f in all_files if f.stem not in processed_ids]

        if new_for_user:
            new_articles[uk] = new_for_user
            log(f"  📰 {uk}: {len(new_for_user)} 篇新增 / {len(all_files)} 篇总计")
        else:
            log(f"  ✅ {uk}: 无新增文章 ({len(all_files)} 篇全部已处理)")

    return new_articles



def ingest_single_article(user_key: str, article_path: Path,
                          model_profile: str = None,
                          dry_run: bool = False) -> bool:
    """对单篇文章执行摄入（调用 main.py ingest 子命令）。

    返回是否成功。
    """
    cmd = [
        sys.executable, str(BASE_DIR / "main.py"),
        "--user", user_key,
    ]
    if model_profile:
        cmd.extend(["--model-profile", model_profile])
    cmd.extend(["ingest", str(article_path)])

    env = os.environ.copy()
    if not dry_run:
        env["WIKI_ENABLE_WFS"] = "1"

    result = subprocess.run(cmd, cwd=str(BASE_DIR), env=env)
    return result.returncode == 0


def ingest_new_articles(user_key: str, articles: list[Path],
                        model_profile: str = None,
                        skip_ingest: bool = False,
                        dry_run: bool = False) -> list[str]:
    """逐篇摄入新增文章。

    返回成功摄入的文章 source_id 列表。
    """
    success_ids = []
    total = len(articles)

    for i, article_path in enumerate(articles, 1):
        source_id = article_path.stem
        log(f"    [{i}/{total}] 摄入: {source_id}")

        if skip_ingest:
            log(f"    [SKIP-INGEST] 跳过实际摄入: {source_id}")
            continue

        ok = ingest_single_article(user_key, article_path, model_profile=model_profile, dry_run=dry_run)
        if ok:
            success_ids.append(source_id)
            log(f"    ✅ 成功: {source_id}")
        else:
            log(f"    ❌ 失败: {source_id}")

    return success_ids



_state_lock = threading.Lock()


def _ingest_user_worker(
    uk: str,
    articles: list[Path],
    model_profile: str,
    skip_ingest: bool,
    dry_run: bool,
    state: dict,
    hdfs_path: str,
    users_with_ingestion: list,
    ingestion_results: dict,
    no_finalize: bool = False,
    no_sync: bool = False,
    kv_concurrency: int = 5,
    kv_mode: str = "incremental",
) -> None:
    """单用户摄入 worker（在线程池中执行）。

    每个用户独立摄入自己的新增文章，通过子进程调用 main.py ingest，
    天然隔离全局变量（config.set_user 等）。
    完成后线程安全地更新共享状态。
    """
    import config as cfg

    log(f"\n{'─'*50}")
    log(f"  📝 摄入 {uk} ({cfg.USER_MAP[uk]['name']}): {len(articles)} 篇新增 [model: {model_profile}]")
    log(f"{'─'*50}")

    success_ids = ingest_new_articles(
        uk, articles,
        model_profile=model_profile,
        skip_ingest=skip_ingest,
        dry_run=dry_run,
    )

    with _state_lock:
        user_state = state.setdefault("users", {}).setdefault(uk, {
            "processed_source_ids": [],
            "last_run": "",
            "total_ingested": 0,
        })
        existing_ids = set(user_state.get("processed_source_ids", []))
        existing_ids.update(success_ids)
        user_state["processed_source_ids"] = sorted(existing_ids)
        user_state["last_run"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        user_state["total_ingested"] = user_state.get("total_ingested", 0) + len(success_ids)

        if skip_ingest:
            ingestion_results[uk] = {"success": 0, "failed": 0, "skipped": len(articles)}
            log(f"  ⏭️ {uk}: 跳过 {len(articles)} 篇（--no-ingest 模式）")
        else:
            failed_count = len(articles) - len(success_ids)
            ingestion_results[uk] = {"success": len(success_ids), "failed": failed_count}

            if success_ids:
                users_with_ingestion.append(uk)
                log(f"  📊 {uk}: 成功 {len(success_ids)} / 失败 {failed_count}")
            else:
                log(f"  ⚠️ {uk}: 全部失败 ({failed_count} 篇)")

            if not dry_run:
                save_incr_state(state, hdfs_path)

    if success_ids:
        if not no_finalize:
            run_finalize(uk, model_profile=model_profile, skip_ingest=skip_ingest, dry_run=dry_run)

        if not no_sync:
            sync_to_kv(uk, kv_concurrency=kv_concurrency,
                       skip_ingest=skip_ingest, dry_run=dry_run,
                       kv_mode=kv_mode)



def run_finalize(user_key: str, model_profile: str = None,
                 skip_ingest: bool = False,
                 dry_run: bool = False) -> bool:
    """对指定用户执行收尾修复。

    调用 main.py finalize 子命令，执行代码修复 ↔ 模型修复闭环循环。
    """
    if skip_ingest:
        log(f"  [NO-INGEST] 跳过 {user_key} 的收尾修复")
        return True

    if dry_run:
        log(f"  [DRY-RUN] 跳过 {user_key} 的收尾修复")
        return True

    cmd = [
        sys.executable, str(BASE_DIR / "main.py"),
        "--user", user_key,
    ]
    if model_profile:
        cmd.extend(["--model-profile", model_profile])
    cmd.extend(["finalize", "--max-rounds", "3"])

    env = os.environ.copy()
    env["WIKI_ENABLE_WFS"] = "1"

    log(f"  🔧 执行收尾修复: {user_key}")
    result = subprocess.run(cmd, cwd=str(BASE_DIR), env=env)
    if result.returncode == 0:
        log(f"  ✅ {user_key} 收尾修复完成")
        return True
    else:
        log(f"  ❌ {user_key} 收尾修复失败 (exit code {result.returncode})")
        return False



def sync_to_kv(user_key: str, kv_concurrency: int = 5,
               skip_ingest: bool = False,
               dry_run: bool = False,
               kv_mode: str = "incremental") -> bool:
    """将指定用户的 Wiki 同步到 KV 存储。

    kv_mode:
        - incremental: 默认，基于 KV sync state 做 diff，只推送变化
        - full:        全量推送但不清空 KV（适合首次/重置 state）
        - clean-rebuild: 先清空 KV 再全量推送（应急/--force-full 时使用）
    """
    wiki_dir = BASE_DIR / f"{user_key}_wiki" / "wiki"
    if not wiki_dir.exists():
        log(f"  ⚠️ {user_key} 的 wiki 目录不存在: {wiki_dir}，跳过同步")
        return False

    cmd = [
        sys.executable, str(BASE_DIR / "wiki_sync_kv.py"),
        "--user", user_key,
        "--wiki-dir", str(wiki_dir),
        "--concurrency", str(kv_concurrency),
        "--mode", kv_mode,
    ]

    if skip_ingest:
        log(f"  [NO-INGEST] 跳过 {user_key} 的 KV 同步")
        return True

    if dry_run:
        log(f"  [DRY-RUN] 跳过 {user_key} 的 KV 同步")
        return True

    log(f"  🚀 同步 {user_key} 到 KV (mode={kv_mode})...")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode == 0:
        log(f"  ✅ {user_key} KV 同步完成")
        return True
    else:
        log(f"  ❌ {user_key} KV 同步失败 (exit code {result.returncode})")
        return False



def main():
    parser = argparse.ArgumentParser(
        description="增量运行脚本：定期检测新文章 → 逐篇摄入 → 收尾修复 → KV 同步",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument("--users", "-u", type=str, default=None,
                        help="指定用户（逗号分隔），默认处理所有已注册用户")
    parser.add_argument("--only-in-state", action="store_true",
                        help="只处理 HDFS 状态文件中已有记录的用户（即已完成全量构建并初始化过状态的用户）")

    parser.add_argument("--hdfs-state", type=str, default=DEFAULT_HDFS_STATE_PATH,
                        help=f"HDFS 上的增量状态文件路径（默认: {DEFAULT_HDFS_STATE_PATH}）")
    parser.add_argument("--no-hdfs", action="store_true",
                        help="不使用 HDFS，仅使用本地状态文件")

    parser.add_argument("--skip-fetch", action="store_true",
                        help="跳过文章拉取和预处理（已有 raw 数据时使用）")
    parser.add_argument("--no-sync", action="store_true",
                        help="跳过 KV 同步步骤")
    parser.add_argument("--no-finalize", action="store_true",
                        help="跳过收尾修复步骤")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行：实际摄入和修复正常执行，但不写 HDFS/WFS/KV 状态")
    parser.add_argument("--no-ingest", action="store_true",
                        help="跳过实际摄入（不调用模型），同时跳过 finalize/KV/HDFS 写入")
    parser.add_argument("--force-full", action="store_true",
                        help="强制全量处理（忽略 HDFS 状态中的已处理记录）")
    parser.add_argument("--kv-mode", choices=["incremental", "full", "clean-rebuild", "auto"],
                        default="auto",
                        help="KV 同步模式（默认 auto：normal 时 incremental / --force-full 时 clean-rebuild）")

    parser.add_argument("--model-profile", "-m", default="auto",
                        choices=["model1", "model2", "auto"],
                        help="模型 profile（默认: auto，轮询分配 model1+model2）")
    parser.add_argument("--concurrency", "-c", type=int, default=6,
                        help="摄入阶段用户并发数（默认: 6，单篇模式比批处理可多并行）")
    parser.add_argument("--fetch-workers", type=int, default=9,
                        help="文章拉取并行进程数（默认: 9）")
    parser.add_argument("--kv-concurrency", type=int, default=5,
                        help="KV 同步并发数（默认: 5）")

    args = parser.parse_args()

    import config as cfg

    if args.users:
        user_keys = [u.strip() for u in args.users.split(",") if u.strip()]
        bad_keys = [k for k in user_keys if k not in cfg.USER_MAP]
        if bad_keys:
            log(f"❌ 未知用户: {', '.join(bad_keys)}")
            log(f"   可选: {', '.join(cfg.USER_MAP.keys())}")
            sys.exit(1)
    else:
        user_keys = list(cfg.USER_MAP.keys())

    hdfs_path = "" if args.no_hdfs else args.hdfs_state

    _pre_state = None
    if args.only_in_state:
        _pre_state = load_incr_state(hdfs_path)
        state_users = set(_pre_state.get("users", {}).keys())
        original_count = len(user_keys)
        user_keys = [uk for uk in user_keys if uk in state_users]
        skipped = original_count - len(user_keys)
        log(f"  🔍 --only-in-state: 保留 {len(user_keys)} 个有状态记录的用户"
            f"（跳过 {skipped} 个无状态用户）")
        if not user_keys:
            log(f"❌ 没有用户在 HDFS 状态中有记录，请先运行 init_hdfs_state.py")
            sys.exit(1)

    log("=" * 60)
    log("  🔄 LLM Wiki 增量运行")
    log("=" * 60)
    log(f"  用户: {', '.join(user_keys[:10])}{'...' if len(user_keys) > 10 else ''} ({len(user_keys)} 个)")
    log(f"  HDFS 状态: {hdfs_path or '(不使用 HDFS)'}")
    log(f"  摄入并发: {args.concurrency} 个用户 | 模型: {args.model_profile}")
    log(f"  步骤:")
    steps = []
    if not args.skip_fetch:
        steps.append("① 拉取文章 + 预处理")
    steps.append("② 检测新增 + 逐篇摄入")
    if not args.no_finalize:
        steps.append("③ 收尾修复 (finalize)")
    if not args.no_sync:
        steps.append("④ KV 同步")
    steps.append("⑤ 保存状态到 HDFS")
    for s in steps:
        log(f"    {s}")
    if args.dry_run:
        log(f"  ⚠️ 模拟运行模式（摄入/修复正常执行，不写 HDFS/WFS/KV 状态）")
    if args.no_ingest:
        log(f"  ⚠️ 跳过摄入模式（不调用模型，不写任何存储）")
    if args.force_full:
        log(f"  ⚠️ 强制全量模式（忽略已处理记录）")
    if args.only_in_state:
        log(f"  🔍 仅处理 HDFS 状态中已有记录的用户")
    log("")

    t_total_start = time.time()

    log("[0] 加载增量状态...")
    if _pre_state is not None:
        state = _pre_state
        log(f"  复用 --only-in-state 预加载的状态")
    else:
        state = load_incr_state(hdfs_path)

    state = init_state_from_existing(state, user_keys)

    if args.skip_fetch:
        log("\n[1] 跳过文章拉取（--skip-fetch），直接从 raw 目录检测新增...")
        new_articles = detect_new_from_existing(user_keys, state,
                                                 force_full=args.force_full)
    else:
        log("\n[1] 拉取文章并检测新增...")
        new_articles = fetch_and_detect_new(user_keys, state,
                                             fetch_workers=args.fetch_workers,
                                             force_full=args.force_full)

    total_new = sum(len(arts) for arts in new_articles.values())
    if total_new == 0:
        log(f"\n✅ 所有用户均无新增文章，本次增量运行结束")
        for uk in user_keys:
            state.setdefault("users", {}).setdefault(uk, {})["last_run"] = \
                datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if not args.dry_run:
            save_incr_state(state, hdfs_path)
        t_total = time.time() - t_total_start
        log(f"\n总耗时: {t_total:.1f}s")
        return

    log(f"\n📊 新增文章汇总: {len(new_articles)} 个用户共 {total_new} 篇新增")
    for uk, arts in new_articles.items():
        log(f"  {uk} ({cfg.USER_MAP[uk]['name']}): {len(arts)} 篇")

    concurrency = min(args.concurrency, len(new_articles))  # 不超过实际用户数
    log(f"\n{'='*60}")
    log(f"  [2] 逐篇增量摄入（{len(new_articles)} 个用户，并发: {concurrency}）")
    log(f"{'='*60}")

    model_profile = args.model_profile
    if model_profile == "auto":
        model_keys = list(cfg.MODEL_PROFILES.keys())  # ['model1', 'model2']
        user_model_map = {}
        for idx, uk in enumerate(new_articles.keys()):
            user_model_map[uk] = model_keys[idx % len(model_keys)]
        log(f"  🔀 auto 模式：轮询分配模型")
        for mk in model_keys:
            assigned = [uk for uk, m in user_model_map.items() if m == mk]
            if assigned:
                log(f"     {mk} ({cfg.MODEL_PROFILES[mk]['description']}): {', '.join(assigned)}")
    else:
        user_model_map = {uk: model_profile for uk in new_articles}
        if model_profile:
            log(f"  🔧 所有用户使用模型: {model_profile}")

    users_with_ingestion = []  # 有实际摄入的用户列表（线程安全，由 _state_lock 保护）
    ingestion_results = {}     # {user_key: {"success": N, "failed": N}}

    if args.kv_mode == "auto":
        kv_mode = "clean-rebuild" if args.force_full else "incremental"
    else:
        kv_mode = args.kv_mode
    log(f"  🔧 KV 同步模式: {kv_mode}")


    completed_count = 0
    total_users = len(new_articles)
    t_ingest_start = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {}
        for uk, articles in new_articles.items():
            profile = user_model_map.get(uk) or None
            future = executor.submit(
                _ingest_user_worker,
                uk, articles, profile, args.no_ingest, args.dry_run,
                state, hdfs_path, users_with_ingestion, ingestion_results,
                no_finalize=args.no_finalize,
                no_sync=args.no_sync,
                kv_concurrency=args.kv_concurrency,
                kv_mode=kv_mode,
            )
            futures[future] = uk

        for future in concurrent.futures.as_completed(futures):
            completed_count += 1
            uk = futures[future]
            elapsed = time.time() - t_ingest_start
            try:
                future.result()
                log(f"  📈 进度: {completed_count}/{total_users} "
                    f"({completed_count*100//total_users}%) | 已用 {elapsed:.0f}s")
            except Exception as e:
                log(f"  ❌ {uk} 摄入异常: {e} | "
                    f"进度: {completed_count}/{total_users}")

    if users_with_ingestion and not args.no_finalize:
        log(f"\n  ✅ [3] 收尾修复已在各用户摄入完成后立刻执行")
    elif not users_with_ingestion:
        log(f"\n[3] 无用户有实际摄入，跳过收尾修复")

    if users_with_ingestion and not args.no_sync:
        log(f"\n  ✅ [4] KV 同步已在各用户摄入完成后立刻执行")
    elif not users_with_ingestion:
        log(f"\n[4] 无用户有实际摄入，跳过 KV 同步")

    if not args.dry_run and not args.no_ingest:
        log(f"\n[5] 保存增量状态...")
        save_incr_state(state, hdfs_path)
    elif args.no_ingest:
        log(f"\n[5] --no-ingest 模式，跳过状态保存")

    t_total = time.time() - t_total_start
    log(f"\n{'='*60}")
    log(f"  📊 增量运行完成 | 总耗时: {t_total:.1f}s")
    log(f"{'='*60}")

    total_success = 0
    total_failed = 0
    total_skipped = 0
    for uk, result in ingestion_results.items():
        name = cfg.USER_MAP[uk]["name"]
        s, f = result["success"], result["failed"]
        skipped = result.get("skipped", 0)
        total_success += s
        total_failed += f
        total_skipped += skipped
        if skipped:
            log(f"  ⏭️ {uk} ({name}): 跳过 {skipped} 篇")
        else:
            icon = "✅" if f == 0 else "⚠️"
            finalized = "🔧" if uk in users_with_ingestion else ""
            log(f"  {icon} {uk} ({name}): 成功 {s} / 失败 {f} {finalized}")

    log(f"\n  总计: 成功 {total_success} / 失败 {total_failed} / 跳过 {total_skipped} / 新增 {total_new}")
    if users_with_ingestion:
        log(f"  收尾修复: {', '.join(users_with_ingestion)}")

    if total_failed > 0:
        log(f"\n⚠️ 有 {total_failed} 篇文章摄入失败，可通过 --force-full 重试")
        sys.exit(1)
    else:
        log(f"\n✅ 全部完成！")


if __name__ == "__main__":
    main()