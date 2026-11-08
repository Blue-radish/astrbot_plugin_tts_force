"""说一声就念给你听 —— 按对方的意愿和会话节奏决定要不要用语音。

判断顺序（先尊重意愿，再谈主动语音）：

  1. **对方明确拒绝**（“别发语音”“我在开会，不方便听”）→ 本会话进入文字模式，
     模型标记和随机都被挡住，直到过期或对方改口。说“以后”则长期生效。
  2. **对方点名**（“这句念给我听”）→ 本轮一定语音，放宽字数上限；如果这条回复里有
     代码、链接、命令这类要复制的内容，语音之外**文字也保留**，不能让内容消失。
  3. **对方想要语音模式**（“接下来用语音聊”）→ 本会话提高主动语音倾向，间隔也缩短。
  4. **系统主动语音**（模型标记 / 随机）→ 共享同一套约束：场合、内容、间隔、长度、等待预算。
     主动语音遇到可复制信息（代码/链接/清单/长数字）一律不发语音。
  5. **咨询句**（“发语音功能怎么设置”“voice 是什么意思”）→ 当普通提问，不触发。

合成期间对方改了主意、或者已经继续说下一句了，那条旧语音会被丢弃，不会补发。
任何失败都保留文字回复。
"""

import asyncio
import json
import os
import random
import re
import time
import wave
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_tts_force"
CONFIG_VERSION = 3  # 配置迁移标记用；改版式时 +1

DEFAULT_MARKERS = ["<语音>", "<语音/>", "【语音】", "[语音]", "#语音", "<voice>", "[voice]"]
# 注意：不要放裸的 "voice" —— “voice 这个单词是什么意思”会被它误判成点名
DEFAULT_FORCE = ["发语音", "发条语音", "发个语音", "语音说", "语音回", "用语音", "念给我听"]
DEFAULT_STRIP = [
    r"（[^（）]{0,120}）",   # 全角括号：动作/神态
    r"\([^()]{0,120}\)",     # 半角括号
    r"【[^【】]{0,120}】",   # 方头括号：注释
    r"〔[^〔〕]{0,120}〕",
    r"\[[^\[\]]{0,120}\]",   # 方括号
    r"\*[^*\n]{0,120}\*",    # *动作*
]

# 拒绝：否定词 + 语音动作词（“别发语音”“不要念了”“别用声音”）
RE_REFUSE = re.compile(
    r"(别|不要|不用|不能|甭|禁止|不许|没必要|不想|别再|先别|停止)"
    r"(再|给我|帮我|对我|这边|这边儿)?"
    r"(发|用|来|讲|说|听|放|开|收)?(个|条|段)?"
    r"(语音|声音|念|读|voice)"
)
# 明确的收听限制（自己说的，一定是当前状态）
RE_BUSY_STRONG = re.compile(
    r"不方便(听|接|开)|听不了|没法听|听不见|不方便放声音|不能放声音|静音"
)
# 场景词：需要第一人称主语、且没有被否定
RE_BUSY_WEAK = re.compile(r"(开会|地铁|公交|车上|在外面|在公司|在办公室|在医院|在图书馆)")
RE_FIRST_PERSON = re.compile(r"我|咱|俺")
RE_NEGATED = re.compile(r"(没|不|别|未|无)(有)?(在|去|用)?\s*$")
# 咨询/设置/概念解释（有意不放“为什么”：它经常出现在“用语音解释为什么…”这种明确点名里）
RE_CONSULT = re.compile(r"怎么|如何|是什么|什么意思|啥意思|设置|配置|在哪个|在哪|教程|文档|原理|功能|开关|按钮|选项")
# 强请求：哪怕这条消息同时是个问句（“用语音解释一下为什么会这样”），也按点名处理
RE_REQUEST_STRONG = re.compile(
    r"念给我听|念一下|读给我听|读一下"
    r"|发条语音|发个语音|发段语音"
    r"|用语音(说|讲|解释|念|回|聊)"
    r"|语音回我"
)
# 弱请求：只是出现了“语音/念”这类字眼，遇到问句要让位（“语音念出来是什么原理”）
RE_IMPERATIVE = re.compile(
    r"念|读|语音(说|回|念)|发语音(?![功开按选])"
)
# 长期偏好
RE_LONG_TERM = re.compile(r"以后|之后|从现在开始|一直|往后|接下来|后面|永久")
RE_LONG_TEXT_STRICT = re.compile(r"(以后|之后|从现在开始|一直|往后|接下来|后面).{0,6}(文字|打字)")
RE_LONG_VOICE = re.compile(r"(以后|之后|从现在开始|一直|往后|接下来|后面)(就|都|只)?(用|发|说)?(语音|声音)")
RE_VOICE_CHAT = re.compile(r"(用|拿)?语音聊|语音交流|别打字|不想打字")
# 回到自动：必须指向交流方式，不能是“今晚吃什么随便”
RE_MODE_AUTO = re.compile(
    r"(语音|文字|回复|交流|聊天).{0,10}(都行|都可以|都成|随便|随意|无所谓|没所谓)"
    r"|^(好了?|那就?)?(恢复|回到)(到)?(默认|正常)(回复|交流)?(方式)?[。！!.～~]*$"
)
# 可复制信息：这些东西被念掉就等于丢了
RE_URL = re.compile(r"https?://|www\.|\.com|\.cn|\.io\b")
RE_LONG_NUMBER = re.compile(r"\d{6,}")

