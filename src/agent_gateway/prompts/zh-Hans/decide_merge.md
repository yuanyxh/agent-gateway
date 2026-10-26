---
name: decide_merge
placeholders: [topic, facts, candidates]
json_keys: [action]
---
判断以下记忆主题是否应该归并到已有的知识笔记中。

待沉淀主题：{topic}
相关事实：
{facts}

已有笔记候选（检索命中，按相关度降序）：
{candidates}

判定标准：
1. **归并（action=merge）**：已有笔记与当前主题高度相关——同技术栈/同项目/同一件事的持续演进
2. **新建（action=new）**：仅沾边/命中通用词，或本质是新主题，强行归并会污染已有笔记
3. 多命中时选最相关的一个 target；内容演进/冲突仍可归并（reason 注明）
4. target 必须是上方候选列表中的 rel_path，不要臆造路径

返回 JSON：{"action": "merge" 或 "new", "target": "相对Extracts的md路径（merge时）", "reason": "一句话理由"}
只返回严格标准的 JSON 对象，不要代码块包裹。