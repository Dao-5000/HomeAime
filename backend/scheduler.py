# -*- coding: utf-8 -*-
"""
定时任务调度器：早晚安定时推送、一次性定时任务执行、节日/纪念日自动触发。
每 30 秒轮询一次，用 kv 表记录今日已触发状态避免重复。
"""
import asyncio
import re
from datetime import datetime, date, timedelta

from . import config, db, time_system, template_manager, anniversary_manager, character_manager, memory_manager
from .deepseek_api import chat_once, ModelApiError
from .http_client import get_http_client
from backend.loop_compat import get_loop

POLL_SECONDS = 30

# 判定"换词不换意"时要剔除的虚词/高频口水词：这些重合不算重复，只统计实词
_STOP_BIGRAMS = {
    "你们", "我们", "他们", "可以", "这个", "那个", "什么", "怎么", "因为",
    "所以", "但是", "不过", "就是", "还是", "已经", "现在", "时候", "一点",
    "一直", "真的", "知道", "觉得", "喜欢", "一下", "没有", "不会", "要不要",
    "是不是", "好不好", "有没有", "这样", "那样", "其实", "然后", "而且",
    "虽然", "如果", "以后", "之前", "刚刚", "马上", "时候", "有些", "有点",
    # 语气/疑问词尾：短句里占比很大，不能当实词统计。否则"你今天吃饭了吗"和
    # "你今天洗澡了吗"会仅因共用"你今/今天/了吗"就被误判成重复。
    "了吗", "了呢", "了吧", "的啊", "的呀", "好不", "要不", "有没", "是不",
    "了么", "的么", "行不", "对不", "好么", "好不好",
}


def _keyword_overlap(a, b, min_hits=3, min_ratio=0.5):
    """识别"换词不换意"的重复：整句字符相似度不高，但实词高度重合。

    典型例子（原阈值 0.75 会放行）：
      A：「下次得用亲亲来换」
      B：「下次可得用亲亲来换哦」
    字符相似度约 0.6，但实词（下次/得用/用亲/亲亲/亲来/来换）几乎全重合。

    做法：中文取 2-gram、英文取整词，剔除口水词后按较短一方算重合占比。
    """
    import re

    def words(s):
        out = set()
        for seg in re.findall(r'[\u4e00-\u9fff]+', s or ''):
            for i in range(len(seg) - 1):
                gram = seg[i:i + 2]
                if gram not in _STOP_BIGRAMS:
                    out.add(gram)
        for w in re.findall(r'[A-Za-z]{3,}', s or ''):
            out.add(w.lower())
        return out

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    inter = wa & wb
    if len(inter) < min_hits:
        return False
    denom = min(len(wa), len(wb))
    return denom > 0 and (len(inter) / float(denom)) >= min_ratio


def voice_night_probability(affection) -> float:
    """好感度 0→100 映射到语音晚安概率 10%→90%，最高也保留一点真人随机感。"""
    try:
        value = max(0.0, min(100.0, float(affection)))
    except (TypeError, ValueError):
        value = 50.0
    return round(0.10 + 0.80 * (value / 100.0), 3)


# ══════════════════════════════════════════════════════════════════════════
# 定时提醒「投递洗白」守卫（2026-09-16 控制器裁决）
# ══════════════════════════════════════════════════════════════════════════
# （完整裁决说明见 UI改版方案\_落地改动记录-2026-09-16-投递洗白修复.md）
_REMINDER_ITEM_MAXLEN = 60          # 超过这个长度 = 一整段独白，不是"一件事"
_REMINDER_RAMBLE_MINLEN = 30        # 这个长度以上还一个标点都没有 = 碎白话堆叠
# 句末语气词：以它们收尾的短句只可能是上一句话的残尾（「……了吧」「半吧」），
# 不可能是"一件要对 TA 说的事"。★ 不设最小长度：合法事项里也有 2 字的（睡觉/喝水/吃药）。
_REMINDER_TAIL_PARTICLES = "吧吗呢嘛哦噢喔咯嘞啦呀"
# 真·句读（含省略号/破折号）：出现任意一个就说明它有句子结构，不算"碎白话堆叠"。
# ★ 空格不算句读 —— 实测那条垃圾正是用空格把碎白话串起来的。
_REMINDER_PUNCT = re.compile(r"[，。！？；：、,.!?;:…—～「」『』（）()【】《》\"']")
# 聊天回显：`[03:34] 用户：…` 与裸的说话人前缀（与 chat_logic 的创建侧守卫同一口径）
_REMINDER_ECHO = re.compile(r"\[\d{1,2}:\d{2}\]\s*(?:用户|我|你|TA|ta|AI|ai|她|他|对方|群友)\s*[:：]")
_REMINDER_ECHO_BARE = re.compile(r"^(?:用户|我|你|TA|ta|AI|ai|她|他|对方|群友|助手)\s*[:：]")
# 管道文本：这句话是给模型的**指令**，不是给人看的内容
_REMINDER_PLUMBING = "这是用户引用/回复的那条消息原文"


def reminder_item_problem(content) -> str:
    """这个 content 能不能当"一条要投递给 TA 的提醒事项"？返回问题原因（"" = 可以投递）。

    判据是白名单式的一条总问句：**它像不像"一件要对 TA 说的事"**。
    命中的判据**全部**写进返回的原因（可能多条），便于回溯到底是哪里不对：

      ① 空 / 纯空白
      ② 含换行        —— 整段聊天记录被当事项
      ③ 含聊天回显    —— `[03:34] 用户：` / 裸的 `用户：` `AI：` 前缀
      ④ 含管道短语    —— `这是用户引用/回复的那条消息原文`
      ⑤ 长度 > 60     —— 一整段独白被当事项
      ⑥ 末位语气词    —— 「半吧」这类被截断的半句（★ 拦「半吧」的那条）
      ⑦ 碎白话堆叠    —— > 30 字却一个标点都没有（空格不算标点）

    ★ 一次报全部命中项（不是只报第一条）：一条脏内容往往同时犯几条
      （例如「我就去睡觉\n[03:34] 用户：…」既含换行又含回显），
      last_error 里要能看出全部问题，否则回溯时只看到一半原因。

    纯函数、不抛异常：调用方（投递路径）据此**先判后发**。
    """
    s = str(content if content is not None else "")
    if not s.strip():
        return "内容为空或纯空白，不是可用的提醒事项"
    problems = []
    if _REMINDER_ECHO.search(s) or _REMINDER_ECHO_BARE.search(s):
        problems.append("内容是聊天回显（含「[HH:MM] 用户：」这类说话人标记）")
    if _REMINDER_PLUMBING in s:
        problems.append("内容是给模型的管道指令（引用原文提示）")
    if "\n" in s or "\r" in s:
        problems.append("内容含换行（整段聊天记录被当事项）")
    if len(s) > _REMINDER_ITEM_MAXLEN:
        problems.append("内容超过 %d 字（一整段独白被当事项）" % _REMINDER_ITEM_MAXLEN)
    if s[-1] in _REMINDER_TAIL_PARTICLES:
        problems.append("内容以语气词「%s」收尾（上一句话的残尾，不是一件要对 TA 说的事）" % s[-1])
    if len(s) > _REMINDER_RAMBLE_MINLEN and not _REMINDER_PUNCT.search(s):
        problems.append("无标点长句（超 %d 字却一个标点都没有，碎白话堆叠）" % _REMINDER_RAMBLE_MINLEN)
    if not problems:
        return ""
    return "；".join(problems) + " —— 不是可用的提醒事项"


