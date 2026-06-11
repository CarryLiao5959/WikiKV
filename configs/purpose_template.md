# Wiki Purpose Descriptor (Template)

This file is the first-class *positioning descriptor* `P = <focus, audience, ingestion-bias>`
read by the schema cold-start and the evolution operators. Fill in the four
sections below for a new knowledge base. Keep it short (a few lines per section);
it is injected into construction prompts, not stored as long-form text.

## 核心定位 (Focus)
<One paragraph: what this wiki is about, whose corpus it is built from, and who
it serves. e.g., "A personal knowledge base compiled from the articles of
account X, serving followers interested in domain Y.">

## 重点知识领域 (Key Topics)
<A comma-separated list of the principal knowledge dimensions the wiki should
cover, e.g., topic-1, topic-2, topic-3, ...>

## 摄入侧重 (Ingestion Bias)
- 优先提取 (Prefer): <high-signal content the pipeline should always extract>
- 适度提取 (Moderate): <content to extract when relevant>
- 禁止提取 (Skip): <low-information categories to drop, e.g., pure call-to-action
  / "scan to follow" promotions, off-domain advertisements>

## 用户画像 (Audience)
<One or two sentences describing the intended readers and their information needs.>

---

### Example (filled)

> **核心定位**: 本 Wiki 是「示例作者」的专属知识库，服务关注该领域的读者，知识来源是该作者的文章与文字记录。
>
> **重点知识领域**: 领域A，领域B，领域C，……
>
> **摄入侧重**: 优先提取该作者的独到见解与深度分析；适度提取具体作品评论与自传性内容；禁止提取纯引流话术与无关广告。
>
> **用户画像**: 对该作者作品及其所在领域有浓厚兴趣的读者。
