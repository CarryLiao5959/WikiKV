# Wiki Schema

## 目录结构

知识页类型定义在 `wiki/page_types.yaml` 中，首次构建时由 LLM 自动分析文章内容生成，也可手动设定。
每个author拥有个性化的目录体系。

**固定基础设施目录**：
```
wiki/
├── sources/               # 来源页总目录
│   ├── digests/           #   摘要页 — LLM 生成的每篇文章结构化摘要
│   ├── articles/          #   原文页 — source-document originals
│   └── instructions/      #   author instructions — the author's personal style and writing habits
├── syntheses/             # 查询沉淀页 — 有价值的查询结果存档
├── index.md               # 目录概览（列出所有目录及其描述 + 页面数）
├── overview.md            # 全局综合概览（每次摄入修订）
├── log.md                 # 操作日志（追加式）
└── page_types.yaml        # 知识页类型注册表
```

**动态知识页目录**（由 `page_types.yaml` 定义，首次构建时 LLM 自动生成，每个author不同）：
```
wiki/
├── {类型A}/               # 由 page_types.yaml 定义的知识页目录
├── {类型B}/               # 每个author拥有个性化的目录体系
└── {类型C}/               # 具体目录名和描述见系统提示词中的「可用目录」
```

## Frontmatter 格式

```yaml
---
type: source|synthesis|{动态类型}
aliases: [别名/外文名/不同译名列表]
tags: [标签列表]
---
```

> `type` 字段由目录名决定。
> `aliases` 字段用于检索匹配，填写该实体的常见别名、外文名、不同译名。
> `created` 和 `updated` 由代码自动注入，不需要 LLM 输出。

### 知识页通用模板 ({dir}/)

```markdown
---
type: {目录名}
aliases: [别名列表]
tags: [标签列表]
---

# 页面标题

## 基本信息
- 关键属性1：
- 关键属性2：

## 核心事实
- 事实1（用完整表述，不用代词）
- 事实2

## 相关页面
- [[目录名/页面名]] — 关联说明

## 相关来源
- [[sources/digests/YYYY-MM-DD-标题关键词]] — 来源说明
```

### 来源摘要页模板 (sources/digests/)

> ⚠️ 以下 5 个章节**全部必须输出，不可省略**（即使内容很少也必须写）。

```markdown
---
type: source
source_date: YYYY-MM-DD
source_article: <原文 stem，由系统给出，照抄，不带路径前缀、不带 .md 后缀>
tags: [标签列表]
---

# 文章标题

> Source: corpus | {date}

## 摘要
一段不超过200字的摘要。（必须）

## 核心观点
- The author's view (the author's own judgment/opinion, not encyclopedic fact)（必须）

## 关键引用
- "原文中可直接引用的精彩原话"——the author's comment / opinion（必须，至少1条）

## 关键信息
- 事实1
- 事实2
（必须，至少2条）

## 提及实体
- [[实体名]]
（必须，至少1个）
```

## 写作规范（知识库供 AI 检索）

- **信息密度优先**：用结构化的事实列表，不要散文段落
- **关键词完整**：人名、作品名用完整表述，不用代词（"他""其"）
- **标题信息型**：用"生平与风格"而非"音乐人生"，用"结构分析"而非"走进音乐深处"
- **别名标注**：所有实体的常见别名、外文名、不同译名写入 frontmatter 的 aliases 字段
- **相关来源具体化**：链接到摘要页使用完整路径 `[[sources/digests/YYYY-MM-DD-标题]]`

## 命名规范

- 知识页：使用中文名（如 `谭盾.md`、`有机音乐.md`）
- 来源摘要页：`YYYY-MM-DD-标题关键词.md`（存放在 `sources/digests/` 下）
- 文件名避免特殊字符：`/` → `-`，`"` → 删除，`|` → `-`
- 双向链接统一用 `[[页面名]]` 语法（不含 .md 后缀）
