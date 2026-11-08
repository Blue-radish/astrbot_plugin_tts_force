"""说一声就念给你听 —— 按对方的意愿和会话节奏决定要不要用语音。

判断顺序（先尊重意愿，再谈主动语音）：

  1. **对方明确拒绝**（“别发语音”“我在开会，不方便听”）→ 本会话进入文字模式，
     模型标记和随机都被挡住，直到过期或对方改口。句尾带“以后/一直”则长期生效。
  2. **对方点名**（“这句念给我听”）→ 本轮一定语音，放宽字数上限。
  3. **对方想要语音模式**（“接下来用语音聊”）→ 本会话提高主动语音倾向。
  4. **系统主动语音**（模型标记 / 随机）→ 共享同一套约束：
     群白名单、内容检查（链接/代码/清单/大段英文数字）、主动语音最小间隔、字数上限。
  5. **咨询句**（“发语音功能怎么设置”“voice 是什么意思”）→ 当普通提问，不触发。

合成失败、超时、冷却中、对方刚说过不方便听——一律保留文字回复。
"""

import asyncio
import json
import random
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_tts_force"

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
    r"(别|不要|不用|甭|禁止|不许|没必要|不想|别再|先别|停止)"
    r"(再|给我|帮我|对我)?"
    r"(发|用|来|讲|说)?(个|条)?"
    r"(语音|声音|念|读|voice)"
)
# “我在开会，不方便听”这类临时不便
RE_BUSY = re.compile(r"不方便(听|接|开)|听不了|没法听|听不见|在开会|正在开会|开会中|旁边有人|在外面|在地铁|静音中")
# 咨询/设置/概念解释
RE_QUESTION = re.compile(r"怎么|如何|为什么|为啥|是什么|什么意思|啥意思|设置|配置|在哪个|在哪|教程|文档|原理")
RE_VOICE_WORD = re.compile(r"语音|声音|念|朗读|读出来|voice")
# 长期偏好
RE_LONG_TERM = re.compile(r"以后|之后|从现在开始|一直|往后|接下来|后面|永久")
RE_LONG_TEXT_STRICT = re.compile(r"(以后|之后|从现在开始|一直|往后|接下来|后面).{0,6}(文字|打字)")
RE_LONG_VOICE = re.compile(r"(以后|之后|从现在开始|一直|往后|接下来|后面)(就|都|只)?(用|发|说)?(语音|声音)")
RE_VOICE_CHAT = re.compile(r"(用|拿)?语音聊|语音交流|别打字|不想打字")
RE_MODE_AUTO = re.compile(r"(恢复|回到)?默认|正常(发|来|聊)|随便|都行|都可以|你看着办")

DEFAULT_HINT = """[交流方式] 你可以选择这一轮用文字还是用语音：在回复最前面写 {marker} 表示这一句用语音说（标记本身不会被念出来），不写就是文字。
判断顺序：先看对方是否方便听——对方说不方便、在忙、先别发语音，就用文字；对方明确要求语音，就优先用语音。
再看内容是否适合听：需要复制、逐项核对、反复回看的信息（代码、链接、参数、清单）用文字；一句问候、轻松回应、收尾、需要表达关切的短句可以用语音。
最后看节奏：保持和最近几轮一致的交流方式，不要突然来回切换；连续主动发过几条语音后，收一收。
默认是文字，你没写标记就是文字。只有确定这一轮适合主动语音时才写 {marker}。"""


