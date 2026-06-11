"""Wiki 知识库同步到 KV 存储脚本。

用法（容器内，Wiki 数据已内置为 zip 包）:
    python wiki_sync_kv.py --user caobian

    python wiki_sync_kv.py --all

    python wiki_sync_kv.py --list-bundles

    python wiki_sync_kv.py --user caobian --no-sources

    python wiki_sync_kv.py --user caobian --only-sources

    python wiki_sync_kv.py --user caobian --clean

    python wiki_sync_kv.py --user caobian --mode incremental
    python wiki_sync_kv.py --user caobian --mode full
    python wiki_sync_kv.py --user caobian --mode clean-rebuild

    python wiki_sync_kv.py --user caobian --no-hdfs-state

    python wiki_sync_kv.py --user caobian --dry-run

    python wiki_sync_kv.py --user caobian --concurrency 10

    python wiki_sync_kv.py --user caobian --wiki-dir /path/to/wiki

    python wiki_sync_kv.py --user caobian --no-verify

    python wiki_sync_kv.py --user caobian --verify-only

    python wiki_sync_kv.py --user caobian --verify-sample 50
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import config

CMD_UPSERT = 75
CMD_DELETE = 76
CMD_QUERY = 77

RPC_METHOD_MAP = {
    CMD_UPSERT: "UpsertWikiNode",
    CMD_DELETE: "DeleteWikiNode",
    CMD_QUERY: "QueryWikiNode",
}

DEVCLOUD_PROXY_URL = os.environ.get("WIKI_KV_PROXY_URL", "")

DEFAULT_API_URL = os.environ.get("WIKI_KV_API_URL", "http://localhost:8080")

BUNDLE_DIR = Path(__file__).parent / "wiki_bundles"

HADOOP_BIN = shutil.which("hadoop") or os.environ.get("HADOOP_BIN", "hadoop")
HDFS_BASE_PATH = os.environ.get("WIKI_HDFS_BASE", "hdfs:///llm_wiki")
DEFAULT_HDFS_KV_STATE_DIR = f"{HDFS_BASE_PATH}/kv_sync_state"
LOCAL_KV_STATE_DIR = Path(__file__).parent / ".kv-sync-state"


def _kv_state_log(msg: str):
    """state 模块专用日志，附带时间戳。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _check_hdfs_env() -> bool:
    """检查 HDFS 访问所需的环境变量和二进制是否就绪。"""
    if not os.path.exists(HADOOP_BIN):
        return False
    if not os.environ.get("TQ_USER_NAME") or not os.environ.get("TQ_USER_TOKEN"):
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
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def _hdfs_put(local_path: str, hdfs_path: str) -> bool:
    """上传本地文件到 HDFS（覆盖），返回是否成功。"""
    if not _check_hdfs_env():
        return False
    try:
        hdfs_dir = hdfs_path.rsplit("/", 1)[0]
        subprocess.run(
            [HADOOP_BIN, "fs", "-mkdir", "-p", hdfs_dir],
            capture_output=True, text=True, timeout=60,
        )
        subprocess.run(
            [HADOOP_BIN, "fs", "-rm", "-f", hdfs_path],
            capture_output=True, text=True, timeout=60,
        )
        result = subprocess.run(
            [HADOOP_BIN, "fs", "-put", local_path, hdfs_path],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0
    except Exception:
        return False


def _kv_state_hdfs_path(user_key: str, override: str = None) -> str:
    """返回某author的 KV sync state 在 HDFS 上的路径。"""
    base = override.rstrip("/") if override else DEFAULT_HDFS_KV_STATE_DIR
    return f"{base}/{user_key}.json"


def _kv_state_local_path(user_key: str) -> Path:
    """返回某author的 KV sync state 在本地的兜底路径。"""
    LOCAL_KV_STATE_DIR.mkdir(parents=True, exist_ok=True)
    return LOCAL_KV_STATE_DIR / f"{user_key}.json"


def load_kv_sync_state(user_key: str, hdfs_dir: str = None,
                       use_hdfs: bool = True) -> dict:
    """加载某author的 KV sync state。

    state 结构：
    {
        "version": 1,
        "user": "luxun",
        "updated_at": "2026-06-02 13:30:00",
        "nodes": {
            "literature/人的文学.md": {
                "type": "file",
                "hash": "sha256:...",
                "size": 1234
            },
            "literature": {
                "type": "dir",
                "hash": "sha256:...",
                "size": 567
            },
            ...
        }
    }

    优先从 HDFS 读取，失败则从本地兜底文件读取。
    """
    if use_hdfs:
        hdfs_path = _kv_state_hdfs_path(user_key, hdfs_dir)
        local_tmp = str(_kv_state_local_path(user_key)) + ".hdfs_tmp"
        if _hdfs_get(hdfs_path, local_tmp):
            try:
                with open(local_tmp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _kv_state_log(f"  📥 从 HDFS 加载 KV state: {user_key} ({len(data.get('nodes', {}))} 个节点)")
                try:
                    shutil.copy(local_tmp, _kv_state_local_path(user_key))
                except OSError:
                    pass
                return data
            except (json.JSONDecodeError, IOError) as e:
                _kv_state_log(f"  ⚠️ HDFS state 解析失败 ({user_key}): {e}")
            finally:
                if os.path.exists(local_tmp):
                    try:
                        os.remove(local_tmp)
                    except OSError:
                        pass

    local_path = _kv_state_local_path(user_key)
    if local_path.exists():
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            _kv_state_log(f"  📁 从本地加载 KV state: {user_key} ({len(data.get('nodes', {}))} 个节点)")
            return data
        except (json.JSONDecodeError, IOError):
            pass

    return {}


def save_kv_sync_state(user_key: str, state: dict, hdfs_dir: str = None,
                       use_hdfs: bool = True):
    """保存 KV sync state 到本地 + HDFS。"""
    state["version"] = state.get("version", 1)
    state["user"] = user_key
    state["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    local_path = _kv_state_local_path(user_key)
    try:
        local_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except IOError as e:
        _kv_state_log(f"  ⚠️ 本地 KV state 写入失败 ({user_key}): {e}")
        return

    if use_hdfs:
        hdfs_path = _kv_state_hdfs_path(user_key, hdfs_dir)
        if _hdfs_put(str(local_path), hdfs_path):
            _kv_state_log(f"  ☁️ KV state 已同步到 HDFS: {hdfs_path}")
        else:
            _kv_state_log(f"  ⚠️ HDFS 写入失败，仅保留本地: {local_path}")


def _node_hash(node: dict) -> str:
    """计算节点的内容哈希，作为 diff 比对基准。

    哈希字段：type + name + text + meta，对 dir 节点同样有意义
    （目录的 _index.md 内容/页面数变化会反映在 text/meta 里）。
    """
    h = hashlib.sha256()
    payload = "|".join([
        node.get("type", ""),
        node.get("name", ""),
        node.get("text", "") or "",
        node.get("meta", "") or "",
    ])
    h.update(payload.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def build_state_from_nodes(nodes: list) -> dict:
    """根据当前收集到的节点列表生成 state 快照。"""
    snapshot = {}
    for n in nodes:
        snapshot[n["dir"]] = {
            "type": n["type"],
            "hash": _node_hash(n),
            "size": len(n.get("text", "") or ""),
        }
    return {"version": 1, "nodes": snapshot}


SKIP_FILES = {
    ".periodic-state.json",
    "error_book.yaml",
    "lint_ledger.jsonl",
    "lint-report.md",
    "log.md",
    "page_types.yaml",
    "overview.md",
    "index.md",
    "_index.md",
}


def encode_bizuin(uin: str) -> str:
    """将 uin 字符串进行 base64 编码，生成 bizuin。"""
    return base64.b64encode(uin.encode("utf-8")).decode("utf-8")


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def extract_description(body: str) -> str:
    """从正文中提取第一行 > 引用作为一句话描述。

    匹配标题行后紧跟的 > 开头的行，如：
        > 这是一句话描述
    """
    for line in body.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(">"):
            return line.lstrip("> ").strip()
        break
    return ""


def extract_summary(body: str) -> str:
    """从正文中提取 ## 摘要 段落的内容作为 description。

    适用于 source 类型文件（digest），其结构为：
        本文讲述了...
    返回 ## 摘要 标题下到下一个 ## 标题之间的全部文本（合并为一行）。
    """
    lines = body.strip().splitlines()
    in_summary = False
    summary_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## ") and "摘要" in stripped:
            in_summary = True
            continue
        if in_summary:
            if stripped.startswith("## "):
                break
            if stripped:
                summary_lines.append(stripped)
    return " ".join(summary_lines)


def extract_links_to(body: str, current_dir: str) -> list[str]:
    """从正文中提取 [[...]] wikilink，转为标准路径格式。

    - 如果 wikilink 已包含 / 则视为完整路径，补 .md 后缀
    - 如果不含 / 则加上 current_dir 前缀，补 .md 后缀
    - 去重并保持顺序
    """
    links = []
    seen = set()
    for m in _WIKILINK_RE.finditer(body):
        raw = m.group(1).strip()
        if not raw:
            continue
        if "/" in raw:
            path = raw if raw.endswith(".md") else raw + ".md"
        else:
            path = f"{current_dir}/{raw}.md" if current_dir else f"{raw}.md"
        if path not in seen:
            seen.add(path)
            links.append(path)
    return links


def build_file_meta(frontmatter: dict, body: str, dir_key: str) -> str:
    """从 frontmatter 和正文构建文件节点的 meta JSON 字符串。

    包含字段：page_type, aliases, tags, description, links_to。
    """
    page_type = frontmatter.get("type", "")

    aliases = frontmatter.get("aliases", [])
    if not isinstance(aliases, list):
        aliases = [str(aliases)] if aliases else []
    tags = frontmatter.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)] if tags else []

    if page_type == "source":
        description = extract_summary(body) or extract_description(body)
    else:
        description = extract_description(body)

    current_dir = dir_key.rsplit("/", 1)[0] if "/" in dir_key else ""
    links_to = extract_links_to(body, current_dir)

    meta = {
        "page_type": page_type,
        "aliases": aliases,
        "tags": tags,
        "description": description,
        "links_to": links_to,
    }
    return json.dumps(meta, ensure_ascii=False)


def build_dir_meta(index_text: str, page_count: int) -> str:
    """构建目录节点的 meta JSON 字符串。

    包含字段：page_count, description。
    """
    description = extract_description(index_text)
    return json.dumps({
        "page_count": page_count,
        "description": description,
    }, ensure_ascii=False)


def parse_md_file(file_path: Path) -> tuple[dict, str]:
    """解析 Markdown 文件，返回 (frontmatter_dict, body_text)。

    body_text 不含 YAML frontmatter，只有纯正文。
    使用 config.split_frontmatter 进行健壮的 frontmatter 解析。
    """
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        print(f"  ⚠️ 读取文件失败 {file_path}: {e}")
        return {}, ""

    fm_dict = {}
    body = text  # 默认整个文件作为 body
    result = config.split_frontmatter(text)
    if result is not None:
        _, fm_text, body = result
        try:
            fm_dict = yaml.safe_load(fm_text) or {}
        except yaml.YAMLError:
            pass
        body = body.lstrip("\n")  # 去掉 frontmatter 后的前导空行

    return fm_dict, body


def _read_index_file(directory: Path) -> str:
    """读取目录下的 index.md 或 _index.md，返回 text 内容。

    优先读取 _index.md，其次 index.md。如果都不存在返回 ""。
    注意：_index.md 通常没有 frontmatter，直接返回全文。
    """
    for name in ("_index.md", "index.md"):
        idx_file = directory / name
        if idx_file.exists():
            _, body = parse_md_file(idx_file)
            return body
    return ""


def list_bundles() -> list[str]:
    """列出内置的 wiki zip 包。"""
    if not BUNDLE_DIR.exists():
        return []
    return sorted(
        p.stem for p in BUNDLE_DIR.glob("*.zip")
    )


def extract_bundle(user_key: str, target_dir: Path) -> Path:
    """解压内置的 wiki zip 包到目标目录，返回 wiki 目录路径。

    zip 包内部结构为 wiki/...，解压后 target_dir/wiki/ 即为 wiki 目录。
    """
    zip_path = BUNDLE_DIR / f"{user_key}.zip"
    if not zip_path.exists():
        raise FileNotFoundError(f"未找到内置 wiki 包: {zip_path}")

    print(f"📦 解压 {zip_path.name} → {target_dir}/")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir)

    wiki_dir = target_dir / "wiki"
    if not wiki_dir.exists():
        raise FileNotFoundError(f"解压后未找到 wiki 目录: {wiki_dir}")

    md_count = sum(1 for _ in wiki_dir.rglob("*.md"))
    print(f"   ✅ 解压完成: {md_count} 个 .md 文件")
    return wiki_dir


def _count_pages(directory: Path) -> int:
    """统计目录下的 .md 文件数（不含 SKIP_FILES 和 _index.md/index.md）。"""
    count = 0
    for md_file in directory.glob("*.md"):
        if md_file.name not in SKIP_FILES:
            count += 1
    return count


def collect_wiki_nodes(wiki_dir: Path, include_sources: bool = True, only_sources: bool = False) -> list[dict]:
    """收集 wiki 目录下所有需要同步的节点。

    返回 [{dir, type, name, text, meta}] 列表，其中：
    - dir: 节点路径 Key（如 "culture" 或 "culture/余华.md"）
    - type: "dir" 或 "file"
    - name: 当前目录名或文件名
    - text: 文件正文（不含 frontmatter）；目录节点存放其 _index.md 的内容
    - meta: 元数据 JSON 字符串（目录：page_count + description；文件：page_type + aliases + tags + description + links_to）
    """
    nodes = []

    if not wiki_dir.exists():
        print(f"  ❌ Wiki 目录不存在: {wiki_dir}")
        return nodes

    if not only_sources:
        root_text = _read_index_file(wiki_dir)
        if root_text:
            total_pages = 0
            for sub_dir in wiki_dir.iterdir():
                if sub_dir.is_dir():
                    if sub_dir.name == "sources":
                        for ss in sub_dir.iterdir():
                            if ss.is_dir():
                                total_pages += _count_pages(ss)
                    else:
                        total_pages += _count_pages(sub_dir)
            nodes.append({
                "dir": "/",
                "type": "dir",
                "name": "/",
                "text": root_text,
                "meta": build_dir_meta(root_text, total_pages),
            })

    for sub_dir in sorted(wiki_dir.iterdir()):
        if not sub_dir.is_dir():
            continue

        dir_name = sub_dir.name

        is_sources = dir_name == "sources"
        if only_sources and not is_sources:
            continue
        if not include_sources and is_sources:
            continue

        dir_text = _read_index_file(sub_dir)

        if is_sources:
            page_count = 0
            for ss in sub_dir.iterdir():
                if ss.is_dir():
                    page_count += _count_pages(ss)
        else:
            page_count = _count_pages(sub_dir)

        nodes.append({
            "dir": dir_name,
            "type": "dir",
            "name": dir_name,
            "text": dir_text,
            "meta": build_dir_meta(dir_text, page_count),
        })

        if is_sources:
            for sources_sub in sorted(sub_dir.iterdir()):
                if not sources_sub.is_dir():
                    continue
                sub_dir_key = f"sources/{sources_sub.name}"

                sub_text = _read_index_file(sources_sub)
                sub_page_count = _count_pages(sources_sub)

                nodes.append({
                    "dir": sub_dir_key,
                    "type": "dir",
                    "name": sources_sub.name,
                    "text": sub_text,
                    "meta": build_dir_meta(sub_text, sub_page_count),
                })
                for md_file in sorted(sources_sub.glob("*.md")):
                    if md_file.name in SKIP_FILES:
                        continue
                    file_key = f"{sub_dir_key}/{md_file.name}"
                    fm_dict, body = parse_md_file(md_file)
                    meta = build_file_meta(fm_dict, body, file_key)
                    nodes.append({
                        "dir": file_key,
                        "type": "file",
                        "name": md_file.name,
                        "text": body,
                        "meta": meta,
                    })
        else:
            for md_file in sorted(sub_dir.glob("*.md")):
                if md_file.name in SKIP_FILES:
                    continue
                file_key = f"{dir_name}/{md_file.name}"
                fm_dict, body = parse_md_file(md_file)
                meta = build_file_meta(fm_dict, body, file_key)
                nodes.append({
                    "dir": file_key,
                    "type": "file",
                    "name": md_file.name,
                    "text": body,
                    "meta": meta,
                })

    return nodes


class WikiKVClient:
    """Wiki KV 存储客户端。

    调用方式：POST http://host:port/{RPC方法名}
    与 tRPC 泛 HTTP RPC 规范一致，body 为纯 JSON（不含 cmd_id）。
    Supports an optional HTTP proxy (set WIKI_KV_PROXY_URL) for environments
    that cannot reach the KV service directly.
    """

    def __init__(self, api_url: str, bizuin: str, timeout: int = 30,
                 use_devcloud: bool = False):
        self.api_url = api_url.rstrip("/")
        self.bizuin = bizuin
        self.timeout = timeout
        self.use_devcloud = use_devcloud
        self.session = requests.Session()
        try:
            self.raw_uin = base64.b64decode(bizuin).decode("utf-8")
        except Exception:
            self.raw_uin = bizuin
        self.stats = {
            "created": 0,
            "updated": 0,
            "deleted": 0,
            "failed": 0,
            "skipped": 0,
        }

    def _call_api(self, cmd_id: int, payload: dict) -> dict:
        """调用 KV 接口。

        URL format: http://host:port/{rpc_method}
        Body: plain JSON (no cmd_id).
        """
        rpc_name = RPC_METHOD_MAP.get(cmd_id)
        if not rpc_name:
            return {"code": -9997, "errmsg": f"未知 cmd_id: {cmd_id}"}

        target_url = f"{self.api_url}/{rpc_name}"

        headers = {
            "Content-Type": "application/json",
            "Cookie": f"uin={self.raw_uin}",
        }

        if self.use_devcloud:
            from urllib.parse import urlencode
            params = urlencode({"url": target_url})
            url = f"{DEVCLOUD_PROXY_URL}?{params}"
        else:
            url = target_url

        try:
            resp = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code", 0) != 0:
                if not hasattr(self, '_debug_fail_count'):
                    self._debug_fail_count = 0
                self._debug_fail_count += 1
                if self._debug_fail_count <= 3:
                    print(f"  🔍 [DEBUG] {rpc_name} 返回非 0: {json.dumps(result, ensure_ascii=False)[:500]}")
                    print(f"  🔍 [DEBUG] 请求 payload keys: {list(payload.keys())}, bizuin={payload.get('bizuin','?')[:20]}")
                    print(f"  🔍 [DEBUG] HTTP status={resp.status_code}, url={url[:100]}")
            return result
        except requests.RequestException as e:
            return {"code": -9999, "errmsg": str(e)}
        except json.JSONDecodeError:
            return {"code": -9998, "errmsg": f"响应非 JSON: {resp.text[:200]}"}

    def upsert_node(self, node: dict) -> dict:
        """创建或更新节点。"""
        payload = {
            "bizuin": self.bizuin,
            "dir": node["dir"],
            "type": node["type"],
            "name": node.get("name", ""),
        }
        if node.get("text"):
            payload["text"] = node["text"]
        if node.get("meta"):
            payload["meta"] = node["meta"]

        result = self._call_api(CMD_UPSERT, payload)

        if result.get("code", -1) == 0:
            action = result.get("action", "unknown")
            if action == "created":
                self.stats["created"] += 1
            else:
                self.stats["updated"] += 1
        else:
            self.stats["failed"] += 1

        return result

    def delete_node(self, dir_key: str, recursive: bool = False) -> dict:
        """删除节点。"""
        payload = {
            "bizuin": self.bizuin,
            "dir": dir_key,
            "recursive": recursive,
        }
        result = self._call_api(CMD_DELETE, payload)
        if result.get("code", -1) == 0:
            self.stats["deleted"] += result.get("deleted_count", 1)
        else:
            self.stats["failed"] += 1
        return result

    def query_node(self, dir_key: str) -> dict:
        """查询节点。"""
        payload = {
            "bizuin": self.bizuin,
            "dir": dir_key,
        }
        return self._call_api(CMD_QUERY, payload)

    def print_stats(self):
        """打印统计信息。"""
        s = self.stats
        total = s["created"] + s["updated"] + s["failed"] + s["skipped"]
        print(f"\n📊 同步统计:")
        print(f"   总计处理: {total} 个节点")
        print(f"   ✅ 新建: {s['created']}")
        print(f"   🔄 更新: {s['updated']}")
        print(f"   🗑️  删除: {s['deleted']}")
        print(f"   ❌ 失败: {s['failed']}")
        if s["skipped"]:
            print(f"   ⏭️  跳过: {s['skipped']}")


def compute_diff(nodes: list[dict], prev_state: dict) -> dict:
    """对比当前节点列表和上次的 state，输出差异。

    返回:
        {
            "to_create_dirs":  [node, ...],   # state 中无的 dir 节点
            "to_create_files": [node, ...],   # state 中无的 file 节点
            "to_update_dirs":  [node, ...],   # hash 变化的 dir 节点
            "to_update_files": [node, ...],   # hash 变化的 file 节点
            "to_delete":       [{"dir": k, "type": t}, ...],  # state 中有但当前不存在
            "skip_count":      N,             # 未变化数量
            "stats": { "create": n, "update": n, "delete": n, "skip": n, "total": n }
        }

    注意：删除按"先文件后目录"排序，避免删非空目录。
    """
    prev_nodes = prev_state.get("nodes", {}) if prev_state else {}
    curr_keys = {n["dir"] for n in nodes}

    to_create_dirs, to_create_files = [], []
    to_update_dirs, to_update_files = [], []
    skip_count = 0

    for n in nodes:
        key = n["dir"]
        new_hash = _node_hash(n)
        prev = prev_nodes.get(key)
        if prev is None:
            (to_create_dirs if n["type"] == "dir" else to_create_files).append(n)
        elif prev.get("hash") != new_hash:
            (to_update_dirs if n["type"] == "dir" else to_update_files).append(n)
        else:
            skip_count += 1

    to_delete = []
    for key, info in prev_nodes.items():
        if key not in curr_keys:
            to_delete.append({"dir": key, "type": info.get("type", "file")})

    files_to_delete = [d for d in to_delete if d["type"] == "file"]
    dirs_to_delete = sorted(
        [d for d in to_delete if d["type"] == "dir"],
        key=lambda d: d["dir"].count("/"),
        reverse=True,
    )
    to_delete_sorted = files_to_delete + dirs_to_delete

    create_count = len(to_create_dirs) + len(to_create_files)
    update_count = len(to_update_dirs) + len(to_update_files)
    delete_count = len(to_delete_sorted)

    return {
        "to_create_dirs": to_create_dirs,
        "to_create_files": to_create_files,
        "to_update_dirs": to_update_dirs,
        "to_update_files": to_update_files,
        "to_delete": to_delete_sorted,
        "skip_count": skip_count,
        "stats": {
            "create": create_count,
            "update": update_count,
            "delete": delete_count,
            "skip": skip_count,
            "total": create_count + update_count + delete_count + skip_count,
        },
    }


def apply_diff(client: WikiKVClient, diff: dict, concurrency: int = 5,
               dry_run: bool = False) -> tuple[set, list]:
    """根据 diff 结果应用变更。

    执行顺序（保证 KV 一致性）:
        1. 创建/更新目录节点（先建目录，再写文件）
        2. 并发创建/更新文件节点
        3. 删除节点（先删文件，再删空目录，深层优先）

    返回 (success_keys, failed_items)：
        success_keys: 成功同步/删除的 dir key 集合（用于刷新 state）
        failed_items: [(dir, errmsg), ...]
    """
    success_keys = set()
    failed_items = []

    dir_jobs = diff["to_create_dirs"] + diff["to_update_dirs"]
    if dir_jobs:
        print(f"\n📁 同步 {len(dir_jobs)} 个目录节点（新增 {len(diff['to_create_dirs'])} / 更新 {len(diff['to_update_dirs'])}）...")
        for node in dir_jobs:
            if dry_run:
                print(f"  [DRY-RUN] upsert dir: {node['dir']}")
                client.stats["skipped"] += 1
                success_keys.add(node["dir"])
                continue
            result = client.upsert_node(node)
            if result.get("code", -1) == 0:
                action = result.get("action", "?")
                print(f"  ✅ {node['dir']}/ ({action})")
                success_keys.add(node["dir"])
            else:
                errmsg = result.get("errmsg", "未知错误")
                print(f"  ❌ {node['dir']}/ — {errmsg}")
                failed_items.append((node["dir"], errmsg))

    file_jobs = diff["to_create_files"] + diff["to_update_files"]
    if file_jobs:
        print(f"\n📄 同步 {len(file_jobs)} 个文件节点 "
              f"（新增 {len(diff['to_create_files'])} / 更新 {len(diff['to_update_files'])}），并发 {concurrency}...")

        if dry_run:
            for node in file_jobs:
                print(f"  [DRY-RUN] upsert file: {node['dir']} ({len(node['text'])} chars)")
                success_keys.add(node["dir"])
            client.stats["skipped"] += len(file_jobs)
        else:
            total = len(file_jobs)
            done = 0
            t_start = time.time()

            def _upsert_one(node):
                return node, client.upsert_node(node)

            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {executor.submit(_upsert_one, node): node for node in file_jobs}
                for future in as_completed(futures):
                    done += 1
                    node, result = future.result()
                    if result.get("code", -1) == 0:
                        success_keys.add(node["dir"])
                        if done % 50 == 0 or done == total:
                            elapsed = time.time() - t_start
                            rate = done / elapsed if elapsed > 0 else 0
                            eta = (total - done) / rate if rate > 0 else 0
                            print(f"  📊 进度: {done}/{total} ({done*100//total}%) | "
                                  f"{rate:.1f} 个/秒 | 预计剩余 {eta:.0f}s")
                    else:
                        errmsg = result.get("errmsg", "未知错误")
                        failed_items.append((node["dir"], errmsg))
                        print(f"  ❌ {node['dir']} — {errmsg}")

    delete_jobs = diff["to_delete"]
    if delete_jobs:
        print(f"\n🗑️  删除 {len(delete_jobs)} 个节点（不再存在于本地）...")
        for item in delete_jobs:
            dir_key = item["dir"]
            if dry_run:
                print(f"  [DRY-RUN] delete: {dir_key} ({item['type']})")
                success_keys.add(dir_key)
                client.stats["skipped"] += 1
                continue
            recursive = item["type"] == "dir"
            result = client.delete_node(dir_key, recursive=recursive)
            code = result.get("code", -1)
            if code == 0:
                print(f"  🗑️  {dir_key} ({item['type']})")
                success_keys.add(dir_key)
            elif code == -1004:
                client.stats["failed"] = max(0, client.stats["failed"] - 1)
                success_keys.add(dir_key)
                print(f"  ⏭️  {dir_key} 已不存在，跳过")
            else:
                errmsg = result.get("errmsg", "未知错误")
                failed_items.append((dir_key, f"delete: {errmsg}"))
                print(f"  ❌ 删除失败 {dir_key} — {errmsg}")

    return success_keys, failed_items


def update_state_after_apply(prev_state: dict, nodes: list[dict],
                             diff: dict, success_keys: set) -> dict:
    """根据本次 apply 结果生成新的 state。

    策略：
      - 对成功 upsert 的节点，写入新 hash
      - 对成功 delete 的节点，从 state 中移除
      - 对失败的节点，**保留旧 state**（下次自动重试）
    """
    new_nodes = dict(prev_state.get("nodes", {})) if prev_state else {}

    upserted_nodes = (
        diff["to_create_dirs"] + diff["to_create_files"]
        + diff["to_update_dirs"] + diff["to_update_files"]
    )
    for n in upserted_nodes:
        if n["dir"] in success_keys:
            new_nodes[n["dir"]] = {
                "type": n["type"],
                "hash": _node_hash(n),
                "size": len(n.get("text", "") or ""),
            }

    for item in diff["to_delete"]:
        if item["dir"] in success_keys:
            new_nodes.pop(item["dir"], None)


    return {
        "version": 1,
        "nodes": new_nodes,
    }


def sync_nodes(client: WikiKVClient, nodes: list[dict], concurrency: int = 5,
               dry_run: bool = False) -> None:
    """并发同步节点到 KV。

    先同步目录节点（确保目录存在），再并发同步文件节点。
    """
    dir_nodes = [n for n in nodes if n["type"] == "dir"]
    file_nodes = [n for n in nodes if n["type"] == "file"]

    print(f"\n📁 同步 {len(dir_nodes)} 个目录节点...")
    for node in dir_nodes:
        if dry_run:
            print(f"  [DRY-RUN] upsert dir: {node['dir']}")
            client.stats["skipped"] += 1
            continue
        result = client.upsert_node(node)
        code = result.get("code", -1)
        action = result.get("action", "?")
        if code == 0:
            print(f"  ✅ {node['dir']}/ ({action})")
        else:
            print(f"  ❌ {node['dir']}/ — {result.get('errmsg', '未知错误')}")

    print(f"\n📄 同步 {len(file_nodes)} 个文件节点（并发 {concurrency}）...")

    if dry_run:
        for node in file_nodes:
            print(f"  [DRY-RUN] upsert file: {node['dir']} ({len(node['text'])} chars)")
        client.stats["skipped"] += len(file_nodes)
        return

    total = len(file_nodes)
    done = 0
    failed_nodes = []
    t_start = time.time()

    def _upsert_one(node):
        return node, client.upsert_node(node)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_upsert_one, node): node for node in file_nodes}

        for future in as_completed(futures):
            done += 1
            node, result = future.result()
            code = result.get("code", -1)

            if code == 0:
                if done % 50 == 0 or done == total:
                    elapsed = time.time() - t_start
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done) / rate if rate > 0 else 0
                    print(f"  📊 进度: {done}/{total} ({done*100//total}%) | "
                          f"{rate:.1f} 个/秒 | 预计剩余 {eta:.0f}s")
            else:
                failed_nodes.append((node["dir"], result.get("errmsg", "未知错误")))
                print(f"  ❌ {node['dir']} — {result.get('errmsg', '未知错误')}")

    if failed_nodes:
        print(f"\n⚠️ {len(failed_nodes)} 个文件同步失败:")
        for dir_key, errmsg in failed_nodes[:20]:
            print(f"    {dir_key}: {errmsg}")
        if len(failed_nodes) > 20:
            print(f"    ... 还有 {len(failed_nodes) - 20} 个")


