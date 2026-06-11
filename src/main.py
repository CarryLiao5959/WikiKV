"""LLM Wiki 主入口 — CLI 工具。

用法:
    python main.py --user tym ingest <文件路径>             摄入单篇文章
    python main.py --user tym ingest-batch [--n 50]        批量摄入（自动选取种子文章）
    python main.py --user tym ingest-all [--force]         摄入所有文章
    python main.py --user tym ingest-all --limit 500       只摄入最近500篇文章
    python main.py --all-users ingest-all [--force]        所有author并行摄入（默认并发2）
    python main.py --all-users ingest-all --concurrency 3  控制同时跑几个author
    python main.py --all-users --model-profile auto ingest-all --limit 500 --concurrency 6 -y  自动轮询分配 model1+model2
    python main.py --user tym,yusi,xiyou ingest-all --concurrency 3 -y  指定用户子集并行摄入
    python main.py --user tym query "问题"                 查询 Wiki
    python main.py --user tym query "问题" --save          查询并保存结果到 Wiki
    python main.py --user tym lint                         健康检查
    python main.py --user tym lint --llm                   健康检查（含 LLM 矛盾检测）
    python main.py --user tym stats                        查看 Wiki 统计信息
    python main.py --user tym select [--n 50]              查看种子文章推荐列表
    python main.py --user tym errors                       查看错题本
    python main.py --user tym fix                          手动触发错题本 LLM 修复
    python main.py --user tym fix-pending                  归位各目录「## 待整理」
    python main.py --user tym maintain                     一键维护（推荐每轮 ingest 后跑一次）
    python main.py --user tym finalize                     收尾修复（摄入完成后跑，格式错误清零）
    python main.py --user tym finalize --max-rounds 5      收尾修复（最多5轮闭环）
    python main.py --user tym ledger-report [--days 30]    查看修复日志聚合报告
"""

import argparse
import json
import sys
import subprocess
import os
import time as _time_mod
import threading
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
else:
    sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)

import config



LOCK_STALE_SECONDS = 3600 * 6  # 锁超过 6 小时视为过期（stale）
LOCK_HEARTBEAT_INTERVAL = 60   # 每 60 秒刷新一次锁的 timestamp（心跳）
LOCK_STALE_THRESHOLD = LOCK_HEARTBEAT_INTERVAL * 3  # 180 秒


