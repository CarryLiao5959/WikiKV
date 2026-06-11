"""
数据预处理：将 data/*.csv 中的source article和video transcript 转为按author分目录的 Markdown 文件。

支持两种 CSV 格式：
1. source article CSV：bizuin, query, time, title, content
   其中 content 是 JSON 字符串，包含 uin/title/content 字段。
2. video source CSV：分类, 作者昵称, finderuin, feedid, 描述, ASR, cover_ocr

输出格式：
raw/{user_key}/articles/{source_id}.md
---
source_id: {source_id}
source_type: article|video   # 数据来源类型
title: {title}
date: YYYY-MM-DD
---

（正文内容）
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RAW_BASE = BASE_DIR / "raw"

BIZUIN_MAP = {
    "1000000001": "demo",
    "1111122222": "luxun",
    "2222233333": "zhouzuoren",
    "3333344444": "zhuziqing",
    "4444455555": "xiaohong",
    "5555566666": "yudafu",
}

FINDERUIN_MAP = {
}


def _strip_zero_width(text: str) -> str:
    """移除字符串中的零宽字符（U+200B/200C/200D/FEFF/00AD）。"""
    return re.sub(r'[\u200b\u200c\u200d\ufeff\u00ad]', '', text)



_FULLWIDTH_TO_HALFWIDTH = str.maketrans(
    '！＂＃＄％＆＇（）＊＋，－．／'
    '０１２３４５６７８９'
    '：；＜＝＞？＠'
    'ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ'
    'ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ'
    '｛｜｝～',
    '!"#$%&\'()*+,-./'
    '0123456789'
    ':;<=>?@'
    'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    'abcdefghijklmnopqrstuvwxyz'
    '{|}~',
)


def normalize_title(title: str) -> str:
    """规范化标题文本。

    在 preprocess 源头统一处理，避免后续 ingest/lint 到处修补：
    1. 移除零宽字符
    2. 全角数字/字母→半角（全角中文标点保留，因为中文语境下是正确的）
    3. 连续空白合并为单个空格
    4. 去除首尾空白
    5. 移除 emoji（文件名中 emoji 会导致 YAML 解析、文件系统兼容性等问题）
    """
    if not title:
        return title
    title = _strip_zero_width(title)
    title = _fullwidth_alphanum_to_half(title)
    title = _remove_emoji(title)
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def normalize_content(content: str) -> str:
    """规范化正文内容。

    比标题宽松：保留 emoji（正文中 emoji 不影响文件名和 YAML），
    只做零宽字符清理和空白规范化。
    """
    if not content:
        return content
    content = _strip_zero_width(content)
    content = _fullwidth_alphanum_to_half(content)
    lines = content.split('\n')
    lines = [line.rstrip() for line in lines]
    content = '\n'.join(lines)
    return content


def _fullwidth_alphanum_to_half(text: str) -> str:
    """全角数字和字母转半角，保留全角中文标点不变。

    只转换：０-９ Ａ-Ｚ ａ-ｚ
    不转换：！？。，、；：""''（）【】等中文标点
    """
    result = []
    for ch in text:
        cp = ord(ch)
        if 0xFF10 <= cp <= 0xFF19:
            result.append(chr(cp - 0xFF10 + ord('0')))
        elif 0xFF21 <= cp <= 0xFF3A:
            result.append(chr(cp - 0xFF21 + ord('A')))
        elif 0xFF41 <= cp <= 0xFF5A:
            result.append(chr(cp - 0xFF41 + ord('a')))
        else:
            result.append(ch)
    return ''.join(result)


def _remove_emoji(text: str) -> str:
    """移除 emoji 和特殊 Unicode 符号，保留中日韩文字和基本标点。

    保留范围：ASCII、中日韩统一表意文字、中日韩标点、常用符号
    移除范围：Emoji、Dingbats、Enclosed Alphanumerics、Miscellaneous Symbols 等
    """
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # Emoticons
        "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs
        "\U0001F680-\U0001F6FF"  # Transport and Map
        "\U0001F1E0-\U0001F1FF"  # Flags
        "\U0001F900-\U0001F9FF"  # Supplemental Symbols
        "\U0001FA00-\U0001FA6F"  # Chess Symbols
        "\U0001FA70-\U0001FAFF"  # Symbols Extended-A
        "\U00002702-\U000027B0"  # Dingbats
        "\U0000FE00-\U0000FE0F"  # Variation Selectors
        "\U0000200D"             # Zero Width Joiner
        "\U000020E3"             # Combining Enclosing Keycap
        "\U00002600-\U000026FF"  # Misc Symbols
        "\U0000231A-\U0000231B"  # Watch/Hourglass
        "\U00002934-\U00002935"  # Arrows
        "\U000025AA-\U000025AB"  # Squares
        "\U000025FB-\U000025FE"  # Squares
        "\U00002B05-\U00002B07"  # Arrows
        "\U00002B1B-\U00002B1C"  # Squares
        "\U00002B50"             # Star
        "\U00002B55"             # Circle
        "\U00003030"             # Wavy Dash
        "\U0000303D"             # Part Alternation Mark
        "\U00003297"             # Circled Ideograph Congratulation
        "\U00003299"             # Circled Ideograph Secret
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub('', text)
    text = re.sub(r'  +', ' ', text)
    return text.strip()


def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    name = normalize_title(name)
    name = re.sub(r'[<>:"/\\|?*\[\]]', '_', name)
    name = name.strip('. ')
    return name[:200]  # 限制长度


def parse_date(time_str: str) -> str:
    """将 '2026/3/29 20:05' 格式转为 'YYYY-MM-DD'。"""
    try:
        dt = datetime.strptime(time_str.strip(), "%Y/%m/%d %H:%M")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        try:
            dt = datetime.strptime(time_str.strip(), "%Y/%m/%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return time_str.strip()


def extract_content(content_raw: str) -> str:
    """从 content 字段提取正文。content 可能是 JSON 字符串。"""
    try:
        data = json.loads(content_raw)
        return data.get("content", content_raw)
    except (json.JSONDecodeError, TypeError):
        return content_raw


def _detect_csv_type(csv_path: Path) -> str:
    """根据 CSV 表头检测文件类型：'article'(source corpus) 或 'video'(video source)。"""
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        header_lower = [h.strip().lower() for h in header]
        if "finderuin" in header_lower or "feedid" in header_lower:
            return "video"
        return "article"


def _extract_title_from_desc(desc: str) -> str:
    """从video source描述中提取标题（取 # 标签前或前 50 字）。"""
    desc = _strip_zero_width(desc.strip())
    if not desc:
        return ""
    title = re.split(r'\s*#', desc)[0].strip()
    if len(title) > 60:
        title = title[:60] + "…"
    return title