def clean_kv(client: WikiKVClient, wiki_dir: Path = None, dry_run: bool = False) -> None:
    """清空 KV 中该author的所有数据。

    通过递归删除根目录 "/" 来清空所有数据，不依赖本地 wiki 目录结构。
    这样即使旧版本 KV 中有已改名/删除的目录，也能被正确清理。
    """
    print("\n🗑️  清空 KV 中的现有数据...")

    if dry_run:
        print(f"  [DRY-RUN] delete recursive: / (根目录，清空所有数据)")
        return

    original_timeout = client.timeout
    client.timeout = max(original_timeout * 4, 120)
    result = client.delete_node("/", recursive=True)
    client.timeout = original_timeout
    code = result.get("code", -1)
    if code == 0:
        deleted = result.get("deleted_count", 0)
        print(f"  🗑️  / — 删除 {deleted} 个节点（全部清空）")
    elif code == -1004:
        client.stats["failed"] = max(0, client.stats["failed"] - 1)
        print(f"  ⏭️  KV 中无数据，无需清空")
    else:
        print(f"  ❌ 清空失败 — {result.get('errmsg', '未知错误')}")


def verify_sync(client: WikiKVClient, nodes: list[dict], sample_size: int = 20,
                 concurrency: int = 5) -> dict:
    """同步后验证：随机抽样节点，通过 Query 接口读回并对比内容。

    验证项：
    1. 节点是否存在（code == 0）
    2. type 是否一致
    3. text 内容是否一致（文件节点）
    4. meta 是否一致（文件节点）

    返回 {"total": N, "passed": N, "failed": N, "errors": [...], "details": [...]}
    """
    import random

    file_nodes = [n for n in nodes if n["type"] == "file"]
    dir_nodes = [n for n in nodes if n["type"] == "dir"]

    verify_dirs = dir_nodes
    if len(file_nodes) <= sample_size:
        verify_files = file_nodes
    else:
        verify_files = random.sample(file_nodes, sample_size)

    all_verify = verify_dirs + verify_files
    total = len(all_verify)

    print(f"\n🔍 同步验证（抽样 {len(verify_dirs)} 个目录 + {len(verify_files)} 个文件）...")

    passed = 0
    failed = 0
    errors = []
    details = []

    def _verify_one(node):
        """验证单个节点。"""
        result = client.query_node(node["dir"])
        checks = []
        ok = True

        code = result.get("code", -1)
        if code != 0:
            return {
                "dir": node["dir"],
                "ok": False,
                "checks": [{"name": "存在性", "ok": False, "detail": f"code={code}, {result.get('errmsg', '?')}"}],
            }

        checks.append({"name": "存在性", "ok": True, "detail": "✓"})

        remote_type = result.get("type", "")
        if remote_type != node["type"]:
            checks.append({"name": "类型", "ok": False, "detail": f"期望={node['type']}, 实际={remote_type}"})
            ok = False
        else:
            checks.append({"name": "类型", "ok": True, "detail": "✓"})

        if node["type"] == "file" and node.get("text"):
            remote_text = result.get("text", "")
            local_text = node["text"]
            if remote_text == local_text:
                checks.append({"name": "内容", "ok": True, "detail": f"✓ ({len(local_text)} chars)"})
            else:
                len_diff = abs(len(remote_text) - len(local_text))
                diff_pos = 0
                for i, (a, b) in enumerate(zip(remote_text, local_text)):
                    if a != b:
                        diff_pos = i
                        break
                else:
                    diff_pos = min(len(remote_text), len(local_text))

                checks.append({
                    "name": "内容",
                    "ok": False,
                    "detail": f"不一致: 本地={len(local_text)}chars, 远端={len(remote_text)}chars, "
                              f"长度差={len_diff}, 首差位置={diff_pos}",
                })
                ok = False

        if node["type"] == "file" and node.get("meta"):
            remote_meta = result.get("meta", "")
            local_meta = node["meta"]
            if remote_meta == local_meta:
                checks.append({"name": "元数据", "ok": True, "detail": "✓"})
            else:
                try:
                    local_obj = json.loads(local_meta) if isinstance(local_meta, str) else local_meta
                    remote_obj = json.loads(remote_meta) if isinstance(remote_meta, str) and remote_meta else {}
                    if local_obj == remote_obj:
                        checks.append({"name": "元数据", "ok": True, "detail": "✓ (JSON 语义一致)"})
                    else:
                        checks.append({"name": "元数据", "ok": False,
                                       "detail": f"不一致: 本地={str(local_meta)[:80]}, 远端={str(remote_meta)[:80]}"})
                        ok = False
                except (json.JSONDecodeError, TypeError):
                    checks.append({"name": "元数据", "ok": False,
                                   "detail": f"不一致: 本地={str(local_meta)[:80]}, 远端={str(remote_meta)[:80]}"})
                    ok = False

        return {"dir": node["dir"], "ok": ok, "checks": checks}

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_verify_one, node): node for node in all_verify}
        for future in as_completed(futures):
            detail = future.result()
            details.append(detail)
            if detail["ok"]:
                passed += 1
            else:
                failed += 1
                errors.append(detail)

    t_elapsed = time.time() - t_start

    print(f"\n{'─'*50}")
    print(f"📋 验证结果 ({t_elapsed:.1f}s)")
    print(f"{'─'*50}")

    if failed == 0:
        print(f"   ✅ 全部通过: {passed}/{total} 个节点验证成功")
    else:
        print(f"   ⚠️ 通过: {passed}/{total} | 失败: {failed}/{total}")
        print(f"\n   ❌ 失败详情:")
        for err in errors[:15]:
            print(f"      📄 {err['dir']}")
            for chk in err["checks"]:
                icon = "✅" if chk["ok"] else "❌"
                print(f"         {icon} {chk['name']}: {chk['detail']}")
        if len(errors) > 15:
            print(f"      ... 还有 {len(errors) - 15} 个失败节点")

    print(f"{'─'*50}")

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "details": details,
    }


