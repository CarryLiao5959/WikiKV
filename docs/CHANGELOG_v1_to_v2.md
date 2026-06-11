# LLM Wiki v1 → v2 变更日志

从 `architecture_v1.md` 到 `architecture.md` 的所有变更，按"出了什么问题 → 怎么修改"组织。

---

## 背景

对比 v1（`author_a_wiki_v1/`、`author_b_wiki_v1/`）和 v2（`author_a_wiki/`、`author_b_wiki/`）的实际产出，发现 v1 存在以下系统性问题：

1. **知识概览缺失**：v1 的 `index.md` 只有目录概览，没有 LLM 生成的知识概览 blockquote。排查发现是 `_rebuild_global_index` 的 bug——概览生成失败时 `last_overview_at` 仍然更新，导致后续不再重试，形成死锁。
2. **Token 浪费严重**：Step2 每次都把所有目录的所有页面名+别名全量拼入 prompt，但每批通常只涉及少数目录，大量 token 被浪费。
3. **修复质量差**：三个 LLM 修复函数只给截断的页面内容（body 截 1500-2000 字），LLM 看不到全貌，修复不准。
4. **修复历史不可追溯**：`error_book.yaml` 只记录当前状态，不记录"谁在什么时候用什么方法修了什么"，无法回溯历史。
5. **问题频率无法统计**：没有聚合统计手段，只能手动翻 error_book 猜趋势，无法数据驱动地改 prompt。
6. **Digest 残缺章节直接入库**：写盘前只检查 `source_article`，LLM 漏输出某个章节照样写盘，要等事后修复。
7. **知识页缺必要章节无检测**：LLM 经常遗漏 `## 核心事实`/`## 相关页面`/`## 相关来源`，但没有 lint 检测项。
8. **全角标点断链频发**：LLM 生成全角标点的 wikilink，v1 直接删除而非修正。
9. **同名页面冲突丢失**：`WikiGraph.pages` 同名覆盖，被挤掉的页面信息丢失。
10. **高频错题未系统化治理**：错题本只做"发现→注入→验证"，没有从高频错题反向驱动 prompt 迭代的流程。
11. **`_index.md` 与实际文件不一致**：v1 存在两类系统性问题——（a）幽灵条目：`_index.md` 引用了不存在的 `.md` 文件（LLM 写了索引但没写盘）；（b）遗漏条目：`.md` 文件存在但没列入 `_index.md`（LLM 写盘了但忘更新索引，或文件被放错目录）。实测 author_a_wiki_v1 有 11 处不一致，author_b_wiki_v1 有 11 处不一致；v2 的 author_a_wiki 已降至 0 处，author_b_wiki 仍有 7 处残留。

代码量变化也印证了这些问题的修复量：`ingest.py` +1177 行，`lint.py` +789 行，总计 +2400+ 行。

---

## 1. Prompt Token 浪费严重

**问题**：每次摄入时，Step2 的"已有知识页清单"可能上万字符，但每批 3 篇文章通常只涉及少数目录。全量展开所有目录的所有页面名和别名，token 利用率极低,速度也慢。

**修改**：新增 **Prompt 三级裁剪**（6.1 子节），节省 60-80% token：
- **Level 1 — 目录白名单**：Step1 选中的页面所在目录 + `topics`（易重名）完整展开，其余目录仅给一行摘要（目录名+页数）。砍 50-70% 字符
- **Level 2 — 别名按热度精简**：从本批文章粗抽候选实体词，只有命中 hot_names 的页面才展开 aliases，其余只列主名。再砍 30%
- **Level 3 — 紧凑格式**：从 `【composers】composers/巴赫(别名:...)` 压缩为 `【composers】\n  巴赫 | J.S.巴赫, Bach`。再省 10-15%

---

## 2. 修复历史不可追溯

**问题**：`error_book.yaml` 只记录当前状态（什么类型的问题出现过、哪些 sample 待修），不记录修复历史。无法回答：
- 这个项目历史上总共出过多少断链？
- 哪些是代码自动修的、哪些靠 LLM？
- 哪类问题最高频？