def process_article_csv(csv_path: Path, output_base: Path, stats: dict,
                        allowed_bizuins: set = None):
    """处理source article CSV 文件，按 bizuin 分目录输出。

    Args:
        allowed_bizuins: 如果指定，只处理这些 bizuin 的文章；None 表示处理所有。
    """
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            bizuin = row.get("bizuin", "").strip()
            query = row.get("query", "").strip()
            time_str = row.get("time", "").strip()
            title = row.get("title", "").strip()
            content_raw = row.get("content", "")

            if not query or not title:
                stats["skipped"] += 1
                continue

            if allowed_bizuins is not None and bizuin not in allowed_bizuins:
                continue

            if not query or not title:
                stats["skipped"] += 1
                continue

            content = extract_content(content_raw)
            if not content or len(content) < 50:
                stats["skipped"] += 1
                continue

            title = normalize_title(title)
            content = normalize_content(content)

            user_key = BIZUIN_MAP.get(bizuin)
            if user_key:
                output_dir = output_base / user_key / "articles"
            else:
                output_dir = output_base / "articles"
            output_dir.mkdir(parents=True, exist_ok=True)

            date_str = parse_date(time_str)
            filename = sanitize_filename(query) + ".md"
            filepath = output_dir / filename

            if filepath.exists():
                stats["skipped"] += 1
                continue

            escaped_title = title.replace('"', '\\"')
            md_content = f"""---
source_id: {query}
source_type: article
bizuin: {bizuin}
title: "{escaped_title}"
date: {date_str}
---


{content}
"""
            filepath.write_text(md_content, encoding="utf-8")
            stats["processed"] += 1
            stats["by_source"][f"article:{bizuin}"] = stats["by_source"].get(f"article:{bizuin}", 0) + 1


def process_video_csv(csv_path: Path, output_base: Path, stats: dict,
                      allowed_finderuins: set = None):
    """处理video source CSV（author_feed_data.csv），按 finderuin 分目录输出。

    video source数据的特点：
    - ASR 字段是视频语音转文字，是主要知识内容
    - 描述字段作为标题来源
    - cover_ocr 是封面文字识别，可辅助补充

    Args:
        allowed_finderuins: 如果指定，只处理这些 finderuin 的视频；None 表示处理所有。
    """
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            finderuin = str(row.get("finderuin", "")).strip()
            feedid = str(row.get("feedid", "")).strip()
            desc = row.get("描述", row.get("desc", "")).strip()
            asr = row.get("ASR", row.get("asr", "")).strip()
            cover_ocr = row.get("cover_ocr", "").strip()

            if not feedid:
                stats["skipped"] += 1
                continue

            if allowed_finderuins is not None and finderuin not in allowed_finderuins:
                continue

            if not asr or len(asr) < 50:
                stats["skipped"] += 1
                continue

            title = _extract_title_from_desc(desc)
            if not title:
                title = f"video source_{feedid}"

            title = normalize_title(title)
            asr = normalize_content(asr)
            if cover_ocr:
                cover_ocr = normalize_content(cover_ocr)

            user_key = FINDERUIN_MAP.get(finderuin)
            if user_key:
                output_dir = output_base / user_key / "articles"
            else:
                output_dir = output_base / "articles"
            output_dir.mkdir(parents=True, exist_ok=True)

            filename = sanitize_filename(f"video_{feedid}") + ".md"
            filepath = output_dir / filename

            if filepath.exists():
                stats["skipped"] += 1
                continue

            escaped_title = title.replace('"', '\\"')

            content_parts = [asr]
            if cover_ocr and cover_ocr != title:
                content_parts.append(f"\n\n> 封面文字：{cover_ocr}")

            md_content = f"""---
source_id: video_{feedid}
source_type: video
finderuin: {finderuin}
title: "{escaped_title}"
date: unknown
tags_raw: {desc}
---


{''.join(content_parts)}
"""
            filepath.write_text(md_content, encoding="utf-8")
            stats["processed"] += 1
            stats["by_source"][f"video:{finderuin}"] = stats["by_source"].get(f"video:{finderuin}", 0) + 1