DEFAULT_HINT = """[交流方式] 你可以选择这一轮用文字还是用语音：在回复最前面写 {marker} 表示这一句用语音说（标记本身不会被念出来），不写就是文字。
判断顺序：先看对方是否方便听——对方说不方便、在忙、先别发语音，就用文字；对方明确要求语音，就优先用语音。
再看内容是否适合听：需要复制、逐项核对、反复回看的信息（代码、链接、参数、清单）用文字；一句问候、轻松回应、收尾、需要表达关切的短句可以用语音。
最后看节奏：保持和最近几轮一致的交流方式，不要突然来回切换；连续主动发过几条语音后，收一收。
默认是文字，你没写标记就是文字。只有确定这一轮适合主动语音时才写 {marker}。"""


@register(
    PLUGIN_NAME,
    "Ray",
    "按对方意愿与会话节奏决定是否语音：拒绝优先、点名一定念、主动语音共享一套约束",
    "2.3.0",
)
class TTSForcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._last_voice_at: dict[str, float] = {}
        self._last_auto_at: dict[str, float] = {}
        self._busy: set[str] = set()
        self._prefs: dict[str, dict] = {}
        self._prefs_loaded = False
        # 会话偏好版本号 + 轮次号：合成完成前复核用
        self._pref_ver: dict[str, int] = {}
        self._turn_seq: dict[str, int] = {}
        self._hint_logged = False

        pats = self._cfg_list("strip_patterns", DEFAULT_STRIP)
        self._strip_res = []
        for p in pats:
            try:
                self._strip_res.append(re.compile(p))
            except re.error as e:
                logger.warning(f"[{PLUGIN_NAME}] 忽略无效清洗正则 {p!r}: {e}")

        self._migrate_config()

    # ---------------- 配置 ----------------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        v = self.config.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(v)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        v = self.config.get(key, default)
        return str(v).strip() if v is not None else default

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        v = self.config.get(key)
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip() != ""]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
        return list(default)

    def _marker(self) -> str:
        markers = self._cfg_list("voice_markers", DEFAULT_MARKERS)
        return markers[0] if markers else "<语音>"

    # ---------------- 配置迁移 ----------------
    def _state_file(self) -> Path | None:
        base = self._data_dir()
        return None if base is None else base / "state.json"

    def _data_dir(self) -> Path | None:
        try:
            from astrbot.core.star.star_tools import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return None

    def _read_state(self) -> dict:
        path = self._state_file()
        if path is None or not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        path = self._state_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 写状态失败：{e}")

    def _migrate_config(self) -> None:
        """把 2.1.0 的 random_* 搬到 auto_*，并去掉过宽的 voice 关键词。

        旧键在 _conf_schema.json 里留了隐藏字段（invisible），否则宿主在插件初始化前
        就会按新 schema 把它们删掉，这里根本读不到。
        """
        state = self._read_state()
        if int(state.get("config_migrated_v", 0) or 0) >= CONFIG_VERSION:
            return  # 已经迁过，别再动用户的新设置
        moved: list[str] = []

        def take(new: str, old: str) -> None:
            # 首次迁移时旧值才是用户真正设过的；新键里那份是宿主刚补的默认值
            value = self.config.get(old)
            if value:  # 0 / 空列表 = 从没设过
                self.config[new] = self.config.pop(old)
                moved.append(f"{old}→{new}")

        take("auto_min_gap", "random_min_gap")
        take("auto_groups", "random_groups")

        keywords = self.config.get("force_keywords")
        if isinstance(keywords, list) and "voice" in keywords:
            self.config["force_keywords"] = [k for k in keywords if k != "voice"]
            moved.append("force_keywords 去掉裸 voice")

        if moved:
            logger.info(f"[{PLUGIN_NAME}] 配置迁移：{'，'.join(moved)}")
            saver = getattr(self.config, "save_config", None)
            if callable(saver):
                try:
                    saver()
                    logger.info(f"[{PLUGIN_NAME}] 配置迁移已保存")
                except Exception as e:
                    logger.warning(f"[{PLUGIN_NAME}] 配置迁移保存失败：{e}")
        state["config_migrated_v"] = CONFIG_VERSION
        self._write_state(state)

    # ---------------- 会话偏好 ----------------
    def _pref_file(self) -> Path | None:
        base = self._data_dir()
        return None if base is None else base / "prefs.json"

    def _load_prefs(self) -> None:
        if self._prefs_loaded:
            return
        self._prefs_loaded = True
        path = self._pref_file()
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 偏好文件损坏，按空处理：{e}")
            return
        if not isinstance(raw, dict):
            logger.warning(f"[{PLUGIN_NAME}] 偏好文件格式不对，按空处理")
            return
        now = time.time()
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            if item.get("mode") not in ("text", "voice"):
                continue
            until = item.get("until")
            if until is not None:
                try:
                    if float(until) <= now:
                        continue
                except (TypeError, ValueError):
                    continue
            self._prefs[key] = item

    def _save_prefs(self) -> None:
        path = self._pref_file()
        if path is None:
            return
        keep = {
            key: item
            for key, item in self._prefs.items()
            if item.get("until") is None  # 只持久化长期偏好
        }
        if not keep and not path.exists():
            return  # 临时偏好不落盘，别留空文件
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")  # 原子写：先写临时文件再替换
            tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存会话偏好失败：{e}")

    def _pref_key(self, event: AstrMessageEvent) -> str:
        """群里按发言人分开记：一个人在群里说不方便听，不该让整个群静音。"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if not gid:
            return umo
        try:
            sender = str(event.get_sender_id() or "unknown")
        except Exception:
            sender = "unknown"
        return f"{umo}#{sender}"

    def _bump_pref_ver(self, key: str) -> None:
        self._pref_ver[key] = self._pref_ver.get(key, 0) + 1

    def _set_pref(self, key: str, mode: str, ttl: int, why: str) -> None:
        self._load_prefs()
        until = None if ttl <= 0 else time.time() + ttl
        current = self._prefs.get(key)
        if current and current.get("mode") == mode and current.get("until") == until:
            return  # 幂等：同一事件里解析两次不该重复写盘
        self._prefs[key] = {"mode": mode, "until": until, "why": why}
        self._bump_pref_ver(key)
        self._save_prefs()
        logger.info(
            f"[{PLUGIN_NAME}] 会话偏好 -> {mode}（{why}，"
            f"{'长期' if until is None else f'{ttl}s 后失效'}）"
        )

    def _clear_pref(self, key: str) -> None:
        self._load_prefs()
        if self._prefs.pop(key, None) is not None:
            self._bump_pref_ver(key)
            self._save_prefs()
            logger.info(f"[{PLUGIN_NAME}] 会话偏好已恢复默认（auto）")

    def _pref_mode(self, key: str) -> tuple[str, str]:
        """返回 (mode, why)，mode 为 auto / text / voice。过期的临时偏好自动清掉。"""
        self._load_prefs()
        item = self._prefs.get(key)
        if not item:
            return "auto", ""
        until = item.get("until")
        if until is not None and float(until) <= time.time():
            self._prefs.pop(key, None)
            self._bump_pref_ver(key)
            logger.info(f"[{PLUGIN_NAME}] 临时偏好已过期，回到默认（auto）")
            return "auto", ""
        return item.get("mode") or "auto", item.get("why") or ""

    def _apply_intent(self, key: str, text: str) -> dict:
        """解析这句话的交流意愿并落成偏好（幂等，可以重复调用）。"""
        intent = self._parse_intent(text)
        if intent["mode"] == "auto":
            self._clear_pref(key)
        elif intent["mode"]:
            self._set_pref(key, intent["mode"], intent["ttl"], intent["why"])
        return intent

    # ---------------- 读对方的意思 ----------------
    def _parse_intent(self, text: str) -> dict:
        """把用户这句话读成：要文字 / 要语音 / 点名 / 咨询 / 没有表态。"""
        msg = (text or "").strip()
        out = {"force_round": False, "mode": None, "ttl": 0, "why": ""}
        if not msg:
            return out
        ttl = self._cfg_int("text_mode_ttl", 1800)

        # 1) 长期文字：以后都用文字
        if RE_LONG_TEXT_STRICT.search(msg):
            out.update(mode="text", ttl=0, why="对方说以后都用文字")
            return out
        # 2) 拒绝语音
        if RE_REFUSE.search(msg):
            long_term = bool(RE_LONG_TERM.search(msg))
            out.update(
                mode="text",
                ttl=0 if long_term else ttl,
                why="对方说以后都别发语音" if long_term else "对方明确说别发语音",
            )
            return out
        # 3) 不方便听：强信号直接生效；场景词要有第一人称、且没被否定
        if RE_BUSY_STRONG.search(msg):
            out.update(mode="text", ttl=ttl, why="对方说不方便听")
            return out
        weak = RE_BUSY_WEAK.search(msg)
        if weak and RE_FIRST_PERSON.search(msg):
            head = msg[: weak.start()]
            negated = bool(re.search(r"(没|不|别|未|无)(有)?(在|去|用)?\s*$", head))
            if not negated:
                out.update(mode="text", ttl=ttl, why=f"对方说在{weak.group(1)}，不方便听")
                return out
        # 4) 想用语音聊（持续）
        if RE_LONG_VOICE.search(msg) or RE_VOICE_CHAT.search(msg):
            out.update(mode="voice", ttl=ttl, why="对方想用语音聊")
            return out
        # 5) 回到自动：必须指向交流方式
        if RE_MODE_AUTO.search(msg):
            out.update(mode="auto", ttl=0, why="对方说都行")
            return out
        # 6) 点名 or 咨询：强请求 > 问句 > 弱请求
        keywords = self._cfg_list("force_keywords", DEFAULT_FORCE)
        strong = bool(RE_REQUEST_STRONG.search(msg))
        keyword_hit = any(k in msg for k in keywords)
        consult = bool(RE_CONSULT.search(msg))
        mentions_voice = bool(re.search(r"语音|声音|念|朗读|读出来|voice", msg))
        if strong:
            out.update(force_round=True, why="对方点名要语音")
            return out
        if consult and mentions_voice:
            out.update(why="像是咨询语音功能，不当成点名")
            return out
        if keyword_hit or RE_IMPERATIVE.search(msg):
            out.update(force_round=True, why="对方点名要语音")
        return out

    # ---------------- 文本处理 ----------------
    def _strip_markers(self, text: str) -> tuple[str, bool]:
        hit = False
        out = text
        for m in self._cfg_list("voice_markers", DEFAULT_MARKERS):
            if m and m in out:
                hit = True
                out = out.replace(m, "")
        return out.strip(), hit

    @staticmethod
    def _copyable(text: str) -> str:
        """这条回复里有没有「念了就等于丢了」的内容。要在清洗前判断。"""
        if "```" in text:
            return "含代码块"
        if text.count("`") >= 2:
            return "含行内代码"
        if RE_URL.search(text):
            return "含链接"
        bullets = [
            line
            for line in text.splitlines()
            if re.match(r"^\s*([-*•·]|\d+[.、)])\s*\S", line)
        ]
        if len(bullets) >= 2:
            return "是清单/步骤"
        if RE_LONG_NUMBER.search(text):
            return "含长数字/参数"
        return ""

    def _clean_for_speech(self, text: str) -> str:
        out = text
        # 思考块（AstrBot 有时会把 reasoning 混进正文）
        out = re.sub(r"\{\s*['\"]type['\"]\s*:\s*['\"]think['\"].*?\}", " ", out, flags=re.S)
        # 整块系统注记（system_reminder 之类，连内容一起丢）
        out = re.sub(
            r"<(system_reminder|thinking|think|reasoning|context|analysis)\b[^>]*>.*?</\1>",
            " ",
            out,
            flags=re.S | re.I,
        )
        # 尖括号系统提示
        out = re.sub(r"<[^>\n]{0,300}>", " ", out)
        out = re.sub(r"</?[A-Za-z][^>\n]{0,200}$", " ", out)
        # 动作/神态描写等
        for rx in self._strip_res:
            out = rx.sub("", out)
        if self._cfg_bool("strip_markdown", True):
            out = re.sub(r"```.*?```", " ", out, flags=re.S)   # 代码块不念
            out = re.sub(r"`([^`]*)`", r"\1", out)
            out = re.sub(r"[*_#>~|]+", "", out)
        out = re.sub(r"[ \t\u3000]+", " ", out)
        out = re.sub(r"\n{2,}", "\n", out)
        return out.strip()

    # ---------------- 能不能念 ----------------
    @staticmethod
    def _voice_worthy(speech: str, min_chars: int = 2) -> tuple[bool, str]:
        """念出来会不会是灾难（作用于清洗后的朗读文本）。"""
        meaningful = [ch for ch in speech if ch.isalnum() or _is_cjk(ch)]
        if len(meaningful) < min_chars:
            return False, "没有实际内容（纯符号/语气残留）"
        ascii_alnum = sum(1 for ch in speech if ch.isascii() and ch.isalnum())
        if ascii_alnum / len(speech) > 0.4:
            return False, "英文数字太多"
        digits = sum(1 for ch in speech if ch.isdigit())
        if digits / len(speech) > 0.3:
            return False, "数字太多"
        return True, ""

    def _auto_scope_ok(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """主动语音的场合限制：私聊总是可以，群里只认白名单。"""
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid:
            allowed = {g.strip() for g in self._cfg_list("auto_groups", [])}
            if gid not in allowed:
                return False, f"群 {gid} 不在主动语音白名单里"
        return True, ""

    def _auto_gap_ok(self, key: str, mode: str) -> tuple[bool, str]:
        if mode == "voice":
            gap = self._cfg_int("voice_mode_min_gap", 25)
        else:
            gap = self._cfg_int("auto_min_gap", 600)
        if gap <= 0:
            return True, ""
        since = time.time() - self._last_auto_at.get(key, 0.0)
        if since < gap:
            return False, f"距上次主动语音才 {since:.0f}s（下限 {gap}s）"
        return True, ""

    def _rate_for(self, mode: str) -> int:
        if mode == "voice":
            return self._cfg_int("voice_mode_rate", 80)
        return self._cfg_int("random_rate", 15)

    # ---------------- 提示词与偏好记录 ----------------
    def _build_hint(self, pref_mode: str = "auto", pref_why: str = "") -> str:
        custom = self._cfg_str("prompt_hint", "")
        base = custom or DEFAULT_HINT
        base = base.replace("{marker}", self._marker()).replace(
            "{max_chars}", str(self._cfg_int("max_chars", 50))
        )
        if pref_mode == "text":
            state = f"\n[当前状态] 对方表示过（{pref_why or '不方便听'}），这一轮请用文字回复，不要写标记。"
        elif pref_mode == "voice":
            state = "\n[当前状态] 对方想用语音聊，短句可以多用语音；需要复制或核对的内容仍然用文字。"
        else:
            state = ""
        return base + state

    @filter.on_llm_request()
    async def track_voice_pref(self, event: AstrMessageEvent, req) -> None:
        """在模型生成之前就把对方的交流意愿记下来，本轮提示词才不会拿到上一轮的状态。"""
        if not self._cfg_bool("enable", True):
            return
        self._apply_intent(self._pref_key(event), getattr(event, "message_str", "") or "")

    @filter.on_llm_request()
    async def inject_voice_hint(self, event: AstrMessageEvent, req) -> None:
        """把「按对方意愿和节奏选交流方式」写进系统提示词，而不是靠掷骰子。"""
        if not self._cfg_bool("enable", True) or not self._cfg_bool("inject_prompt", True):
            return
        key = self._pref_key(event)
        mode, why = self._pref_mode(key)
        hint = self._build_hint(mode, why)
        if not hint:
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + hint
        if not self._hint_logged:
            self._hint_logged = True
            logger.info(
                f"[{PLUGIN_NAME}] 已注入交流方式规则（标记 {self._marker()}，"
                f"主动语音 {self._cfg_int('random_rate', 15)}%，当前会话 {mode}）"
            )

    # ---------------- 主逻辑 ----------------
    @filter.on_decorating_result()
    async def maybe_tts(self, event: AstrMessageEvent):
        if not self._cfg_bool("enable", True):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:
            return
        if "STREAMING" in str(getattr(result, "result_content_type", "")).upper():
            return

        raw_text = "".join(c.text for c in result.chain if isinstance(c, Plain))
        if not raw_text.strip():
            return

        key = self._pref_key(event)
        user_msg = (event.message_str or "").strip()
        # 幂等：on_llm_request 已经落过一次，这里兜住没走 LLM 请求钩子的情况
        intent = self._apply_intent(key, user_msg)
        self._turn_seq[key] = self._turn_seq.get(key, 0) + 1
        my_turn = self._turn_seq[key]

        text, marked = self._strip_markers(raw_text)
        if marked:
            for comp in result.chain:
                if isinstance(comp, Plain):
                    comp.text = self._strip_markers(comp.text)[0]
            event.set_result(result)

        copyable = self._copyable(text)  # 必须看原文，不能看清洗后的
        pref_mode, pref_why = self._pref_mode(key)
        dual = False

        if intent["force_round"]:
            source = "对方点名"
            limit = self._cfg_int("max_chars_user", 2000)
            timeout = self._cfg_int("timeout", 300)
            dual = bool(copyable)
            if dual:
                logger.info(
                    f"[{PLUGIN_NAME}] 对方点名且这条回复{copyable}：语音之外保留文字，避免内容丢失"
                )
        else:
            if not self._cfg_bool("auto_enable", True):
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：主动语音总开关已关闭")
                return
            if pref_mode == "text":
                logger.info(
                    f"[{PLUGIN_NAME}] 本条发文字：对方说过「{pref_why or '不方便听'}」"
                    f"（标记={'有' if marked else '无'}，已拦下主动语音）"
                )
                return
            if copyable:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：回复{copyable}，主动语音跳过")
                return
            limit = self._cfg_int("max_chars", 50)
            timeout = self._cfg_int("auto_timeout", 40)
            source = "模型标记" if marked else "随机"

            ok, reason = self._auto_scope_ok(event)
            if not ok:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return
            ok, reason = self._auto_gap_ok(key, pref_mode)
            if not ok:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return

        speech = self._clean_for_speech(text)
        if len(speech) < 1:
            logger.info(f"[{PLUGIN_NAME}] 去掉括号/标记后没内容了，本条发文字")
            return
        ok, reason = self._voice_worthy(speech, 1 if (marked or intent["force_round"]) else 2)
        if not ok:
            logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
            return
        if not intent["force_round"] and not marked:
            rate = self._rate_for(pref_mode)
            if rate <= 0:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：随机语音已关闭")
                return
            if random.uniform(0, 100) >= rate:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：这次没掷中（{rate}%）")
                return
        if limit > 0 and len(speech) > limit:
            logger.info(
                f"[{PLUGIN_NAME}] 要念的文本 {len(speech)} 字 > 上限 {limit}（来源={source}），本条发文字"
            )
            return

        cooldown = self._cfg_int("cooldown", 0)
        if cooldown > 0 and time.time() - self._last_voice_at.get(key, 0.0) < cooldown:
            logger.info(f"[{PLUGIN_NAME}] 冷却中，本条发文字")
            return
        if key in self._busy:
            logger.info(f"[{PLUGIN_NAME}] 上一条语音还在合成，本条发文字")
            return

        provider, pid_desc = await self._resolve_provider(event)
        if provider is None or not hasattr(provider, "get_audio"):
            logger.error(f"[{PLUGIN_NAME}] 没拿到可用的 TTS provider：{pid_desc}")
            return

        # 记下此刻的偏好版本与轮次，合成完再复核一次
        ver = self._pref_ver.get(key, 0)
        self._busy.add(key)
        try:
            logger.info(
                f"[{PLUGIN_NAME}] 合成语音（{len(speech)} 字，来源={source}，"
                f"提供商={pid_desc}，超时 {timeout}s）：{speech[:50]}"
            )
            t0 = time.time()
            audio_path = await asyncio.wait_for(provider.get_audio(speech), timeout=timeout)
            if not audio_path:
                logger.error(f"[{PLUGIN_NAME}] 合成失败，保留文字")
                return
            stale = self._stale_reason(key, ver, my_turn, source)
            if stale:
                logger.info(f"[{PLUGIN_NAME}] 丢弃这条语音：{stale}")
                return
            if dual:
                result.chain = list(result.chain) + [Record(file=audio_path)]
            else:
                keep = [c for c in result.chain if not isinstance(c, Plain)]
                result.chain = keep + [Record(file=audio_path)]
            event.set_result(result)
            now = time.time()
            self._last_voice_at[key] = now
            if source != "对方点名":
                self._last_auto_at[key] = now
            logger.info(f"[{PLUGIN_NAME}] 语音就绪（{now - t0:.1f}s）：{audio_path}")
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] 合成超过 {timeout}s（来源={source}），先发文字，避免让对方干等"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 合成异常，保留文字：{e}")
        finally:
            self._busy.discard(key)

    def _stale_reason(self, key: str, ver: int, my_turn: int, source: str = "") -> str:
        """合成期间对方改主意或已经继续说话了，这条语音就不该再发。"""
        if self._pref_ver.get(key, 0) != ver:
            return "对方在这期间改了交流偏好"
        if source != "对方点名" and self._pref_mode(key)[0] == "text":
            return "对方现在想用文字"
        if self._turn_seq.get(key, 0) != my_turn:
            return "对方已经继续说下一句了，旧语音不再合适"
        return ""

    async def _resolve_provider(self, event: AstrMessageEvent) -> tuple[object | None, str]:
        pid = self._cfg_str("provider_id", "")
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] get_provider_by_id({pid}) 失败：{e}")
                provider = None
            if provider is not None:
                return provider, f"{pid}"
            logger.warning(f"[{PLUGIN_NAME}] 配置的提供商 {pid} 不存在，改用当前会话的 TTS")
        try:
            fetcher = self.context.get_using_tts_provider_async
            umo = getattr(event, "unified_msg_origin", "")
            return await fetcher(umo), f"会话默认（{pid or '未配置'}）"
        except Exception as e:
            return None, f"按会话取 TTS 失败：{e}"

    # ---------------- 自检命令 ----------------
    @filter.command("语音测试")
    async def tts_self_test(self, event: AstrMessageEvent):
        """真跑一次合成：报告用的是哪个提供商、耗时、音频时长，或具体失败原因。"""
        provider, desc = await self._resolve_provider(event)
        if provider is None or not hasattr(provider, "get_audio"):
            yield event.plain_result(f"❌ 没拿到 TTS 提供商：{desc}")
            return
        sample = self._cfg_str("test_text", "语音测试成功，这是一句示例。")
        timeout = self._cfg_int("timeout", 300)
        t0 = time.time()
        try:
            audio = await asyncio.wait_for(provider.get_audio(sample), timeout=timeout)
        except asyncio.TimeoutError:
            yield event.plain_result(f"❌ 合成超时（>{timeout}s），提供商 {desc}")
            return
        except Exception as e:
            yield event.plain_result(f"❌ 合成失败：{_classify_error(e)}\n提供商 {desc}\n{_short(e)}")
            return
        elapsed = time.time() - t0
        if not audio:
            yield event.plain_result(f"❌ 提供商没返回音频路径（提供商 {desc}，{elapsed:.1f}s）")
            return
        path = Path(str(audio))
        if not path.is_file() or path.stat().st_size == 0:
            yield event.plain_result(f"❌ 返回的不是可用音频：{path.name}（提供商 {desc}）")
            return
        yield event.plain_result(
            f"✅ 合成成功\n提供商：{desc}\n耗时：{elapsed:.1f}s\n"
            f"格式：{path.suffix or '未知'}　大小：{path.stat().st_size / 1024:.0f} KB"
            f"{_duration_hint(path)}\n文本：{sample}"
        )


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"


def _short(e: Exception, limit: int = 200) -> str:
    """错误信息脱敏：别把 key 写进日志或聊天里。"""
    text = str(e)
    text = re.sub(r"(?i)(sk-|key|token|authorization)[\w\-:=\s]{0,80}", r"\1***", text)
    return text[:limit]


def _classify_error(e: Exception) -> str:
    text = str(e).lower()
    if any(k in text for k in ("401", "403", "unauthorized", "invalid_api_key", "authentication")):
        return "鉴权失败（检查 API Key）"
    if any(k in text for k in ("404", "model_not_found", "invalid model", "voice", "speaker")):
        return "模型或音色无效"
    if any(k in text for k in ("timeout", "timed out")):
        return "请求超时"
    if any(k in text for k in ("connect", "dns", "refused", "unreachable")):
        return "连不上接口地址"
    return "未知错误"


def _duration_hint(path: Path) -> str:
    if path.suffix.lower() != ".wav":
        return ""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return f"　时长：{frames / rate:.1f}s"
    except Exception:
        return ""