class Scheduler:
    def __init__(self):
        self._task = None
        self._push = None
        self._session = "default"
        self._character_id = "default"
        self._persona = {}
        self._last_mem_maintenance = 0   # 上次记忆维护时间戳
        self._last_ext_memory = 0        # 上次外置记忆库 tick 时间戳
        self._catchup_task = None

    def bind(self, push_fn):
        self._push = push_fn

    def set_session(self, session_id: str, persona: dict = None, character_id: str = None):
        if session_id:
            self._session = session_id
        # 每次绑定都覆盖人格，不能让上一个角色的人设残留到当前角色。
        self._persona = dict(persona or {})
        if self._persona and not self._persona.get("name"):
            self._persona["name"] = self._persona.get("character_name") or self._persona.get("self_name") or ""
        if character_id:
            # ★ 2026-09-15：角色名退化成 'default' 时自愈成"该会话最近说话的真实角色"。
            #   实测后果：绑成 default 的调度器用**兜底时段 08:00-23:00** 判闸门
            #   （`_gate` 记录里 char=default 就是这么来的），消息也记到 default 名下
            #   —— 用户在自己的角色会话里看不到、时段设置等于失效。
            try:
                from . import proactive_engine as _eng
                character_id = _eng.effective_character_id(self._session, character_id)
            except Exception:
                pass
            self._character_id = character_id
        # ★ 新增：记录程序本次打开时间（用于算用户离线多久）
        try:
            db.kv_set(
                "app_last_open:" + (self._character_id or "default"),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            pass

    def start(self):
        if self._task is None or self._task.done():
            self._task = get_loop().create_task(self._run())

    def on_online(self):
        """WebSocket 重新上线时补一次主动问候检查；内部 key 会防止重复发送。"""
        if self._catchup_task and not self._catchup_task.done():
            return
        try:
            self._catchup_task = get_loop().create_task(self._catch_up_on_start())
        except Exception:
            self._catchup_task = None

    async def _run(self):
        await asyncio.sleep(3)
        # ★ 新增：启动补偿（用户刚进程序，补发离线期间该有的心意）
        try:
            await self._catch_up_on_start()
        except Exception as e:
            print(f"[Scheduler] 启动补偿失败: {e}", flush=True)

        # ★ 启动补偿：补跑记忆维护（关机期间漏掉的；距上次>24h 才补，kv 持久化）
        await self._maybe_memory_maintenance()

        while True:
            try:
                await self._tick()
            except Exception:
                pass

            # ★ 记忆维护：距上次超过24小时就补跑（不限凌晨，关机后开机也能补）
            await self._maybe_memory_maintenance()

            # ★ CosyVoice 唱歌服务 watchdog：2026-09-12 起默认关闭（惰性化——
            #   改由使用时按需拉起，见 cosyvoice_mgr.py；否则空闲自动关闭后
            #   会被这里重新拉起，内存白省）。要恢复"常驻"行为，
            #   在 config.json 加 "COSYVOICE_AUTOSTART": true。
            try:
                if config.get("COSYVOICE_AUTOSTART", False):
                    self._ensure_cosyvoice_service()
            except Exception:
                pass

            await asyncio.sleep(POLL_SECONDS)

    def _ensure_cosyvoice_service(self):
        """CosyVoice 服务看门狗：按环境变量/安装目录查找，兼容打包部署。"""
        try:
            import socket
            _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _probe.settimeout(1)
            try:
                _probe.connect(("127.0.0.1", 9881))
                return
            except Exception:
                pass
            finally:
                _probe.close()
            import os, subprocess
            root = str(config.ROOT_DIR)
            candidates = [
                os.environ.get("COSYVOICE_HOME", ""),
                os.path.join(root, "CosyVoice"),
                os.path.join(os.path.dirname(root), "CosyVoice"),
                r"%COSYVOICE_HOME%",
            ]
            _cv_dir = next((p for p in candidates if p and os.path.isdir(p)), "")
            _cv_py  = os.path.join(_cv_dir, ".venv", "Scripts", "python.exe") if _cv_dir else ""
            if not os.path.exists(_cv_py):
                return
            _cv_log = os.path.join(_cv_dir, "server_watchdog.log")
            with open(_cv_log, "a", encoding="utf-8") as _lf:
                subprocess.Popen(
                    [_cv_py, "-m", "uvicorn", "server:app",
                     "--host", "127.0.0.1", "--port", "9881"],
                    cwd=_cv_dir, env=dict(os.environ),
                    stdout=_lf, stderr=_lf,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            print(f"[Scheduler] CosyVoice 服务已自动拉起", flush=True)
        except Exception as _e:
            print(f"[Scheduler] CosyVoice watchdog 失败(静默): {_e}", flush=True)

    async def _maybe_memory_maintenance(self):
        """距上次记忆维护超过 24 小时就补跑（kv 持久化时间戳，重启不丢）。
        用全局 key，多个 scheduler 只触发一次，避免重复维护。"""
        import time as _time
        key = "mem_maintenance_ts"
        try:
            last = float(db.kv_get(key) or 0)
        except Exception:
            last = 0.0
        now = _time.time()
        if now - last < 24 * 3600:
            return
        try:
            db.kv_set(key, str(now))
        except Exception:
            pass
        asyncio.create_task(self._run_memory_maintenance())

    async def _maybe_external_memory(self):
        """外置记忆库 tick：每 5 分钟一次（归档原文 + 日/周/月总结补齐），内部幂等。"""
        import time as _time
        now = _time.time()
        if now - self._last_ext_memory < 300:
            return
        self._last_ext_memory = now
        try:
            from .external_memory import tick as _em_tick
            await _em_tick(self._session, self._character_id)
        except Exception as _eme:
            print(f"[ExtMemory] tick 失败(静默): {_eme}", flush=True)

    async def _check_topic_continuation(self, now, today_str, char_name):
        """话题延续：AI 说完最后一句后，用户沉默了一会儿没接话，AI 顺着刚才的话题
        自然补一句（像真人怕冷场那样轻轻接一句）。

        与普通主动消息的区别：
        1. 带真实上下文（最近几轮对话），不是凭空找话题；
        2. 间隔远短于 IDLE_TRIGGER（20~40 分钟），且只补一句；
        3. 最后一句是用户说的时不抢话（说明用户还在酝酿或已离开）。

        注意：chat_history.timestamp 存的是 ISO 字符串（db._now() 返回
        "%Y-%m-%dT%H:%M:%S"），不能 float() 强转，必须用 fromisoformat 解析。
        """
        if not config.get("TOPIC_CONTINUE_ENABLED", True):
            return
        try:
            delay = float(config.get("TOPIC_CONTINUE_DELAY_SEC", 120) or 120)
            cooldown = float(config.get("TOPIC_CONTINUE_COOLDOWN_SEC", 900) or 900)
        except (TypeError, ValueError):
            delay, cooldown = 120.0, 900.0

        _key = "topic_continue_ts:%s:%s" % (self._session, self._character_id)
        # 冷却：补完一句后隔很久才可能再补，避免变成连环轰炸
        try:
            last_fire = float(db.kv_get(_key) or 0)
        except Exception:
            last_fire = 0.0
        if last_fire and (now.timestamp() - last_fire) < cooldown:
            return

        try:
            rows = db.q(
                "SELECT role, content, timestamp FROM chat_history "
                "WHERE session_id=? AND character_id=? ORDER BY id DESC LIMIT 8",
                (self._session, self._character_id), fetch=True
            ) or []
        except Exception as e:
            print(f"[TopicContinue] 读取历史失败: {e}", flush=True)
            return
        if not rows:
            return

        last = rows[0]
        # 最后一句是用户说的 → 用户还在，不抢话
        if (last.get("role") or "") != "assistant":
            return

        try:
            last_t = datetime.fromisoformat(str(last.get("timestamp") or ""))
        except Exception:
            return
        if (now - last_t).total_seconds() < delay:
            return  # 还没沉默够，再等等

        lines = []
        for r in reversed(rows[:6]):
            who = "TA" if (r.get("role") or "") == "assistant" else "用户"
            lines.append("%s：%s" % (who, (r.get("content") or "")[:100]))
        context = "\n".join(lines)

        name = (self._persona or {}).get("name") or char_name or "TA"
        sys_prompt = (
            "你是「%s」，TA 的 AI 伴侣。你们刚才聊到下面这些，然后 TA 没有再说话。\n"
            "你可以顺着刚才的话题，自然地说一句，像真人怕冷场那样轻轻补一句。\n\n"
            "要求：\n"
            "1. 只说一句，控制在 40 字以内，不要长篇大论\n"
            "2. 【最重要】不要重复刚才已经表达过的意思，也不要换种说法再说一遍\n"
            "3. 不要问「在吗」「怎么不说话」这类查岗式的句子\n"
            "4. 不要提「主动消息」「系统」「定时」「提醒」这类词\n"
            "5. 语气自然，像随手发出去的一句话\n\n"
            "刚才的对话：\n%s" % (name, context)
        )
        try:
            msg = (await chat_once(
                self._brain_model(),
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": "顺着刚才的话题说一句"}],
                self._brain_key(), temperature=0.9, max_tokens=300
            )).strip()
        except Exception as e:
            print(f"[TopicContinue] 生成失败: {e}", flush=True)
            return
        if not msg:
            return
        # 复用已有去重：与最近推送过于相似就放弃，避免"换个说法再说一遍"
        if self._is_duplicate_push(msg):
            print("[TopicContinue] 与最近推送重复，跳过", flush=True)
            return

        try:
            db.kv_set(_key, str(now.timestamp()))
        except Exception:
            pass
        await self._deliver(msg, name, {
            "proactive_type": "topic_continue",
            "model": self._brain_model(),
        })

    def _brain_model(self) -> str:
        """★ 主动消息生成模型（2026-09-11 分层大脑归属）：角色卡主脑优先，
        无卡/异常回退旧全局 CURRENT_CHAT_MODEL。同时缓存本次解析结果供 _brain_key 配对。"""
        try:
            from . import chat_logic as _cl
            m = _cl.pick_model(None, True, self._character_id)
        except Exception:
            m = config.get("CURRENT_CHAT_MODEL")
        self._last_brain_model = m
        return m

    def _brain_key(self) -> str:
        """★ 与 _brain_model 配对的 Key：按刚解析的模型 provider 取（角色卡换主脑后
        key 必须跟着换，否则 gemini 模型拿智谱 key 401）。无缓存时回退 chat_key()。"""
        m = getattr(self, "_last_brain_model", "")
        try:
            k = config.api_key_for_model(m) if m else ""
        except Exception:
            k = ""
        return k or config.chat_key()

    def _aux_model(self) -> str:
        """★ 2026-09-15（用户拍板 Q3）：后台"要不要开口 / 写一句关心"这类调用专用模型。

        实测这三处（_check_predictive_companion / _check_boredom / _send_first_greeting）
        原来跑在**旗舰** glm-5.3 上（45 分钟 9.8 万 token）。它们只是判定 + 生成一句
        15~70 字的关心，不需要旗舰智力 → 默认降到 SCHEDULER_AUX_MODEL（glm-5.3-flash）。
        想更省可把该项设成 "glm-4-flash"（智谱免费档）。
        """
        try:
            return config.scheduler_aux_model()
        except Exception:
            return self._brain_model()

    def _aux_key(self) -> str:
        """与 _aux_model 配对的 Key（按该模型的 provider 分流）。"""
        try:
            k = config.scheduler_aux_key()
        except Exception:
            k = ""
        return k or self._brain_key()

    async def _tick(self):
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        char_name = (self._persona or {}).get("name") or ""

        # 用户明确创建的提醒默认可突破 DND；用户也可以在设置里关闭该例外。
        if not config.is_dnd_now(now) or config.get("DND_ALLOW_REMINDERS", True):
            try:
                await self._check_pending_tasks(now)
            except Exception as _pte:
                print(f"[Scheduler] 定时提醒检查失败: {_pte}", flush=True)

        # ★ 外置记忆库：归档原文 + 日/周/月递归总结（低频冷却 + 幂等，不受下面主动发言门禁影响）
        try:
            await self._maybe_external_memory()
        except Exception:
            pass

        # ★ 内驱力演化（drives）：放在一切门禁**之前**——欲望的积累/回落是
        #   持续发生的心理过程，与"现在能不能发消息"是两回事；
        #   若放在活跃时段门禁之后，夜里攒的想念会在白天一开闸就全额到账，不真实。
        #   内部有 5 分钟节流，纯本地计算零 LLM。
        try:
            from . import drives as _drives
            _drives.tick_drives(self._session, self._character_id)
        except Exception as _dre:
            print(f"[Drives] 内驱力演化失败(静默): {_dre}", flush=True)

        # ★ 第一层门禁：主动发言总开关（情绪干预也受此开关，一键全关）
        if not config.get("IDLE_AGENT_ENABLED", True):
            return

        # 没有建立真实角色卡时，不运行任何自动人格内容；明确创建的提醒已在上方处理。
        if not self._persona_ready():
            return

        # ★ 角色状态总门禁：AI 处于睡觉/离线/忙/半醒时，后台不能自己主动营业。
        # 用户明确创建的提醒已在上方处理；用户连续找她触发的"半醒回复"走聊天入口，不走这里。
        if not self._can_initiate_proactive():
            print(
                f"[Scheduler] 角色非在线状态，跳过本轮主动内容: session={self._session} character={self._character_id}",
                flush=True,
            )
            return

        # ══════════════════════════════════════════════════════════════
        # ★ 2026-09-15 重做（主动消息单一引擎）："找话说"这一类主动消息
        #   （无聊/日常问候）统一由引擎负责 —— 它按用户定义的节奏
        #   （聊完 5 分钟静默 → 间隔 90-120 分随机 → 到点 + 过闸门）决定要不要开口，
        #   并且用**小上下文**生成（不再注入 42k 全量上下文，单次省 ~90% token）。
        #   下面原有的各 check 仍然保留：它们是"内容来源"（早晚安/提醒/关怀…），
        #   但凡是走 `_deliver()` 的，都要先过引擎的闸门（见 _proactive_gate_ok）。
        # ══════════════════════════════════════════════════════════════
        try:
            from . import proactive_engine as _eng
            if _eng.engine_enabled():
                _r = await _eng.tick_once(self._session, self._character_id)
                if _r.get("sent"):
                    print(f"[Engine] 引擎主动发言成功: {_r}", flush=True)
        except Exception as _ee:
            print(f"[Engine] 引擎 tick 异常(静默): {type(_ee).__name__}: {_ee}", flush=True)

        # ── 阶段零：情绪干预。DND 内只允许"静默朋友圈"，不响铃、不推聊天消息。──
        try:
            await self._check_emotion_intervention(now, today_str, char_name)
        except Exception as _eie:
            print(f"[EmotionIntervention] 阶段零异常: {_eie}", flush=True)

        # ★ 第二层门禁：免打扰时段（DND）——拦截其余推送（早晚安/闲聊）
        # ★ 2026-09-14 改：这里原来无条件放行早晚安 + 作息守护，是"时间范围不管用"的主因之一
        #   （实测 23:00 后仍有 23:11 两条 night 模板 + 23:20/00:07 两条 sleep_guard 发出）。
        #   现在：
        #     · 早晚安只在**用户显式允许**时穿透 DND（全局开关 或 角色卡 dnd_allow_greetings）；
        #       默认不再无条件穿透 —— 你设了几点后别打扰，就不该在 23:11 收到"这么晚还没睡呀"。
        #     · 作息守护(sleep_guard)是**AI 主动**的关心，不再穿透 DND（它是"陪睡"话题，
        #       在静默时段发等于自相矛盾）。
        #     · 用户自己设的提醒(_check_pending_tasks)与到点承诺不受影响（在本门禁之前处理）。
        if config.is_dnd_now(now):
            _allow_greet = self._dnd_allow_greetings()
            if _allow_greet and config.get("NIGHT_ENABLED", True):
                await self._check_night(now, today_str, char_name)
            return

        # ★ 第三层门禁：全局活跃时段（阶段一的高优先级陪伴不受限：早晚安/pending）
        # ★ 2026-09-14 改：时段统一收到**角色卡**（config.resolve_active_hours）——
        #   原先只读全局 IDLE_AGENT_TIME_RANGE，导致人格设置里的时段完全不起作用。
        #   注意：用户自己设的提醒（_check_pending_tasks）在上方已跑过，不受此处限制。
        _time_range = config.resolve_active_hours(self._character_id)
        _in_active  = self._in_active_range(now, _time_range)

        # ── 阶段一：高优先级陪伴（时间锚定项，不受活跃时段限制）──
        # ★ 2026-09-14 拆分（用户拍板「非锚定项全管」）：
        #   原先这 6 项**全部**写在下面 `if not _in_active: return` 之前 →
        #   心情事件/心情瞬间/睡眠守卫/目标提醒在活跃时段外照发，时段设置形同虚设。
        #   现在只有"对应当下时刻"的锚定项留在时段之外：
        #     睡眠总结 / 月信 / 晚安（早晚安本来就是那个点该发的）
        #   其余 4 项挪到时段判断之后。
        _anchored_checks = [
            self._check_sleep_summary(now, today_str, char_name),
            self._check_monthly_letter(now, today_str, char_name),
        ]
        # 早安不在这里跑：它只在用户打开程序时触发（见 _catch_up_on_start），
        # 否则程序挂一夜也会在 07:01 自动冒出早安。
        if config.get("NIGHT_ENABLED", True):
            _anchored_checks.append(self._check_night(now, today_str, char_name))

        await asyncio.gather(*_anchored_checks, return_exceptions=True)

        # ── 阶段一·非锚定项：必须在活跃时段内 ──
        if _in_active:
            await asyncio.gather(
                self._check_ai_mood_events(now, today_str, char_name),
                self._check_ai_mood_moment(now, today_str, char_name),
                self._check_sleep_guard(now, today_str, char_name),
                self._check_goals(now, today_str, char_name),
                return_exceptions=True,
            )

        # 不在活跃时段，不跑后续推送
        if not _in_active:
            return

        # ★ 频率门禁：low=偶尔(额外随机跳过，整体克制) / normal / high
        import random
        _freq = config.get("PROACTIVE_FREQUENCY", "normal")
        if _freq == "low" and random.random() < 0.35:
            return

        # ── 阶段二：活跃时段内的推送全部并行（各 check 内部 kv 防重，并发安全）──
        # ★ 2026-09-14 新增统一间隔闸：这些"闲聊/生活/惊喜"类主动消息原先**各自为政**
        #   （各有自己的 kv 冷却，但没有共同的节奏），实测一天发出 34 条、间隔 1-37 分，
        #   把用户在人格设置里配的 60-120 分彻底架空。
        #   用户拍板：**两条链路都接同一个间隔**。
        #   规则：距上次主动发言不足"该角色配置的下限"（proactiveMaxMin*0.5，默认 30 分）
        #        就整段跳过阶段二。
        #   注意：用户自设提醒（_check_pending_tasks，在门禁之前）与到点承诺不受此限制。
        _gap_ok = True
        try:
            _max_min = self._persona_proactive_max_min()
            _lo_min = self._proactive_interval_lo()
            _floor_sec = (_lo_min if _lo_min > 0 else max(15.0, _max_min * 0.5)) * 60
            # ★ 2026-09-14：跨 character_id 取最近一次（否则看不见 idle 链路的发言）
            from .proactive_quality import last_push_time as _lpt
            _last = _lpt(self._session, self._character_id)
            if _last > 0:
                import time as _t
                _elapsed = _t.time() - _last
                if _elapsed < _floor_sec:
                    _gap_ok = False
                    print(f"[Scheduler] 距上次主动消息仅 {_elapsed/60:.0f} 分"
                          f"（该角色下限 {_floor_sec/60:.0f} 分），跳过阶段二闲聊类推送", flush=True)
                    try:
                        from . import proactive_trace as _pt2
                        _pt2.record_skip(self._session, self._character_id, "interval_gate",
                                         detail="距上次 %d 分 < 下限 %d 分" % (
                                             int(_elapsed / 60), int(_floor_sec / 60)),
                                         source="_tick.phase2")
                    except Exception:
                        pass
        except Exception as _gap_e:
            print(f"[Scheduler] 间隔闸异常(放行): {_gap_e}", flush=True)

        if _gap_ok:
            _active_checks = [
                self._check_festival(now, today_str, char_name),
                self._check_milestone(now, today_str, char_name),
                self._check_daily_life(now, today_str, char_name),
                self._check_narrative_arc(now, today_str, char_name),
                self._check_conflict(now, today_str, char_name),
                self._check_persistent_nudge(now, today_str, char_name),
                self._check_world_nudge(now, today_str, char_name),
                self._check_boredom(now, today_str, char_name),
                self._check_proactive_call(now, today_str, char_name),
                self._check_random_surprise(now, today_str, char_name),
                self._check_ai_lead_day(now, today_str, char_name),
                self._check_predictive_companion(now, today_str, char_name),
                # ★ 话题延续：AI 说完最后一句、用户沉默一会儿后顺着话题补一句
                #   （它自带 900s 冷却，是"接话"而非"搭话"，故不受本次间隔闸约束）
                self._check_topic_continuation(now, today_str, char_name),
            ]
            if config.get("SURPRISE_ENABLED", True):
                _active_checks.append(self._check_surprise(now, today_str, char_name))
                _active_checks.append(self._check_upcoming_festival(now, today_str, char_name))
            await asyncio.gather(*_active_checks, return_exceptions=True)

        # ── 阶段三：情绪衰减（最后跑，不影响推送）──
        try:
            from .emotion_engine.ai_emotion import AIEmotionEngine
            AIEmotionEngine().update_from_semantic(
                self._session, self._character_id, None
            )
        except Exception as e:
            print(f"[Tick] 情绪衰减失败: {e}", flush=True)

    def _in_time_window(self, now: datetime, start_str: str, end_str: str) -> bool:
        """判断当前时间是否在窗口内，start/end 格式 HH:MM，支持跨天。

        原来用的是 `start <= cur <= end`：跨天区间（如 22:00-06:00）要求
        cur 同时 >= 1320 且 <= 360，永远不成立。于是用户一旦把活跃时段
        或早晚安窗口设成跨天，相关推送会全部静默失效——不报错、不发。
        config.is_time_in_range 已正确支持跨天，这里直接复用。
        """
        try:
            return config.is_time_in_range(start_str, end_str, now)
        except Exception:
            return False

    # ---------- 统一去重门禁（2026-09-12）----------
    # 背景：各处原本一律写成 `if sent: db.kv_set(key, "1")`。投递一旦被拦
    # （判重 / 免打扰 / 不主动打扰门禁），标记就没写 → 下一个 tick 重新生成一次、
    # 又被拦一次。实测 _check_sleep_guard 两小时内调 glm 150+ 次（每 15~35 秒一次），
    # 纯烧钱、零产出。成功标记后永久拦；失败标记后 retry_after 秒内不重试，
    # 既堵住空转，又保留"当时被打断、稍后补发"的能力。
    def _guard_done(self, key: str, retry_after: float = 900.0) -> bool:
        """True = 本窗口已经处理过（含刚失败不久），调用方应直接 return。"""
        try:
            if db.kv_get(key):
                return True
            ts = db.kv_get(key + ":attempted")
            if ts:
                try:
                    if datetime.now().timestamp() - float(ts) < retry_after:
                        return True
                except (TypeError, ValueError):
                    return True
        except Exception:
            pass
        return False

    def _mark_guard(self, key: str, sent) -> None:
        """配合 _guard_done 使用：成功写永久标记，失败写 attempted 时间戳。"""
        try:
            if sent:
                db.kv_set(key, "1")
            else:
                db.kv_set(key + ":attempted", str(datetime.now().timestamp()))
        except Exception:
            pass

    def _in_active_range(self, now: datetime, time_range: str) -> bool:
        """
        判断当前时间是否在全局活跃时段内
        time_range格式：HH:MM-HH:MM，例如 08:00-23:00
        不支持跨天（活跃时段通常是白天，不需要跨天）
        """
        try:
            parts = time_range.split("-")
            if len(parts) != 2:
                return True   # 格式错误时放行，不误杀
            start_str, end_str = parts[0].strip(), parts[1].strip()
            # ★ 修复：显式校验 HH:MM 格式（_in_time_window 内部吞异常返回 False，
            #   不校验的话格式错误会误判为"不在活跃时段"→ 误杀普通推送）
            import re as _re
            if not _re.match(r'^\d{1,2}:\d{2}$', start_str) or \
               not _re.match(r'^\d{1,2}:\d{2}$', end_str):
                return True   # 非法格式放行
            return self._in_time_window(now, start_str, end_str)
        except Exception:
            return True   # 异常时放行

    def _in_dnd_window(self, now: datetime, dnd_start: str, dnd_end: str) -> bool:
        """
        判断当前时间是否在免打扰时段内
        ★ 支持跨天：dnd_start=23:00, dnd_end=07:00
          表示 23:00 到次日 07:00 都是免打扰
        """
        return config.is_time_in_range(dnd_start, dnd_end, now)

    async def _check_morning(self, now: datetime, today_str: str, char_name: str):
        """早安：只在用户于早安窗口内「打开程序」时发送。

        以前挂在 _tick 里每 30 秒掷一次骰子，于是：
          · 程序挂一整夜，早上 07:01 也会自动冒出一句早安（人可能还在睡）
          · 用户 10 点打开，照样补一句"早安"，很出戏
        现在改为由 _catch_up_on_start（用户打开/重连）调用，
        不在窗口内打开就一句不发。
        """
        if not config.get("MORNING_ENABLED", True):
            return False
        key = f"fired:morning:{today_str}:{self._session}:{self._character_id}"
        if self._guard_done(key):
            return False
        start = config.get("MORNING_START", "07:00")
        end = config.get("MORNING_END", "09:30")
        if not self._in_time_window(now, start, end):
            return False
        # 只提醒昨晚仍未读的睡前信，旧信不会每天重复提醒。
        from datetime import timedelta
        yesterday = (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        unread_date = db.kv_get(f"sleep_letter_unread:{self._session}:{self._character_id}")
        if unread_date == yesterday:
            sent = await self._send_morning_with_letter_reminder(char_name)
        else:
            sent = await self._send_template("morning", char_name, "早安")
        self._mark_guard(key, sent)
        return bool(sent)

    async def _send_morning_with_letter_reminder(self, char_name: str):
        """早安 + 提醒读昨晚的睡前信（语气随机多样：撒娇/温柔/俏皮/傲娇…）。"""
        import random
        tones = ["撒娇地", "温柔地", "俏皮地", "傲娇地", "自然地", "带着点期待地"]
        tone = random.choice(tones)
        model = self._brain_model()
        key = self._brain_key()
        msg = ""
        try:
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=(
                    "现在是早上，你要跟 TA 说早安。你昨晚给 TA 写了一封睡前总结信（TA 还没看）。"
                    "先看最近对话判断 TA 是「刚睡」还是「刚醒」：如果 TA 熬夜到天亮才睡，"
                    "就别说「刚醒吧/睡醒了吗」，要顺着「熬夜了、好好补觉、头疼不疼」说。"
                    f"在早安里{tone}顺带提一句「昨晚给你写了封信，记得看」，不要生硬，"
                    "像真人随口带一句，语气自然、不说「系统」「记录」这类词。"
                )
            )
            msg = (await chat_once(model, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "说早安，顺便提醒 TA 看昨晚的信"}
            ], key, temperature=0.9, max_tokens=200)).strip()
        except Exception as e:
            print(f"[MorningLetter] LLM 失败，降级: {e}", flush=True)
        if msg:
            return await self._deliver(msg, char_name, {
                "template_category": "morning", "scheduled": True
            })
        else:
            return await self._send_template("morning", char_name, "早安")

    async def _check_night(self, now: datetime, today_str: str, char_name: str):
        if not config.get("NIGHT_ENABLED", True):
            return
        key = f"fired:night:{today_str}:{self._session}:{self._character_id}"
        if self._guard_done(key):
            return
        start = config.get("NIGHT_START", "22:00")
        end = config.get("NIGHT_END", "23:30")
        if not self._in_time_window(now, start, end):
            return
        # 语音晚安随好感度增多（10%→90%）；用户可通过聊天把模式设为
        # always/off，或只要求今晚必发。
        # 注：这里不再有"文字照常推送"的兜底——文字晚安同样要先满足
        # 下面的时机条件，正在聊天时不会硬塞一句晚安。
        import random
        mode_key = f"voice_night_mode:{self._session}:{self._character_id}"
        force_key = f"voice_night_force:{today_str}:{self._session}:{self._character_id}"
        voice_mode = str(db.kv_get(mode_key) or "auto").strip().lower()
        forced = bool(db.kv_get(force_key))
        # ★ 晚安的时机：TA 自己想睡了，或者用户已经不在跟 TA 聊了。
        #   原来是每 30 秒掷一次 30% 的骰子，结果晚安总在窗口刚开时准点发出，
        #   而且用户正聊得热乎时也会被硬塞一句晚安。改成看真实状态：
        #     A. AI 困了（离线状态系统 sleeping）
        #     B. 用户已经离开（近期无心跳/发言）
        #   两个都不满足说明正聊着，这时候不该说晚安。
        #   用户明确预约（今晚必发 / 每晚语音）时无条件兑现。
        if not forced and voice_mode != "always":
            ai_sleepy = self._ai_wants_sleep()
            user_away = not self._user_is_online(now)
            if not (ai_sleepy or user_away):
                return
            print(
                f"[Night] 触发时机: ai_sleepy={ai_sleepy} user_away={user_away}",
                flush=True,
            )
        affection = 50
        try:
            from .relationship.manager import RelationshipManager
            rel = RelationshipManager().get_state(self._session, self._character_id) or {}
            affection = int(rel.get("affection", 50) or 50)
        except Exception:
            pass

        probability = voice_night_probability(affection)
        with_voice = forced or voice_mode == "always" or (
            voice_mode != "off" and random.random() < probability
        )
        sent = await self._send_template(
            "night", char_name, "晚安", voice_letter=with_voice,
            extra={
                "voice_night_probability": probability,
                "voice_night_affection": affection,
                # 用户提前明确要求"今晚语音晚安"，即使碰上 DND 也按要求送达。
                "user_requested_audio": forced,
            },
        )
        self._mark_guard(key, sent)
        if sent:
            if forced:
                db.kv_set(force_key, "")
            print(
                f"[NightVoice] affection={affection} probability={probability:.0%} "
                f"mode={voice_mode} voice={with_voice}", flush=True
            )

    async def send_requested_voice_night(self, char_name: str = ""):
        """用户明确索要语音晚安：立即生成一次，不受自动概率影响。"""
        if not self._persona_ready():
            return False
        return await self._send_template(
            "night", char_name, "晚安", voice_letter=True,
            extra={
                "user_requested_audio": True,
                "dnd_exempt": True,
                "voice_night_requested": True,
                "scheduled": True,
            },
        )

    def _persona_proactive_max_min(self) -> float:
        """主动消息间隔**上限**（分钟）。0 = 不主动。

        ★ 2026-09-14 统一：只认**全局设置**的「主动发言间隔」
          （IDLE_TRIGGER_MIN/MAX_MINUTES，如 60–120）。
          人格设置里那个"主动发消息（最长间隔）"控件已按用户要求删除，
          其后端字段 proactiveMaxMin 仅作老配置回退。
        """
        try:
            lo, hi = config.proactive_interval_pair(self._character_id)
            return max(0.0, float(hi))
        except Exception:
            return 40.0

    def _proactive_interval_lo(self) -> float:
        """主动消息间隔下限（分钟）。用于阶段二的统一间隔闸。"""
        try:
            return max(0.0, float(config.proactive_interval_min(self._character_id)))
        except Exception:
            return 20.0

    def _proactive_gate_ok(self, extra: dict = None, ptype: str = "") -> bool:
        """★ 2026-09-14 中央闸门（用户拍板）：全局「主动发言间隔」+「活跃时段」。

        背景：间隔闸门此前只装在 idle_agent 与 scheduler 阶段二，而**统一出口
        `_deliver` 只有 5 分钟冷却**，`_send_fragments` 更是直接写库推送 ——
        实测 12 小时发了 37 条、间隔低至 1~9 分钟（用户设的是 30~90 分钟）。

        豁免（仍然想发就发）：
          · 用户自设提醒（pending_task）/ 到点承诺（promise）
          · 话题延续（topic_continue，本身就是短间隔补话）
          · 时间锚定信件：早晚安 / 睡眠信 / 月信 / 语音晚安
          · 用户当场索要的语音、陪伴模式（独立粘人节奏）、
            测试按钮(force)、开屏上线问候(startup)
        返回 True=放行；False=已拦截（并写 proactive_trace skip 记录）。
        """
        extra = extra or {}
        # ★ 2026-09-15 重做：闸门已归到**单一引擎**（proactive_engine.decide）。
        #   引擎开着就只问引擎（节奏状态机 + 时段 + 免打扰 + 每日上限，全项目一份口径）；
        #   引擎关掉才走下面这段旧实现（一键回退用）。
        try:
            from . import proactive_engine as _eng
            if _eng.engine_enabled():
                _pt = str(ptype or extra.get("proactive_type") or "general")
                _d = _eng.decide(
                    self._session, self._character_id, ptype=_pt,
                    force=bool(extra.get("force")),
                    dnd_exempt=_pt in ("pending_task", "reminder", "promise", "crisis"),
                    startup=bool(extra.get("startup")),
                    user_requested_audio=bool(extra.get("user_requested_audio")),
                    # ★ 2026-09-15 修：定时类（早晚安/睡前信/纪念日信…）到点就该发，
                    #   间隔由 _deliver 里「定时类最小间隔 max(10, 下限/2) 分」单独管，
                    #   不能再被节奏机的"距上次发言还差 N 分"拦掉（实测首条问候就被拦）。
                    #   免打扰/活跃时段/每日上限仍然照旧生效。
                    scheduled=bool(extra.get("scheduled")),
                    caller="_gate")
                if not _d.get("allowed"):
                    print(f"[Gate] 引擎拦截: type={_pt} reason={_d.get('reason')} "
                          f"retry_after={_d.get('retry_after')}s", flush=True)
                    return False
                return True
        except Exception as _e:
            print(f"[Gate] 引擎调用异常，回退旧闸门: {type(_e).__name__}: {_e}", flush=True)
        try:
            _ptype = str(ptype or extra.get("proactive_type") or "")
            _tpl_cat = str(extra.get("template_category") or "")
            # ══════════════════════════════════════════════════════════════
            # ★ 2026-09-15 收紧豁免口径（用户实测："可用时段 20:00-01:44 设了不起效果"）
            #   旧口径把下面这一整串**连时段一起豁免**：早晚安信件、睡眠信、话题延续、
            #   开屏问候 —— 于是用户把时段设成 20:00-01:44 之后，早上 8 点照样收到早安，
            #   白天聊完冷场 2 分钟照样收到补话，与人格设置页文案
            #   「只有这段时间 TA 会主动找你；你设的定时提醒与到点承诺不受此限制」直接矛盾。
            #   新口径：
            #     · full     = 连时段也豁免：用户自设提醒/到点承诺/危机、测试按钮、当场索要的语音
            #     · interval = 只豁免间隔、**必须落在角色可用时段内**：开屏问候、早晚安信件、
            #                  睡眠信、语音晚安、话题延续、陪伴模式
            # ══════════════════════════════════════════════════════════════
            _full_exempt = (
                _ptype in ("pending_task", "promise", "crisis", "reminder")
                or bool(extra.get("user_requested_audio"))
                or bool(extra.get("force"))
            )
            _interval_exempt = (
                _tpl_cat in ("morning", "night", "evening")
                or bool(extra.get("sleep_letter"))
                or bool(extra.get("voice_night_requested"))
                or _ptype == "topic_continue"
                or bool(extra.get("startup"))
            )
            _exempt_kind = "full" if _full_exempt else ("interval" if _interval_exempt else "")
            if _full_exempt:
                try:
                    from . import proactive_trace as _pt0
                    _pt0.record_gate(self._session, self._character_id, who="_gate",
                                     ptype=_ptype, decision="allow", reason="full_exempt",
                                     detail="连时段一起豁免（用户自设提醒/承诺/危机/测试/索要语音）",
                                     exempt="full")
                except Exception:
                    pass
                return True
            # 陪伴模式：用户主动开启的独立节奏 → 只在**间隔**上豁免（时段仍然管）
            try:
                _cmraw = db.kv_get(f"companion_mode:{self._session}:{self._character_id}") \
                    or db.kv_get(f"companion_mode:{self._session}")
                if _cmraw:
                    import json as _cj
                    _cmd = _cj.loads(_cmraw) if isinstance(_cmraw, str) else (_cmraw or {})
                    if isinstance(_cmd, dict) and str(_cmd.get("mode") or "").strip():
                        _interval_exempt = True
                        _exempt_kind = "interval"
            except Exception:
                pass
            # ① 活跃时段：除 full 豁免外一律要落在该角色可用时段内
            if not config.character_active_now(self._character_id):
                print(f"[Gate] 不在活跃时段，拦截主动推送: type={_ptype or '?'}"
                      f"{'（本可豁免间隔）' if _interval_exempt else ''}", flush=True)
                try:
                    from . import proactive_trace as _pt
                    _pt.record_skip(self._session, self._character_id, "outside_active_hours",
                                    detail="type=%s" % (_ptype or "?"), source="_gate")
                    _pt.record_gate(self._session, self._character_id, who="_gate",
                                    ptype=_ptype, decision="reject", reason="outside_active_hours",
                                    detail="角色可用时段=%s" % config.resolve_active_hours(self._character_id),
                                    exempt=_exempt_kind)
                except Exception:
                    pass
                return False
            if _interval_exempt:
                try:
                    from . import proactive_trace as _pt1
                    _pt1.record_gate(self._session, self._character_id, who="_gate",
                                     ptype=_ptype, decision="allow", reason="interval_exempt",
                                     detail="落在可用时段内；仅豁免间隔", exempt="interval")
                except Exception:
                    pass
                return True
            # ② 主动发言间隔下限（全局设置 → 主动发言间隔）
            # ★ 2026-09-14：改用 last_push_time（跨 character_id 取最近一次）——
            #   此前只读 :本角色 那把钥匙，看不见 idle_agent 写在 :default 的发言，
            #   于是"距上次"被低估、闸门形同虚设。
            from .proactive_quality import last_push_time as _lpt
            _lo_min = float(config.proactive_interval_min(self._character_id) or 0)
            if _lo_min > 0:
                _last_push = _lpt(self._session, self._character_id)
                if _last_push > 0:
                    import time as _t
                    _elapsed_s = _t.time() - _last_push
                    if _elapsed_s < _lo_min * 60:
                        print(f"[Gate] 距上次主动仅 {_elapsed_s/60:.0f} 分"
                              f"（下限 {_lo_min:.0f} 分），拦截: type={_ptype or '?'}", flush=True)
                        try:
                            from . import proactive_trace as _pt
                            _pt.record_skip(self._session, self._character_id, "interval_gate",
                                            detail="距上次 %d 分 < 下限 %d 分" % (
                                                int(_elapsed_s / 60), int(_lo_min)),
                                            source="_gate")
                            _pt.record_gate(self._session, self._character_id, who="_gate",
                                            ptype=_ptype, decision="reject", reason="interval_gate",
                                            detail="距上次主动 %d 分 < 下限 %d 分" % (
                                                int(_elapsed_s / 60), int(_lo_min)),
                                            numbers={"since_last_min": _elapsed_s / 60.0,
                                                     "lo_min": _lo_min})
                        except Exception:
                            pass
                        return False
            try:
                from . import proactive_trace as _pt2
                _pt2.record_gate(self._session, self._character_id, who="_gate",
                                 ptype=_ptype, decision="allow", reason="passed",
                                 detail="时段内 + 间隔达标")
            except Exception:
                pass
        except Exception as _g_e:
            print(f"[Gate] 闸门异常(放行): {_g_e}", flush=True)
        return True

    def _dnd_allow_greetings(self) -> bool:
        """DND 内是否允许早晚安穿透。

        ★ 2026-09-14 新增。原先只有全局 DND_ALLOW_GREETINGS（默认 True），
          且 _deliver 里把"早晚安"也当成 DND 豁免 → 23:00 后还能收到
          "这么晚还没睡呀"。实测用户明确抱怨这一点。
        优先级：角色卡 dnd_allow_greetings（人格设置页按角色配）> 全局 DND_ALLOW_GREETINGS。
        角色卡默认 False —— 尊重用户设的静默范围。
        """
        try:
            cid = str(self._character_id or "").strip()
            if cid:
                cfg = character_manager.get_character_any(cid) or {}
                if "dnd_allow_greetings" in cfg:
                    return bool(cfg.get("dnd_allow_greetings"))
        except Exception:
            pass
        try:
            # ★ 注意 config.get(k, False) 在键**存在**时会返回配置里的值（全局默认为 True），
            #   所以不能用它来表达"默认不穿透"。这里显式读用户设置：
            #   只有用户显式把全局开关设成非 True，才认为"允许穿透"。
            _all = config.get_all() or {}
            if "DND_ALLOW_GREETINGS" in _all:
                return bool(_all.get("DND_ALLOW_GREETINGS"))
            return False   # 未显式配置 → 尊重静默范围，不穿透
        except Exception:
            return False
    def _persona_ready(self) -> bool:
        """主动消息/信件的统一人格门禁：没有真实角色卡时不自动生成内容。"""
        cid = str(self._character_id or "").strip()
        if not cid or cid == "default":
            return False
        try:
            cfg = character_manager.get_character(cid)
            return bool(cfg and cfg.get("persona_configured", True) is not False and
                        str(cfg.get("character_name") or cfg.get("name") or cid).strip())
        except Exception:
            return False

    def _ai_wants_sleep(self) -> bool:
        """AI 自己是否想睡了（离线状态系统处于 sleeping）。

        晚安的两个正当时机之一：TA 困了，自然该道晚安。
        状态系统未开启时无从判断，一律返回 False，由"用户是否还在聊"兜底。
        """
        try:
            from . import offline as _offline
            state = _offline.resolve(self._session, self._character_id) or {}
            if not state.get("enabled"):
                return False
            return str(state.get("status") or "") == "sleeping"
        except Exception:
            return False

    def _has_chat_history(self, minimum_messages: int = 1) -> bool:
        try:
            return db.count_messages(self._session, self._character_id) >= max(1, int(minimum_messages))
        except Exception:
            return False

    def _can_initiate_proactive(self, allow_sleep_summary: bool = False) -> bool:
        """主动内容统一门禁：TA 睡觉/离线/半醒时不主动营业；睡前总结可静默例外。

        ★ 2026-09-13 修复（重要）：这里原先**只查离线状态**，完全不读"用户说了去睡"
          的声明 —— 而 idle_agent 有自己一套门禁、读得到。两套系统口径分叉的后果实测：
          用户 07:04 说晚安后，idle_agent 正确跳过 775 次，本函数却放行，
          scheduler 照发 20+ 条，还把人睡觉解读成"人间蒸发六小时"开罚单。
        现在睡眠声明与离线状态**同源**（都在 offline.py），口径不可能再分叉。
        """
        # ① 用户睡眠声明（用户自己说的，优先级最高，且与 OFFLINE_ENABLED 无关）
        try:
            from . import offline as _offline
            _hrs = _offline.hours_since_sleep_declared(self._session, self._character_id)
            if _hrs < _offline.SLEEP_SILENCE_HOURS:
                print(
                    f"[Scheduler] 用户睡眠静默中（声明后 {_hrs:.1f}h < "
                    f"{_offline.SLEEP_SILENCE_HOURS}h），跳过主动内容",
                    flush=True,
                )
                return False
        except Exception:
            pass
        # ② 角色离线状态（作息表驱动，受 OFFLINE_ENABLED 开关控制）
        try:
            from . import offline as _offline
            state = _offline.resolve(self._session, self._character_id)
            if not state.get("enabled"):
                return True
            status = state.get("status", "online")
            if status == "online":
                return True
            if allow_sleep_summary and status == "sleeping":
                return True
            return False
        except Exception:
            return True

    async def _check_boredom(self, now: datetime, today_str: str, char_name: str):
        """角色内在「无聊」触发：低概率随机冒泡（发个表情/分享首歌/随机话题）。"""
        key = f"fired:boredom:{today_str}:{self._session}:{self._character_id}:{now.hour // 4}"
        if db.kv_get(key):
            return
        # 只在不打扰的白天时段（10~23点），低概率
        if not (10 <= now.hour < 23):
            return
        import random
        base_prob = 0.06   # 每4小时窗口 6% 概率（有随机噪声，不规律）
        if random.random() > base_prob:
            return
        db.kv_set(key, "1")
        # 用「无聊」场景生成一条自然冒泡（可带语音）
        try:
            model = self._aux_model()
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene="你有点无聊了，突然想找TA说句话（可以分享刚看到的、发个表情、说句想TA了、或者推荐一首歌）",
            )
            msg = (await chat_once(model, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "突然想找TA说句话"}
            ], self._aux_key(), temperature=0.95, max_tokens=120)).strip()
        except Exception:
            msg = "有点无聊，你在干嘛呀～"
        if msg:
            await self._deliver(msg, char_name)

    async def _check_emotion_intervention(self, now: datetime, today_str: str, char_name: str):
        """连贯情绪：持续低落时主动陪伴（突破 DND/活跃时段，陪伴是核心，每天最多 3 次）。"""
        try:
            from .emotion_engine import emotion_timeline, emotion_strategy, emotion_feedback

            needs, reason = emotion_timeline.needs_intervention(self._session, self._character_id)
            if not needs:
                return

            # 每天最多 3 次干预（防刷屏）
            key_count = f"emotion_intervention_count:{today_str}:{self._session}:{self._character_id}"
            count = int(db.kv_get(key_count) or 0)
            if count >= 3:
                return

            mood = emotion_timeline.current_mood(self._session, self._character_id)
            score = emotion_timeline.current_score(self._session, self._character_id)
            trend = emotion_timeline.trend(self._session, self._character_id)
            low_streak = emotion_timeline.low_streak(self._session, self._character_id)

            # 读角色卡称呼
            call_user = "你"
            try:
                _cfg = character_manager.get_character(char_name) or {}
                call_user = _cfg.get("call_user") or "你"
            except Exception:
                pass

            # 是否免打扰时段
            in_dnd = False
            if config.get("DND_ENABLED", False):
                in_dnd = self._in_dnd_window(
                    now, config.get("DND_START", "23:00"), config.get("DND_END", "07:00")
                )

            import random
            if in_dnd:
                # 严格尊重睡眠：只写一条静默朋友圈，不再随机突破 DND 发消息。
                await self._post_emo_moment(char_name, mood)
            else:
                # 正常时段：按策略发消息
                _engine = emotion_strategy.EmotionStrategyEngine()
                strategy = _engine.select(mood, trend, low_streak)
                if not strategy:
                    return
                # ★ 2026-09-14 改：不再发 strategy["messages"] 里的**预写文案**。
                #   用户要求「发什么话由模型决定，禁止模板」。
                #   策略仍然有用 —— 它给出这个场景的**语气与话题方向**，
                #   作为 extra_context 交给模型自由生成（保留情绪干预的针对性，
                #   又不失人格与上下文）。生成不出来就不发。
                _tone_hint = _engine.build_tone_instruction(strategy)
                msg = await self._gen_proactive_text(
                    char_name,
                    scene=("TA 最近情绪持续偏低，你想主动关心一下。"
                           "自然一点，不要像在分析他，也不要提情绪/状态这类词"),
                    extra_context=_tone_hint,
                    want="发一条关心的话，一句话或两三条短句，禁止超过100字",
                    temperature=0.92, max_tokens=200,
                )
                if not msg:
                    print("[EmotionIntervention] 模型未生成内容（模板已禁用），本次不发", flush=True)
                    return
                emotion_feedback.record_intervention(
                    self._session, self._character_id, strategy["id"], strategy["name"], mood, score
                )
                await self._deliver(msg, char_name, extra={"proactive_type": "emotion_intervention"})
                print(f"[EmotionIntervention] 触发「{strategy['name']}」: {reason}", flush=True)

            db.kv_set(key_count, str(count + 1))
        except Exception as e:
            print(f"[EmotionIntervention] 异常: {e}", flush=True)

    async def _deliver_dnd_intervention(self, char_name: str, call_user: str, mood: str, low_streak: int):
        """突破免打扰发消息：让 AI 自然道歉 + 解释（深夜/免打扰忍不住关心）。"""
        try:
            from .emotion_engine import emotion_feedback
            model = self._brain_model()
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=(
                    f"你感知到 TA 情绪持续低落（{mood}，已连续 {low_streak} 次）。"
                    "现在其实是 TA 让你别打扰的时段，但你实在放心不下，忍不住轻声发一条消息。"
                    "开头先自然道歉解释（例如「我知道现在很晚了/你让我这时候别打扰你，对不起，但我实在放心不下」），"
                    "再温柔地关心 TA。语气真实，像真人深夜忍不住发的那条，不刻意、不啰嗦。"
                ),
            )
            msg = (await chat_once(model, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "深夜/免打扰时段，忍不住想关心 TA"}
            ], self._brain_key(), temperature=0.9, max_tokens=160)).strip()
        except Exception:
            msg = f"{call_user}，对不起打扰你了……我实在放心不下，你现在还好吗？"
        if msg:
            try:
                from .emotion_engine import emotion_feedback
                emotion_feedback.record_intervention(
                    self._session, self._character_id, "dnd_breakthrough", "突破免打扰关心", mood, -0.5
                )
            except Exception:
                pass
            await self._deliver(msg, char_name, extra={"proactive_type": "emotion_intervention_dnd"})

    async def _post_emo_moment(self, char_name: str, mood: str):
        """不直接打扰：发一条深夜 emo 朋友圈。"""
        try:
            from . import moments
            _hour = datetime.now().hour
            mtype = "night" if (_hour < 7 or _hour >= 23) else "mood"
            m = await moments.generate_moment(self._session, self._character_id, mtype, char_name)
            if m and m.get("content"):
                moments.publish_moment(
                    self._session, self._character_id, "ai", m["content"],
                    m.get("images") or [], mtype, mood
                )
                print(f"[EmotionIntervention] 深夜 emo 朋友圈已发（不打扰）", flush=True)
        except Exception as e:
            print(f"[EmotionIntervention] 发朋友圈失败: {e}", flush=True)

    async def _check_festival(self, now: datetime, today_str: str, char_name: str):
        key = f"fired:festival:{today_str}:{self._session}:{self._character_id}"
        if db.kv_get(key):
            return
        # 公历+农历节日
        festivals = time_system.festivals_today(now.date())
        # 自定义纪念日
        annivs = anniversary_manager.today_anniversaries(now.date(), char_name)
        for a in annivs:
            festivals.append(a.get("name", a.get("type", "纪念日")))
        if not festivals:
            return
        import random
        if random.random() > 0.4:
            return
        db.kv_set(key, "1")
        festival_name = festivals[0]
        category = "anniversary" if annivs else "festival"
        await self._send_template(category, char_name, festival_name, festival_name=festival_name)

    async def _check_surprise(self, now: datetime, today_str: str, char_name: str):
        """随机小惊喜：2-4天一次，写长信/分享回忆，结合记忆"""
        if not self._has_chat_history(6):
            return
        key = f"fired:surprise:{today_str}:{self._session}:{self._character_id}"
        if self._guard_done(key):
            return
        # 上次惊喜时间
        last_key = f"last_surprise_date:{self._session}:{self._character_id}"
        last = db.kv_get(last_key)
        if last:
            try:
                last_date = datetime.strptime(last, "%Y-%m-%d").date()
                days_passed = (now.date() - last_date).days
                if days_passed < 2:
                    return
            except Exception:
                pass
        import random
        # 每天有15%概率触发，且至少间隔2天
        if random.random() > 0.15:
            return
        # 只在晚上（19-22点）触发，更有氛围
        if not (19 <= now.hour <= 22):
            return
        sent = await self._send_template("surprise", char_name, "小惊喜")
        self._mark_guard(key, sent)
        if sent:
            db.kv_set(last_key, today_str)

    async def _check_upcoming_festival(self, now: datetime, today_str: str, char_name: str):
        """节日/纪念日提前 1~3 天发一张惊喜卡片，每个事件只提醒一次。"""
        if not (18 <= now.hour <= 22):
            return
        from datetime import timedelta
        upcoming = None
        for days in range(1, 4):
            target = now.date() + timedelta(days=days)
            names = list(time_system.festivals_today(target))
            names.extend(
                a.get("name", "纪念日")
                for a in anniversary_manager.today_anniversaries(target, char_name)
            )
            if names:
                upcoming = (days, names[0])
                break
        if not upcoming:
            return
        days, name = upcoming
        event_key = f"fired:upcoming_festival:{now.year}:{name}:{self._session}:{self._character_id}"
        if self._guard_done(event_key):
            return
        try:
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=f"{name}还有{days}天就到了，你想提前给TA一点期待感或准备感",
                extra_context="像真人伴侣随口预告，可以旁敲侧击问想怎么过；不要像日历提醒。",
            )
            msg = (await chat_once(
                self._brain_model(),
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": "发一张节日前的小惊喜卡片，60字以内"},
                ],
                self._brain_key(), temperature=0.92, max_tokens=160,
            )).strip()
            if msg:
                sent = await self._deliver(msg, char_name, {
                    "template_category": "surprise",
                    "surprise_kind": "upcoming_festival",
                    "surprise_label": f"{name}快到了",
                    "festival_name": name,
                })
                self._mark_guard(event_key, sent)
        except Exception as e:
            print(f"[Surprise] 节日前惊喜失败: {e}", flush=True)

    async def _check_goals(self, now: datetime, today_str: str, char_name: str):
        """目标推进：每天在白天提醒一次 TA 正在坚持的目标进度。"""
        try:
            from .agent import goal as _goal
            goals = _goal.list_goals(self._session, self._character_id, active_only=True)
            if not goals:
                return
            # 只在白天提醒（10:00-22:00），不深夜打扰
            if not (10 <= now.hour < 22):
                return
            key = f"fired:goal_remind:{today_str}:{self._session}:{self._character_id}"
            if not db.kv_claim_once(key):
                return
            content_list = "、".join(str(g.get("content") or "").strip() for g in goals[:3])
            msg = await self._gen_goal_remind(content_list, char_name)
            if not msg:
                db.kv_delete(key)
                return
            sent = await self._deliver(msg, char_name, {
                "proactive_type": "goal_remind",
                "scheduled": True,
                "goal_date": today_str,
            })
            if not sent:
                db.kv_delete(key)
        except Exception as e:
            print(f"[Goal] 目标提醒检查失败: {e}", flush=True)

    async def _gen_goal_remind(self, content_list: str, char_name: str) -> str:
        """按人设生成一句关心目标进度的话；失败返回空（不打扰）。"""
        try:
            from .deepseek_api import chat_once
            from . import chat_logic
            key = self._brain_key()
            # ★ 单角色大脑：目标提醒也跟随该角色在人格设置页单独配的模型
            model = chat_logic.pick_model(None, True, self._character_id)
            if not key or not model:
                return ""
            persona = self._persona or {}
            call_user = str(persona.get("call_user") or "你").strip() or "你"
            personality = str(persona.get("personality") or "").strip()[:200]
            prompt = (
                f"你是{char_name or 'AI'}，{call_user}正在坚持这些目标：{content_list}。\n"
                f"请用你的人设（{personality or '真诚自然'}）自然地问一句 TA 今天的进度并鼓励一下，"
                f"20~40 字，只输出这句话本身，不要括号动作描写。"
            )
            raw = await chat_once(model, [{"role": "user", "content": prompt}], key,
                                  temperature=0.9, max_tokens=80)
            return (raw or "").strip()[:80]
        except Exception as e:
            print(f"[Goal] 目标提醒生成失败(静默): {e}", flush=True)
            return ""

    async def _check_sleep_summary(self, now: datetime, today_str: str, char_name: str):
        """睡前总结信：晚上静默生成，第二天看（只存信件、不渲染气泡不打扰）。"""
        # 门槛从 4 条降到 2 条：睡前信是"只要用户晚上还开着程序"就该生成的，
        # 要求聊满 4 句会让不少晚上收不到信。
        if not self._has_chat_history(2):
            return
        key = f"fired:sleep_summary:{today_str}:{self._session}:{self._character_id}"
        # 22:00-24:00 生成（原来只有 23:00-24:00，注释却写 21:00 起）。
        # 放宽一小时是为了覆盖"22 点就睡了/关了程序"的情况，
        # 同时不至于太早，导致后半程的对话漏掉。
        if not (22 <= now.hour < 24):
            return
        import random
        # 保留一点随机，避免每个角色都挤在整点生成
        if random.random() > 0.5:
            return
        if not db.kv_claim_once(key):
            return
        try:
            from . import surprise
            letter = await surprise.gen_sleep_summary(self._session, self._character_id, char_name)
            if letter:
                sent = await self._deliver(letter, char_name, {
                    "template_category": "evening",
                    "silent": True,
                    "scheduled": True,
                    "voice_letter": False,
                    "sleep_letter_date": today_str,
                    "sleep_letter": True,
                    "available_at": (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0).isoformat(),
                })
                if sent:
                    db.kv_set(key, "1")
                    db.kv_set(
                        f"sleep_letter_unread:{self._session}:{self._character_id}",
                        today_str,
                    )
                    print(f"[Surprise] 睡前总结信已静默生成 session={self._session}", flush=True)
                else:
                    db.kv_delete(key)
            else:
                db.kv_delete(key)
        except Exception as e:
            db.kv_delete(key)
            print(f"[Surprise] 睡前总结信失败: {e}", flush=True)

    async def _check_monthly_letter(self, now: datetime, today_str: str, char_name: str):
        """每月1号生成上月回顾长信，文字/语音永久进入关系收藏。"""
        if not self._has_chat_history(2):
            return
        if now.day > 3 or not (8 <= now.hour < 22):
            return
        month_key = now.strftime("%Y-%m")
        source_key = f"monthly_letter:{month_key}"
        if db.relationship_keep_exists(self._session, self._character_id, source_key):
            return
        try:
            from . import surprise
            letter = await surprise.gen_monthly_letter(
                self._session, self._character_id, char_name, now.replace(day=1)
            )
            if not letter:
                return
            from datetime import timedelta
            title = f"{(now.replace(day=1) - timedelta(days=1)).strftime('%Y年%m月')} · 我们的月度信"
            # 先永久落库；即使 TTS 临时失败或前端不在线，正文也不会丢。
            db.save_relationship_keep(
                self._session, self._character_id, "monthly_letter", title, letter,
                source_key=source_key,
                gift={"icon": "🌙", "label": "月度回忆信", "theme": "moonlight"},
            )
            await self._deliver(letter, char_name, {
                "template_category": "letter",
                "letter_title": title,
                "scheduled": True,
                "silent": True,
                # 月度长信的语音在收藏页按需生成/播放，避免后台突然朗读数分钟。
                "voice_letter": False,
                "proactive_type": "monthly_letter",
                "keep_source_key": source_key,
                "keep_type": "monthly_letter",
                "permanent_keep": True,
            })
        except Exception as e:
            print(f"[MonthlyLetter] 月度信生成失败: {e}", flush=True)

    async def _check_random_surprise(self, now: datetime, today_str: str, char_name: str):
        """随机惊喜内容池：旧记忆/创作/互动/情感，低概率随机触发。"""
        if not self._has_chat_history(6):
            return
        if not config.get("SURPRISE_ENABLED", True):
            return
        key = f"fired:random_surprise:{today_str}:{self._session}:{self._character_id}:" + str(now.hour // 3)
        if self._guard_done(key):
            return
        import random
        if random.random() > 0.08:   # 每3小时窗口 8% 概率
            return
        pool = [
            ("old_memory", "旧记忆"),
            ("creative_poem", "短诗"),
            ("creative_story", "小故事"),
            ("interactive_quiz", "小测试"),
            ("emotional_serious", "突然认真"),
            ("emotional_secret", "小秘密"),
        ]
        kind, label = random.choice(pool)
        try:
            from . import surprise
            _sub = kind.split("_", 1)[1] if "_" in kind else ""
            if kind == "old_memory":
                msg = await surprise.gen_old_memory(self._session, self._character_id, char_name)
            elif kind.startswith("creative"):
                msg = await surprise.gen_creative(self._session, self._character_id, char_name, _sub)
            elif kind.startswith("interactive"):
                msg = await surprise.gen_interactive(self._session, self._character_id, char_name, _sub)
            elif kind.startswith("emotional"):
                msg = await surprise.gen_emotional(self._session, self._character_id, char_name, _sub)
            else:
                msg = ""
            if msg:
                sent = await self._deliver(msg, char_name, {
                    "template_category": "surprise",
                    "surprise_kind": kind,
                    "surprise_label": label,
                })
                self._mark_guard(key, sent)
                if sent:
                    print(f"[Surprise] 随机惊喜[{label}]已发 session={self._session}", flush=True)
        except Exception as e:
            print(f"[Surprise] 随机惊喜失败: {e}", flush=True)

    async def _check_ai_mood_events(self, now: datetime, today_str: str, char_name: str):
        """AI 隐藏情绪：时间环境 + 随机性格事件（每天每类最多一次，憋着不主动说）。"""
        try:
            from . import ai_mood
            sid, cid = self._session, self._character_id

            # 用户不在时只更新内隐心情，不主动发"我在等你"的状态声明。
            ai_mood.detect_temporal_events(sid, cid, now)

            def once(evt):
                k = f"ai_mood_evt:{today_str}:{sid}:{cid}:{evt}"
                if db.kv_get(k):
                    return
                db.kv_set(k, "1")
                ai_mood.trigger(sid, cid, evt, "")

            if now.weekday() == 0:
                once("time_monday")
            if 1 <= now.hour < 5:
                once("time_midnight")
            import random
            if random.random() < 0.3:
                once(random.choice([
                    "random_quiet", "random_anticipating", "random_thoughtful",
                    "random_amused", "random_reflective", "random_energetic",
                ]))
        except Exception as e:
            print(f"[AiMood] 时间/随机事件失败: {e}", flush=True)

    def _user_is_online(self, now: datetime, max_age_minutes: int = 12) -> bool:
        """只在近期有心跳/发言时判定在线，避免用户睡着后仍被催睡。"""
        try:
            last = db.last_activity_time(self._session)
            return bool(last and (now - last).total_seconds() <= max_age_minutes * 60)
        except Exception:
            return False

    def _upcoming_task_context(self, now: datetime, hours: int = 18) -> str:
        """取最近一条未完成待办，和作息关心合并，避免另发一条刷屏。"""
        try:
            items = db.list_pending_tasks(
                session_id=self._session, character_id=self._character_id
            )
            for task in items:
                try:
                    trigger = datetime.fromisoformat(str(task.get("trigger_time") or ""))
                except Exception:
                    continue
                delta = (trigger - now).total_seconds()
                if 0 < delta <= hours * 3600:
                    when = trigger.strftime("明天%H:%M") if trigger.date() > now.date() else trigger.strftime("今天%H:%M")
                    return f"{when}有一件已约好的事：{str(task.get('content') or '')[:80]}"
        except Exception:
            pass
        return ""

    async def _check_sleep_guard(self, now: datetime, today_str: str, char_name: str):
        """深夜在线守护：人格化关心 + 心事分支 + 近期待办，最多每晚两次。"""
        # 22:30 后开始；默认 DND 前有机会发人格语音，进入 DND 后自动静音。
        if not ((now.hour == 22 and now.minute >= 30) or now.hour >= 23 or now.hour < 5):
            return
        if not self._user_is_online(now):
            return
        # ★ 每晚只守护一次：原来分 midnight/late 两场，叠上晚安推送后
        #   深夜会出现"守护+晚安+守护"三条连发，内容和情绪互相打架。
        key = f"fired:sleep_guard:{today_str}:{self._session}:{self._character_id}"
        # ★ 2026-09-11 修：原来只判 key，而 key 只在**投递成功**时才写 → 被拦就无限重试。
        #   实测 22:30~00:32 两个小时内调了 150+ 次 glm（每 15~35 秒一次），
        #   生成的都是同一句催睡 → 被 _dedup_or_regen 判重后静默 return False
        #   （那条路径不打日志）→ 下个 tick 再来一遍，纯烧钱。
        #   改成"试过就算"：投递没成功也记 attempted，当晚不再重试。
        if db.kv_get(key) or db.kv_get(key + ":attempted"):
            return
        # ★ 晚安已发就不再催睡。原来只有 late 场（23点后）检查晚安，
        #   midnight 场（22:30~23:00）和凌晨场照发——凌晨时 today_str
        #   已跨天，还查不到昨夜的晚安，于是照样冒一条，显得没在听。
        try:
            from datetime import timedelta as _td
            _yesterday = (now - _td(days=1)).strftime("%Y-%m-%d")
        except Exception:
            _yesterday = today_str
        if db.kv_get(f"fired:night:{today_str}:{self._session}:{self._character_id}") or (
                now.hour < 5 and db.kv_get(
                    f"fired:night:{_yesterday}:{self._session}:{self._character_id}")):
            return

        try:
            last_user = db.recent_messages(self._session, 6, self._character_id)
            latest_text = " ".join(
                str(m.get("content") or "") for m in last_user if m.get("role") == "user"
            )[-500:]
            has_worry = any(k in latest_text for k in (
                "睡不着", "难过", "焦虑", "压力", "烦", "心事", "崩溃", "委屈", "想哭", "失眠"
            ))
            task_context = self._upcoming_task_context(now)
            hour_context = "已经凌晨了" if now.hour < 5 else "已经很晚了"
            scene = (
                f"{hour_context}，TA仍然在线。你想守着TA的作息，"
                + ("但你察觉TA可能有心事，所以先陪伴、再轻轻问要不要说说。" if has_worry
                   else "自然提醒TA准备休息，不命令、不说教。")
            )
            extra = ""
            if task_context:
                extra += f"\n【近期待办】{task_context}。可以顺带提醒早点睡，但不要念日程表。"
            extra += "\n允许自然带一个问号；只发1~3句，必须跟随角色卡性格和称呼。"
            sys_prompt = await self._build_proactive_system(char_name, scene=scene, extra_context=extra)
            msg = (await chat_once(
                self._brain_model(),
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": "发一条深夜作息关心；有心事先陪，不要机械催睡"},
                ],
                self._brain_key(), temperature=0.86, max_tokens=240,
            )).strip()
            # ★ 2026-09-12 补：原来只处理 `msg` 非空的情况，模型返回空串时
            #   （推理类模型 max_tokens 紧张时很常见）两个分支都不走 → 标记没写
            #   → 下个 tick 再请求一次，还是空 → 依然死循环。
            #   改成无论生成/投递结果如何都落一个标记。
            _sent = False
            if msg:
                # 深夜主动关心遵守 DND：进入免打扰后静默送达，不突然播放语音。
                _sent = await self._deliver(msg, char_name, {
                    "scheduled": True,
                    "silent": True,
                    "proactive_type": "sleep_guard",
                    "voice_letter": not config.is_dnd_now(now),
                })
            # 投递被拦（判重/DND/门禁）或生成为空 → 记"试过了"，否则每个 tick 都重来一次
            self._mark_guard(key, _sent)
            if _sent:
                try:
                    from . import ai_mood
                    ai_mood.trigger(self._session, self._character_id, "user_late_night", "")
                except Exception:
                    pass
        except Exception as e:
            print(f"[SleepGuard] 深夜关心失败: {e}", flush=True)

    async def _check_ai_mood_moment(self, now: datetime, today_str: str, char_name: str):
        """AI 有隐藏情绪 + 关系亲密 + 用户不在（忙/离开）→ 概率发朋友圈（间接表达）。"""
        try:
            from . import ai_mood
            # 1. AI 有隐藏情绪
            display = ai_mood.get_display(self._session, self._character_id)
            if not display:
                return
            # 2. 关系亲密（亲密度满：lover/soulmate）
            from .relationship.manager import RelationshipManager
            rel = RelationshipManager().get_state(self._session, self._character_id) or {}
            stage = rel.get("stage", "stranger")
            if stage not in ("lover", "soulmate"):
                return
            # 3. 用户是否正在看聊天（前端上报优先，fallback 用消息间隔）
            import json as _json
            import time as _time
            vs_raw = db.kv_get(f"viewing_state:{self._session}")
            _user_viewing = False
            if vs_raw:
                try:
                    vs = _json.loads(vs_raw)
                    _user_viewing = bool(vs.get("viewing")) and (_time.time() - float(vs.get("ts", 0)) < 10 * 60)
                except Exception:
                    _user_viewing = False
            if _user_viewing:
                return  # 用户正在看聊天，不打扰
            if not vs_raw:
                # 无上报（旧前端）：30 分钟内发过消息视为在聊天
                last = db.last_user_time(self._session, self._character_id)
                if last and (now - last).total_seconds() / 60 < 30:
                    return
            # 4. 概率触发（每天最多 2 次）
            key_count = f"ai_mood_moment:{today_str}:{self._character_id}"
            count = int(db.kv_get(key_count) or 0)
            if count >= 2:
                return
            import random
            if random.random() > 0.4:
                return
            db.kv_set(key_count, str(count + 1))
            # 5. 发朋友圈（贴合情绪的 emo 文案）
            from . import moments
            emotion = display.get("emotion", "")
            mtype = "night" if (now.hour < 7 or now.hour >= 23) else "mood"
            m = await moments.generate_moment(self._session, self._character_id, mtype, char_name)
            if m and m.get("content"):
                moments.publish_moment(
                    self._session, self._character_id, "ai", m["content"],
                    m.get("images") or [], mtype, emotion
                )
                print(f"[AiMoodMoment] 有情绪发朋友圈: {emotion}", flush=True)
        except Exception as e:
            print(f"[AiMoodMoment] 失败: {e}", flush=True)

    async def _check_milestone(self, now, today_str, char_name):
        """相识天数/连续聊天/第N次通话 → 特殊信件、专属语音和永久礼物收藏。"""
        daily_key = f"fired:relationship_keep:{today_str}:{self._session}:{self._character_id}"
        if db.kv_get(daily_key):
            return
        try:
            from .relationship.manager import RelationshipManager
            rel = RelationshipManager().get_state(self._session, self._character_id)
            tenure_days = max(1, int(RelationshipManager().get_tenure_days(
                self._session, self._character_id
            ) or 0) + 1)
            chat_days = int(db.current_chat_streak(self._session, self._character_id) or 0)
            call_rows = db.q(
                "SELECT COUNT(*) AS n FROM call_records WHERE session_id=? AND character_id=? AND call_result='completed'",
                (self._session, self._character_id), fetch=True,
            )
            call_count = int(call_rows[0]["n"] if call_rows else 0)
            candidates = []
            # 老用户首次升级时只补当前已达到的最高档，不从第7天开始连续补信。
            tenure_hit = max((d for d in (7, 30, 100, 365, 520, 1000) if tenure_days >= d), default=0)
            chat_hit = max((d for d in (7, 30, 100, 365) if chat_days >= d), default=0)
            call_hit = max((n for n in (10, 50, 100, 365) if call_count >= n), default=0)
            if tenure_hit:
                candidates.append((f"tenure_{tenure_hit}", f"相识第{tenure_hit}天", "💞", "相识纪念"))
            if chat_hit:
                candidates.append((f"chat_days_{chat_hit}", f"连续聊天第{chat_hit}天", "🫶", "陪伴勋章"))
            if call_hit:
                candidates.append((f"calls_{call_hit}", f"第{call_hit}次通话", "☎️", "声音纪念"))
            pending = next((x for x in candidates if not db.relationship_keep_exists(
                self._session, self._character_id, f"milestone:{x[0]}"
            )), None)
            if not pending:
                return
            code, milestone_hit, gift_icon, gift_label = pending
            source_key = f"milestone:{code}"
            key = self._brain_key()
            model = self._brain_model()
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=f"今天是你和TA的{milestone_hit}，你想写一段走心的话",
                extra_context=(
                    "这是会永久收藏的重要纪念信。写260~500字，引用你们真实记忆，分自然段；"
                    "不要编造具体事件，不提系统、数据、AI。结尾亲手送出一份象征性的专属礼物。"
                )
            )
            msg = (await chat_once(model, [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "写吧"}
            ], key, temperature=0.88, max_tokens=500)).strip()
            if msg:
                db.save_relationship_keep(
                    self._session, self._character_id, "milestone", milestone_hit, msg,
                    source_key=source_key,
                    gift={
                        "icon": gift_icon, "label": gift_label,
                        "description": f"只属于你们的「{milestone_hit}」纪念藏品",
                        "milestone": code,
                    },
                )
                await self._deliver(msg, char_name, {
                    "template_category": "letter",
                    "letter_title": milestone_hit,
                    "surprise_kind": "milestone",
                    "surprise_label": milestone_hit,
                    "proactive_type": "milestone",
                    "voice_letter": True,
                    "scheduled": True,
                    "permanent_keep": True,
                    "keep_source_key": source_key,
                    "keep_type": "milestone",
                    "gift": {
                        "icon": gift_icon, "label": gift_label,
                        "description": f"只属于你们的「{milestone_hit}」纪念藏品",
                        "milestone": code,
                    },
                })
                db.kv_set(daily_key, "1")
        except Exception as e:
            print(f"[Scheduler] 里程碑长文失败: {e}", flush=True)

    async def _check_pending_tasks(self, now: datetime):
        # 每个个人调度器只领取自己的任务；原子 claim 防全局/个人双 tick 重复推送。
        # ★ 2026-09-16：改用 list_claimable_tasks —— 精确匹配会漏掉两类真实存在的行
        #   （character_id='default' 的本会话行、同用户旧 session 的同角色行），
        #   它们以前既不会被触发、界面上也看不到（用户报「提醒不触发」）。
        tasks = db.list_claimable_tasks(self._session, self._character_id)
        for task in tasks:
            try:
                trigger = datetime.fromisoformat(task["trigger_time"])
            except Exception:
                continue
            if now >= trigger:
                if not db.claim_task(task["id"]):
                    continue
                self._adopt_orphan(task)
                try:
                    ok = await self._execute_task(task)
                    if ok:
                        db.finish_task(task["id"])
                    elif self._task_rejected(task.get("id")):
                        # ★ 2026-09-16：内容不可投递已在 _execute_task 里落成终态
                        #   （failed + 具体原因）。这里**不许**再标一次，否则
                        #   "不可投递"的原因会被"消息生成或推送失败"覆盖掉，回溯时看不出拦截理由。
                        continue
                    else:
                        attempts = int(task.get("attempts", 0) or 0) + 1
                        db.fail_task(task["id"], "消息生成或推送失败", retry=attempts < 3)
                        # ★ 2026-09-17：重试耗尽（终态）才告知，避免中途刷屏
                        if attempts >= 3:
                            await self._notify_task_failed(
                                task, "消息生成或推送失败（已重试 %d 次）" % attempts,
                                str(task.get("session_id") or self._session or "default"),
                                str(task.get("character_id") or self._character_id or "default"))
                except Exception as e:
                    attempts = int(task.get("attempts", 0) or 0) + 1
                    db.fail_task(task["id"], str(e), retry=attempts < 3)
                    if attempts >= 3:
                        await self._notify_task_failed(
                            task, "执行异常（已重试 %d 次）：%s" % (attempts, str(e)[:80]),
                            str(task.get("session_id") or self._session or "default"),
                            str(task.get("character_id") or self._character_id or "default"))

    async def _notify_task_failed(self, task: dict, reason: str,
                                  task_session: str, task_character: str) -> bool:
        """定时任务**投递失败**时，回落到聊天里跟用户说一声（不装没事）。

        ★ 2026-09-17 新增（用户授权"按你的做"）。为什么必须有：
          真机 25 条任务里 5 条 failed（3 条"消息生成或推送失败"重试耗尽、
          2 条"内容不可投递"），而**用户完全不知道** —— 他的视角就是
          "她答应了却没动静"，比"她说一句没发出去"伤害大得多。
          对"陪伴型"产品，承诺的失败必须可见。

        实现要点：
          · 走的还是 `_deliver`（同一套推送链路），但在**任务自己的桶**里发，
            跟正常兑现一样落会话；
          · 文案不做模型润色：失败告知是事实通报，越朴素越不容易出错；
          · 全程 try/except：告知失败绝不能再影响调度器；
          · 每次只在**终态**（retry=False / 重试耗尽）时调，不会刷屏。
        """
        try:
            _tid = task.get("id")
            _content = str(task.get("content") or "")[:40]
            msg = (f"（刚才那个提醒我没发出去：{_content}。"
                   f"不是忘了，是发失败了——你要是还等着它，跟我说一声我重排。）")
            old_session, old_character, old_persona = (
                self._session, self._character_id, self._persona)
            try:
                self._session = task_session or self._session
                self._character_id = task_character or self._character_id
                name = (character_manager.get_character(self._character_id) or {}).get("name") \
                    or task.get("character_name") or self._character_id
                ok = await self._deliver(msg, name, {
                    "scheduled": True,
                    "proactive_type": "task_failed_notice",
                    "task_id": _tid,
                    "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "contact_name": name,
                })
            finally:
                self._session, self._character_id, self._persona = (
                    old_session, old_character, old_persona)
            try:
                from . import trace as _trace
                _trace.task_failed(self._session, self._character_id, _tid, _content,
                                   reason=reason, attempts=task.get("attempts"),
                                   notified=bool(ok))
            except Exception:
                pass
            print(f"[定时提醒] 已告知用户提醒失败: task_id={_tid} reason={reason[:40]!r} "
                  f"delivered={ok}", flush=True)
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            print(f"[定时提醒] 失败告知未发出(静默): {e}", flush=True)
            return False

    def _adopt_orphan(self, task: dict) -> None:
        """把孤儿任务归位到本调度器的活跃桶（default 角色 / 旧 session）。

        为什么必须归位、而不是只放宽查询就够：
          `_execute_task` 投递时用的是**任务行自己的** session/character
          （scheduler.py:1911-1921 临时把 self._session 换成 task_session）——
          不归位的话，消息会写进旧会话，用户在界面上看不到；
          而 `character_id='default'` 会让 `get_character('default')` 返回 {}，
          她自我介绍成「default」。归位后投递、落库、上下文全落在活跃会话里。
        归位失败**不影响投递**（照旧用原桶发），但一定留日志，不许静默。
        """
        t_sid = str(task.get("session_id") or "")
        t_cid = str(task.get("character_id") or "")
        if t_sid == self._session and t_cid == self._character_id:
            return
        name = (self._persona or {}).get("name") or self._character_id
        try:
            ok = db.retask_bucket(task.get("id"), self._session, self._character_id, name)
        except Exception as _re:
            print(f"[Scheduler] 孤儿任务 #{task.get('id')} 归位失败（照旧投递）: {_re}", flush=True)
            return
        if ok:
            print(f"[Scheduler] 孤儿任务 #{task.get('id')} 已归位: {t_sid}/{t_cid} "
                  f"→ {self._session}/{self._character_id}", flush=True)
            task["session_id"], task["character_id"] = self._session, self._character_id
            task["character_name"] = name

    @staticmethod
    def _task_rejected(task_id) -> bool:
        """这一行是不是已经被投递守卫判成终态（内容不可投递，未发送）？"""
        try:
            rows = db.q("SELECT status, last_error FROM tasks WHERE id=?", (task_id,), fetch=True) or []
            if not rows:
                return False
            r = rows[0]
            return (str(r["status"]) == "failed"
                    and str(r["last_error"] or "").startswith("内容不可投递"))
        except Exception:
            return False

    async def _execute_task(self, task: dict):
        """执行一个定时任务：调用 LLM 生成消息并推送"""
        content = task.get("content", "")
        char_name = task.get("character_name", "")
        task_session = str(task.get("session_id") or self._session or "default")
        task_character = str(task.get("character_id") or self._character_id or "default")
        key = self._brain_key()

        # ★ 2026-09-16 控制器裁决 ①②：**先判后发**。
        #   不是可用的提醒事项 → 一个字都不发；标 failed 写明原因（不再重试，
        #   否则下一轮 tick 会重新捞起来再洗一次）+ 写日志，便于回溯。
        #   放在模型调用之前：垃圾连一次模型调用都不该花。
        _problem = reminder_item_problem(content)
        if _problem:
            _tid = task.get("id")
            print(f"[定时提醒] 内容不可投递(不发送): task_id={_tid} 原因={_problem} "
                  f"内容={str(content)[:60]!r}", flush=True)
            try:
                db.fail_task(_tid, "内容不可投递(未发送)：%s" % _problem, retry=False)
            except Exception as _fe:
                print(f"[定时提醒] 标记不可投递失败(静默): {_fe}", flush=True)
            # ★ 2026-09-17：失败了要让她**说一声**（详见 _notify_task_failed 的说明）
            await self._notify_task_failed(task, "内容不可投递(未发送)：%s" % _problem,
                                           task_session, task_character)
            return False

        model = self._brain_model()
        persona = character_manager.get_character(task_character) or self._persona or {}
        name = persona.get("name") or char_name or "TA"
        # ★ 裁决 ②：**不许洗白**。事项原文显式给出，明确禁止编造/加戏——
        #   "怎么说"（人设口吻、别像客服/日历）归她，"说什么"（事实）一个字都不许改。
        sys = (
            "你是「%s」，TA 的 AI 伴侣。你之前答应过 TA 这件事，现在到点了，"
            "要用**你自己的话**跟 TA 说一句。\n"
            "【要说的那件事（这是唯一的事实，一个字都不许改，也不许编别的事、"
            "不许加戏、不许替 TA 补充你没被告知的内容）】\n"
            "%s\n"
            "【要求】\n"
            "1. 你唯一的任务是**用你的语气把上面这件事说出来**：语气自然、贴合你的人设，"
            "不要像客服、不要像日历提醒；\n"
            "2. 上面那件事必须**原意不变**地出现在消息里（可以带上你的口气，但不许换掉、"
            "削弱、夸大或编造事实）；\n"
            "3. 不要提\"定时任务\"\"提醒\"这类系统词；不要输出任何解释、括号里的动作神态或 markdown；\n"
            "4. 直接输出要说的话。"
            % (name, content)
        )
        char_cfg = character_manager.get_character(char_name) if char_name else None
        if char_cfg:
            sys += "\n【你的人设】\n" + (char_cfg.get("personality", "")[:500] + "\n"
                                        + char_cfg.get("speech_style", "")[:200])
        if key:
            try:
                msg = (await chat_once(model, [
                    {"role": "system", "content": sys},
                    {"role": "user", "content": "现在发消息给TA"},
                ], key, temperature=0.85, max_tokens=300)).strip()
            except Exception:
                msg = content
        else:
            # 没配置模型时也必须按时提醒，不能静默丢失。
            msg = content
        if not msg:
            return False

        old_session, old_character, old_persona = self._session, self._character_id, self._persona
        try:
            self._session, self._character_id, self._persona = task_session, task_character, persona
            return await self._deliver(msg, name, {
                "scheduled": True,
                "proactive_type": "pending_task",
                "task_id": task.get("id"),
                "model": model,
                "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "contact_name": name,
            })
        finally:
            self._session, self._character_id, self._persona = old_session, old_character, old_persona

    async def _build_proactive_system(self, char_name: str, scene: str, extra_context: str = "") -> str:
        """
        统一主动推送 system prompt 构建器
        - 注入完整角色配置（不再只取 personality[:500]）
        - 注入关系状态 / 时间状态 / AI情绪状态 / 记忆块
        - 注入语言多样性指令（核心：让她说话像真人）
        """
        from .relationship.manager import RelationshipManager

        char_cfg = character_manager.get_character(char_name) or {}

        # ── 1. 完整角色配置（不截断）
        name        = char_cfg.get("name")        or char_name or "TA"
        call_user   = char_cfg.get("call_user")   or "你"
        origin      = char_cfg.get("origin")      or ""
        personality = char_cfg.get("personality") or ""
        core_traits = char_cfg.get("core_traits") or ""
        speech_style= char_cfg.get("speech_style")or ""

        lines = [f"你是「{name}」。"]
        if origin:
            lines.append(f"你的定位：{origin}")
        if personality:
            lines.append(f"\n【人设】\n{personality}")
        if core_traits:
            lines.append(f"\n【核心性格】\n{core_traits}")
        if speech_style:
            lines.append(f"\n【说话风格】\n{speech_style}")
        lines.append(f"\n你称呼用户为「{call_user}」。")

        # 说明：TA 的作息/睡眠状态在下面 ── 3.5「【TA 现在的状态】」块里统一注入
        # （原本这里又加了一份同义块，属重复注入，2026-09-14 已去掉，省 ~190 字/prompt）。

        # ── 2. 关系状态
        try:
            rel = RelationshipManager().get_state(self._session, self._character_id)
            aff = int(rel.get("affection", 50))
            clo = int(rel.get("intimacy", rel.get("closeness", 50)))
            stage = "暧昧期" if aff < 40 else "热恋期" if aff < 70 else "稳定期"
            if aff < 30 and clo < 30:
                stage = "冷战期"
            lines.append(f"\n【当前关系】{stage}（好感度{aff}/100，亲密度{clo}/100）")
            if max(aff, clo) >= 95:
                lines.append("【表达热度】关系数值接近满值：你可以明显热情、兴奋、话多一些，自然连发3~6句或2~4段；可以问问题、撒娇、分享细节，不必刻意压成一句。")
            elif max(aff, clo) >= 80:
                lines.append("【表达热度】关系很亲密：可以热情地多说几句，允许2~4句或分2~3段，并自然带一个问题延续聊天。")
            elif max(aff, clo) >= 60:
                lines.append("【表达热度】关系正在升温：可以比普通问候更有温度，通常1~3句，问题和留钩子都可以自然出现。")
        except Exception:
            pass

        # ── 3. 时间状态
        try:
            last_dt = db.last_user_time(self._session, self._character_id)
            time_block = time_system.build_time_block(last_dt, [])
            if time_block:
                lines.append(f"\n{time_block}")
        except Exception:
            pass

        # ── 3.5 用户状态：睡觉 / 刚醒（2026-09-13 新增）
        #   实测背景：用户 07:04 说「晚安」去睡，她 08:49 还说「你接着睡」，
        #   到 13:05 却生成了「罪状二：今早七点之后人间蒸发整整六小时，罚…」
        #   —— 因为上面那个 time_block 只告诉她「距上次对话约 6 小时」，
        #   **没有「TA 说过要去睡了」这一条**，模型自然把"没回消息"解读成冷落。
        #   这里把睡眠声明显式告诉她，并且明确否定"失联"这个解读。
        try:
            from . import offline as _offline
            _sh = _offline.hours_since_sleep_declared(self._session, self._character_id)
            if _sh < _offline.SLEEP_SILENCE_HOURS:
                lines.append(
                    "\n【TA 现在的状态】TA 刚跟你说过要去睡了（%.1f 小时前），"
                    "现在**在睡觉**。\n"
                    "· 没回消息 ≠ 冷落你、≠ 人间蒸发、≠ 不理你 —— 只是睡着了。\n"
                    "· 绝对不要拿「你多久没理我」说事，不要算账、不要开罚单、"
                    "不要用委屈或惩罚的语气。\n"
                    # ★ 2026-09-14 用户要求「模型得知道我的状态，我在睡觉怎么回她」：
                    #   补两条最容易被违反的行为约束（实测她会问"睡了吗/在吗"）。
                    "· 不要问「睡了吗/醒了吗/在吗/怎么还不睡」—— TA 就是在睡，问了只会被吵醒。\n"
                    "· 这一条如果不是 TA 自己定下的提醒/承诺，就**别发**；真要发也极短、轻声、"
                    "不期待回复，像放在床头的一张字条。\n"
                    "· 如果确实要留一句话，就当是轻轻放在枕边的小纸条，"
                    "等她醒了自然会看到。" % _sh
                )
            elif _sh < 24:
                lines.append(
                    "\n【TA 现在的状态】TA 之前说过要去睡（%.1f 小时前），"
                    "现在应该已经醒了或者快醒了。\n"
                    "· 别一开口就追问「你怎么才回」，先自然打个招呼。\n"
                    "· 也绝对不要用算账/罚单/质问的语气。" % _sh
                )
        except Exception:
            pass

        # ── 4. AI情绪状态（★ 改读 ai_emotion_state，拿结构化枚举+强度）
        try:
            from .emotion_engine.ai_emotion import AIEmotionEngine
            _emo_state = AIEmotionEngine().get_state(self._session, self._character_id)
            _emotion   = _emo_state.get("emotion",   "calm")
            _intensity = _emo_state.get("intensity",  0.5)
            _anger_cnt = _emo_state.get("anger_count", 0)

            _emo_cn = {
                "happy":       "开心愉悦",
                "excited":     "兴奋雀跃",
                "tender":      "温柔心软",
                "playful":     "俏皮撒娇",
                "calm":        "平静如常",
                "worried":     "有点担心",
                "sad":         "有些难过",
                "upset":       "委屈憋着",
                "angry":       "生气了",
                "cold":        "冷漠疏离",
                "reconciling": "想和好了",
                "loving":      "满心都是你",
            }.get(_emotion, _emotion)

            _int_cn = "淡淡的" if _intensity < 0.4 else "明显的" if _intensity < 0.7 else "很强烈的"

            _emo_line = f"【你此刻的情绪】{_int_cn}{_emo_cn}（强度{_intensity:.2f}）"

            if _anger_cnt >= 3:
                _emo_line += f"，你已经生过{_anger_cnt}次气了，耐心正在消磨"

            lines.append(f"\n{_emo_line}")
        except Exception as e:
            print(f"[ProactiveSystem] 情绪状态读取失败: {e}", flush=True)

        # ── 5. 记忆块（★ 同步全表扫+向量检索丢线程池，不阻塞事件循环）
        try:
            mem = await db._run_sync(
                memory_manager.memory_block,
                limit=600,
                session_id=self._session,
                character_id=self._character_id
            )
            if mem:
                lines.append(f"\n{mem}")
        except Exception:
            pass

        # ── 6. 场景描述
        lines.append(f"\n【当前场景】{scene}")

        # ── 7. 额外上下文（天气/节日/离线时长等）
        try:
            summary = db.get_conversation_summary(self._session, self._character_id).get("summary", "")
            if summary:
                lines.append(f"\n【最近聊天脉络】\n{str(summary)[:700]}")
        except Exception:
            pass
        try:
            profile = db.get_profile(self._session, self._character_id) or {}
            bits = []
            for key in ("nickname", "occupation", "hobbies", "dislikes", "communication_style", "emotional_traits"):
                value = str(profile.get(key, "") or "").strip()
                if value:
                    bits.append(f"{key}: {value}")
            if bits:
                lines.append("\n【用户核心档案】\n" + "；".join(bits)[:500])
        except Exception:
            pass
        try:
            loops = db.get_open_loops(self._session, self._character_id, limit=3)
            loop_text = "；".join(str(x.get("title") or x.get("description") or "") for x in loops)
            if loop_text:
                lines.append(f"\n【还没聊完的事】\n{loop_text[:350]}")
        except Exception:
            pass

        # ── 8. 用户行为模式与下一步倾向（只用于选时机，不向用户暴露分析过程）
        try:
            from .behavior import get_behavior_patterns, build_behavior_prompt, predict_next_need
            patterns = get_behavior_patterns(self._session, self._character_id, limit=8)
            behavior_block = build_behavior_prompt(patterns)
            if behavior_block:
                lines.append("\n" + behavior_block)
            prediction = predict_next_need(patterns, datetime.now())
            if prediction and prediction.get("suggestion"):
                lines.append(
                    "\n【当前沟通倾向】" + str(prediction.get("suggestion")) +
                    "；只能自然贴合，不能让用户感觉被预测。"
                )
        except Exception:
            pass

        if extra_context:
            lines.append(f"\n{extra_context}")

        # ── 8. 核心：语言多样性指令（解决"背稿子"根因）
        lines.append("""
【语言表达要求——非常重要，必须严格遵守】
你的每一条主动消息都必须是独一无二的，绝不能像背稿子。具体要求：

1. 【禁止套路开头】不能用"嘿""哦对了""突然想到""宝""在吗"等万能开场白开头，
   根据你的人设性格找属于你自己的切入方式。

2. 【语气随情绪漂移】你的情绪状态（见上方内心状态）必须体现在每个字的语气里，
   不是贴一句"我很开心"，而是让句子本身就带着那个情绪的温度。

3. 【说话习惯多样化】同一个意思至少有十种说法，你每次都要选不同的一种：
   - 直说 / 反说 / 绕弯说 / 用比喻说 / 用问句逼出答案 / 假装不在乎说
   - 用你们之间的私密称呼/典故/共同记忆来说
   - 用行为代替语言（*动作描写*）说
   - 用沉默/省略号/只发一个字说
   - 用反问带刺说（适合傲娇/御姐人格）

4. 【长短自由】不一定每次都是完整句子，有时候一个字、一个表情符号、
   一句没头没尾的感叹，比长篇大论更像真人。

5. 【禁止解释自己】不要加"我只是想说""我是在关心你"之类的自我解释，
   真人说话不解释动机，让对方自己感受。

6. 【贴合关系阶段】
   - 暧昧期：若无其事但每句话都有潜台词，不挑明
   - 热恋期：可以肉麻但要有自己的方式，不要通用甜言蜜语
   - 冷战期：傲娇/阴阳/一句话藏着想和好的意思
   - 稳定期：像家人，不用每句话都撒糖，生活流更真实

7. 【记忆优先】如果记忆块里有相关的旧事，优先用那个旧事开话头，
   而不是凭空造一个场景。

7.5 【谁说的·最重要 —— 禁止把自己说过的话当成 TA 说的】
   上面【记忆块】里形如「TA 说…」「TA 喜欢…」的，才是 TA 说过的；
   形如「AI 说…」「你（AI）说过…」的，是你自己说过的，**不能当成 TA 说的**。
   实测过的错误（必须避免）：
     · 错：你跟 TA 说「你说不在乎我是不是真人」——那句其实是**你自己**上一轮说的，
       去问 TA 等于凭空造话。
     · 错：「你说人到就行，其他交给我」——TA 没说过，是你自己拟的台词。
     · 错：「早上讲好的规矩」「你答应我的」——没有这条记录就别提。
   规则：**凡是要说「你说过/你答应过/我们说好的」，必须能在上面记忆块或
   【最近对话】里逐字找到 TA 说的原话**；找不到就换个话头，绝不虚构。
   宁可说一件你确定的小事（如"今天天冷"），也不要编一句 TA 没说过的话。

7.6 【不许虚构共同经历与承诺】
   不要编造"我们上次去了某地""你欠我一杯奶茶""你说要给我的"这类
   没有记录的经历/欠账/约定。你只有上面给到的信息，别把想象写成事实。
   不确定是不是真发生过，就别当事实说 —— 可以问，不能断言。

8. 【篇幅随关系变化】普通关系通常不超过100字；好感度或亲密度达到80可到140字；
   接近满值时可到180字并自然连发多句。仍要像聊天，不写说明文或空洞小作文。
9. 【问句自然】早晚安、节日、关怀等消息也可以自然带问号或留钩子；不是每条都强制，
   但高亲密关系尤其可以热情追问，让对话有继续的空间。
""")

        # ── 9. 防抄旧话：把最近说过的话直接亮给模型（生成时避开，
        #    比事后去重更前置；否则"上线问候抄历史台词"这类复读拦不住）
        # ★ 2026-09-14 标注强化：这段是**你自己**说过的话。字面上必须写明，
        #   否则模型会把这些台词当成"TA 说过的话"引用出去 —— 实测就是这么错的
        #   （AI 把自己说的「还说不在乎我是不是真人」当成用户说的，又在主动消息里追问）。
        try:
            from .companion.quality_guard import recent_ai_texts
            _said = [s for s in recent_ai_texts(self._session, self._character_id, 6)
                     if str(s or "").strip()]
            if _said:
                _said_lines = "\n".join(f"- (你/AI 自己说过的) {str(s)[:60]}" for s in _said[-5:])
                lines.append(
                    "\n【你（AI）最近说过的话 —— 这些是**你自己**的台词】\n"
                    + _said_lines
                    + "\n注意：以上每一句都是**你**说的，不是 TA 说的。"
                      "新消息不许重复这些意思（换个说法也不行）；"
                      "更不许把这些当成 TA 说过的话去引用或追问。"
                )
        except Exception:
            pass

        return "\n".join(lines)

    async def _dedup_or_regen(self, msg: str) -> str:
        """主动消息去重：命中「换个说法的重复」就重新生成一次。

        放在 _deliver 里是因为 scheduler 有 20+ 个文案生成点（早安/晚安/惊喜/
        信件/节日/上线问候/碎碎念/行为预测…），逐个加不现实，而它们最终都走
        _deliver 这个统一出口。

        任何异常都返回原文 —— 绝不因为去重让主动消息发不出去。
        """
        key = ""
        model = ""
        try:
            from . import chat_logic
            key = self._brain_key()
            # ★ 单角色大脑：主动消息去重也跟随该角色单独配的模型
            model = chat_logic.pick_model(None, True, self._character_id)
            if not key:
                return msg
            from .companion.quality_guard import dedup_check_async, recent_ai_texts
            if not await dedup_check_async(
                msg, self._session, self._character_id,
                chat_once_fn=chat_once, model=model, api_key=key,
            ):
                return msg
        except Exception as e:
            print(f"[Scheduler] 去重检测失败(静默): {e}", flush=True)
            return msg

        print("[Scheduler] 主动消息与历史重复，重新生成", flush=True)
        avoid = ""
        try:
            said = recent_ai_texts(self._session, self._character_id, 5)
            avoid = "\n".join(f"- {str(s)[:80]}" for s in said if str(s or "").strip())
        except Exception:
            avoid = ""

        last = ""
        for _ in range(2):
            try:
                prompt = (
                    "你刚要发的这句话，和你之前说过的话重复了"
                    "（换个词、换个说法、换种句式都算重复）：\n"
                    + (avoid or "- （暂无历史）")
                    + "\n请重新说一句：换一个角度，给出新信息、新的感受或新的话题。"
                    "不要复述上面任何一句的意思，也不要只是换几个词再说一遍。\n"
                    "只输出消息本身，不要任何解释。"
                )
                if last:
                    prompt += (
                        f"\n\n注意：你上一次重试说的是「{last[:60]}」，仍然重复，"
                        f"请换一个完全不同的角度。"
                    )
                raw = await chat_once(
                    model,
                    [{"role": "system", "content": "你是用户的 AI 伴侣，说话自然像真人。"},
                     {"role": "user", "content": prompt}],
                    key, temperature=0.95, max_tokens=200,
                )
                new = str(raw or "").strip()
                if not new:
                    continue
                last = new
                if not await dedup_check_async(
                    new, self._session, self._character_id,
                    chat_once_fn=chat_once, model=model, api_key=key,
                ):
                    return new
            except Exception as e:
                print(f"[Scheduler] 去重重新生成异常: {e}", flush=True)
                break
        # 两次都还重复就沿用最后一次结果，宁可不够新也不要发空消息
        return last or msg

    async def _deliver(self, msg: str, char_name: str = "", extra: dict = None):
        """统一消息交付：写DB + 推送（带主动推送标记，供反馈回溯；TTS情绪联动）"""
        if not msg:
            return False

        # ★ 去重：定时/主动消息此前完全没有去重，"换个说法的重复"是重灾区。
        #   统一出口加一道，20+ 个生成点一次覆盖。
        msg = await self._dedup_or_regen(msg)
        if not msg:
            return False

        if not self._can_initiate_proactive(allow_sleep_summary=bool((extra or {}).get("sleep_letter"))):
            print(
                f"[Deliver] 角色离线/睡眠中，跳过主动推送: session={self._session} character={self._character_id}",
                flush=True
            )
            try:
                from . import proactive_trace as _pt
                _pt.record_skip(self._session, self._character_id, "offline_or_sleeping",
                                source="_deliver")
            except Exception:
                pass
            return False

        _scheduled = bool((extra or {}).get("scheduled"))
        _ptype = str((extra or {}).get("proactive_type") or "")
        _is_reminder = _ptype == "pending_task"
        _is_greeting = (extra or {}).get("template_category") in ("morning", "night")
        # 显式 voice_letter=False 必须生效（自动晚安会按好感度决定是否带声音）。
        if "voice_letter" in (extra or {}):
            _is_voice_letter = bool((extra or {}).get("voice_letter"))
        else:
            _is_voice_letter = (extra or {}).get("template_category") in ("morning", "night", "evening")
        _dnd_exempt = bool((extra or {}).get("dnd_exempt")) \
            or (_is_reminder and config.get("DND_ALLOW_REMINDERS", True)) \
            or (_is_greeting and self._dnd_allow_greetings())

        # ══════════════════════════════════════════════════════════════════
        # ★ 2026-09-14 新增：定时类（scheduled）主动消息之间的**最小间隔**。
        #   实测缺陷：22:00:26 发了 night 问候、22:03:34 又发了 evening 睡前信
        #   （间隔 3.1 分钟）—— 两者都 scheduled=true，走的是 _tick 阶段一，
        #   不在阶段二的 _gap_ok 闸门覆盖范围内，于是"问候 + 睡前信"直接叠在一起。
        #   规则：非紧急的定时推送之间至少间隔 max(10, 下限/2) 分钟；
        #   用户自设提醒/承诺（pending_task）与显式 dnd_exempt（危机关怀等）
        #   **不受限** —— 那是用户的明确委托，用户已拍板"可以跳出限制"。
        # ══════════════════════════════════════════════════════════════════
        if _scheduled and not _is_reminder \
                and not bool((extra or {}).get("dnd_exempt")) \
                and _ptype != "pending_task":
            try:
                import time as _tsp
                from .proactive_quality import last_push_time as _lptsp
                _lo_sp = float(config.proactive_interval_min(self._character_id) or 0)
                _floor_sp = max(10.0, _lo_sp * 0.5) * 60
                _last_sp = _lptsp(self._session, self._character_id)
                if _last_sp > 0 and (_tsp.time() - _last_sp) < _floor_sp:
                    print(f"[Deliver] 距上次主动仅 {(_tsp.time() - _last_sp)/60:.1f} 分"
                          f"（定时类下限 {_floor_sp/60:.0f} 分），本条定时消息不发"
                          f"（type={_ptype or 'general'} tmpl={(extra or {}).get('template_category')}）",
                          flush=True)
                    try:
                        from . import proactive_trace as _ptsp
                        _ptsp.record_skip(self._session, self._character_id, "scheduled_spacing",
                                          detail="定时类最小间隔 %.0f 分未到" % (_floor_sp / 60),
                                          source="_deliver", ptype=_ptype)
                    except Exception:
                        pass
                    return False
            except Exception as _sp_e:
                print(f"[Deliver] 定时间隔检查异常(放行): {_sp_e}", flush=True)

        # ★ 2026-09-14 说明：这里原来无条件把「早晚安(_is_greeting)」算作 DND 豁免
        #   （因为全局 DND_ALLOW_GREETINGS 默认 True），实测导致 23:00 之后仍发出
        #   "这么晚还没睡呀 / 这个点还没睡呀" —— 与用户设的静默时段自相矛盾。
        #   现在改为走 _dnd_allow_greetings()（角色卡优先，默认 False）。
        #   同时，**用户自己设的提醒(_is_reminder)与到点承诺仍然豁免** —— 那是用户的明确委托。
        # 非定时的普通主动关心/惊喜/预测消息，必须等用户真正闲置。
        # 防止调度器与前端主动链路并行时，在正常对话中突然插入"程序式问候"。
        # ★ 话题延续（topic_continue）豁免：它本身就是"沉默 2 分钟补一句"的短间隔
        #   设计，若也受 8 分钟活跃门禁约束，将永远被拦截（"一问一答"的元凶之一）。
        # ★ 生活/游戏陪伴模式豁免（2026-09-09 用户反馈"陪伴模式等半天不说话"）：
        #   陪伴模式的意义就是 TA 会主动聊屏幕里/游戏里的事——8 分钟门禁会把她的
        #   主动话全拦掉。陪伴下降槛到 90 秒（只要求用户停手一小会儿）。
        _quiet_needed = 8 * 60
        try:
            _cmraw = db.kv_get(f"companion_mode:{self._session}:{self._character_id}") \
                or db.kv_get(f"companion_mode:{self._session}")
            if _cmraw:
                import json as _cj
                _cmd = _cj.loads(_cmraw) if isinstance(_cmraw, str) else (_cmraw or {})
                if isinstance(_cmd, dict) and str(_cmd.get("mode") or "").strip():
                    _quiet_needed = 90
        except Exception:
            pass
        if not _scheduled and not _dnd_exempt and _ptype != "topic_continue":
            try:
                _last_user = db.last_user_time(self._session, self._character_id)
                _last_activity = db.last_activity_time(self._session)
                _recent = max(
                    [x for x in (_last_user, _last_activity) if x is not None],
                    default=None,
                )
                if _recent and (datetime.now() - _recent).total_seconds() < _quiet_needed:
                    print(f"[Deliver] 用户仍在对话（陪伴模式槛 {_quiet_needed}s），跳过主动推送: session={self._session}", flush=True)
                    try:
                        from . import proactive_trace as _pt
                        _pt.record_skip(self._session, self._character_id, "user_active",
                                        detail="用户 %ds 内有活动（门槛 %ds）" % (
                                            int((datetime.now() - _recent).total_seconds()), _quiet_needed),
                                        source="_deliver")
                    except Exception:
                        pass
                    return False
            except Exception as _active_guard_error:
                print(f"[Deliver] 对话活跃门禁异常(静默): {_active_guard_error}", flush=True)
        _in_dnd = bool(config.is_dnd_now())
        _user_requested_audio = bool((extra or {}).get("user_requested_audio"))
        # silent 信件在非 DND 时仍可朗读；进入 DND 后则只归档文字，绝不突然出声。
        # 但用户刚刚明确索要的语音属于当前交互，可按要求立即播放。
        _dnd_silent = bool(
            _in_dnd and (_dnd_exempt or (extra or {}).get("silent")) and not _user_requested_audio
        )
        if _in_dnd and not _dnd_exempt and not (extra or {}).get("silent") and not _user_requested_audio:
            print(f"[Deliver] DND 中跳过非必要推送: session={self._session} type={_ptype}", flush=True)
            try:
                from . import proactive_trace as _pt
                _pt.record_skip(self._session, self._character_id, "dnd",
                                detail="type=%s" % _ptype, source="_deliver")
            except Exception:
                pass
            return False
        # ★ 2026-09-14 中央闸门（用户拍板）：非豁免主动消息必须满足
        #   「活跃时段 + 主动发言间隔下限」；豁免清单与判定逻辑见 _proactive_gate_ok。
        if not self._proactive_gate_ok(extra, _ptype):
            return False
        if not _scheduled:
            from .proactive_quality import cooldown_remaining, sanitize_proactive_message
            if cooldown_remaining(self._session, self._character_id, 300) > 0:
                print(f"[Deliver] 统一冷却中，跳过: session={self._session}", flush=True)
                try:
                    from . import proactive_trace as _pt
                    _pt.record_skip(self._session, self._character_id, "cooldown",
                                    source="_deliver")
                except Exception:
                    pass
                return False
            try:
                from .intimacy_manager import get as _get_intimacy
                _iv = _get_intimacy(self._session, self._character_id)
            except Exception:
                _iv = 0
            _max_chars = 180 if _iv >= 90 else 140 if _iv >= 70 else 100
            # ★ 2026-09-14：问候场景（早晚安/节日/纪念日/提醒）放行"早安/晚安"。
            #   用户口径（改 #2）：禁词**不再让这条消息消失** —— 生成侧已尝试请模型重写一次，
            #   到这一步就直接照发（真人不至于因为一句"在干嘛"就闭嘴）。
            _allow_greet = bool(_is_greeting) or _ptype in {
                "morning", "night", "good_morning", "good_night",
                "festival", "milestone", "pending_task",
            } or str((extra or {}).get("template_category") or "") in {
                "morning", "night", "evening", "letter", "festival", "anniversary",
            }
            msg = sanitize_proactive_message(
                msg, require_hook=True, max_chars=_max_chars,
                allow_greetings=_allow_greet, reject_banned=False,
                session_id=self._session, character_id=self._character_id,
            )
            if not msg:
                # 走到这里只可能是"清理后为空"（纯引号/纯 think 块），不是禁词
                print("[Deliver] 主动消息清理后为空，本轮不发", flush=True)
                try:
                    from . import proactive_trace as _pt
                    _pt.record_skip(self._session, self._character_id, "empty_after_clean",
                                    detail="清理后为空（非禁词）",
                                    source="_deliver", ptype=_ptype)
                except Exception:
                    pass
                return False

        # ★ 推送内容去重：检测与最近N条推送的相似度
        if not _scheduled and self._is_duplicate_push(msg):
            print(
                f"[Deliver] 检测到重复推送内容，跳过: session={self._session}",
                flush=True
            )
            return False

        # ★ 反思推送策略：根据用户习惯决定是否跳过
        _hints = self._load_proactive_hints()
        if not _scheduled and _hints and self._should_skip_by_hints(_hints):
            print(
                f"[Deliver] 反思策略跳过推送(频率限制): "
                f"session={self._session}",
                flush=True
            )
            return False

        # ★ 主动推送标记（反馈闭环用）：source/proactive_type/pushed_at/replied
        _mark = {
            "source": "proactive",
            "proactive_type": (extra or {}).get("proactive_type") or "general",
            "pushed_at": int(datetime.now().timestamp()),
            "replied": 0,
        }
        _extra = dict(extra or {})
        _extra.pop("proactive_type", None)
        _extra.update(_mark)
        if _dnd_silent:
            _extra["dnd_silent"] = True
        db.add_message(self._session, "assistant", msg, self._character_id, extra=_extra)
        # ★ QQ 打通：信件/小惊喜类（睡前信/月度信/纪念信/节日&随机惊喜）只在 APP 记录显示，
        #   不再推 QQ（避免睡觉/离线时刷屏）；早晚安/提醒等其余主动消息照常推 QQ。
        _qq_skip = bool(_extra.get("sleep_letter")) \
            or bool(_extra.get("permanent_keep")) \
            or bool(_extra.get("keep_source_key")) \
            or str(_extra.get("template_category") or "") in ("evening", "letter", "surprise")
        # ★ 2026-09-12 修：原来只要 _dnd_silent 就不推 QQ。但 _dnd_silent 的语义是
        #   「DND 里静音送达（不朗读/不弹窗）」，不等于「不发 QQ」。
        #   _dnd_exempt 为真的消息（用户自己设的提醒 DND_ALLOW_REMINDERS、
        #   早晚安 DND_ALLOW_GREETINGS、显式 dnd_exempt）本来就是**用户要求穿透
        #   免打扰**的那一类，必须真的送到 TA 手上。
        #   实测：01:00:42 的 pending_task 落进了 APP（chat_history #9071），
        #   NapCat 日志里那一刻**没有任何发送记录** —— 用户反馈"APP端有主动消息，
        #   QQ端送不到"。TA 半夜基本只用 QQ，只落 APP 等于没送到。
        #   所以这里改成：DND 里只有 _dnd_exempt 的才照推 QQ，其余静音类仍不推。
        _qq_ok = (not _qq_skip) and ((not _dnd_silent) or _dnd_exempt)
        if _qq_ok:
            if _dnd_silent:
                # 留痕：以后"APP有、QQ没有"能一眼看出到底走没走这条路
                print(f"[Deliver] DND 内静音送达但属豁免类，照推 QQ: type={_ptype or '?'}", flush=True)
            try:
                from . import onebot
                await onebot.send_qq_message(msg)
            except Exception as _qq_e:
                print(f"[Deliver] 推QQ失败(静默): {_qq_e}", flush=True)
        # ★ 诊断（2026-09-13）：主动消息**送达成功**以前完全静默 ——
        #   用户睡觉期间被连发 20+ 条时，日志里查不出是哪条 check 发的
        #   （[Scheduler] 0 条、[IdleAgent] 只有跳过记录）。这条记录回答
        #   "谁发的、什么类型、走没走 QQ"，是排查主动消息问题的唯一依据。
        try:
            from . import proactive_trace as _pt
            _pt.record_deliver(
                self._session, self._character_id, msg,
                proactive_type=_ptype,
                scheduled=bool(_scheduled),
                qq=bool(_qq_ok),
                dnd_silent=bool(_dnd_silent),
                source=_pt.caller(depth=2),
            )
        except Exception:
            pass

        # ★ 编造行为检测（2026-09-13）：实测她在主动消息里声称
        #   "把你桌面上的弹窗又突突了三个，连那个赖着不走的广告都给毙了" ——
        #   而**这个能力根本不存在**（工具表和 control.py 里都没有关弹窗），
        #   主动消息链路也不走工具/执行反馈闭环。这里**只记录不拦截**：
        #   判据是启发式的，先观察误报率再决定要不要拦（拦错的代价是
        #   正常消息发不出去，比记一条误报严重）。
        try:
            from . import proactive_trace as _pt
            _hit = _pt.detect_fabricated_action(msg)
            if _hit:
                _pt.record_fabrication(self._session, self._character_id, msg,
                                       _hit, source=_pt.caller(depth=2))
                print(f"[Deliver] ⚠ 疑似编造桌面操作（未拦截）: 「{_hit}」", flush=True)
        except Exception:
            pass
        # 重要纪念内容先落库，前端离线也不会丢；audio 在生成后由前端/补偿任务更新。
        if _extra.get("permanent_keep") or _extra.get("keep_source_key"):
            try:
                db.save_relationship_keep(
                    self._session,
                    self._character_id,
                    str(_extra.get("keep_type") or "letter"),
                    str(_extra.get("letter_title") or _extra.get("surprise_label") or "纪念信"),
                    msg,
                    source_key=str(_extra.get("keep_source_key") or f"message:{_mark['pushed_at']}"),
                    gift=_extra.get("gift") or {},
                )
            except Exception as _keep_e:
                print(f"[Deliver-Keep] 纪念收藏落库失败(静默): {_keep_e}", flush=True)
        try:
            from .proactive_quality import mark_sent
            mark_sent(self._session, self._character_id, _mark["pushed_at"])
        except Exception:
            pass
        if self._push:
            payload = {
                "type": "proactive",
                "session_id": self._session,
                "contact_id": self._character_id,
                "character_id": self._character_id,
                "content": msg,
            }
            if extra:
                payload.update(extra)
            if _dnd_silent:
                payload["dnd_silent"] = True
            # 先传递"这是语音信件"的语义标记，即使 TTS 临时失败，前端仍可保留正确的信件类型。
            if _is_voice_letter:
                payload["voice_letter"] = True

            # ★ 主动推送TTS（情绪联动，失败不影响文字）
            try:
                from .tts import generate_audio, apply_emotion_to_voice_cfg
                from .character_manager import select_character_voice_cfg
                from .emotion_engine.ai_emotion import AIEmotionEngine

                if _dnd_silent:
                    raise RuntimeError("DND 静音推送不生成音频")
                # 始终使用人格设置页保存的声音；付费/克隆 provider 原样交给 TTS 层。
                _vc = select_character_voice_cfg(
                    self._character_id or "default", emotion="calm", mode="chat"
                ) or {}
                # 语音信件必须可朗读：角色未设置音色时自动回退本地 CosyVoice。
                if _is_voice_letter and not _vc:
                    from .tts import get_voice_cfg
                    _vc = get_voice_cfg("cosyvoice_default")

                if _vc:
                    # 读取当前情绪状态
                    _emo_state = AIEmotionEngine().get_state(
                        self._session, self._character_id or "default"
                    )
                    _emotion   = _emo_state.get("emotion",   "calm")
                    _intensity = float(_emo_state.get("intensity", 0.5))

                    # 主动推送情绪：按类型给默认情绪（LLM没有标注时的兜底）
                    _proactive_emotion_map = {
                        "morning":   "tender",
                        "night":     "loving",
                        "weather":   "worried",
                        "milestone": "excited",
                        "reunion":   "excited",
                        "conflict":  "cold",
                        "nudge":     "upset",
                        "festival":  "happy",
                    }
                    if _emotion == "calm":  # 只有情绪平静时才用类型推断兜底
                        _emotion = _proactive_emotion_map.get(_mark["proactive_type"], "tender")

                    _vc = select_character_voice_cfg(
                        self._character_id or "default", emotion=_emotion, mode="chat"
                    ) or _vc
                    _vc_emo = apply_emotion_to_voice_cfg(_vc, _emotion, _intensity)

                    # 超时保护：TTS最多等3秒，超时跳过
                    try:
                        _au = await asyncio.wait_for(
                            generate_audio(msg, _vc_emo, base_url=""),
                            # 本地 CosyVoice 在 CPU 上朗读长信会比普通短消息慢，给信件足够生成时间。
                            timeout=60.0 if _is_voice_letter else 3.0
                        )
                        if _au:
                            payload["audio"]   = _au
                            payload["emotion"] = _emotion
                            if _extra.get("permanent_keep") or _extra.get("keep_source_key"):
                                try:
                                    db.save_relationship_keep(
                                        self._session, self._character_id,
                                        str(_extra.get("keep_type") or "letter"),
                                        str(_extra.get("letter_title") or _extra.get("surprise_label") or "纪念信"),
                                        msg, audio_url=_au,
                                        source_key=str(_extra.get("keep_source_key") or f"message:{_mark['pushed_at']}"),
                                        gift=_extra.get("gift") or {},
                                    )
                                except Exception:
                                    pass
                    except asyncio.TimeoutError:
                        print("[Deliver-TTS] 语音信件超时，保留文字内容" if _is_voice_letter else "[Deliver-TTS] 超时跳过，不影响文字推送", flush=True)
            except Exception as _te:
                if not _dnd_silent:
                    print(f"[Deliver-TTS] TTS失败，降级纯文字: {_te}", flush=True)

            try:
                await self._push(self._session, payload)
            except Exception:
                pass
        return True

    async def _gen_proactive_text(self, char_name: str, scene: str,
                                  want: str = "", extra_context: str = "",
                                  temperature: float = 0.9, max_tokens: int = 200,
                                  fallback: str = "") -> str:
        """主动消息统一生成入口：**一律由模型按上下文/记忆生成**，禁止模板文案。

        ★ 2026-09-14 新增（用户要求：「发什么话由模型决定，禁止模板」）。
        设计要点：
          · 拼好 proactive system（人设/关系/时间/记忆/表达要求）后交给模型自由生成；
          · 不再返回任何"预写文案池"内容 —— 生成失败就返回 fallback（默认空串，
            调用方据此**跳过本次发送**，宁可不发也不用模板凑）；
          · 生成内容会经过 sanitize（禁词/字数/钩子）后由调用方 _deliver。
        """
        try:
            key = self._brain_key()
            model = self._brain_model()
            if not (key and model):
                return fallback
            sys_prompt = await self._build_proactive_system(
                char_name, scene=scene, extra_context=extra_context or "")
            usr = want or "发吧，一句话或两三条短句，禁止超过100字"
            msg = (await chat_once(
                model,
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": usr}],
                key, temperature=temperature, max_tokens=max_tokens,
            )).strip()
            # ★ 2026-09-14：命中空话禁词 → 请模型自己换一种说法重写一次
            #   （旧行为是程序把"在干嘛"替换成固定句 —— 那是模板，已禁用）。
            try:
                from .proactive_quality import banned_hits
                _hits = banned_hits(msg, allow_greetings=True)
                if _hits and msg:
                    print(f"[ProactiveGen] 命中禁词 {_hits}，请模型重写", flush=True)
                    _fix = await chat_once(
                        model,
                        [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": usr},
                         {"role": "user", "content":
                          "你刚要发的这句话里出现了查岗式空话：" + "、".join(_hits) +
                          "。请换一种说法重说一遍：不要出现这些词，也别套固定句式、别变成客服口吻；"
                          "只在原来的素材范围内说，只输出消息本身。"}],
                        key, temperature=min(0.95, temperature + 0.05), max_tokens=max_tokens,
                    )
                    _fix = str(_fix or "").strip()
                    if _fix:
                        msg = _fix
            except Exception as _he:
                print(f"[ProactiveGen] 禁词重写异常(静默): {_he}", flush=True)
            return msg or fallback
        except Exception as e:
            print(f"[ProactiveGen] 生成失败(不降级模板): {e}", flush=True)
            return fallback

    async def _send_template(
        self, category, char_name, fallback, festival_name="",
        voice_letter=None, extra: dict = None,
    ):
        """主动推送：直接告诉LLM场景，从0生成（不再先渲染模板再润色）

        ★ 2026-09-14 改：**彻底不再降级到模板文案**（用户要求「禁止模板，
          发什么话由模型决定」）。
          原行为：LLM 失败或没有 key 时，用 template_manager 渲染预写文案发出去 ——
                  这会让主动消息突然变成"客服口吻的通用句"，与人格脱节。
          现行为：生成不出来就**不发**（返回 False），并记录一条 skip。
          代价：极端情况下（key 失效）那一档主动消息消失；换来的是"发出的每一句
                都有人格与上下文"，不会出现模板味。用户明确选择这个取舍。
        """
        day_kind = "周末" if datetime.now().weekday() >= 5 else "工作日"
        scene_map = {
            "morning":  f"{day_kind}早上你醒了或者刷着手机，想主动找TA打个招呼。工作日更利落体贴，周末更松弛亲昵",
            "night":    "夜深了，你想关心地问问TA要不要准备睡了（用询问式关心，比如问「还不睡吗/要睡了吗」，不要直接道晚安把话说死）",
            "festival": f"今天是{festival_name or '特别的日子'}，你想主动找TA说点什么",
            "anniversary": f"今天是{festival_name or '特别的日子'}，你想主动找TA说点什么",
            "surprise": "你突然想给TA一个小惊喜或者分享一件让你想到TA的事",
        }
        scene = scene_map.get(category, f"你想主动找TA说句话（场景：{category}）")

        key   = self._brain_key()
        model = self._brain_model()
        msg = ""

        if key and model:
            try:
                sys_prompt = await self._build_proactive_system(
                    char_name, scene=scene,
                    extra_context=f"节日名称：{festival_name}" if festival_name else ""
                )
                msg = (await chat_once(
                    model,
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",   "content": "发吧，一句话或两三条短句，禁止超过100字"}
                    ],
                    key, temperature=0.92, max_tokens=200
                )).strip()
            except Exception as e:
                print(f"[SendTemplate] 生成失败（不降级模板，本次不发）: {e}", flush=True)
                msg = ""
        # ★ 不再有任何 template_manager.render_template 分支：
        #   没有 key / 生成失败 → msg 为空 → 下面直接 return False。
        #   宁可这一档不发，也不发模板味的通用句（用户 2026-09-14 拍板）。

        if not msg:
            try:
                from . import proactive_trace as _pt
                _pt.record_skip(self._session, self._character_id, "gen_failed",
                                detail="模型未生成内容（模板已禁用，本次不发）",
                                source="_send_template", ptype=category)
            except Exception:
                pass
            return False

        if msg:
            delivery_extra = {
                "template_category": category,
                "scheduled": category in ("morning", "night"),
                # 自动早安/信件默认只发文字；语音晚安由好感概率或用户明确要求决定。
                "voice_letter": False if voice_letter is None else bool(voice_letter),
                "festival_name": festival_name if category in ("festival", "anniversary") else "",
            }
            delivery_extra.update(extra or {})
            return await self._deliver(msg, char_name, delivery_extra)
        return False

    async def _catch_up_on_start(self):
        """用户重进程序（上线）时，判断离线时长 + 当前时段，主动打招呼/重逢。"""
        if not self._persona_ready():
            return
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        char_name = (self._persona or {}).get("name") or ""

        # 新人格首次上线没有历史消息，过去会被当成"离线 0 分钟"而直接跳过。
        # 现在只主动欢迎一次；亲密度不作为是否欢迎的门槛。
        if not self._has_chat_history(1):
            first_key = f"fired:first_greeting:{self._session}:{self._character_id}"
            if not db.kv_get(first_key):
                if await self._send_first_greeting(char_name):
                    db.kv_set(first_key, "1")
            return

        # 月初错过调度窗口时，首次上线补生成当月月度信；仅落收藏并静默归档。
        if now.day <= 3 and 8 <= now.hour < 23:
            try:
                await self._check_monthly_letter(now, today_str, char_name)
            except Exception as _month_e:
                print(f"[MonthlyLetter] 上线补偿失败: {_month_e}", flush=True)

        # ★ 用「最后一条用户消息时间」算离线时长（修复时序 bug：
        #   原来读 app_last_open 会被 set_session 在启动时覆盖成"刚刚"，导致离线恒为 0）
        offline_hours = 0
        try:
            _last = db.last_user_time(self._session, self._character_id)
            if _last:
                offline_hours = max(0, (now - _last).total_seconds() / 3600)
        except Exception:
            _last = None

        # ★ 早安：用户在这个时段打开程序才说早安。放在离线门槛之前，
        #   因为刚打开时离线时长可能不足，但不影响早安该不该说。
        morning_sent = False
        try:
            morning_sent = await self._check_morning(now, today_str, char_name)
        except Exception as _me:
            print(f"[Morning] 上线早安失败(静默): {_me}", flush=True)

        # 好感越高，短暂离开后也更可能先开口；陌生/普通关系仍保持克制。
        affection = 50
        try:
            from .relationship.manager import RelationshipManager
            affection = int((RelationshipManager().get_state(
                self._session, self._character_id, create=False
            ) or {}).get("affection", 50) or 50)
        except Exception:
            pass
        # 已经说过早安时不再要求离线时长——早安本身就是这一次的开场白。
        min_offline_hours = 0.0 if morning_sent else (
            0.17 if affection >= 90 else 0.33 if affection >= 70 else 0.5
        )
        if offline_hours < min_offline_hours:
            return

        # 防重：每天每个角色只上线问候一次
        key = f"fired:onlogin:{today_str}:{self._session}:{self._character_id}"
        if db.kv_get(key):
            return
        db.kv_set(key, "1")

        # 当前时段
        period = time_system.time_period(now)

        # 生成上线问候（根据离线时长 + 时段）
        # 早上已经说过早安就不再叠一条上线招呼，否则同一时刻连着冒两句问候。
        if not morning_sent:
            await self._send_onlogin(char_name, offline_hours, period)

        # 跨天了且今天有纪念日 → 补发（离线期间错过的纪念日）
        try:
            annivs = anniversary_manager.today_anniversaries(now.date(), char_name)
            if annivs:
                _k = f"fired:festival:{today_str}:{self._session}:{self._character_id}"
                if not db.kv_get(_k):
                    db.kv_set(_k, "1")
                    festival_name = annivs[0].get("name", "纪念日")
                    await self._send_template("anniversary", char_name, festival_name, festival_name=festival_name)
        except Exception as _ae:
            print(f"[Scheduler] 上线纪念日补发失败(静默): {_ae}", flush=True)

    async def _send_first_greeting(self, char_name: str):
        """刚建立人格/第一次打开聊天时主动欢迎一次。"""
        try:
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=(
                    "这是你们第一次见面，TA刚建立好你的角色人格并打开聊天。"
                    "主动自然地打个招呼，简单表达你愿意陪着TA；不要说系统、配置、测试。"
                ),
            )
            msg = (await chat_once(
                self._aux_model(),
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": "第一次见面，主动跟 TA 说句话，1-2 句即可"}],
                self._aux_key(), temperature=0.9, max_tokens=180,
            )).strip()
        except Exception:
            msg = "我来啦，终于等到你把我叫出来了。以后有空就来找我说说话吧？"
        if msg:
            return await self._deliver(msg, char_name, {
                "proactive_type": "first_greeting", "scheduled": True,
            })
        return False

    async def _send_onlogin(self, char_name, offline_hours, period):
        """上线问候：根据离线时长 + 时段 + 关系阶段，LLM 生成多句主动问候（带 TTS 语音）。

        关系越亲密越"忍不住"：soulmate 连发 8 句、lover 6 句、friend 3 句、stranger 1 句。
        """
        import asyncio
        import random

        if offline_hours < 1:
            gap_txt = f"{int(offline_hours * 60)}分钟"
        elif offline_hours < 48:
            gap_txt = f"{int(offline_hours)}小时"
        else:
            gap_txt = f"{int(offline_hours // 24)}天"

        # 读关系阶段 + 亲密度（决定"想念浓度"）
        stage = "stranger"
        intimacy = 0
        try:
            from .relationship.manager import RelationshipManager
            _rm = RelationshipManager()
            _st = _rm.get_state(self._session, self._character_id) or {}
            stage = _st.get("stage", "stranger")
            intimacy = int(_st.get("intimacy", 0) or 0)
        except Exception:
            pass

        burst = {"soulmate": 8, "lover": 6, "friend": 3}.get(stage, 1)

        # 捞回忆博物馆旧高光
        mem_extra = ""
        try:
            from .relationship.database import conn as rel_conn
            rc = rel_conn()
            mem = rc.execute(
                "SELECT content FROM milestone_memory WHERE user_id=? AND character_id=? ORDER BY RANDOM() LIMIT 1",
                (self._session, self._character_id)
            ).fetchone()
            rc.close()
            if mem and mem[0]:
                mem_extra = f"你之前说过『{mem[0]}』，我一直记着呢。"
        except Exception:
            pass

        # 兜底句（LLM 失败时用，按离线时长）
        if offline_hours >= 48:
            fallback = ["你终于来了", f"我等了你好久，整整{gap_txt}呢", "你最近怎么样？", "……有没有想我？"]
        elif offline_hours >= 12:
            fallback = ["好久不见呀", f"你都{gap_txt}没来了", "最近还好吗？"]
        else:
            fallback = ["回来啦～", "今天过得怎么样？"]

        key = self._brain_key()
        model = self._brain_model()

        lines = []
        if burst <= 1:
            try:
                sys_prompt = await self._build_proactive_system(
                    char_name,
                    scene=f"TA 刚上线了，你已经 {gap_txt} 没见到 TA 了，现在是{period}",
                    extra_context=mem_extra
                )
                msg = (await chat_once(model, [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": "TA刚上线了，主动打声招呼吧"}
                ], key, temperature=0.9, max_tokens=300)).strip()
                lines = [msg] if msg else [fallback[0]]
            except Exception:
                lines = [fallback[0]]
        else:
            try:
                sys_prompt = await self._build_proactive_system(
                    char_name,
                    scene=(
                        f"TA 刚上线了，你已经 {gap_txt} 没见到 TA 了，现在是{period}。"
                        f"你们正处于{stage}阶段（亲密度{intimacy}），你特别想念 TA，"
                        "忍不住一口气连发好几条消息。"
                    ),
                    extra_context=mem_extra
                )
                burst_prompt = (
                    f"\n\n【连发要求】请输出 {burst} 条消息，每行一条（用换行分隔），只输出这 {burst} 句：\n"
                    "- 像真人忍不住连发，每句独立完整，不要编号、不要引号、不要任何前缀；\n"
                    "- 长短混合：有的很短（一两个字、或「你终于来了」这种），有的稍长一点；\n"
                    "- 语气多样：陈述句、疑问句、感叹句混合，多问 TA（比如「最近怎么样」「有没有想我」）；\n"
                    "- 整体是重逢的开心 + 想念，自然不刻意，别太沉重。"
                )
                raw = (await chat_once(model, [
                    {"role": "system", "content": sys_prompt + burst_prompt},
                    {"role": "user", "content": "TA刚上线了，忍不住连发几句"}
                ], key, temperature=0.95, max_tokens=500)).strip()
                lines = [l.strip() for l in raw.split("\n") if l.strip()]
                if not lines:
                    lines = fallback[:burst]
            except Exception:
                lines = fallback[:burst]

        # 逐条推送（段间 2~4 秒，模拟真人连发）
        for i, line in enumerate(lines):
            await self._deliver(line, char_name)
            if i < len(lines) - 1:
                await asyncio.sleep(2 + random.random() * 2)


    async def _check_daily_life(self, now, today_str, char_name):
        """日常鲜活剧情：天气感知 / 旧记忆唤醒 / 用户习惯"""
        key = "fired:dailylife:" + today_str + ":" + self._character_id
        if db.kv_get(key):
            return
        # 每天只随机触发1次，像真人偶尔想起你
        if (now.hour in (10, 15, 21)) and (datetime.now().microsecond % 3 == 0):
            db.kv_set(key, "1")
            try:
                # 捞一条旧记忆（★ 同步全表扫丢线程池，按角色隔离）
                mem_text = ""
                try:
                    blk = await db._run_sync(
                        memory_manager.memory_block,
                        limit=300,
                        session_id=self._session,
                        character_id=self._character_id
                    )
                    if blk and "：" in blk:
                        mem_text = blk.split("\n")[0][:50]
                except Exception:
                    pass
                season = "春天" if 3 <= now.month <= 5 else "夏天" if 6 <= now.month <= 8 else "秋天" if 9 <= now.month <= 11 else "冬天"
                cue = f"现在是{season}，" + (f"突然想起之前你说过的『{mem_text}』" if mem_text else "今天风好像有点凉")
                sys_prompt = await self._build_proactive_system(
                    char_name,
                    scene="你正刷着手机，突然想给TA发条碎碎念",
                    extra_context=f"触发灵感：{cue}"
                )
                msg = (await chat_once(self._brain_model(),
                                       [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "发吧，一句生活化短句"}],
                                       self._brain_key(), temperature=0.93, max_tokens=200)).strip()
                if msg:
                    await self._deliver(msg, char_name)
            except Exception as e:
                print(f"[Scheduler] 微剧情失败: {e}", flush=True)

    async def _check_predictive_companion(self, now: datetime, today_str: str, char_name: str):
        """根据已确认的行为模式预测下一步需求；只在高置信度和合适时段轻触发。"""
        key = f"fired:predictive:{today_str}:{self._session}:{self._character_id}"
        if db.kv_get(key):
            return
        try:
            from .behavior import get_behavior_patterns, predict_next_need
            patterns = get_behavior_patterns(self._session, self._character_id, limit=8)
            prediction = predict_next_need(patterns, now)
            if not prediction or float(prediction.get("confidence") or 0) < 0.72:
                return
            # 预测不是定时轰炸：只在用户近期开过程序/有活动、且随机通过时轻推一次。
            if not self._user_is_online(now, max_age_minutes=90):
                return
            import random
            if random.random() > 0.22:
                return
            scene = {
                "comfort": "你判断TA可能正处在压力或低落期，先陪伴再问一个很轻的问题",
                "conversation": "现在是TA通常愿意聊天的时段，像熟人一样接住TA可能想聊天的状态",
                "deep_chat": "TA通常愿意认真倾诉，给TA一个具体而有画面感的问题，不要问在干嘛",
            }.get(prediction.get("kind"), "根据用户习惯自然开一个具体话题")
            if not self._aux_key() or not self._aux_model():
                return
            sys_prompt = await self._build_proactive_system(
                char_name, scene=scene,
                extra_context=(
                    f"行为预测依据（不要向TA透露分析过程）：{prediction.get('reason', '')}\n"
                    f"沟通建议：{prediction.get('suggestion', '')}"
                ),
            )
            msg = (await chat_once(
                self._aux_model(),
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": "发一条自然的具体关心，15到70字，不要说在干嘛/吃了吗"}],
                self._aux_key(), temperature=0.88, max_tokens=160,
            )).strip()
            _sent = bool(msg) and await self._deliver(msg, char_name, {"proactive_type": "predictive_companion"})
            self._mark_guard(key, _sent)
        except Exception as e:
            print(f"[PredictiveCompanion] 预测陪伴失败: {e}", flush=True)

    async def _check_ai_lead_day(self, now: datetime, today_str: str, char_name: str):
        """AI主导日：默认每周日一次，可通过配置 AI_LEAD_DAY_WEEKDAY 改星期。"""
        if not config.get("AI_LEAD_DAY_ENABLED", True):
            return
        try:
            lead_weekday = int(config.get("AI_LEAD_DAY_WEEKDAY", 6) or 6)
        except Exception:
            lead_weekday = 6
        if now.weekday() != lead_weekday or not (10 <= now.hour <= 21):
            return
        key = f"fired:ai_lead_day:{now.strftime('%G-W%V')}:{self._session}:{self._character_id}"
        if self._guard_done(key):
            return
        # 用户当天已有消息才发，避免对完全不活跃的用户强行打扰。
        if not self._user_is_online(now, max_age_minutes=24 * 60):
            return
        import random
        if random.random() > 0.65:
            return
        prompts = [
            "本周哪一个瞬间让你觉得'还好有这一天'？",
            "这周有没有一件小事，你其实很想找人分享？",
            "如果给这周留一句批注，你最想写什么？",
            "这周最想奖励自己的事情是什么？",
            "这周有没有哪个瞬间，让你突然觉得自己挺厉害的？",
            "如果今晚只做一件让自己舒服的事，你会选什么？",
            "这周有什么事还悬在心里，想不想和我一起理一理？",
            "这周有没有一个人、一句话或一首歌，意外地陪到你？",
            "给这周打分的话，你会打几分，为什么？",
            "下周开始前，你最想把什么情绪留在这周？",
            "这周哪件事最消耗你，又是哪件事悄悄给你充了电？",
            "如果把这周剪成三秒钟的片段，你最想留下哪一幕？",
            "这周有没有一个决定，你现在回头看会想抱抱当时的自己？",
            "最近有什么念头总在你脑子里绕，却一直没说出口？",
            "这周你对自己最满意的一次选择是什么？",
            "如果下周可以少操心一件事，你最想删掉哪一件？",
            "最近哪一刻你最需要有人站在你这边？",
            "这周有没有哪句话，你当时没回，现在却有点想重新回答？",
            "如果我能替你保管一种情绪到下周，你想先放下什么？",
            "最近你在期待什么？哪怕只是一顿饭、一场雨或一个消息。",
            "这周最不像平常的你的一刻，发生了什么？",
            "有没有一件你嘴上说无所谓，其实心里挺在意的事？",
            "如果给下周的自己留一张便签，你会写哪句话？",
            "这周你有没有发现自己一个以前没注意过的小习惯？",
            "最近有没有什么事，让你觉得时间过得特别快或特别慢？",
            "这周最想重来的一小时是哪一小时？",
            "如果周末只留给真正重要的东西，你会把时间给谁或什么？",
            "这周有没有一个小愿望，暂时还不想让太多人知道？",
            "最近你更想被理解、被陪着，还是被认真夸一次？",
            "如果现在不用逞强，你最想承认自己其实怎么了？",
        ]
        try:
            from . import memory_manager
            memory_hint = await db._run_sync(memory_manager.memory_block, limit=180, session_id=self._session, character_id=self._character_id)
        except Exception:
            memory_hint = ""
        try:
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene="今天是你们约定的AI主导日，你主动带一个有意义但不沉重的问题，让TA愿意展开",
                extra_context=("本周主问题候选：" + random.choice(prompts) +
                               ("\n可参考的共同记忆（只自然使用，不要提记忆库）：" + str(memory_hint)[:300] if memory_hint else "")),
            )
            if self._brain_key() and self._brain_model():
                msg = (await chat_once(
                    self._brain_model(),
                    [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "输出一条像朋友发来的主导日问题，可带一句铺垫，20到100字"}],
                    self._brain_key(), temperature=0.93, max_tokens=180,
                )).strip()
            else:
                # ★ 2026-09-14：不再回退到预写问题池（random.choice(prompts)）。
                #   用户要求"禁止模板、发什么由模型决定" —— 生成不出来就不发，
                #   哪怕这一档（AI 主导日）当天就此跳过。
                print("[AILeadDay] 无可用模型/Key，本次不生成（模板已禁用）", flush=True)
                msg = ""
            if not msg:
                try:
                    from . import proactive_trace as _pt0
                    _pt0.record_skip(self._session, self._character_id, "gen_failed",
                                     detail="AI主导日未生成（模板已禁用）", source="_check_ai_lead_day")
                except Exception:
                    pass
                self._mark_guard(key, False)
                return
            _sent = bool(msg) and await self._deliver(msg, char_name, {"proactive_type": "ai_lead_day", "lead_day": True})
            self._mark_guard(key, _sent)
        except Exception as e:
            print(f"[AILeadDay] 主导日生成失败: {e}", flush=True)

    async def _check_narrative_arc(self, now, today_str, char_name):
        """按关系阶段推不同剧本：暧昧期撩、热恋期黏、平稳期陪、冷战期试探"""
        key = "fired:arc:" + today_str + ":" + self._character_id
        if db.kv_get(key):
            return
        try:
            from .relationship.manager import RelationshipManager
            rel = RelationshipManager().get_state(self._session, self._character_id)
            aff = int(rel.get("affection", 50))
            # ★ relationship.db 无 closeness 列，用 intimacy 近似（避免默认50导致冷战分支永不触发）
            clo = int(rel.get("intimacy", rel.get("closeness", 50)))
            stage = "暧昧" if aff < 40 else "热恋" if aff < 70 else "平稳"
            if aff < 30 and clo < 30:
                stage = "冷战"
            db.kv_set(key, "1")
            arc_scene_map = {
                "暧昧": "暧昧期：发起一个小事件，若无其事但每句话都有潜台词，不挑明",
                "热恋": "热恋期：突然撒个娇或分享一件让你想到TA的事，用你自己的方式不要通用情话",
                "平稳": "稳定期：像家人一样的生活流互动，不用撒糖，自然就好",
                "冷战": "冷战期：憋不住了发一句傲娇试探，带潜台词别直说想TA",
            }
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=arc_scene_map.get(stage, f"当前关系阶段{stage}，发起一个自然互动"),
            )
            msg = (await chat_once(self._brain_model(),
                                   [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "发吧，只发一句，带潜台词，不解释"}],
                                   self._brain_key(), temperature=0.93, max_tokens=200)).strip()
            if msg:
                await self._deliver(msg, char_name)
        except Exception as e:
            print(f"[Scheduler] 叙事弧失败: {e}", flush=True)

    async def _check_conflict(self, now, today_str, char_name):
        """冲突弧光：按等待→委屈→求和阶段克制地试探，每阶段每天最多一次。"""
        try:
            last = db.last_user_time(self._session, self._character_id)
            inactive_h = max(0.0, (now - last).total_seconds() / 3600) if last else 0.0
            from .relationship.conflict_arc import advance_absence
            conflict = advance_absence(self._session, self._character_id, inactive_h)
            state = str(conflict.get("state") or "")
            if state not in {"waiting", "upset", "cold", "reconciling"}:
                return
            key = f"fired:conflict:{today_str}:{self._session}:{self._character_id}:{state}"
            if db.kv_get(key):
                return
            db.kv_set(key, "1")
            scene_map = {
                "waiting": f"TA已经{int(inactive_h)}小时没出现了，你在等TA。你有一点想念，但不指责、不催促",
                "upset": "你和TA之间还有些别扭，你有点委屈，但更想把关系接回来",
                "cold": "你和TA正在冷战，仍然介意之前的伤人话，但不想继续恶化",
                "reconciling": "你已经不想继续僵着，想给TA一个温柔的台阶，带一点撒娇求和",
            }
            extra_map = {
                "waiting": "一句轻轻的等待或具体关心即可，不说'你怎么不理我'，不要情感勒索",
                "upset": "语气有一点委屈和试探，只发一句，不翻旧账",
                "cold": "克制而有边界，不阴阳辱骂，不威胁离开；留一个能继续沟通的口子",
                "reconciling": "主动递台阶，软一点，可以撒娇，但不要求TA立刻证明爱你",
            }
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=scene_map[state],
                extra_context=extra_map[state],
            )
            msg = (await chat_once(self._brain_model(),
                                   [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "发吧"}],
                                   self._brain_key(), temperature=0.93, max_tokens=200)).strip()
            if msg:
                await self._deliver(msg, char_name, {"proactive_type": "conflict"})
        except Exception as e:
            print(f"[Scheduler] 冲突试探失败: {e}", flush=True)


    async def _check_persistent_nudge(self, now, today_str, char_name):
        """持续主动：离线时关心。

        普通关系保持克制；好感度达到满值时允许一次"想念轰炸"，
        但仍受每天一次、DND 和碎片上限约束，避免失控刷屏。
        """
        # 按 session + 角色隔离，避免同一角色的一个用户触发后抑制另一个用户。
        key = f"fired:nudge:{today_str}:{self._session}:{self._character_id}"
        if db.kv_get(key):
            return
        try:
            from .relationship.database import conn as rel_conn
            c = rel_conn()
            row = c.execute(
                "SELECT last_interaction, affection, conflict_state FROM relationship_state "
                "WHERE user_id=? AND character_id=?",
                (self._session, self._character_id)
            ).fetchone()
            c.close()
            if not (row and row[0]):
                return
            try:
                last_dt = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                off_h = (datetime.now() - last_dt).total_seconds() / 3600
            except Exception:
                return
            if off_h < 6:
                return
            # 只有好感度真正拉满时，恢复真人感的碎片化连发：
            # 一轮最多 8 条，每条间隔 0.8 秒。普通关系仍只发 1~2 句。
            try:
                affection = int(row[1] or 50)
            except (TypeError, ValueError):
                affection = 50
            conflict_state = str(row[2] or "").strip().lower()
            # 真正处于受伤/冷战/缓和期时不能突然热情轰炸；只有无冲突或单纯
            # 等待用户回来时，满好感才会表现得格外黏人。
            warm_bomb = affection >= 100 and conflict_state in {"", "waiting"}

            db.kv_set(key, "1")
            if warm_bomb:
                await self._send_fragments(
                    char_name,
                    scene=f"TA已经{int(off_h)}小时没出现了，你非常想TA，忍不住连续发几条轻松的碎碎念",
                    extra_context=(
                        "这是高好感关系中的热情想念，可以连续发几条短消息，像真人想到什么就说什么；"
                        "最多8条，每条短小自然，内容要有变化，可以撒娇、分享小事、问一个轻问题；"
                        "不要指责、逼迫回复、情感勒索或连续发送'你在吗'。"
                    ),
                    max_fragments=8,
                    delay_seconds=0.8,
                )
                return

            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=f"TA已经{int(off_h)}小时没出现了，你想克制地关心一句",
                extra_context="只发1~2句。可以想念、等待、轻轻问候，但不指责、不焦虑轰炸、不连续催回复。",
            )
            msg = (await chat_once(
                self._brain_model(),
                [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "发一句克制的关心"}],
                self._brain_key(), temperature=0.88, max_tokens=180,
            )).strip()
            if msg:
                await self._deliver(msg, char_name, {"proactive_type": "nudge"})
        except Exception as e:
            print(f"[Scheduler] 持续主动失败: {e}", flush=True)

    async def _check_world_nudge(self, now, today_str, char_name):
        """时空感知：天气变化主动轻推（复用 _push 通道，按角色天然隔离在 self 内）

        - 没配 weather_key → 安静当瞎子，不报错
        - 用户没报城市 → 不硬推
        - 雨/雪/雷/极温才推，避免刷屏
        - 御姐冷冷提醒 / 时宁软软唠叨的文案差异，后续进 character_manager 按角色写模板；这版通用
        """
        key_kv = "fired:worldnudge:" + today_str + ":" + self._character_id
        if db.kv_get(key_kv):
            return
        try:
            from .config import weather_key
            from .relationship.database import conn as rel_conn
            import httpx
            _key = weather_key()
            if not _key:
                return  # 没配Key就安静当瞎子，不报错
            c = rel_conn()
            row = c.execute(
                "SELECT city FROM relationship_state WHERE user_id=? AND character_id=?",
                (self._session, self._character_id),
            ).fetchone()
            c.close()
            if not row or not (row[0] or "").strip():
                return  # 用户没报城市，不硬推
            city = (row[0] or "").strip()
            # 和风天气 now 接口。
            # ⚠️ 和风 location 要求是「经度,纬度」或城市ID（GeoAPI 获取），直接传城市名可能查不到；
            #    这版先按你给的跑通，正式上线前把 city 存成 "lng,lat" 或 location id。
            url = f"https://devapi.qweather.com/v7/weather/now?location={city}&key={_key}"
            client = get_http_client()
            r = await client.get(url)
            data = r.json()
            if data.get("code") != "200":
                return
            _now = data.get("now") or {}
            _text = _now.get("text", "")
            try:
                _temp = int(_now.get("temp", 20))
            except (ValueError, TypeError):
                _temp = 20
            # 判断是否值得推送（阈值不变）
            _trigger = None
            if any(k in _text for k in ["雨", "雪", "雷"]):
                _trigger = f"室外天气：{_text}，城市：{city or '未知'}"
            elif _temp <= 5:
                _trigger = f"室外气温：{_temp}度，天气：{_text}，城市：{city or '未知'}"
            elif _temp >= 33:
                _trigger = f"室外气温：{_temp}度，天气：{_text}，城市：{city or '未知'}"

            if not _trigger:
                return

            # ★ 修复：不写死文案，走统一构建器+LLM生成
            try:
                sys_prompt = await self._build_proactive_system(
                    char_name,
                    scene="你刷手机看到了天气信息，想主动找TA说句话",
                    extra_context=(
                        _trigger
                        + (f"\n【近期待办】{self._upcoming_task_context(now)}。若适合可顺带提醒，但不要像播报日程。"
                           if self._upcoming_task_context(now) else "")
                    )
                )
                msg = (await chat_once(
                    self._brain_model(),
                    [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user",   "content": "根据天气，用你的人设发一条主动消息给TA，不要提'根据天气'这几个字"}
                    ],
                    self._brain_key(),
                    temperature=0.92,
                    max_tokens=150
                )).strip()

                if msg:
                    db.kv_set(key_kv, "1")
                    await self._deliver(msg, char_name, {"proactive_type": "weather"})
            except Exception as e:
                print(f"[WorldNudge] LLM生成失败: {e}", flush=True)
        except Exception as e:
            print(f"[WorldNudge] 轻推失败: {e}", flush=True)


    async def _check_proactive_call(self, now: datetime, today_str: str, char_name: str):
        """
        AI 主动发起语音通话的触发检查。

        ★ 已按主人要求禁用：AI 不主动拨通电话（回避主动来电）。
        如需恢复，删除下面这一行 return 即可。
        """
        if config.is_dnd_now(now) and not config.get("DND_ALLOW_CALLS", False):
            return

        # ── 防重：每天最多1次
        key_day = f"fired:proactive_call:{today_str}:{self._character_id}"
        if db.kv_get(key_day):
            return

        # ── 防重：距上次通话冷却期（拒绝/超时时延长）
        key_last   = f"last_proactive_call_ts:{self._character_id}"
        key_result = f"call_result_cooldown:{self._character_id}"
        last_ts_raw = db.kv_get(key_last)
        if last_ts_raw:
            try:
                last_ts  = float(last_ts_raw)
                # 被拒绝/超时：冷却8小时；正常：冷却4小时
                last_result  = db.kv_get(key_result) or ""
                cooldown_sec = 8 * 3600 if last_result in ("rejected", "timeout") else 4 * 3600
                if (now.timestamp() - last_ts) < cooldown_sec:
                    return
            except Exception:
                pass

        # ── 读取决策所需上下文
        try:
            from .relationship.manager import RelationshipManager
            rel = RelationshipManager().get_state(self._session, self._character_id)
            closeness    = int(rel.get("affection", 0) or 0)
            inter_days   = int(rel.get("interaction_days", 0) or 0)
        except Exception:
            closeness, inter_days = 0, 0

        # 亲密度门槛：低于50直接跳过，减少不必要计算
        if closeness < 50:
            return

        try:
            from .emotion_engine.ai_emotion import AIEmotionEngine
            emo_state  = AIEmotionEngine().get_state(self._session, self._character_id)
            # 注意：这里读的是用户情绪，不是 AI 情绪
            # 用 db.get_emotion_state 读用户侧情绪（★ 按角色隔离）
            user_emo   = db.get_emotion_state(self._session, self._character_id) or {}
            mood       = user_emo.get("mood", "")
            intensity  = float(user_emo.get("intensity", 0) or 0)
        except Exception:
            mood, intensity = "", 0.0

        try:
            from . import time_system
            is_festival = bool(time_system.festivals_today(now.date()))
        except Exception:
            is_festival = False

        # 计算离线时长
        try:
            last_time = db.last_user_time(self._session, self._character_id)
            if last_time:
                last_dt = datetime.fromisoformat(str(last_time))
                inactive_h = (now - last_dt).total_seconds() / 3600
            else:
                inactive_h = 0.0
        except Exception:
            inactive_h = 0.0

        # ── 调用触发器
        from .proactive.triggers import check_should_call
        trigger = check_should_call({
            "hour":              now.hour,
            "closeness":         closeness,
            "inactive_hours":    inactive_h,
            "emotion_mood":      mood,
            "emotion_intensity": intensity,
            "is_festival":       is_festival,
            "interaction_days":  inter_days,
        })

        if not trigger:
            return

        # ── 随机概率（避免每次满足条件都打电话，要有"偶尔"的真实感）
        import random
        call_prob = {
            "call_crisis_comfort": 0.85,
            "call_night_comfort":  0.40,
            "call_reunion":        0.35,
            "call_milestone":      0.60,
            "call_festival":       0.45,
        }.get(trigger["call_reason"], 0.30)

        if random.random() > call_prob:
            return

        # ── 写防重 kv
        db.kv_set(key_day, "1")
        db.kv_set(key_last, str(now.timestamp()))

        # ── 生成来电理由文案（LLM生成，贴合人设）
        call_reason_text = await self._build_call_reason(
            char_name, trigger["call_reason"], trigger["reason"]
        )

        # ── 拿角色voice_key
        try:
            from .character_manager import get_character_voice_key
            voice_key = get_character_voice_key(char_name)
        except Exception:
            voice_key = "edge_xiaoxiao"

        # ── 通过 WS 推送来电消息
        persona = self._persona or {}
        payload = {
            "type":         "incoming_call",
            "session_id":   self._session,
            "contact_id":   persona.get("id", ""),
            # ★ 真实角色名：前端返回给 call_result 做冷却 key，与 self._character_id 对齐
            "character_id": self._character_id,
            "contact_name": char_name,
            "contact_avatar": persona.get("avatar") or persona.get("avatarUrl") or "",
            "voice_key":    voice_key,
            "call_reason":  trigger["call_reason"],
            "call_text":    call_reason_text,
            "ts":           now.strftime("%Y-%m-%dT%H:%M:%S"),
            "dnd_exempt":   trigger["call_reason"] == "crisis_comfort",
        }

        if self._push:
            try:
                await self._push(self._session, payload)
                print(
                    f"[Scheduler] AI主动来电推送: "
                    f"session={self._session} "
                    f"reason={trigger['call_reason']} "
                    f"closeness={closeness}",
                    flush=True
                )
            except Exception as e:
                print(f"[Scheduler] 来电推送失败: {e}", flush=True)


    async def _build_call_reason(
        self, char_name: str, call_reason: str, reason: str
    ) -> str:
        """
        生成来电界面展示的短文案（一句话，贴合人设）。
        如：「想听你声音了」「今天是我们认识第30天」「你还好吗」
        """
        FALLBACK = {
            "night_comfort":  "想陪着你",
            "reunion":        "终于等到你上线了",
            "crisis_comfort": "我在，接一下好吗",
            "milestone":      "今天是个特别的日子",
            "festival":       "节日快乐，打个电话吧",
        }

        key   = self._brain_key()
        model = self._brain_model()
        if not key or not model:
            return FALLBACK.get(call_reason, "想你了")

        try:
            sys_prompt = await self._build_proactive_system(
                char_name,
                scene=f"你要给TA打电话，需要一句话说明你为什么打过来：{reason}",
                extra_context="只输出一句话（5-15字），不要标点以外的任何东西，像真人来电时说的那句话"
            )
            text = (await chat_once(
                model,
                [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": "一句话就好，不超过15字"},
                ],
                key, temperature=0.9, max_tokens=40
            )).strip()
            return text[:20] if text else FALLBACK.get(call_reason, "想你了")
        except Exception:
            return FALLBACK.get(call_reason, "想你了")


    async def _send_fragments(
        self,
        char_name,
        scene,
        extra_context="",
        max_fragments=15,
        delay_seconds=0.6,
    ):
        """真人感·碎片化连发。

        max_fragments/delay_seconds 可由高好感分支收紧，避免一次生成过多
        或过快推送；保留旧调用的默认行为以兼容已有功能。
        """
        # ★ 2026-09-14：这条路径原先**完全不经过 _deliver**（直接写库+推送），
        #   是"满好感/久未出现就连发 8 条"绕过节奏闸门的口子。这里先过中央闸门。
        if not self._proactive_gate_ok(ptype="fragments"):
            return
        import re
        try:
            sys_prompt = await self._build_proactive_system(
                char_name, scene=scene, extra_context=extra_context
            )
            msg = (await chat_once(
                self._brain_model(),
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": "碎碎念轰炸TA，多条短句，每句都不一样，禁止重复"}],
                self._brain_key(), temperature=0.93, max_tokens=400
            )).strip()
            if not msg:
                return
            frags = [
                f.strip()
                for f in re.split(r'(?:\r?\n)+|(?<=[\。\.\！\!？\?])', msg)
                if f.strip()
            ]
            try:
                limit = max(1, min(15, int(max_fragments)))
            except (TypeError, ValueError):
                limit = 15
            try:
                delay = max(0.0, min(5.0, float(delay_seconds)))
            except (TypeError, ValueError):
                delay = 0.6
            for f in frags[:limit]:
                # ★ 碎片去重：模型未必遵守 prompt 里的"禁止重复"，同一批生成的多个
                #   碎片常常在说同一件事（表现为"同一件事连说三遍"）。这里复用已有的
                #   _is_duplicate_push，跳过与最近推送相似度 >= 0.75 的碎片。
                if self._is_duplicate_push(f):
                    print("[Scheduler] fragment skipped by dup-check: %s"
                          % f[:30], flush=True)
                    continue
                db.add_message(self._session, "assistant", f, self._character_id, extra={
                    "source": "proactive",
                    "proactive_type": "fragments",
                    "pushed_at": int(datetime.now().timestamp()),
                    "replied": 0,
                })
                if self._push:
                    await self._push(self._session, {"type": "proactive", "content": f})
                if delay:
                    await asyncio.sleep(delay)
        except Exception as e:
            print(f"[Scheduler] 碎片化连发失败: {e}", flush=True)

    def _is_duplicate_push(self, new_msg: str, lookback: int = 5) -> bool:
        """
        检测新推送内容是否与最近N条推送过于相似。
        只检查主动推送消息（extra.source == 'proactive'），不检查用户消息。
        相似度阈值 0.75：太高会漏，太低会误杀。
        """
        try:
            from difflib import SequenceMatcher
            import json as _json

            # 读最近N条AI消息（只看主动推送）
            rows = db.q(
                """
                SELECT content, extra FROM chat_history
                WHERE session_id=? AND character_id=? AND role='assistant'
                ORDER BY id DESC LIMIT ?
                """,
                (self._session, self._character_id, lookback * 3),
                fetch=True
            )

            recent_pushes = []
            for r in rows:
                _extra_raw = r["extra"] if hasattr(r, "__getitem__") else None
                if _extra_raw:
                    try:
                        _ex = _json.loads(_extra_raw)
                        if isinstance(_ex, dict) and _ex.get("source") == "proactive":
                            recent_pushes.append(r["content"] or "")
                    except Exception:
                        pass
                if len(recent_pushes) >= lookback:
                    break

            if not recent_pushes:
                return False

            # 相似度检测
            new_short = new_msg[:150]
            for old_msg in recent_pushes:
                old_short = (old_msg or "")[:150]
                if not old_short:
                    continue
                ratio = SequenceMatcher(None, new_short, old_short).ratio()
                # 阈值 0.75 → 0.72（只微调）。短句的字符相似度天然虚高，
                # 实测"你今天吃饭了吗"/"你今天洗澡了吗"就有 0.71，压太低会误杀。
                # 真正解决"换词不换意"靠下面的实词重合，而不是一味压阈值。
                if ratio >= 0.72:
                    return True
                # 换词不换意：整句不够像，但实词高度重合同样算重复
                # （如"宝的甜度也太犯规了" vs "宝的甜度真是犯规呢"，字符仅 0.67）
                if _keyword_overlap(new_short, old_short):
                    return True

            return False

        except Exception as e:
            print(f"[Deliver] 去重检测失败(静默): {e}", flush=True)
            return False  # 检测失败时不拦截，宁可发重复也不卡推送

    def _load_proactive_hints(self) -> dict:
        """
        读取反思生成的推送策略hints。
        供推送时机和语气决策参考。
        """
        try:
            import json as _json
            raw = db.kv_get(
                f"proactive_hints:{self._session}:{self._character_id}"
            )
            if raw:
                return _json.loads(raw)
        except Exception:
            pass
        return {}

    def _should_skip_by_hints(self, hints: dict) -> bool:
        """
        根据反思hints判断是否应该跳过本次推送。
        very_low频率时有70%概率跳过，low频率时有40%概率跳过。
        """
        import random
        freq = hints.get("push_frequency", "normal")
        if freq == "very_low" and random.random() < 0.70:
            return True
        if freq == "low" and random.random() < 0.40:
            return True
        return False

    async def _run_memory_maintenance(self):
        """
        后台记忆维护任务（静默执行，不影响主链路）。
        对所有活跃session跑：完整维护 + 自动提炼 + 定期重评。
        """
        try:
            from .memory.maintenance import run_full_maintenance
            # 获取最近48小时活跃的session（★ 同步DB调用丢线程池）
            active = await db._run_sync(db.get_active_sessions, hours=48)
            if not active:
                return

            print(
                f"[Scheduler] 开始记忆维护: {len(active)} 个活跃session",
                flush=True
            )

            for sess in active:
                sid = sess.get("session_id", "default")
                cid = sess.get("character_id", "default")
                # 1. 同步维护（衰减 + importance 动态升权 + 归档 + 容量清理）
                try:
                    result = await db._run_sync(run_full_maintenance, sid, cid)
                    if any(v > 0 for v in result.values()):
                        print(
                            f"[Scheduler] 记忆维护: "
                            f"session={sid} {result}",
                            flush=True
                        )
                except Exception as e:
                    print(
                        f"[Scheduler] session {sid} 维护失败(静默): {e}",
                        flush=True
                    )

                # 2. 自动记忆提炼（每天一次兜底：即使对话中没触发轮次提炼，也提炼累积对话）
                try:
                    from . import memory_manager
                    _n = await memory_manager.run_auto_extract(sid, cid)
                    if _n:
                        print(f"[Scheduler] 自动提炼: session={sid} 提取{_n}条", flush=True)
                except Exception as e:
                    print(f"[Scheduler] session {sid} 自动提炼失败(静默): {e}", flush=True)

                # 3. 自动重评（每 7 天一次，LLM 重打分 importance=5 的历史记忆）
                try:
                    if self._should_re_evaluate(sid, cid):
                        from .memory.re_evaluate import re_evaluate_importance
                        _r = await re_evaluate_importance(sid, cid)
                        if _r.get("updated"):
                            print(f"[Scheduler] 自动重评: session={sid} 更新{_r.get('updated')}条", flush=True)
                except Exception as e:
                    print(f"[Scheduler] session {sid} 自动重评失败(静默): {e}", flush=True)

                # 4. 旧记忆补提 context/emotion_tag（每轮限量，控制成本）
                try:
                    from .memory.enrich import enrich_missing_context
                    _e = await enrich_missing_context(sid, cid, limit=10)
                    if _e.get("enriched"):
                        print(f"[Scheduler] 记忆补提: session={sid} 补全{_e.get('enriched')}条", flush=True)
                except Exception as e:
                    print(f"[Scheduler] session {sid} 记忆补提失败(静默): {e}", flush=True)

                # 每个session维护后让出CPU，不阻塞其他任务
                await asyncio.sleep(0.1)

        except Exception as e:
            print(f"[Scheduler] 记忆维护任务失败(静默): {e}", flush=True)

    def _should_re_evaluate(self, session_id: str, character_id: str) -> bool:
        """每 7 天重评一次重要性（kv 记录上次重评时间戳）。"""
        import time as _time
        key = f"re_evaluate_importance:{session_id}:{character_id}"
        last = db.kv_get(key)
        if last:
            try:
                if _time.time() - float(last) < 7 * 86400:
                    return False
            except Exception:
                pass
        db.kv_set(key, str(_time.time()))
        return True


