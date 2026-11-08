---
name: govern_groups
placeholders: [candidates_text]
json_keys: [decisions]
---

你是记忆分组审核器。输入是若干候选：多成员记忆组，或两条单条记忆组成的候选对。

{candidates_text}

判断规则：

- 只有属于同一次任务或同一个交付目标的记忆才应在同一组。
- 同项目、同主题但属于不同任务的记忆，不应合并。
- 不确定时返回 keep，不要为了合并而合并。
- 只能使用输入中出现的 id 和 candidate_id。
- split 的 groups 必须覆盖该候选的全部成员，且各组互不重叠。
- 多成员组执行 merge 时，merge_with 必须是同批次另一个 candidate_id。
- pair 执行 merge 表示这两条记忆应归为一组，不需要 merge_with。

返回 JSON: {"decisions": [{"candidate_id": "<id>", "action": "keep|split|merge", "groups": [["<member id>"]], "merge_with": "<candidate id>", "reason": "<不超过 50 字>"}]}

只返回严格标准的 JSON 对象，不要代码块。