class _LockHeartbeat:
    """持锁期间在后台线程定期刷新锁文件的 timestamp，防止被误判为过期锁。"""

    def __init__(self, user_key: str):
        self._user_key = user_key
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _run(self):
        lock_file = _lock_path(self._user_key)
        while not self._stop_event.wait(timeout=LOCK_HEARTBEAT_INTERVAL):
            try:
                if lock_file.exists():
                    data = json.loads(lock_file.read_text(encoding="utf-8"))
                    if data.get("pid") == os.getpid():
                        data["timestamp"] = _time_mod.time()
                        lock_file.write_text(json.dumps(data), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass  # 刷新失败不影响主流程


def _lock_path(user_key: str) -> Path:
    """返回author的锁文件路径：.lock-{user_key}.ingest（放在项目根目录，避免创建空 wiki 目录）"""
    return config.BASE_DIR / f".lock-{user_key}.ingest"


def _acquire_lock(user_key: str) -> bool:
    """尝试获取author的锁。成功返回 True，已被其他活跃进程锁定返回 False。
    
    使用 O_CREAT | O_EXCL 原子创建文件，避免两个进程同时启动时的竞态条件（TOCTOU）。
    """
    lock_file = _lock_path(user_key)
    lock_data = {
        "pid": os.getpid(),
        "timestamp": _time_mod.time(),
        "user_key": user_key,
    }

    try:
        fd = os.open(str(lock_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(lock_data))
        return True
    except FileExistsError:
        pass  # 文件已存在，检查是否是过期锁

    try:
        existing = json.loads(lock_file.read_text(encoding="utf-8"))
        pid = existing.get("pid", 0)
        ts = existing.get("timestamp", 0)
        age = _time_mod.time() - ts

        if pid and _is_pid_alive(pid) and age < LOCK_STALE_THRESHOLD:
            return False  # 锁有效，获取失败

        print(f"  🔓 清理过期锁: {user_key} (pid={pid}, age={age:.0f}s)")
    except (json.JSONDecodeError, KeyError, OSError):
        pass  # 锁文件损坏，继续尝试替换

    tmp_file = lock_file.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp_file.write_text(json.dumps(lock_data), encoding="utf-8")
        tmp_file.rename(lock_file)
        verify = json.loads(lock_file.read_text(encoding="utf-8"))
        if verify.get("pid") != os.getpid():
            return False  # 被另一个进程抢先 rename 了
        return True
    except OSError:
        return False
    finally:
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass


def _release_lock(user_key: str):
    """释放author的锁。"""
    lock_file = _lock_path(user_key)
    try:
        if lock_file.exists():
            lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
            if lock_data.get("pid") == os.getpid():
                lock_file.unlink()
    except (json.JSONDecodeError, OSError):
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


def _is_locked(user_key: str) -> tuple[bool, dict]:
    """检查author是否被锁定。返回 (是否锁定, 锁信息)。"""
    lock_file = _lock_path(user_key)
    if not lock_file.exists():
        return False, {}
    try:
        lock_data = json.loads(lock_file.read_text(encoding="utf-8"))
        pid = lock_data.get("pid", 0)
        ts = lock_data.get("timestamp", 0)
        age = _time_mod.time() - ts

        if pid and _is_pid_alive(pid) and age < LOCK_STALE_THRESHOLD:
            lock_data["age_seconds"] = age
            return True, lock_data
        return False, {}
    except (json.JSONDecodeError, OSError):
        return False, {}


def _is_pid_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否还活着。"""
    try:
        os.kill(pid, 0)  # signal 0 不杀进程，只检查是否存在
        return True
    except (OSError, ProcessLookupError):
        return False


def _take_latest_n(article_paths: list[Path], n: int) -> list[Path]:
    """从文章列表中按 frontmatter date 排序，取最近 n 篇。
    
    文件名中 msgid 越大越新，但最可靠的方式是读 frontmatter 的 date 字段。
    如果没有 date 字段，则按文件名排序（msgid 越大越新）。
    返回的列表按时间从旧到新排列（与原始 sorted 顺序一致），方便后续按时间顺序摄入。
    """
    import re

    _date_re = re.compile(r'^date:\s*["\']?(.+?)["\']?\s*$', re.MULTILINE)

    def _extract_date(path: Path) -> str:
        """从 frontmatter 用正则提取 date，比 yaml.safe_load 快 10 倍以上。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(1024)
            if head.startswith("---"):
                end = head.find("---", 3)
                if end != -1:
                    fm_text = head[3:end]
                    m = _date_re.search(fm_text)
                    if m:
                        return m.group(1).strip()
        except Exception:
            pass
        return ""

    dated = [(path, _extract_date(path)) for path in article_paths]
    dated.sort(key=lambda x: x[1], reverse=True)
    selected = [p for p, _ in dated[:n]]
    selected.reverse()  # 恢复时间正序（旧→新）
    return selected


def _run_ingest_for_user(user_key: str, articles: list[Path], force: bool = False, model_profile: str | None = None, limit: int | None = None):
    """为单个author摄入全部文章（子进程，隔离全局变量）。"""
    if not _acquire_lock(user_key):
        locked, info = _is_locked(user_key)
        pid = info.get("pid", "?")
        age = info.get("age_seconds", 0)
        print(f"  🔒 跳过 {user_key}: 已被进程 {pid} 锁定 ({age:.0f}s 前)")
        return

    heartbeat = _LockHeartbeat(user_key)
    heartbeat.start()
    try:
        cmd = [sys.executable, __file__, "--user", user_key]
        if model_profile:
            cmd.extend(["--model-profile", model_profile])
        cmd.append("--_skip-lock")  # 父进程已持锁，子进程跳过加锁（必须在子命令之前）
        cmd.append("ingest-all")
        if force:
            cmd.append("--force")
        if limit:
            cmd.extend(["--limit", str(limit)])
        cmd.append("--yes")
        print(f"  🚀 启动子进程: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(config.BASE_DIR))
        if result.returncode == 0:
            print(f"  ✅ {user_key} 摄入完成")
        else:
            print(f"  ❌ {user_key} 摄入失败 (exit code {result.returncode})")
    finally:
        heartbeat.stop()
        _release_lock(user_key)


def _run_batch_for_user(user_key: str, n: int, force: bool = False, model_profile: str | None = None):
    """为单个author批量摄入 n 篇文章（子进程，隔离全局变量）。"""
    if not _acquire_lock(user_key):
        locked, info = _is_locked(user_key)
        pid = info.get("pid", "?")
        age = info.get("age_seconds", 0)
        print(f"  🔒 跳过 {user_key}: 已被进程 {pid} 锁定 ({age:.0f}s 前)")
        return

    heartbeat = _LockHeartbeat(user_key)
    heartbeat.start()
    try:
        cmd = [sys.executable, __file__, "--user", user_key]
        if model_profile:
            cmd.extend(["--model-profile", model_profile])
        cmd.extend(["ingest-batch", "--n", str(n), "--yes"])
        if force:
            cmd.append("--force")
        print(f"  🚀 启动子进程: {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=str(config.BASE_DIR))
        if result.returncode == 0:
            print(f"  ✅ {user_key} 批量摄入完成")
        else:
            print(f"  ❌ {user_key} 批量摄入失败 (exit code {result.returncode})")
    finally:
        heartbeat.stop()
        _release_lock(user_key)


def cmd_ingest(args):
    from ingest import ingest_article
    user_key = getattr(args, "user", None)
    if user_key:
        if not _acquire_lock(user_key):
            locked, info = _is_locked(user_key)
            print(f"  🔒 {user_key} 已被进程 {info.get('pid', '?')} 锁定 "
                  f"({info.get('age_seconds', 0):.0f}s 前)，请等待完成或手动清理锁文件")
            return
    try:
        path = Path(args.file)
        if not path.is_absolute():
            path = config.RAW_DIR / path
        if not path.exists():
            if not path.suffix:
                path = path.with_suffix(".md")
        result = ingest_article(path, force=args.force)
        if result:
            print(f"\n写入 {result.get('files', 0)} 个文件")
    finally:
        if user_key:
            _release_lock(user_key)


def cmd_ingest_batch(args):
    from ingest import select_seed_articles, ingest_batch

    _user_subset = getattr(args, "_user_subset", None)
    _parallel = args.all_users or _user_subset

    if _parallel:
        target_users = _user_subset if _user_subset else [k for k, v in config.USER_MAP.items() if not v.get("ablation")]
        concurrency = getattr(args, "concurrency", None) or len(target_users)
        all_users = target_users

        locked_users = []
        available_users = []
        for uk in all_users:
            locked, info = _is_locked(uk)
            if locked:
                locked_users.append((uk, info))
            else:
                available_users.append(uk)

        def _get_ingested_count_batch(user_key: str) -> int:
            cache_file = config.BASE_DIR / f".wiki-cache-{user_key}.json"
            if cache_file.exists():
                try:
                    return len(json.loads(cache_file.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass
            return 0

        available_set = set(available_users)
        all_users.sort(
            key=lambda uk: (_get_ingested_count_batch(uk) if uk in available_set else -1),
            reverse=True,
        )

        if locked_users:
            print(f"  🔒 以下author已被其他进程锁定，将自动跳过:")
            for uk, info in locked_users:
                print(f"     - {uk} (pid={info.get('pid', '?')}, 已运行 {info.get('age_seconds', 0):.0f}s)")

        print(f"多author并行批量摄入模式（每author {args.n} 篇，并发数: {concurrency}，按已摄入数优先排序）")
        print(f"  📊 共 {len(all_users)} 个author，可用 {len(available_users)} 个，"
              f"并发 {concurrency} 个")
        import concurrent.futures
        import time as _time

        current_profile = getattr(args, "model_profile", None)
        if current_profile == "auto":
            model_keys = list(config.MODEL_PROFILES.keys())
            model_queues = {mk: [] for mk in model_keys}
            for idx, uk in enumerate(all_users):
                mk = model_keys[idx % len(model_keys)]
                model_queues[mk].append(uk)
            print(f"  🔀 auto 模式：分队列调度（每个模型独立队列，保证并行）")
            for mk in model_keys:
                print(f"     {mk} ({config.MODEL_PROFILES[mk]['description']}): {', '.join(model_queues[mk])}")

            workers_per_model = max(1, concurrency // len(model_keys))
            print(f"  📊 每个模型并发槽: {workers_per_model}")

            completed_count = 0
            total_users = len(all_users)
            t_start = _time.time()
            _progress_lock = threading.Lock()

            def _run_model_queue_batch(model_key, user_keys):
                """单个模型的队列消费线程：用独立线程池串行/并行执行该模型的所有用户。"""
                nonlocal completed_count
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers_per_model) as pool:
                    futs = {}
                    for uk in user_keys:
                        fut = pool.submit(_run_batch_for_user, uk, args.n, args.force, model_key)
                        futs[fut] = uk
                    for fut in concurrent.futures.as_completed(futs):
                        uk = futs[fut]
                        with _progress_lock:
                            completed_count += 1
                            elapsed = _time.time() - t_start
                        try:
                            fut.result()
                            print(f"  📈 进度: {completed_count}/{total_users} "
                                  f"({completed_count*100//total_users}%) | 已用 {elapsed:.0f}s")
                        except Exception as e:
                            print(f"  ❌ {uk} 批量摄入异常: {e} | "
                                  f"进度: {completed_count}/{total_users}")

            model_threads = []
            for mk in model_keys:
                if model_queues[mk]:  # 跳过空队列
                    t = threading.Thread(target=_run_model_queue_batch, args=(mk, model_queues[mk]))
                    t.start()
                    model_threads.append(t)
            for t in model_threads:
                t.join()
        else:
            completed_count = 0
            total_users = len(all_users)
            t_start = _time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {}
                for user_key in all_users:
                    profile = config.get_model_profile()
                    future = executor.submit(_run_batch_for_user, user_key, args.n, args.force, profile)
                    futures[future] = user_key
                for future in concurrent.futures.as_completed(futures):
                    completed_count += 1
                    user_key = futures[future]
                    elapsed = _time.time() - t_start
                    try:
                        future.result()
                        print(f"  📈 进度: {completed_count}/{total_users} "
                              f"({completed_count*100//total_users}%) | 已用 {elapsed:.0f}s")
                    except Exception as e:
                        print(f"  ❌ {user_key} 批量摄入异常: {e} | "
                              f"进度: {completed_count}/{total_users}")
        return

    user_key = getattr(args, "user", None)
    if user_key:
        if not _acquire_lock(user_key):
            locked, info = _is_locked(user_key)
            print(f"  🔒 {user_key} 已被进程 {info.get('pid', '?')} 锁定 "
                  f"({info.get('age_seconds', 0):.0f}s 前)，请等待完成或手动清理锁文件")
            return
    try:
        articles = select_seed_articles(args.n)
        if not articles:
            print("未找到可摄入的文章")
            return
        print(f"\n即将摄入 {len(articles)} 篇文章")
        if not args.yes:
            confirm = input("继续？(y/n) ")
            if confirm.lower() != 'y':
                print("已取消")
                return
        ingest_batch(articles, force=args.force, batch_size=args.batch_size)
    finally:
        if user_key:
            _release_lock(user_key)


def cmd_ingest_all(args):
    _user_subset = getattr(args, "_user_subset", None)
    _parallel = args.all_users or _user_subset

    if _parallel:
        target_users = _user_subset if _user_subset else [k for k, v in config.USER_MAP.items() if not v.get("ablation")]
        concurrency = getattr(args, "concurrency", None) or len(target_users)
        if _user_subset:
            print(f"指定用户并行摄入模式（{len(target_users)} 个用户，并发数: {concurrency}）")
        else:
            print(f"多author并行摄入模式（并发数: {concurrency}）")
        user_articles = {}
        for user_key in target_users:
            raw_dir = config.BASE_DIR / "raw" / user_key / "articles"
            if not raw_dir.exists():
                raw_dir = config.BASE_DIR / "raw" / "articles"
            articles = sorted(raw_dir.glob("*.md")) if raw_dir.exists() else []
            if args.limit and articles:
                articles = _take_latest_n(articles, args.limit)
            if articles:
                user_articles[user_key] = articles
            else:
                print(f"  ⚠️ {user_key} 未找到文章（检查 raw/{user_key}/articles/ 或 raw/articles/）")

        if not user_articles:
            print("未找到任何author的文章")
            return

        locked_users = []
        available_articles = {}
        for uk, arts in user_articles.items():
            locked, info = _is_locked(uk)
            if locked:
                locked_users.append((uk, info))
            else:
                available_articles[uk] = arts

        def _get_ingested_count(user_key: str) -> int:
            """读取author的缓存文件，返回已摄入文章数。"""
            cache_file = config.BASE_DIR / f".wiki-cache-{user_key}.json"
            if cache_file.exists():
                try:
                    return len(json.loads(cache_file.read_text(encoding="utf-8")))
                except (json.JSONDecodeError, OSError):
                    pass
            return 0

        sorted_users = sorted(
            user_articles.keys(),
            key=lambda uk: (_get_ingested_count(uk) if uk in available_articles else -1),
            reverse=True,
        )
        user_articles = {uk: user_articles[uk] for uk in sorted_users}

        print(f"找到 {len(user_articles)} 个author的文章（按已摄入数优先排序）:")
        for uk, arts in user_articles.items():
            locked, _ = _is_locked(uk)
            lock_tag = " 🔒" if locked else ""
            ingested = _get_ingested_count(uk)
            progress = f" [已摄入 {ingested}]" if ingested > 0 else ""
            print(f"  {uk} ({config.USER_MAP[uk]['name']}): {len(arts)} 篇{progress}{lock_tag}")

        if locked_users:
            print(f"  🔒 以下author已被其他进程锁定，将自动跳过:")
            for uk, info in locked_users:
                print(f"     - {uk} (pid={info.get('pid', '?')}, 已运行 {info.get('age_seconds', 0):.0f}s)")

        print(f"  📊 共 {len(user_articles)} 个author，可用 {len(available_articles)} 个，"
              f"并发 {concurrency} 个")

        if not args.yes:
            confirm = input(f"并行摄入全部 {sum(len(v) for v in user_articles.values())} 篇？(y/n) ")
            if confirm.lower() != 'y':
                print("已取消")
                return

        import concurrent.futures
        import time as _time

        current_profile = getattr(args, "model_profile", None)
        if current_profile == "auto":
            model_keys = list(config.MODEL_PROFILES.keys())  # ['model1', 'model2']
            model_queues = {mk: [] for mk in model_keys}
            for idx, uk in enumerate(user_articles.keys()):
                mk = model_keys[idx % len(model_keys)]
                model_queues[mk].append(uk)
            print(f"  🔀 auto 模式：分队列调度（每个模型独立队列，保证并行）")
            for mk in model_keys:
                print(f"     {mk} ({config.MODEL_PROFILES[mk]['description']}): {', '.join(model_queues[mk])}")

            workers_per_model = max(1, concurrency // len(model_keys))
            print(f"  📊 每个模型并发槽: {workers_per_model}")

            completed_count = 0
            total_users = len(user_articles)
            t_start = _time.time()
            _progress_lock = threading.Lock()

            def _run_model_queue_ingest(model_key, user_keys):
                """单个模型的队列消费线程：用独立线程池执行该模型的所有用户。"""
                nonlocal completed_count
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers_per_model) as pool:
                    futs = {}
                    for uk in user_keys:
                        arts = user_articles[uk]
                        fut = pool.submit(_run_ingest_for_user, uk, arts, args.force, model_key, args.limit)
                        futs[fut] = uk
                    for fut in concurrent.futures.as_completed(futs):
                        uk = futs[fut]
                        with _progress_lock:
                            completed_count += 1
                            elapsed = _time.time() - t_start
                        try:
                            fut.result()
                            print(f"  📈 进度: {completed_count}/{total_users} "
                                  f"({completed_count*100//total_users}%) | 已用 {elapsed:.0f}s")
                        except Exception as e:
                            print(f"  ❌ {uk} 摄入异常: {e} | "
                                  f"进度: {completed_count}/{total_users}")

            model_threads = []
            for mk in model_keys:
                if model_queues[mk]:  # 跳过空队列
                    t = threading.Thread(target=_run_model_queue_ingest, args=(mk, model_queues[mk]))
                    t.start()
                    model_threads.append(t)
            for t in model_threads:
                t.join()
        else:
            completed_count = 0
            total_users = len(user_articles)
            t_start = _time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {}
                for user_key, articles in user_articles.items():
                    profile = config.get_model_profile()
                    future = executor.submit(_run_ingest_for_user, user_key, articles, args.force, profile, args.limit)
                    futures[future] = user_key

                for future in concurrent.futures.as_completed(futures):
                    completed_count += 1
                    user_key = futures[future]
                    elapsed = _time.time() - t_start
                    try:
                        future.result()
                        print(f"  📈 进度: {completed_count}/{total_users} "
                              f"({completed_count*100//total_users}%) | 已用 {elapsed:.0f}s")
                    except Exception as e:
                        print(f"  ❌ {user_key} 摄入异常: {e} | "
                              f"进度: {completed_count}/{total_users}")
    else:
        skip_lock = getattr(args, "_skip_lock", False)
        user_key = getattr(args, "user", None)
        if user_key and not skip_lock:
            if not _acquire_lock(user_key):
                locked, info = _is_locked(user_key)
                print(f"  🔒 {user_key} 已被进程 {info.get('pid', '?')} 锁定 "
                      f"({info.get('age_seconds', 0):.0f}s 前)，请等待完成或手动清理锁文件")
                return
        try:
            articles = sorted(config.RAW_DIR.glob("*.md"))
            if not articles:
                print(f"在 {config.RAW_DIR} 中未找到文章")
                return
            if args.limit:
                articles = _take_latest_n(articles, args.limit)
            print(f"找到 {len(articles)} 篇文章")
            if not args.yes:
                confirm = input(f"即将摄入全部 {len(articles)} 篇，继续？(y/n) ")
                if confirm.lower() != 'y':
                    print("已取消")
                    return
            from ingest import ingest_batch
            ingest_batch(articles, force=args.force, batch_size=args.batch_size)
        finally:
            if user_key and not skip_lock:
                _release_lock(user_key)


def cmd_query(args):
    from query import query_wiki
    result = query_wiki(args.question, save_to_wiki=args.save)
    print(f"\n{'='*60}")
    print(f"问题: {args.question}")
    print(f"{'='*60}")
    print(f"\n{result.get('answer', '无结果')}")
    print(f"\n置信度: {result.get('confidence', '?')}")
    cited = result.get("cited_pages", [])
    if cited:
        print(f"引用页面: {', '.join(cited)}")
    gaps = result.get("gaps", [])
    if gaps:
        print(f"知识缺口: {', '.join(gaps)}")


def cmd_lint(args):
    from lint import lint_wiki, print_lint_report, save_lint_report
    report = lint_wiki(use_llm=args.llm)
    print_lint_report(report)
    save_lint_report(report)


def cmd_consolidate(args):
    from lint import consolidate_wiki
    from config import load_cache
    total_ingested = len(load_cache())
    result = consolidate_wiki(dry_run=args.dry_run, total_ingested=total_ingested)
    status = result.get("status", "")
    if status == "skipped":
        print(f"⏭️ 跳过: {result['reason']}")
    elif status == "no_changes":
        print("✅ 目录结构无需优化")
    elif status == "dry_run":
        print("\n以上为建议，使用 `consolidate`（不带 --dry-run）执行")
    elif status == "executed":
        suggestions = result.get("suggestions", [])
        print(f"\n✅ 已执行 {len(suggestions)} 项目录优化")
        for s in suggestions:
            print(f"  - [{s['action']}] {s.get('from', '')} → {s.get('to', '')}: {s.get('reason', '')}")


def cmd_stats(args):
    from wiki_page import WikiGraph
    graph = WikiGraph()
    graph.load_all()
    stats = graph.stats

    print(f"{'='*50}")
    print(f"  Wiki 统计信息")
    print(f"{'='*50}")
    print(f"  总页面数: {stats['total_pages']}")
    for ptype, count in sorted(stats["by_type"].items()):
        print(f"    {ptype}: {count}")
    print(f"  总链接数: {stats['total_links']}")
    print(f"  平均链接: {stats['avg_links']:.1f}")
    print(f"  孤立页面: {stats['orphans']}")
    print(f"  断裂链接: {stats['broken_links']}")

    raw_count = len(list(config.RAW_DIR.glob("*.md"))) if config.RAW_DIR.exists() else 0
    print(f"\n  Raw 文章数: {raw_count}")

    if config.CACHE_FILE.exists():
        try:
            cache = json.loads(config.CACHE_FILE.read_text(encoding="utf-8"))
            print(f"  已摄入数: {len(cache)}")
            print(f"  待摄入数: {raw_count - len(cache)}")
        except (json.JSONDecodeError, OSError):
            print(f"  已摄入数: (缓存文件损坏)")


def cmd_select(args):
    from ingest import select_seed_articles
    select_seed_articles(args.n)


def cmd_errors(args):
    from error_book import print_error_book, add_error_manually
    if args.add:
        desc, constraint = args.add
        add_error_manually(desc, constraint)
    else:
        print_error_book()


def cmd_fix(args):
    """手动触发错题本修复（断链修复、摘要补全等）。"""
    from ingest import (
        fix_broken_links_from_error_book,
        fix_incomplete_digests,
        fix_missing_source_article_from_error_book,
        fix_missing_summary,
        fix_related_source_format,
        fix_missing_sections,
        _fix_digest_article_links,
    )
    print("🔧 手动触发错题本修复...")
    try:
        _fix_digest_article_links()
    except Exception as e:
        print(f"  ⚠️ digest article 链接修正失败: {e}")
    try:
        fix_incomplete_digests()
    except Exception as e:
        print(f"  ⚠️ 摘要补全失败: {e}")
    try:
        fix_missing_source_article_from_error_book()
    except Exception as e:
        print(f"  ⚠️ source_article 回填失败: {e}")
    try:
        fix_missing_summary()
    except Exception as e:
        print(f"  ⚠️ 一句话概括修复失败: {e}")
    try:
        fix_broken_links_from_error_book()
    except Exception as e:
        print(f"  ⚠️ 断链修复失败: {e}")
    try:
        fix_related_source_format()
    except Exception as e:
        print(f"  ⚠️ 相关来源修复失败: {e}")
    try:
        fix_missing_sections()
    except Exception as e:
        print(f"  ⚠️ 知识页章节补全失败: {e}")
    print("✅ 修复完成")


def cmd_fix_pending(args):
    """把各目录 _index.md 的「## 待整理」条目归入已有分区。"""
    from lint import relocate_pending_entries
    result = relocate_pending_entries(dry_run=args.dry_run, batch_size=args.batch_size)
    mode = "dry-run" if args.dry_run else "已写入"
    print("\n" + "=" * 60)
    print(f"  🗂 待整理归位总结（{mode}）")
    print("=" * 60)
    print(f"处理目录数: {result['dirs_processed']}")
    print(f"归入已有分区: {result['moved']} 条")
    print(f"新建分区: {result['new_sections']} 个")
    print(f"保留在「待整理」: {result['left_pending']} 条")


def cmd_merge_sections(args):
    """合并 _index.md 中过小的分区，降低分类碎片化。"""
    from lint import merge_small_sections
    result = merge_small_sections(
        dry_run=args.dry_run,
        min_items=args.min_items,
        max_sections=args.max_sections,
    )
    mode = "dry-run" if args.dry_run else "已写入"
    print("\n" + "=" * 60)
    print(f"  🔀 分区合并总结（{mode}）")
    print("=" * 60)
    print(f"处理目录数: {result['dirs_processed']}")
    print(f"合并分区数: {result['sections_merged']}")
    print(f"移动条目数: {result['entries_moved']}")


def cmd_finalize(args):
    """收尾修复：代码修复 ↔ 模型修复闭环循环，目标是格式错误清零。

    典型使用场景：
    - 一个author所有文章摄入完成后，手动跑一次确保质量达标
    - 对已构建完的author做最终质量检查和修复

    与 maintain 的区别：
    - maintain 是日常维护（跑一遍 lint+fix）
    - finalize 是终态收尾（闭环循环直到格式错误清零或达到最大轮数）
    """
    from lint import (
        quick_lint, print_quick_lint, auto_fix_all,
        relocate_pending_entries, merge_small_sections,
        detect_alias_overlaps,
    )
    from error_book import record_lint_issues, print_error_book, has_unfixed_samples
    from ingest import (
        fix_broken_links_from_error_book,
        fix_incomplete_digests,
        fix_missing_source_article_from_error_book,
        fix_missing_summary,
        fix_related_source_format,
        fix_missing_sections,
        _fix_digest_article_links,
        merge_duplicate_pages,
        _rebuild_global_index,
    )

    max_rounds = getattr(args, "max_rounds", 3)
    skip_consolidate = getattr(args, "skip_consolidate", False)

    print("\n" + "=" * 60)
    print(f"  🏁 收尾修复（最多 {max_rounds} 轮闭环）")
    print("=" * 60)

    for round_idx in range(1, max_rounds + 1):
        print(f"\n{'─'*50}")
        print(f"  🔄 第 {round_idx}/{max_rounds} 轮")
        print(f"{'─'*50}")

        print(f"\n  [1] 快速 lint...")
        issues = quick_lint()
        if issues:
            total_issues = sum(len(v) for v in issues.values())
            print(f"  📋 发现 {total_issues} 个问题")
            print_quick_lint(issues)
            try:
                record_lint_issues(issues)
            except Exception as e:
                print(f"  ⚠️ 错题登记失败: {e}")
        else:
            print(f"  ✅ 无格式错误")

        print(f"\n  [2] 代码自动修复...")
        code_fixed = 0
        try:
            fixes = auto_fix_all()
            code_fixed = sum(fixes.values())
            if code_fixed > 0:
                parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
                print(f"  🔧 修复 {code_fixed} 个 ({', '.join(parts)})")
            else:
                print(f"  ✅ 无需代码修复")
        except Exception as e:
            print(f"  ⚠️ 代码修复失败: {e}")

        print(f"\n  [3] 模型修复...")
        model_fixed = 0
        for name, fn in [
            ("digest article 链接修正", _fix_digest_article_links),
            ("digest 章节补全", fix_incomplete_digests),
            ("source_article 回填", fix_missing_source_article_from_error_book),
            ("知识页一句话概括", fix_missing_summary),
            ("相关来源补回", fix_related_source_format),
            ("知识页章节补全", fix_missing_sections),
            ("断链创建缺失页", fix_broken_links_from_error_book),
        ]:
            try:
                result = fn()
                if isinstance(result, int) and result > 0:
                    model_fixed += result
                elif isinstance(result, bool) and result:
                    model_fixed += 1
            except Exception as e:
                print(f"  ⚠️ {name} 失败: {e}")

        print(f"\n  [4] 待整理归位...")
        try:
            result = relocate_pending_entries(dry_run=False, batch_size=40)
            moved = result.get("moved", 0)
            if moved > 0:
                print(f"  归入 {moved} 条 / 新建 {result['new_sections']} 个分区")
                model_fixed += moved
            else:
                print(f"  ✅ 无待整理条目")
        except Exception as e:
            print(f"  ⚠️ 归位失败: {e}")

        actionable_issues = {k: v for k, v in issues.items()
                             if k not in ("orphan_pages", "stale_pages", "missing_content")} if issues else {}
        if code_fixed == 0 and model_fixed == 0 and not actionable_issues:
            if issues:
                info_only = sum(len(v) for v in issues.values())
                print(f"\n  ✅ 第 {round_idx} 轮无新修复，剩余 {info_only} 个信息性问题（孤立页面等），收尾完成 🎉")
            else:
                print(f"\n  ✅ 第 {round_idx} 轮无新修复且无错误，收尾完成 🎉")
            break

    if not skip_consolidate:
        print(f"\n  🔄 收尾整理...")
        try:
            merge_duplicate_pages()
        except Exception as e:
            print(f"  ⚠️ 知识页合并失败: {e}")
        try:
            detect_alias_overlaps()
        except Exception as e:
            print(f"  ⚠️ 别名检测失败: {e}")
        try:
            merge_result = merge_small_sections(dry_run=False)
            if merge_result.get("sections_merged", 0) > 0:
                print(f"  🔀 合并 {merge_result['sections_merged']} 个小分区")
        except Exception as e:
            print(f"  ⚠️ 分区合并失败: {e}")
        try:
            from lint import consolidate_wiki
            from ingest import load_cache
            result = consolidate_wiki(dry_run=False, total_ingested=len(load_cache()))
            status = result.get("status", "")
            if status == "executed":
                print(f"  ✅ 目录优化完成")
            elif status == "no_changes":
                print(f"  ✅ 目录结构无需优化")
        except Exception as e:
            print(f"  ⚠️ 目录优化失败: {e}")
        try:
            _rebuild_global_index(update_overview=True)
            print(f"  ✅ 知识概览已更新")
        except Exception as e:
            print(f"  ⚠️ 知识概览更新失败: {e}")

    print(f"\n{'─'*50}")
    print(f"  📊 最终检查")
    print(f"{'─'*50}")
    final_issues = quick_lint()
    if final_issues:
        total_remaining = sum(len(v) for v in final_issues.values())
        print(f"  ⚠️ 仍有 {total_remaining} 个问题（可能需要人工介入或属于主观判断类）:")
        print_quick_lint(final_issues)
        print(f"\n  🔧 尝试修复收尾整理引入的问题...")
        try:
            fixes = auto_fix_all()
            fix_count = sum(fixes.values())
            if fix_count > 0:
                parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
                print(f"  🔧 修复 {fix_count} 个 ({', '.join(parts)})")
        except Exception as e:
            print(f"  ⚠️ 修复失败: {e}")
        final_issues = quick_lint()
        if final_issues:
            total_remaining = sum(len(v) for v in final_issues.values())
            print(f"  ⚠️ 仍有 {total_remaining} 个问题（可能需要人工介入或属于主观判断类）:")
            print_quick_lint(final_issues)
        else:
            print(f"  ✅ 零格式错误 🎉")
    else:
        print(f"  ✅ 零格式错误 🎉")

    print(f"\n{'='*60}")
    print(f"  📔 最终错题本状态")
    print(f"{'='*60}")
    try:
        print_error_book()
    except Exception as e:
        print(f"  ⚠️ 打印错题本失败: {e}")


def cmd_maintain(args):
    """一键维护：lint → fix-pending → fix (错题本所有修复器)。

    典型使用场景：
    - 一轮 ingest 后手动跑一次，确保错题本里所有 unfixed samples 都被处理
    - 定期（例如每周）跑一次，兜底清理
    """
    from lint import (
        quick_lint, print_quick_lint, auto_fix_all,
        relocate_pending_entries, merge_small_sections,
    )
    from error_book import record_lint_issues, print_error_book
    from ingest import (
        fix_broken_links_from_error_book,
        fix_incomplete_digests,
        fix_missing_source_article_from_error_book,
        fix_missing_summary,
        fix_related_source_format,
        fix_missing_sections,
        _fix_digest_article_links,
    )

    print("\n" + "=" * 60)
    print("  🛠  一键维护：lint → 错题登记 → 自动修复 → 错题本 LLM 修复 → 归位 → 分区合并")
    print("=" * 60)

    print("\n[1/5] 快速 lint...")
    issues = quick_lint()
    print_quick_lint(issues)

    if issues:
        print("\n[2/5] 错题本登记...")
        try:
            record_lint_issues(issues)
        except Exception as e:
            print(f"  ⚠️ 错题登记失败: {e}")
    else:
        print("\n[2/5] 无新问题，跳过错题登记")

    print("\n[3/5] 代码自动修复...")
    try:
        fixes = auto_fix_all()
        total = sum(fixes.values())
        if total > 0:
            parts = [f"{k}={v}" for k, v in fixes.items() if v > 0]
            print(f"  ✅ 已修复 {total} 个问题 ({', '.join(parts)})")
        else:
            print("  ✅ 无需代码修复")
    except Exception as e:
        print(f"  ⚠️ auto_fix_all 失败: {e}")

    print("\n[4/5] 错题本 LLM 修复...")
    for name, fn in [
        ("digest article 链接修正", _fix_digest_article_links),
        ("digest 章节补全", fix_incomplete_digests),
        ("source_article 回填", fix_missing_source_article_from_error_book),
        ("知识页一句话概括", fix_missing_summary),
        ("相关来源补回", fix_related_source_format),
        ("知识页章节补全", fix_missing_sections),
        ("断链创建缺失页", fix_broken_links_from_error_book),
    ]:
        try:
            fn()
        except Exception as e:
            print(f"  ⚠️ {name} 失败: {e}")

    print("\n[5/6] 「## 待整理」归位...")
    try:
        result = relocate_pending_entries(dry_run=False, batch_size=40)
        print(f"  归入已有分区 {result['moved']} 条 / 新建分区 {result['new_sections']} 个 / "
              f"保留 {result['left_pending']} 条")
    except Exception as e:
        print(f"  ⚠️ 归位失败: {e}")

    print("\n[6/6] 分区合并...")
    try:
        merge_result = merge_small_sections(dry_run=False)
        if merge_result.get("sections_merged", 0) > 0:
            print(f"  🔀 合并 {merge_result['sections_merged']} 个小分区，移动 {merge_result['entries_moved']} 条")
        else:
            print("  ✅ 无需合并")
    except Exception as e:
        print(f"  ⚠️ 分区合并失败: {e}")

    print("\n" + "=" * 60)
    print("  📔 维护后错题本状态")
    print("=" * 60)
    try:
        print_error_book()
    except Exception as e:
        print(f"  ⚠️ 打印错题本失败: {e}")


def cmd_ledger_report(args):
    """查看修复日志聚合报告。"""
    from error_book import print_ledger_report
    print_ledger_report(days=args.days)


def main():
    parser = argparse.ArgumentParser(description="LLM Wiki — 个人知识库构建工具")
    parser.add_argument("--user", "-u", help="用户标识（tym/yusi），支持逗号分隔多个author如 tym,yusi,xiyou")
    parser.add_argument("--all-users", action="store_true", help="所有author并行处理（ingest-batch/ingest-all 支持）")
    parser.add_argument("--model-profile", "-m", choices=["model1", "model2", "model3", "model4", "model5", "auto"],
                        help="强模型 profile：model1~model5 指定单个，auto=并行时自动轮询分配")
    parser.add_argument("--_skip-lock", action="store_true", help="跳过加锁（内部使用）")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_ingest = subparsers.add_parser("ingest", help="摄入单篇文章")
    p_ingest.add_argument("file", help="文章文件路径")
    p_ingest.add_argument("--force", action="store_true", help="强制重新摄入")

    p_batch = subparsers.add_parser("ingest-batch", help="批量摄入种子文章")
    p_batch.add_argument("--n", type=int, default=50, help="选取文章数量")
    p_batch.add_argument("--force", action="store_true", help="强制重新摄入")
    p_batch.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p_batch.add_argument("--batch-size", "-b", type=int, default=3, help="每批合并文章数（默认3）")
    p_batch.add_argument("--concurrency", "-c", type=int, default=16, help="--all-users 模式下同时跑几个author（默认16，完成一个自动接下一个）")

    p_all = subparsers.add_parser("ingest-all", help="摄入所有文章")
    p_all.add_argument("--force", action="store_true", help="强制重新摄入")
    p_all.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p_all.add_argument("--batch-size", "-b", type=int, default=3, help="每批合并文章数（默认3）")
    p_all.add_argument("--limit", "-l", type=int, default=None, help="只摄入最近 N 篇文章（按日期排序，不指定则摄入全部）")
    p_all.add_argument("--concurrency", "-c", type=int, default=16, help="--all-users 模式下同时跑几个author（默认16，完成一个自动接下一个）")

    p_query = subparsers.add_parser("query", help="查询 Wiki")
    p_query.add_argument("question", help="查询问题")
    p_query.add_argument("--save", action="store_true", help="保存查询结果到 Wiki")

    p_lint = subparsers.add_parser("lint", help="Wiki 健康检查")
    p_lint.add_argument("--llm", action="store_true", help="使用 LLM 检测矛盾")

    p_consolidate = subparsers.add_parser("consolidate", help="目录结构优化（LLM 审计+执行）")
    p_consolidate.add_argument("--dry-run", action="store_true", help="只展示建议，不执行")

    subparsers.add_parser("stats", help="查看 Wiki 统计信息")

    p_select = subparsers.add_parser("select", help="查看种子文章推荐")
    p_select.add_argument("--n", type=int, default=50, help="推荐数量")

    p_errors = subparsers.add_parser("errors", help="查看/管理错题本")
    p_errors.add_argument("--add", nargs=2, metavar=("DESC", "CONSTRAINT"), help="手动添加错题")

    subparsers.add_parser("fix", help="手动触发错题本修复（断链修复、摘要补全）")

    p_pending = subparsers.add_parser("fix-pending", help="把各目录 _index.md 的「## 待整理」条目归入已有分区")
    p_pending.add_argument("--dry-run", action="store_true", help="只展示计划，不写入")
    p_pending.add_argument("--batch-size", type=int, default=40, help="每批提交给 LLM 的条目数（默认 40）")

    p_merge = subparsers.add_parser("merge-sections", help="合并 _index.md 中过小的分区，降低分类碎片化")
    p_merge.add_argument("--dry-run", action="store_true", help="只展示计划，不写入")
    p_merge.add_argument("--min-items", type=int, default=2, help="少于此数的分区视为'小分区'（默认 2）")
    p_merge.add_argument("--max-sections", type=int, default=15, help="每个目录最多保留的分区数（默认 15）")

    subparsers.add_parser("maintain", help="一键维护：lint→登记错题→代码自动修复→错题本 LLM 修复→待整理归位")

    p_finalize = subparsers.add_parser("finalize", help="收尾修复：代码↔模型闭环循环，格式错误清零")
    p_finalize.add_argument("--max-rounds", type=int, default=3, help="最大闭环轮数（默认 3）")
    p_finalize.add_argument("--skip-consolidate", action="store_true", help="跳过目录优化和知识概览更新")

    p_ledger = subparsers.add_parser("ledger-report", help="查看修复日志聚合报告（按问题类型统计修复频次）")
    p_ledger.add_argument("--days", type=int, default=30, help="统计最近 N 天的记录（默认 30）")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    MULTI_USER_COMMANDS = {"finalize", "maintain", "lint", "fix", "stats", "errors", "fix-pending", "merge-sections", "ingest-all", "ingest-batch"}

    cmd_map = {
        "ingest": cmd_ingest,
        "ingest-batch": cmd_ingest_batch,
        "ingest-all": cmd_ingest_all,
        "query": cmd_query,
        "lint": cmd_lint,
        "consolidate": cmd_consolidate,
        "stats": cmd_stats,
        "select": cmd_select,
        "errors": cmd_errors,
        "fix": cmd_fix,
        "fix-pending": cmd_fix_pending,
        "merge-sections": cmd_merge_sections,
        "maintain": cmd_maintain,
        "finalize": cmd_finalize,
        "ledger-report": cmd_ledger_report,
    }

    user_list = []
    if args.user:
        user_list = [u.strip() for u in args.user.split(",") if u.strip()]

    PARALLEL_POOL_COMMANDS = {"ingest-all", "ingest-batch"}
    if len(user_list) > 1 and args.command in MULTI_USER_COMMANDS:
        if args.command in PARALLEL_POOL_COMMANDS:
            args._user_subset = user_list
            print(f"\n🚀 并行池模式: {', '.join(user_list)} | 命令: {args.command} | 并发: {getattr(args, 'concurrency', 2)}")
            print(f"{'='*60}")
            if args.model_profile and args.model_profile != "auto":
                config.set_model_profile(args.model_profile)
            cmd_map[args.command](args)
            return
        print(f"\n🚀 多用户模式: {', '.join(user_list)} | 命令: {args.command}")
        print(f"{'='*60}")
        results = {}
        for i, user_key in enumerate(user_list, 1):
            print(f"\n{'='*60}")
            print(f"  [{i}/{len(user_list)}] 切换到: {user_key} ({config.USER_MAP.get(user_key, {}).get('name', '?')})")
            print(f"{'='*60}")
            try:
                config.set_user(user_key)
                if os.environ.get("WIKI_ENABLE_WFS") == "1":
                    config.enable_wfs_mirror()
                from config import ensure_wiki_dirs
                ensure_wiki_dirs()
                cmd_map[args.command](args)
                results[user_key] = "✅ 成功"
            except Exception as e:
                print(f"  ❌ {user_key} 执行失败: {e}")
                results[user_key] = f"❌ 失败: {e}"
        print(f"\n{'='*60}")
        print(f"  📊 多用户执行汇总")
        print(f"{'='*60}")
        for user_key, status in results.items():
            name = config.USER_MAP.get(user_key, {}).get("name", "")
            print(f"  {user_key} ({name}): {status}")
        return

    if user_list:
        config.set_user(user_list[0])
        if os.environ.get("WIKI_ENABLE_WFS") == "1":
            config.enable_wfs_mirror()
        print(f"当前用户: {user_list[0]} ({config.USER_MAP.get(user_list[0], {}).get('name', '')})")
        print(f"Wiki 目录: {config.WIKI_DIR}")
    elif not args.all_users:
        print(f"未指定 --user，使用默认 wiki 目录: {config.WIKI_DIR}")

    if args.model_profile and args.model_profile != "auto":
        config.set_model_profile(args.model_profile)
    elif args.model_profile == "auto" and not args.all_users and len(user_list) <= 1:
        print("  ⚠️ auto 模式仅在并行（--all-users 或多用户）时有效，将使用默认模型")

    if user_list and not args.all_users:
        from config import ensure_wiki_dirs
        ensure_wiki_dirs()

    cmd_map[args.command](args)


if __name__ == "__main__":
    main()