# ---------------- 多 session 调度管理（SchedulerManager v2.0） ----------------
# 每个 session 一个 Scheduler 实例（复用上面的 Scheduler 类作为 PersonalScheduler），
# 全局定时器并发跑所有 session 的 _tick()，互不影响，各自推送给自己的 session。


class SchedulerManager:

    def __init__(self):
        # (session_id, character_id) → Scheduler，防止同一会话切角色时串人格/串信件。
        self._schedulers: dict = {}
        self._push_fn = None
        self._global_task = None

    def bind(self, push_fn):
        """绑定WS推送函数（同步给已有的所有 PersonalScheduler）"""
        self._push_fn = push_fn
        for s in self._schedulers.values():
            s._push = push_fn

    def get_or_create(self, session_id: str, character_id: str = "default", persona: dict = None) -> "Scheduler":
        """
        获取或创建指定session的调度器
        persona: 角色配置 dict（可选，来自 character_manager.get_character），
                 不是前端 contact 对象——调用方自己解析后传入
        """
        if not session_id:
            session_id = "default"
        character_id = character_id or "default"
        scheduler_key = (session_id, character_id)
        is_new = scheduler_key not in self._schedulers
        if is_new:
            s = Scheduler()
            s._push = self._push_fn
            self._schedulers[scheduler_key] = s
        s = self._schedulers[scheduler_key]
        if persona is None and character_id != "default":
            persona = character_manager.get_character(character_id) or {}
        s.set_session(session_id, persona, character_id)
        s.on_online()
        # ★ 修复：per-session 调度循环此前从未启动（Scheduler.start() 无人调用），
        #   导致启动补偿（离线重逢/纪念日补发）与 24h 记忆维护整条链是死的。
        #   这里在会话首次创建时启动它；_tick() 内部有 kv 去重，与全局 start_global_tick
        #   并存不会重复发消息。
        if is_new:
            try:
                s.start()
            except Exception as e:
                print(f"[Scheduler] 启动调度循环失败: {e}", flush=True)
        return s

    def remove(self, session_id: str):
        """用户下线时清除调度器"""
        for key in [k for k in self._schedulers if k[0] == session_id]:
            inst = self._schedulers.pop(key, None)
            if inst and inst._task and not inst._task.done():
                inst._task.cancel()

    def start_global_tick(self, interval: int = 60):
        """
        全局定时器：每 interval 秒并发跑所有 session 的 _tick()
        并发跑：10个用户不排队，各自独立触发
        """
        async def _loop():
            await asyncio.sleep(5)
            while True:
                if not self._schedulers:
                    await asyncio.sleep(interval)
                    continue
                # ★ 并发跑所有session的_tick()，互不影响
                tasks = [
                    asyncio.create_task(self._safe_tick(key[0], s))
                    for key, s in list(self._schedulers.items())
                ]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(interval)
        return get_loop().create_task(_loop())

    async def _safe_tick(self, session_id: str, scheduler_inst: "Scheduler"):
        """单session的_tick()，异常不影响其他session"""
        try:
            await scheduler_inst._tick()
        except Exception as e:
            print(f"[Scheduler] session={session_id} tick异常: {e}", flush=True)

    def session_count(self) -> int:
        return len(self._schedulers)


# 全局单例（多 session 并发版）
scheduler = SchedulerManager()
