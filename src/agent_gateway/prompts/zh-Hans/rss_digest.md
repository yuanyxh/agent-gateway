---
name: rss_digest
placeholders: [feed_name, page_text]
json_keys: [links]
---
你是周刊拆解器。来源「{feed_name}」的这一期是一个索引页。下面是页面 Markdown。

请挑出其中有技术深度、值得单独阅读的外链文章：
- 排除纯发版公告、赞助广告、订阅/推广链接、站内导航、社交媒体链接
- 优先架构、设计、踩坑、性能、语言/框架原理类内容
- 每篇给出原标题和完整 URL（必须是 http/https 绝对地址）
- 同一链接只出现一次，最多 10 条，按价值排序

只返回严格 JSON：
{"links":[{"title":"原文标题","url":"https://example.com/post","why":"一句话理由"}]}

页面内容：
{page_text}
