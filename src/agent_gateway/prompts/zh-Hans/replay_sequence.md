---
name: replay_sequence
placeholders: [current, sequences_text]
json_keys: [reinforce]
---

你是记忆重放器。当前记忆：

{current}

下面是它出发得到的关联路径，每行一条，`id|标题` 是节点，箭头表示跳转方向：

{sequences_text}

判断哪些路径是真实成立的关联：语义连贯、符合常识；两个毫无关系的话题只因向量相似被凑在一起时不要保留。

返回 JSON: {"reinforce": [["<神经元 id>", "<神经元 id>", "..."]]}
只返回严格标准的 JSON 对象, 不要代码块。
