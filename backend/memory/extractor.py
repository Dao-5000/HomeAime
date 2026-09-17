# -*- coding:utf-8 -*-
"""
记忆提取器：
  使用大模型从聊天记录中提取长期有价值的信息，
  分类为 fact（事实）、preference（喜好）、episode（重要经历）、emotion（长期情绪状态）。
"""
import json


MEMORY_PROMPT = """
分析下面聊天。

只提取长期有价值的信息。

类别：

fact:
用户事实（生日、职业、家庭、住址等稳定信息）

preference:
用户喜好（喜欢什么、讨厌什么、习惯等）

episode:
重要经历（发生过的重要事件、里程碑等）

emotion:
长期情绪状态（持续的情绪倾向、心理状态等）

返回JSON数组:

[
{
"type":"fact/preference/episode/emotion",
"content":"记忆内容，简洁明了",
"importance":1-10
}
]

要求：
1. 只提取长期有价值的信息，不要提取临时闲聊
2. content 要简洁明了，不超过50字
3. importance 1-10，10最重要
4. 不要重复提取已有的信息
5. 如果没有有价值的信息，返回空数组 []

聊天：

{chat}
"""


async def extract_memory(llm, chat):
    """
    从聊天中提取记忆。
    llm: 大模型调用对象，需要有 async chat(prompt) 方法
    chat: 聊天文本
    返回: 记忆数组 [{type, content, importance}]
    """
    result = await llm.chat(
        MEMORY_PROMPT.replace("{chat}", chat)
    )

    try:
        # 清理可能的 markdown 代码块
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        data = json.loads(cleaned)

        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "memories" in data:
            return data["memories"]
        else:
            return []

    except Exception:
        return []