**修改**：新增 `lint_ledger.jsonl` 修复日志（8.3 节），每次修复追加一行 JSON：

```json
{"ts":"2026-04-20T11:21","file":"巴洛克音乐风格.md","issue_type":"broken_link","auto_fixed":true,"fix_method":"delete_link","note":"删除 [[composers/巴赫]]","count":1}
{"ts":"2026-04-20T15:00","file":"composers/巴赫.md","issue_type":"broken_link","auto_fixed":false,"fix_method":"llm_create_page","note":"LLM 定期修复创建缺失页面","count":1}
```

---

## 3. 无法统计问题频率、无法数据驱动改 prompt

**问题**：有了 ledger 日志后，仍需手动翻 JSONL 做统计，不方便识别高频问题优先级。

**修改**：新增 `ledger-report` 子命令（8.5 节）：

```bash
python main.py --user <u> ledger-report [--days 30]
```

输出聚合报告：
```
issue_type        | 总次数 | 自动修 | LLM修 | 最近7天
broken_link       |   42   |  38   |   4   |   2
missing_summary   |   15   |  15   |   0   |   0
incomplete_digest |    8   |   0   |   8   |   1
missing_sections  |    5   |   0   |   5   |   3
```

**用途**：高频问题 = 下一步改生成逻辑的优先级。比如 broken_link 出了 42 次，说明 LLM 生成时老写不存在的链接，应该改 prompt 约束而不是靠修复兜底。

---

## 4. Digest 写盘时残缺章节未拦住

**问题**：prompt 要求 digest 输出 5 个必须章节，但 LLM 有时漏掉（如 `## 提及实体`）。写盘前只检查了 `source_article` 一个字段（`_assert_digest_source_article`），缺章节照样写盘，要等后续 `fix_incomplete_digests` 事后修。

**修改**：新增 `_check_digest_completeness` 写盘前结构校验（6.3 节）：
- 检查 digest 的 5 个必须章节（摘要、核心观点、关键引用、关键信息、提及实体）是否都存在
- 缺失时按正确顺序插入占位内容 `（待补充）`，保证章节结构完整
- 这是"先拦住"策略——即使 LLM 漏输出某个章节，写盘时自动补上占位符，后续 `fix_incomplete_digests` 再用 LLM 填充真实内容

---

## 5. 知识页缺必要章节无检测无修复

**问题**：LLM 生成知识页时经常遗漏 `## 核心事实`/`## 相关页面`/`## 相关来源` 等必要章节，但 v1 没有 lint 检测项也没有修复机制。

**修改**：
- Lint 引擎新增第 15 项检测：`missing_sections`（知识页缺必要章节）
- 错题本新增类别：`missing_sections`
- 定期修复新增：`fix_missing_sections` LLM 修复（给**完整 body + 全部知识页名 + 全部 digest**）
- 维护命令串联步骤中补充 `missing_sections`

---

## 6. LLM 修复函数给截断上下文导致修复不准

**问题**：三个 LLM 修复函数只给截断的页面内容，LLM 看不到全貌，修复质量受限：
- `fix_missing_sections`：body 截取 1500 字；相关页面只给 20 个名字；相关来源只给 20 个 stem
- `fix_related_source_format`：body 截取 2000 字
- `fix_missing_summary`：body 截取 1500 字

**修改**：这三个单页面修复函数改为给**完整上下文**（8.4 节 LLM 上下文策略列）：
- `fix_missing_sections`：完整 body + 全部知识页名 + 全部 digest
- `fix_related_source_format`：完整 body
- `fix_missing_summary`：完整 body

**原则**：单页面修复上下文不长，完整性优先；多文档拼接场景（`fix_incomplete_digests`、`_llm_pick_source_article`、`fix_broken_links_from_error_book`、`_rebuild_global_index`）仍截断以避免上下文爆炸。

