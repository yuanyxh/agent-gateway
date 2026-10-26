---
name: rss_digest
placeholders: [feed_name, page_text]
json_keys: [links]
---
You expand a weekly digest. This issue of "{feed_name}" is an index page; its Markdown is below.

Pick external articles with real technical depth:
- Exclude pure release announcements, sponsor slots, subscribe/promotion links, site navigation, and social media links
- Prefer architecture, design, debugging, performance, and language/framework internals
- Return the original title and a full URL (absolute http/https only)
- Each link appears once, at most 10 links, ordered by value

Return strict JSON only:
{"links":[{"title":"Original title","url":"https://example.com/post","why":"one-line reason"}]}

Page content:
{page_text}