def _resolve_users(user_args: list[str]) -> tuple[set[str], set[str]]:
    """将用户参数解析为 bizuin 集合和 finderuin 集合。

    支持三种格式：
    - user_key（如 xuexishi）→ 反查 BIZUIN_MAP / FINDERUIN_MAP
    - bizuin 数字 ID（如 3282315433）→ 直接使用
    - 逗号分隔的混合格式（如 xuexishi,3213052039）
    """
    key_to_bizuin = {v: k for k, v in BIZUIN_MAP.items()}
    key_to_finderuin = {v: k for k, v in FINDERUIN_MAP.items()}

    allowed_bizuins = set()
    allowed_finderuins = set()

    for arg in user_args:
        for token in arg.split(","):
            token = token.strip()
            if not token:
                continue
            if token in key_to_bizuin:
                allowed_bizuins.add(key_to_bizuin[token])
            elif token in key_to_finderuin:
                allowed_finderuins.add(key_to_finderuin[token])
            elif token in BIZUIN_MAP:
                allowed_bizuins.add(token)
            elif token in FINDERUIN_MAP:
                allowed_finderuins.add(token)
            else:
                print(f"  ⚠️ 未知用户: {token}（不在 BIZUIN_MAP 或 FINDERUIN_MAP 中）")

    return allowed_bizuins, allowed_finderuins


def main():
    import argparse
    parser = argparse.ArgumentParser(description="预处理source corpus/video source CSV 数据为 Markdown 文件")
    parser.add_argument("--users", nargs="+", default=None,
                        help="指定要处理的用户（支持 user_key 或 bizuin/finderuin ID，逗号分隔）。"
                             "不指定则处理所有用户。示例: --users xuexishi,duojin 或 --users 3282315433")
    args = parser.parse_args()

    RAW_BASE.mkdir(parents=True, exist_ok=True)

    allowed_bizuins = None  # None 表示不过滤
    allowed_finderuins = None
    if args.users:
        allowed_bizuins, allowed_finderuins = _resolve_users(args.users)
        names = []
        for b in allowed_bizuins:
            names.append(f"{BIZUIN_MAP.get(b, b)}({b})")
        for f in allowed_finderuins:
            names.append(f"{FINDERUIN_MAP.get(f, f)}({f})")
        print(f"🎯 仅处理指定用户: {', '.join(names)}")
        if not allowed_bizuins and not allowed_finderuins:
            print("错误：未匹配到任何有效用户")
            sys.exit(1)

    stats = {"processed": 0, "skipped": 0, "by_source": {}}

    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"错误：在 {DATA_DIR} 中未找到 CSV 文件")
        sys.exit(1)

    print(f"找到 {len(csv_files)} 个 CSV 文件:")
    for f in csv_files:
        csv_type = _detect_csv_type(f)
        print(f"  - {f.name} ({csv_type})")

    for csv_path in csv_files:
        csv_type = _detect_csv_type(csv_path)
        print(f"\n处理 {csv_path.name} ({csv_type}) ...")
        if csv_type == "video":
            process_video_csv(csv_path, RAW_BASE, stats,
                              allowed_finderuins=allowed_finderuins)
        else:
            process_article_csv(csv_path, RAW_BASE, stats,
                                allowed_bizuins=allowed_bizuins)

    print(f"\n{'='*50}")
    print(f"预处理完成!")
    print(f"  处理文章数: {stats['processed']}")
    print(f"  跳过文章数: {stats['skipped']}")
    print(f"  输出目录: {RAW_BASE}")
    for source_key, count in sorted(stats["by_source"].items()):
        src_type, src_id = source_key.split(":", 1)
        if src_type == "article":
            user_key = BIZUIN_MAP.get(src_id, "unknown")
            print(f"  source corpus {user_key} ({src_id}): {count} 篇")
        else:
            user_key = FINDERUIN_MAP.get(src_id, "unknown")
            print(f"  video source {user_key} ({src_id}): {count} 条")


if __name__ == "__main__":
    main()