---

## 7. 相关来源格式问题无错题类别

**问题**：lint 能检测到 `## 相关来源` 格式问题（缺 `[[...]]` 或缺目录路径），但错题本没有对应的类别记录，无法驱动 LLM 修复。

**修改**：
- 错题本新增类别：`related_source_format`
- 定期修复新增：`fix_related_source_format` LLM 修复（给完整 body）

---

## 8. 知识页缺一句话概括无修复

**问题**：lint 能检测到知识页缺 blockquote 概括，但 v1 没有 LLM 修复机制。

**修改**：
- 定期修复新增：`fix_missing_summary` LLM 生成 blockquote（给完整 body）
- 错题本已有类别：`missing_summary`，现在有了对应的 LLM 修复

---

## 9. 全角标点断链频发

**问题**：LLM 生成的 wikilink 使用全角标点（`？` `！` `：`），而文件名用半角，导致大量断链。v1 的自动修复是直接删除这些"断链"。

**修改**：
- `wiki_page.py`：新增 `_normalize_width()` NFKC 归一化匹配
- `lint.py` 断链修复时先尝试全角→半角 NFKC 归一化修正，修正成功则改链接而非删除
- WikiGraph `get_page()` 新增全角→半角归一化 fallback
- `get_incoming_links()` 含 `_index.md` 和被冲突挤掉的页面

---

## 10. 同名页面冲突处理粗糙

**问题**：v1 的 `WikiGraph.pages` 只有一个 `name → WikiPage` 字典，同名页面冲突时后来的会覆盖前面的，丢失信息。

**修改**：WikiGraph 数据结构增强（7.1 节）：
- `pages` 字典增加同名冲突优先级：知识页(0) > digest(20) > synthesis(10) > article(30)
- 新增 `all_pages`：所有非 `_index` 页面（含被冲突挤掉的）
- 新增 `index_pages`：所有 `_index.md` 页面
- 新增 `_by_relpath`：相对路径 → WikiPage（精确路径匹配，避免同名歧义）
- 链接解析：`[[dir/name]]` 优先走 `_by_relpath` 精确匹配

---

## 11. 索引页引用漏报

**问题**：`_all_link_sources` 未纳入索引页，导致 `_index.md` 中指向某页面的引用不被统计为"入链"，漏报问题。

**修改**：`get_incoming_links()` 含 `_index.md` 和被冲突挤掉的页面，`_all_link_sources` 纳入索引页。

---

## 12. 高频错题无系统化 prompt 治理

**问题**：v1 的错题本只做"发现问题→注入约束→验证消失"，但没有从高频错题反向驱动 prompt 迭代的系统化流程。

**修改**：新增 8.6 节「高频错题的 prompt 治理」，记录了 9 个已识别的高频错题及其根因和 prompt 治理措施：

| 高频错题 | 根因 | prompt 治理 |
|---|---|---|
| `sources/articles/unknown-<标题>` 断链 | LLM 猜不对 stem | 预计算 stem 注入 prompt + 禁止 LLM 自己写 `## 原文` |
| 全角标点断链 | LLM 用全角标点 | NFKC 归一化匹配 + 修复时先尝试归一化 |
| 全名断链 | LLM 倾向用全名 | 命名规则强约束：只用短名 |
| 描述文字污染链接 | LLM 把叙述拉进方括号 | wikilink 边界规则 + 正反示例 |
| 占位符断链 | LLM 照抄日期模板 | 示例改用真实日期 + 禁用占位符 |
| 非音乐人物塞错目录 | 类型判断不准 | 新增"非音乐领域不要硬塞"规则 |
| 待整理堆积 | LLM 宁放待整理也不建新分区 | 每条必须归位 + 单条也允许新建分区 |
| 索引分类不合理 | LLM 只追加不调整 | 增加调整优化指令 |
| 知识页缺必要章节 | LLM 遗漏章节 | lint 检测 + LLM 修复 + 约束注入 prompt |

