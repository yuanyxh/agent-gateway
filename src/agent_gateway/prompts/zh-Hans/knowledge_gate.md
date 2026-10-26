---
name: knowledge_gate
placeholders: [theme, scope, exclude, concept, why_now, disliked]
json_keys: [accept, reason]
---
你在审核一个知识卡片的选题是否越界。只做判断,不写卡片。

主题:{theme}
覆盖范围:{scope}
明确不覆盖:{exclude}
用户明确不感兴趣的方向:{disliked}
候选概念:{concept}
选择理由:{why_now}

五条必须同时满足:

1. 属于覆盖范围
2. 是该领域教科书或标准中的稳定概念,不是产品名、厂商方案、近五年新词或行业趋势
3. 具体到一个机制:一张卡片能讲到原理层面,不是一个领域全景
4. 是该主题的核心概念,不是为了「接龙」而找的相邻话题
5. 不是「用户明确不感兴趣的方向」的延伸(该列表为空时此条自动满足)

任何一条不满足就 accept=false,并在 reason 里写明违反了哪一条(不超过 30 字)。
只输出 JSON:

{"accept": true, "reason": ""}
