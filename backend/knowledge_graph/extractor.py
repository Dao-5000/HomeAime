# -*- coding:utf-8 -*-
"""
Knowledge Extractor v1.0
知识抽取器：

  利用 LLM 从聊天记录中提取实体、关系和事件，
  构建用户个人知识图谱。
"""
import json

from .. import config
from ..deepseek_api import chat_once
from ..llm_json import loads_llm_json
from .database import save_entity, save_relation, save_event, get_entity_by_name
from .entity import normalize_entity_type
from .relation import normalize_relation_type


# 知识抽取 Prompt（v2.0：confidence + event_type 枚举 + related_entity）
KG_PROMPT = """你是知识图谱抽取专家。从以下对话中抽取结构化知识，只输出JSON，不要解释。

对话内容：
{chat}

输出格式：
{{
  "entities": [
    {{
      "name": "实体名称",
      "type": "实体类型（从以下选一个）：user/person/company/place/object/interest/project/emotion/skill/goal/habit/event/concept/media/food/pet/health/other",
      "description": "简短描述（20字内）",
      "importance": 1-10的整数
    }}
  ],
  "relations": [
    {{
      "source": "源实体名称",
      "relation": "关系类型（从以下选一个）：works_at/likes/dislikes/knows/participates/causes/related_to/important_for/lives_in/studies/owns/friends_with/family_of/goal_of/habit_of/skill_of/interested_in/affected_by/leads_to/part_of",
      "target": "目标实体名称",
      "confidence": 置信度（0.3-1.0的小数）,
      "source_type": "关系来源（从以下选一个）：explicit（用户明确说的）/inferred（根据上下文推测的）/contradicted（有矛盾信号的）"
    }}
  ],
  "events": [
    {{
      "description": "事件描述（30字内）",
      "time": "时间（没有则留空字符串）",
      "importance": 1-10的整数,
      "event_type": "事件类型（从以下选一个）：life/work/emotion/relationship/health/achievement/conflict/plan/other",
      "related_entity": "最相关的实体名称（没有则留空字符串）"
    }}
  ]
}}

抽取规则：
1. confidence：用户明确说的填0.8-1.0，推测的填0.4-0.6，有矛盾信号的填0.2-0.3
2. importance：影响用户生活全局的填8-10，一般信息填4-7，偶然提及填1-3
3. 只抽取有实际意义的实体，不抽取代词和泛指词
4. related_entity必须是entities里出现过的实体名称"""


