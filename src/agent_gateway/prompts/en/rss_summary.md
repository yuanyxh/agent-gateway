---
name: rss_summary
placeholders: [items_text]
json_keys: [items]
---
You are a technical editor. Each entry below has a number, title, feed, and body.

For every article:
1. title_translation: if the original title is not English, provide an accurate English translation (title only, no quotes); return an empty string when the title is already English
2. summary: 2-5 English sentences covering what the article says and its key conclusion or method; do not restate the title and do not start with "This article"

Cover every entry. Return strict JSON only:
{"items":[{"index":0,"title_translation":"","summary":""}]}

Entries:
{items_text}