@register(
    PLUGIN_NAME,
    "Ray",
    "按对方意愿与会话节奏决定是否语音：拒绝优先、点名一定念、主动语音共享一套约束",
    "2.2.0",
)
class TTSForcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._last_voice_at: dict[str, float] = {}
        self._last_auto_at: dict[str, float] = {}
        self._busy: set[str] = set()
        # umo -> {"mode": "text"|"voice", "until": float|None, "why": str}
        self._prefs: dict[str, dict] = {}
        self._prefs_loaded = False
        self._migrate_config()

        pats = self._cfg_list("strip_patterns", DEFAULT_STRIP)
        self._strip_res = []
        for p in pats:
            try:
                self._strip_res.append(re.compile(p))
            except re.error as e:
                logger.warning(f"[{PLUGIN_NAME}] 忽略无效清洗正则 {p!r}: {e}")

    def _migrate_config(self) -> None:
        """2.1.0 的 random_* 现在管的是「所有主动语音」，改名后顺手接过来。"""
        for new, old in (("auto_min_gap", "random_min_gap"), ("auto_groups", "random_groups")):
            if new not in self.config and old in self.config:
                self.config[new] = self.config.pop(old)
                logger.info(f"[{PLUGIN_NAME}] 配置项 {old} 已改名为 {new}")
        keywords = self.config.get("force_keywords")
        if isinstance(keywords, list) and "voice" in keywords:
            self.config["force_keywords"] = [k for k in keywords if k != "voice"]
            logger.info(f"[{PLUGIN_NAME}] force_keywords 移除了过宽的 voice 关键词")

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

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
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

    # ---------------- 会话偏好 ----------------
    def _pref_file(self) -> Path | None:
        try:
            from astrbot.core.star.star_tools import StarTools

            base = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return None
        return base / "prefs.json"

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
            logger.warning(f"[{PLUGIN_NAME}] 读取会话偏好失败：{e}")
            return
        now = time.time()
        for umo, item in (raw or {}).items():
            if not isinstance(item, dict):
                continue
            until = item.get("until")
            if until is not None and until <= now:
                continue  # 过期的临时偏好直接丢
            self._prefs[umo] = item

    def _save_prefs(self) -> None:
        path = self._pref_file()
        if path is None:
            return
        keep = {
            umo: item
            for umo, item in self._prefs.items()
            if item.get("until") is None  # 只持久化长期偏好
        }
        if not keep and not path.exists():
            return  # 临时偏好不落盘，别留一个空文件
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(keep, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 保存会话偏好失败：{e}")

    def _set_pref(self, umo: str, mode: str, ttl: int, why: str) -> None:
        self._load_prefs()
        until = None if ttl <= 0 else time.time() + ttl
        self._prefs[umo] = {"mode": mode, "until": until, "why": why}
        self._save_prefs()
        logger.info(
            f"[{PLUGIN_NAME}] 会话偏好 -> {mode}（{why}，"
            f"{'长期' if until is None else f'{ttl}s 后失效'}）"
        )

    def _clear_pref(self, umo: str) -> None:
        self._load_prefs()
        if self._prefs.pop(umo, None) is not None:
            self._save_prefs()
            logger.info(f"[{PLUGIN_NAME}] 会话偏好已恢复默认（auto）")

    def _pref_mode(self, umo: str) -> tuple[str, str]:
        """返回 (mode, why)，mode 为 auto / text / voice。过期的临时偏好自动清掉。"""
        self._load_prefs()
        item = self._prefs.get(umo)
        if not item:
            return "auto", ""
        until = item.get("until")
        if until is not None and until <= time.time():
            self._prefs.pop(umo, None)
            logger.info(f"[{PLUGIN_NAME}] 临时偏好已过期，回到默认（auto）")
            return "auto", ""
        return item.get("mode") or "auto", item.get("why") or ""

    # ---------------- 读对方的意思 ----------------
    def _parse_intent(self, text: str) -> dict:
        """把用户这句话读成：要文字 / 要语音 / 点名 / 咨询 / 没有表态。"""
        msg = (text or "").strip()
        out = {"force_round": False, "mode": None, "ttl": 0, "why": "", "asked_anything": False}
        if not msg:
            return out
        ttl = self._cfg_int("text_mode_ttl", 1800)

        # 1) 长期文字：以后都用文字
        if RE_LONG_TEXT_STRICT.search(msg):
            out.update(mode="text", ttl=0, why="对方说以后都用文字")
            return out
        # 2) 拒绝语音 / 不方便听
        if RE_REFUSE.search(msg):
            long_term = bool(RE_LONG_TERM.search(msg))
            out.update(
                mode="text",
                ttl=0 if long_term else ttl,
                why="对方说以后都别发语音" if long_term else "对方明确说别发语音",
            )
            return out
        if RE_BUSY.search(msg):
            out.update(mode="text", ttl=ttl, why="对方说不方便听")
            return out
        # 3) 想要语音模式（持续）
        if RE_LONG_VOICE.search(msg) or RE_VOICE_CHAT.search(msg):
            out.update(mode="voice", ttl=ttl, why="对方想用语音聊")
            return out
        # 4) 恢复默认
        if RE_MODE_AUTO.search(msg):
            out.update(mode="auto", ttl=0, why="对方说随便")
            return out
        # 5) 咨询/设置问题：不触发语音
        if RE_QUESTION.search(msg) and RE_VOICE_WORD.search(msg):
            out.update(why="像是咨询语音功能，不当成点名")
            out["asked_anything"] = True
            return out
        # 6) 点名本轮语音
        keywords = self._cfg_list("force_keywords", DEFAULT_FORCE)
        if any(k in msg for k in keywords):
            out.update(force_round=True, why="对方点名要语音")
        return out

    def _strip_markers(self, text: str) -> tuple[str, bool]:
        hit = False
        out = text
        for m in self._cfg_list("voice_markers", DEFAULT_MARKERS):
            if m and m in out:
                hit = True
                out = out.replace(m, "")
        return out.strip(), hit

    # ---------------- 文本清洗 ----------------
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
        """念出来会不会是灾难：链接、代码、清单、长串英文数字都不念。"""
        meaningful = [ch for ch in speech if ch.isalnum() or _is_cjk(ch)]
        if len(meaningful) < min_chars:
            return False, "没有实际内容（纯符号/语气残留）"
        if re.search(r"https?://|www\.", speech):
            return False, "带链接，念出来是噪音"
        if "```" in speech or speech.count("`") >= 2:
            return False, "带代码块"
        bullets = [
            line
            for line in speech.splitlines()
            if re.match(r"^\s*([-*•·]|\d+[.、)])\s*\S", line)
        ]
        if len(bullets) >= 2:
            return False, "是清单/步骤，念出来听不清"
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

    def _auto_gap_ok(self, umo: str) -> tuple[bool, str]:
        gap = self._cfg_int("auto_min_gap", 600)
        if gap <= 0:
            return True, ""
        since = time.time() - self._last_auto_at.get(umo, 0.0)
        if since < gap:
            return False, f"距上次主动语音才 {since:.0f}s（下限 {gap}s）"
        return True, ""

    def _rate_for(self, mode: str) -> int:
        if mode == "voice":
            return self._cfg_int("voice_mode_rate", 80)
        return self._cfg_int("random_rate", 15)

    # ---------------- 提示词 ----------------
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
    async def inject_voice_hint(self, event: AstrMessageEvent, req) -> None:
        """把“按对方意愿和节奏选交流方式”写进系统提示词，而不是靠掷骰子。"""
        if not self._cfg_bool("enable", True) or not self._cfg_bool("inject_prompt", True):
            return
        umo = getattr(event, "unified_msg_origin", "") or ""
        mode, why = self._pref_mode(umo) if umo else ("auto", "")
        hint = self._build_hint(mode, why)
        if not hint:
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + hint
        if not getattr(self, "_hint_logged", False):
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

        umo = event.unified_msg_origin
        user_msg = (event.message_str or "").strip()
        intent = self._parse_intent(user_msg)

        # 对方明确表态就落成会话偏好
        if intent["mode"] == "auto":
            self._clear_pref(umo)
        elif intent["mode"]:
            self._set_pref(umo, intent["mode"], intent["ttl"], intent["why"])

        text, marked = self._strip_markers(raw_text)
        if marked:
            for comp in result.chain:
                if isinstance(comp, Plain):
                    comp.text = self._strip_markers(comp.text)[0]
            event.set_result(result)

        speech = self._clean_for_speech(text)
        if len(speech) < 1:
            logger.info(f"[{PLUGIN_NAME}] 去掉括号/标记后没内容了，本条发文字")
            return

        pref_mode, pref_why = self._pref_mode(umo)
        force_round = intent["force_round"]

        # ---- 决策：对方主动要求 > 系统主动语音 ----
        if force_round:
            source = "对方点名"
            limit = self._cfg_int("max_chars_user", 2000)
            timeout = self._cfg_int("timeout", 300)
        else:
            if pref_mode == "text":
                logger.info(
                    f"[{PLUGIN_NAME}] 本条发文字：对方说过「{pref_why or '不方便听'}」"
                    f"（标记={'有' if marked else '无'}，已拦下主动语音）"
                )
                return
            limit = self._cfg_int("max_chars", 50)
            timeout = self._cfg_int("auto_timeout", 40)
            source = "模型标记" if marked else "随机"

            ok, reason = self._auto_scope_ok(event)
            if not ok:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return
            ok, reason = self._auto_gap_ok(umo)
            if not ok:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return
            ok, reason = self._voice_worthy(speech, 1 if marked else 2)
            if not ok:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return
            if not marked:
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
        if cooldown > 0 and time.time() - self._last_voice_at.get(umo, 0.0) < cooldown:
            logger.info(f"[{PLUGIN_NAME}] 冷却中，本条发文字")
            return
        if umo in self._busy:
            logger.info(f"[{PLUGIN_NAME}] 上一条语音还在合成，本条发文字")
            return

        provider = None
        pid = self._cfg_str("provider_id", "qwen3-tts")
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] get_provider_by_id({pid}) 失败：{e}")
        if provider is None:
            try:
                provider = await self.context.get_using_tts_provider_async(umo)
            except Exception:
                provider = None
        if provider is None or not hasattr(provider, "get_audio"):
            logger.error(f"[{PLUGIN_NAME}] 没拿到可用的 TTS provider（provider_id={pid}）")
            return

        self._busy.add(umo)
        try:
            logger.info(
                f"[{PLUGIN_NAME}] 合成语音（{len(speech)} 字，来源={source}，超时 {timeout}s）：{speech[:50]}"
            )
            t0 = time.time()
            audio_path = await asyncio.wait_for(provider.get_audio(speech), timeout=timeout)
            if not audio_path:
                logger.error(f"[{PLUGIN_NAME}] 合成失败，保留文字")
                return
            keep = [c for c in result.chain if not isinstance(c, Plain)]
            result.chain = keep + [Record(file=audio_path)]
            event.set_result(result)
            now = time.time()
            self._last_voice_at[umo] = now
            if source != "对方点名":
                self._last_auto_at[umo] = now
            logger.info(f"[{PLUGIN_NAME}] 语音就绪（{now - t0:.1f}s）：{audio_path}")
        except asyncio.TimeoutError:
            logger.warning(
                f"[{PLUGIN_NAME}] 合成超过 {timeout}s（来源={source}），先发文字，避免让对方干等"
            )
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 合成异常，保留文字：{e}")
        finally:
            self._busy.discard(umo)


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