async def extract_knowledge(chat_text, character_id="default"):
    """
    使用 LLM 从聊天中提取知识。

    Args:
        chat_text: 聊天文本

    Returns:
        dict: {entities, relations, events}
    """
    key = config.memory_key()
    if not key:
        return {"entities": [], "relations": [], "events": []}

    if not chat_text or not str(chat_text).strip():
        return {"entities": [], "relations": [], "events": []}

    prompt = KG_PROMPT.format(chat=str(chat_text)[:6000])

    try:
        result = await chat_once(
            config.memory_extract_model(character_id),
            [
                {
                    "role": "system",
                    "content": "你是知识图谱抽取专家，只输出JSON，不要解释。"
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            key,
            temperature=0.2,
            # ★ 2026-09-12：1000 太小 —— 实测输出在 char 3100 附近被截断，
            #   日志里 56 次 "Unterminated string / Expecting ',' delimiter" 全是这个。
            #   max_tokens 只是上限、不额外计费，正常输出仍会自然 stop。
            max_tokens=4000
        )
    except Exception as e:
        print(f"[KnowledgeGraph] LLM调用失败: {e}", flush=True)
        return {"entities": [], "relations": [], "events": []}

    try:
        # ★ 2026-09-12：改用容错解析（剥围栏 → 抠最外层 {} → 截断救援）。
        #   原来直接 json.loads(cleaned)，一旦输出被 max_tokens 截断就**整批丢弃**，
        #   只打一行日志、不抛异常 —— 实测 422 次抽取里 325 次是
        #   「0实体, 0关系, 0事件」，知识在无声无息地丢。
        data = loads_llm_json(result)
        if not isinstance(data, dict):
            raise ValueError("模型未返回 JSON 对象: %r" % str(result or "")[:200])
        return {
            "entities": data.get("entities", []),
            "relations": data.get("relations", []),
            "events": data.get("events", []),
        }
    except Exception as e:
        print(f"[KnowledgeGraph] JSON解析失败: {e}", flush=True)
        return {"entities": [], "relations": [], "events": []}


async def run_knowledge_extraction(session_id, character_id, messages,
                                    last_extracted_id=0):
    """
    升级版抽取：增量抽取 + confidence + event_type + related_entity_id
    last_extracted_id: 上次已抽取到的消息id，只抽取新增部分

    Args:
        session_id: 会话ID
        character_id: 角色ID
        messages: 消息列表
        last_extracted_id: 增量游标（0=首次全量）

    Returns:
        dict: {entity_count, relation_count, event_count}
    """
    if not messages:
        return {"entity_count": 0, "relation_count": 0, "event_count": 0}

    # 只取新增消息（增量抽取，不重复处理）
    if last_extracted_id > 0:
        new_msgs = [m for m in messages
                    if int(m.get("id", 0)) > last_extracted_id]
    else:
        new_msgs = messages[-10:]   # 首次抽取取最近10条

    if not new_msgs:
        return {"entity_count": 0, "relation_count": 0, "event_count": 0}

    # 构建聊天文本（新增消息 + 最近5条上下文）
    context_msgs = messages[-5:] if len(messages) > 5 else messages
    all_msgs = {m.get("id"): m for m in context_msgs + new_msgs}
    chat_text = "\n".join(
        f"{m.get('role','')}: {str(m.get('content',''))[:200]}"
        for m in sorted(all_msgs.values(), key=lambda x: x.get("id", 0))
    )

    # 提取知识
    knowledge = await extract_knowledge(chat_text, character_id=character_id)

    # 实体保存（带importance）
    entity_name_to_id = {}
    for entity in knowledge.get("entities", []):
        name        = str(entity.get("name", "")).strip()
        etype       = normalize_entity_type(entity.get("type", "other"))
        desc        = str(entity.get("description", ""))[:100]
        importance  = int(entity.get("importance", 5))
        if not name:
            continue
        try:
            eid = save_entity(
                session_id, character_id, etype, name, desc,
                importance=importance
            )
            if eid:
                entity_name_to_id[name] = eid
        except Exception as e:
            print(f"[KnowledgeGraph] 保存实体失败: {e}", flush=True)

    # 关系保存（带confidence和source_type）
    rel_count = 0
    for rel in knowledge.get("relations", []):
        source_name = str(rel.get("source", "")).strip()
        target_name = str(rel.get("target", "")).strip()
        rel_type    = normalize_relation_type(rel.get("relation", "related_to"))
        try:
            confidence  = float(rel.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence  = 0.5
        source_type = str(rel.get("source_type", "inferred"))

        # confidence兜底：按source_type校正
        _conf_floor = {"explicit": 0.7, "inferred": 0.4, "contradicted": 0.2}
        confidence  = max(confidence, _conf_floor.get(source_type, 0.4))

        if not source_name or not target_name:
            continue
        try:
            source_id = (entity_name_to_id.get(source_name)
                         or _get_or_create_entity(session_id, character_id, source_name))
            target_id = (entity_name_to_id.get(target_name)
                         or _get_or_create_entity(session_id, character_id, target_name))
            if source_id and target_id:
                save_relation(
                    session_id, source_id, target_id, rel_type,
                    confidence=confidence, source_type=source_type,
                    character_id=character_id
                )
                rel_count += 1
        except Exception as e:
            print(f"[KnowledgeGraph] 保存关系失败: {e}", flush=True)

    # 事件保存（带event_type和related_entity_id）
    evt_count = 0
    for evt in knowledge.get("events", []):
        desc           = str(evt.get("description", "")).strip()
        time_str       = str(evt.get("time", ""))
        try:
            importance     = int(evt.get("importance", 5))
        except (TypeError, ValueError):
            importance     = 5
        event_type     = evt.get("event_type", "other")
        related_entity = str(evt.get("related_entity", "")).strip()

        # event_type枚举校验
        _valid_types = {
            "life","work","emotion","relationship","health",
            "achievement","conflict","plan","other"
        }
        if event_type not in _valid_types:
            event_type = "other"

        # 查related_entity_id
        related_entity_id = None
        if related_entity:
            related_entity_id = (
                entity_name_to_id.get(related_entity)
                or _get_entity_id_by_name(session_id, character_id, related_entity)
            )

        if desc:
            try:
                save_event(
                    session_id, character_id, event_type, desc,
                    time_str, importance,
                    related_entity_id=related_entity_id
                )
                evt_count += 1
            except Exception as e:
                print(f"[KnowledgeGraph] 保存事件失败: {e}", flush=True)

    # 记录本次已抽取到的最新消息id（增量游标）
    if new_msgs:
        try:
            last_id = max(int(m.get("id", 0)) for m in new_msgs)
            _save_extraction_cursor(session_id, character_id, last_id)
        except Exception:
            pass

    print(f"[KnowledgeGraph] 抽取完成: {len(entity_name_to_id)}实体, {rel_count}关系, {evt_count}事件", flush=True)
    return {
        "entity_count": len(entity_name_to_id),
        "relation_count": rel_count,
        "event_count": evt_count,
    }


def _get_or_create_entity(session_id, character_id, name):
    """查找或创建other类型实体"""
    try:
        existing = get_entity_by_name(session_id, character_id, name)
    except Exception:
        existing = None
    if existing:
        return existing["id"]
    try:
        return save_entity(session_id, character_id, "other", name, "")
    except Exception:
        return None


def _get_entity_id_by_name(session_id, character_id, name):
    try:
        row = get_entity_by_name(session_id, character_id, name)
    except Exception:
        row = None
    return row["id"] if row else None


def _save_extraction_cursor(session_id, character_id, last_msg_id):
    """记录增量抽取游标到db"""
    try:
        from .. import db as _db
        _db.q("""INSERT OR REPLACE INTO kg_extraction_cursor
               (session_id, character_id, last_msg_id, updated_at)
               VALUES(?,?,?,datetime('now'))""",
              (session_id, character_id, last_msg_id))
    except Exception:
        pass