流程：`error_book.yaml` 定期 review → 识别 top 错题模式 → 改 prompt 规则/示例 → 下次 ingest 验证 → 错题消失则 pass_count++ → 自动 close。


---

## 13. `_index.md` 与实际文件不一致

**问题**：v1 的 `_index.md` 存在两类系统性不一致，经自动化脚本实测：

**A. 幽灵条目（索引有但文件没有）**：
- `author_a_wiki_v1`：education 目录引用了 `钢琴学习与陪练`、`音乐教育核心价值`，但无对应 `.md` 文件
- `author_b_wiki_v1`：composers 引用 `亨利·卢梭`、`弗里达·卡罗` 无文件；appreciation 引用 `G大调钢琴协奏曲`、`印象派音乐`、`西西里岛舞曲` 无文件；lifestyle 引用 `鲁迅` 无文件

根因：LLM 生成 `_index.md` 时引用了它计划写但最终没有写盘的页面，代码没有在写盘后校验索引与实际文件的一致性。

**B. 遗漏条目（文件有但索引没有）**：
- `author_a_wiki_v1`：`Author A.md`、`钢琴学习与陪练.md`、`音乐教育核心价值.md` 被错误写入 `composers/` 目录且不在索引中；`落日音乐会.md`(works)、`古典圣诞音乐.md`(appreciation)、`古典露天音乐节.md`(appreciation)、`音乐与技艺专注.md`(lifestyle) 等存在但不在索引
- `author_b_wiki_v1`：`印象派音乐.md`、`西西里岛舞曲.md` 出现在 composers 目录而非 topics/works；`弗里达·卡罗.md`、`鲁迅.md` 在 topics 但不在 lifestyle 索引

根因：LLM 把页面写到了错误的目录，而 `_index.md` 是按目录生成的，文件不在目标目录自然不会出现在索引中；另外索引生成时遗漏了部分页面。

**实测数据**：
| Wiki | PHANTOM | MISSING | 合计 |
|------|---------|---------|------|
| author_a_wiki_v1 | 2 | 9 | 11 |
| author_b_wiki_v1 | 5 | 6 | 11 |
| author_a_wiki (v2) | 0 | 0 | **0** |
| author_b_wiki (v2) | 4 | 3 | 7 |

**修改**：
- ✅ lint 断链检测新增 `_all_link_sources` 纳入索引页（第 11 项变更），`_index.md` 中的幽灵条目会被识别为断链
- ✅ WikiGraph 数据结构增强（`all_pages` + `_by_relpath`），放错目录的页面不再丢失
- ✅ `write_wiki_files()` 成对校验：知识页和 `_index.md` 必须同时存在才写入（第 4、5 项校验），防止"有索引无页面"或"有页面无索引"
- ✅ `write_wiki_files()` 写入顺序调整：先写非 `_index.md` 页面，再写 `_index.md`，避免索引引用了因校验被跳过的页面
- ✅ `_remove_phantom_index_entries()`：写 `_index.md` 前检查引用的页面文件是否实际存在于磁盘，移除幽灵条目
- ✅ `apply_dir_changes()` 迁移页面时同步更新 `_index.md`：源目录删除条目，目标目录追加条目（修复 author_b_wiki 的 7 处不一致）

**author_b_wiki v2 残留问题根因**：`apply_dir_changes()` 的 `move_page`/`split` 操作只移动了 `.md` 文件，没有更新两个目录的 `_index.md`——源目录索引仍引用已迁走的页面（幽灵条目），目标目录索引缺少新迁入的页面（遗漏条目）。这 7 处不一致全部是目录合并/迁移操作造成的。

---

## 14. 维护命令不完整

**问题**：v1 的 `maintain` 命令串联步骤缺少新增的修复项（missing_sections、related_source_format、summary），也没有 `ledger-report` 子命令。

**修改**：
- `maintain` 串联步骤补充 `missing_sections`、`related_source_format`
- 新增 `ledger-report [--days 30]` 子命令

---
