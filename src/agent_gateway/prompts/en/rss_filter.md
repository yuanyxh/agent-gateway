---
name: rss_filter
placeholders: [items_text]
json_keys: [items]
---
You triage an RSS daily digest. Below are the unread entries, numbered (title | feed | description).

Keep content with lasting technical value. Drop personal narratives, pure statistics, one-off operational incidents, pure release announcements (no architecture or design reasoning), and stale items.

For every entry return:
- index: entry number
- keep: true to keep / false to drop
- kind: article (normal post) / digest (weekly, roundup, or newsletter index that must be expanded into its external links) / skip (same as keep=false)
- reason: one short English sentence when dropped; empty string when kept

Cover every entry, none may be omitted. Return strict JSON only:
{"items":[{"index":0,"keep":true,"kind":"article","reason":""}]}

Entries:
{items_text}