def sync_one_user(user_key: str, args) -> bool:
    """同步单个author的 wiki 到 KV。返回是否成功。"""
    user_info = config.USER_MAP[user_key]
    user_name = user_info["name"]
    uin = user_info["uin"]
    bizuin = encode_bizuin(uin)

    print(f"\n{'='*60}")
    print(f"🚀 同步: {user_key} ({user_name})")
    print(f"   UIN: {uin} | bizuin: {bizuin}")

    if getattr(args, 'clean_only', False):
        client = WikiKVClient(args.api_url, bizuin, timeout=args.timeout,
                              use_devcloud=args.devcloud)
        clean_kv(client, dry_run=args.dry_run)
        print(f"\n✅ 已清空 {user_key} ({user_name}) 的 KV 数据，跳过同步")
        return True

    tmp_dir = None
    if args.wiki_dir:
        wiki_dir = Path(args.wiki_dir)
        print(f"   Wiki 来源: 手动指定 → {wiki_dir}")
    else:
        zip_path = BUNDLE_DIR / f"{user_key}.zip"
        if not zip_path.exists():
            print(f"   ❌ 未找到内置 wiki 包: {zip_path}")
            print(f"      请确认构建镜像时已包含该author的 wiki 数据")
            return False
        tmp_dir = tempfile.mkdtemp(prefix=f"wiki_{user_key}_")
        try:
            wiki_dir = extract_bundle(user_key, Path(tmp_dir))
        except Exception as e:
            print(f"   ❌ 解压失败: {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False

    try:
        include_sources = not args.no_sources
        only_sources = args.only_sources
        print(f"\n📦 收集 Wiki 节点...")
        nodes = collect_wiki_nodes(wiki_dir, include_sources=include_sources, only_sources=only_sources)

        dir_count = sum(1 for n in nodes if n["type"] == "dir")
        file_count = sum(1 for n in nodes if n["type"] == "file")
        total_size = sum(len(n["text"]) for n in nodes)
        print(f"   📁 目录: {dir_count} 个")
        print(f"   📄 文件: {file_count} 个")
        print(f"   💾 总大小: {total_size / 1024 / 1024:.2f} MB")

        if not nodes:
            print("  ⚠️ 没有需要同步的节点")
            return True

        client = WikiKVClient(args.api_url, bizuin, timeout=args.timeout,
                              use_devcloud=args.devcloud)

        if args.verify_only:
            print(f"\n🔍 仅验证模式（不执行同步）")
            verify_result = verify_sync(
                client, nodes,
                sample_size=getattr(args, 'verify_sample', 20),
                concurrency=args.concurrency,
            )
            return verify_result["failed"] == 0

        mode = getattr(args, "mode", "incremental")
        if args.clean and mode == "incremental":
            mode = "clean-rebuild"  # 兼容旧 --clean
        use_hdfs_state = not getattr(args, "no_hdfs_state", False)
        hdfs_state_dir = getattr(args, "hdfs_state_dir", None)

        t_start = time.time()
        sync_failed = False  # 用于决定 state 是否需要保存

        if mode == "clean-rebuild":
            print(f"\n🔧 模式: clean-rebuild（先清空 KV，再全量推送）")
            clean_kv(client, dry_run=args.dry_run)
            sync_nodes(client, nodes, concurrency=args.concurrency, dry_run=args.dry_run)
            sync_failed = client.stats["failed"] > 0

        elif mode == "full":
            print(f"\n🔧 模式: full（不清空 KV，推送所有节点）")
            sync_nodes(client, nodes, concurrency=args.concurrency, dry_run=args.dry_run)
            sync_failed = client.stats["failed"] > 0

        else:
            print(f"\n🔧 模式: incremental（基于 sync state 做 diff）")
            prev_state = load_kv_sync_state(
                user_key,
                hdfs_dir=hdfs_state_dir,
                use_hdfs=use_hdfs_state,
            )

            if not prev_state or not prev_state.get("nodes"):
                print(f"  ⚠️ 未找到 KV sync state，本次按 full 模式同步并生成新 state")
                sync_nodes(client, nodes, concurrency=args.concurrency, dry_run=args.dry_run)
                sync_failed = client.stats["failed"] > 0
            else:
                diff = compute_diff(nodes, prev_state)
                stats = diff["stats"]
                print(f"\n📋 Diff 统计:")
                print(f"   ✅ 未变化: {stats['skip']}")
                print(f"   ➕ 新增:   {stats['create']} (dir {len(diff['to_create_dirs'])} / file {len(diff['to_create_files'])})")
                print(f"   🔄 更新:   {stats['update']} (dir {len(diff['to_update_dirs'])} / file {len(diff['to_update_files'])})")
                print(f"   🗑️  删除:   {stats['delete']}")
                print(f"   📦 总计:   {stats['total']}")

                if stats["create"] + stats["update"] + stats["delete"] == 0:
                    print(f"\n✨ 无变化，跳过同步")
                else:
                    success_keys, failed_items = apply_diff(
                        client, diff,
                        concurrency=args.concurrency,
                        dry_run=args.dry_run,
                    )
                    sync_failed = len(failed_items) > 0
                    if not args.dry_run:
                        new_state = update_state_after_apply(
                            prev_state, nodes, diff, success_keys,
                        )
                        save_kv_sync_state(
                            user_key, new_state,
                            hdfs_dir=hdfs_state_dir,
                            use_hdfs=use_hdfs_state,
                        )

        if mode in ("full", "clean-rebuild") and not args.dry_run and not sync_failed:
            new_state = build_state_from_nodes(nodes)
            save_kv_sync_state(
                user_key, new_state,
                hdfs_dir=hdfs_state_dir,
                use_hdfs=use_hdfs_state,
            )

        t_elapsed = time.time() - t_start

        client.print_stats()
        print(f"\n⏱️  耗时: {t_elapsed:.1f}s")

        if not args.dry_run and not getattr(args, 'no_verify', False):
            verify_result = verify_sync(
                client, nodes,
                sample_size=getattr(args, 'verify_sample', 20),
                concurrency=args.concurrency,
            )
            if verify_result["failed"] > 0:
                print(f"\n⚠️ 验证发现 {verify_result['failed']} 个节点不一致！")
                return False

        return client.stats["failed"] == 0

    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(
        description="将指定author的 Wiki 知识库同步到 KV 存储（从镜像内置 zip 解压）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    user_group = parser.add_mutually_exclusive_group()
    user_group.add_argument("--user", "-u",
                            help=f"author key，支持逗号分隔多个如 tym,yusi。可选: {', '.join(config.USER_MAP.keys())}")
    user_group.add_argument("--all", action="store_true",
                            help="同步所有内置 wiki 包的author")
    user_group.add_argument("--list-bundles", action="store_true",
                            help="列出镜像中内置的 wiki 包")

    parser.add_argument("--verify-only", action="store_true",
                        help="只验证不同步（检查 KV 中已有数据是否正确）")

    parser.add_argument("--api-url", default=DEFAULT_API_URL,
                        help=f"KV 接口地址（默认: {DEFAULT_API_URL}）")
    parser.add_argument("--devcloud", action="store_true",
                        help="Route requests through the HTTP proxy set in WIKI_KV_PROXY_URL")
    parser.add_argument("--no-sources", action="store_true",
                        help="不同步 sources 目录（原文/摘要/指令）")
    parser.add_argument("--only-sources", action="store_true",
                        help="只同步 sources 目录")
    parser.add_argument("--clean", action="store_true",
                        help="[兼容] 同步前先清空 KV，等价于 --mode clean-rebuild")
    parser.add_argument("--clean-only", action="store_true",
                        help="只清空 KV 中该author的所有数据，不执行同步")
    parser.add_argument("--mode", choices=["incremental", "full", "clean-rebuild"],
                        default="incremental",
                        help="同步模式：incremental(默认,基于 state diff) / full(全量推送但不清空 KV) / clean-rebuild(清空后全量推送)")
    parser.add_argument("--no-hdfs-state", action="store_true",
                        help="不使用 HDFS 持久化 sync state，仅使用本地兜底文件")
    parser.add_argument("--hdfs-state-dir", type=str, default=None,
                        help=f"HDFS 上存放 sync state 的目录（默认: {DEFAULT_HDFS_KV_STATE_DIR}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，不实际调用接口")
    parser.add_argument("--concurrency", "-c", type=int, default=5,
                        help="并发请求数（默认: 5）")
    parser.add_argument("--timeout", type=int, default=30,
                        help="单次请求超时秒数（默认: 30）")
    parser.add_argument("--wiki-dir", type=str, default=None,
                        help="手动指定 wiki 目录路径（不使用内置 zip 包）")
    parser.add_argument("--no-verify", action="store_true",
                        help="同步后跳过自动验证")
    parser.add_argument("--verify-sample", type=int, default=20,
                        help="验证时随机抽样的文件节点数（默认: 20）")

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
                print(f"   {b}: {size/1024:.0f} KB {in_config}")
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
        print(f"🚀 批量同步 {len(users)} 个author: {', '.join(users)}")
    elif args.user:
        raw_keys = [k.strip() for k in args.user.split(",") if k.strip()]
        bad_keys = [k for k in raw_keys if k not in config.USER_MAP]
        if bad_keys:
            print(f"❌ 未知用户: {', '.join(bad_keys)}")
            print(f"   可选: {', '.join(config.USER_MAP.keys())}")
            sys.exit(1)
        users = raw_keys
        if len(users) > 1:
            print(f"🚀 批量同步 {len(users)} 个author: {', '.join(users)}")
    else:
        parser.print_help()
        sys.exit(1)

    print(f"   API: {args.api_url}")
    if args.devcloud:
        print(f"   🔀 通过 devcloud 代理转发")
    if args.dry_run:
        print(f"   ⚠️ 模拟运行模式")

    t_total_start = time.time()
    results = {}

    for user_key in users:
        success = sync_one_user(user_key, args)
        results[user_key] = success

    t_total = time.time() - t_total_start
    print(f"\n{'='*60}")
    print(f"📊 全部完成 | 总耗时: {t_total:.1f}s")
    print(f"{'='*60}")

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