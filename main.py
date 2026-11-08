"""说一声就念给你听 —— 模型按内容自己判断要不要发语音，日常闲聊也有概率发。

三条路，优先级从高到低：

  1. **用户点名**：这句消息里带 force_keywords（“发语音”“念给我听”…）→ 一定念。
  2. **模型标记**：回复里写了 <语音> → 念（标记本身不会念，也不会留在文字里）。
     插件会把「什么时候该用语音」写进系统提示词，让模型按内容判断（可在设置里关掉）。
  3. **随机**：普通短回复按 random_rate 掷一次骰子。掷骰子不是纯随机——
     先过一遍硬门槛（字数、链接、代码、列表、英文数字占比、语音间隔），
     私聊总是允许，群里只认 random_groups 白名单。

合成前会清洗文本：括号里的动作/神态描写（（她低下头））、【】注释、markdown 符号都不念。
任何一步失败都保留原来的文字回复，不会变成没人说话。
"""

import asyncio
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_tts_force"

DEFAULT_MARKERS = ["<语音>", "<语音/>", "【语音】", "[语音]", "#语音", "<voice>", "[voice]"]
DEFAULT_FORCE = ["发语音", "语音说", "用语音", "念给我听", "语音回", "voice"]
DEFAULT_STRIP = [
    r"（[^（）]{0,120}）",   # 全角括号：动作/神态
    r"\([^()]{0,120}\)",     # 半角括号
    r"【[^【】]{0,120}】",   # 方头括号：注释
    r"〔[^〔〕]{0,120}〕",
    r"\[[^\[\]]{0,120}\]",   # 方括号
    r"\*[^*\n]{0,120}\*",    # *动作*
]

DEFAULT_HINT = """[语音规则] 你也可以直接把一句话说给对方听：在回复最前面写 {marker}，插件会把这一句合成语音发出去（标记本身不会被念出来）。
适合用语音：安慰、撒娇、道晚安、情绪很重的一句话、一两句就能说完的短回应。
别用语音：解释原理、列步骤或清单、带代码/链接/数字对比，以及超过 {max_chars} 字的长回复（太长会自动改回文字）。
不写标记就是普通文字回复——这是默认情况，绝大多数回复都应该是文字，只在真的适合“说出口”的时候才写标记。"""


@register(
    PLUGIN_NAME,
    "Ray",
    "模型自己判断要不要发语音（提示词可关），日常闲聊按概率念，括号动作与代码不念",
    "2.1.0",
)
class TTSForcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._last_voice_at: dict[str, float] = {}
        self._last_random_at: dict[str, float] = {}
        self._busy: set[str] = set()

        pats = self._cfg_list("strip_patterns", DEFAULT_STRIP)
        self._strip_res = []
        for p in pats:
            try:
                self._strip_res.append(re.compile(p))
            except re.error as e:
                logger.warning(f"[{PLUGIN_NAME}] 忽略无效清洗正则 {p!r}: {e}")

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
        # 尖括号系统提示（<system_reminder>…</system_reminder>、残留的 <s 之类）
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

    def _strip_markers(self, text: str) -> tuple[str, bool]:
        hit = False
        out = text
        for m in self._cfg_list("voice_markers", DEFAULT_MARKERS):
            if m and m in out:
                hit = True
                out = out.replace(m, "")
        return out.strip(), hit

    # ---------------- 能不能念 ----------------
    @staticmethod
    def _voice_worthy(speech: str) -> tuple[bool, str]:
        """念出来会不会是灾难：链接、代码、清单、长串英文数字都不念。"""
        if len(speech) < 6:
            return False, "太短，念了没内容"
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

    def _random_scope_ok(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """私聊总是可以；群里只认白名单。"""
        if self._cfg_int("random_rate", 15) <= 0:
            return False, "随机语音已关闭（random_rate=0）"
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            gid = ""
        if gid:
            allowed = {g.strip() for g in self._cfg_list("random_groups", [])}
            if gid not in allowed:
                return False, f"群 {gid} 不在随机白名单里"
        return True, ""

    def _random_roll(self, event: AstrMessageEvent, speech: str) -> tuple[bool, str]:
        ok, reason = self._random_scope_ok(event)
        if not ok:
            return False, reason
        ok, reason = self._voice_worthy(speech)
        if not ok:
            return False, reason
        umo = event.unified_msg_origin
        gap = self._cfg_int("random_min_gap", 600)
        if gap > 0:
            since = time.time() - self._last_random_at.get(umo, 0.0)
            if since < gap:
                return False, f"距上次随机语音才 {since:.0f}s（下限 {gap}s）"
        rate = self._cfg_int("random_rate", 15)
        if random.uniform(0, 100) >= rate:
            return False, f"这次没掷中（{rate}%）"
        return True, "掷中了"

    # ---------------- 提示词 ----------------
    def _build_hint(self) -> str:
        custom = self._cfg_str("prompt_hint", "")
        if custom:
            return custom.replace("{marker}", self._marker()).replace(
                "{max_chars}", str(self._cfg_int("max_chars", 50))
            )
        return DEFAULT_HINT.format(
            marker=self._marker(), max_chars=self._cfg_int("max_chars", 50)
        )

    @filter.on_llm_request()
    async def inject_voice_hint(self, event: AstrMessageEvent, req) -> None:
        """把「什么时候该用语音」写进系统提示词，让模型按内容判断，而不是靠掷骰子。"""
        if not self._cfg_bool("enable", True) or not self._cfg_bool("inject_prompt", True):
            return
        hint = self._build_hint()
        if not hint:
            return
        req.system_prompt = (req.system_prompt or "") + "\n\n" + hint
        if not getattr(self, "_hint_logged", False):
            self._hint_logged = True
            logger.info(
                f"[{PLUGIN_NAME}] 已把语音规则注入系统提示词（标记 {self._marker()}，"
                f"上限 {self._cfg_int('max_chars', 50)} 字，随机 {self._cfg_int('random_rate', 15)}%）"
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

        # 1) 模型标记
        text, marked = self._strip_markers(raw_text)

        # 标记无论如何都从聊天文本里去掉（即使最后没发语音）
        if marked:
            for comp in result.chain:
                if isinstance(comp, Plain):
                    comp.text = self._strip_markers(comp.text)[0]
            event.set_result(result)

        # 2) 用户点名要语音（模型没写标记时的兜底）
        user_msg = (event.message_str or "").strip()
        asked = any(k in user_msg for k in self._cfg_list("force_keywords", DEFAULT_FORCE))

        # 3) 清洗文本
        speech = self._clean_for_speech(text)
        logger.info(
            f"[{PLUGIN_NAME}] 原文 {len(raw_text)} 字 -> 清洗后 {len(speech)} 字 | 原文首段: {raw_text[:80]!r}"
        )
        if len(speech) < 2:
            logger.info(f"[{PLUGIN_NAME}] 去掉括号/标记后没内容了，本条发文字")
            return
        max_chars = self._cfg_int("max_chars", 50)
        if max_chars > 0 and len(speech) > max_chars:
            logger.info(
                f"[{PLUGIN_NAME}] 要念的文本 {len(speech)} 字 > 上限 {max_chars}，本条发文字：{speech[:30]}"
            )
            return

        # 4) 三条路定来源：用户点名 > 模型标记 > 随机
        if asked:
            source = "用户要求"
        elif marked:
            source = "模型标记"
        else:
            hit, reason = self._random_roll(event, speech)
            if not hit:
                logger.info(f"[{PLUGIN_NAME}] 本条发文字：{reason}")
                return
            source = "随机"

        umo = event.unified_msg_origin
        cooldown = self._cfg_int("cooldown", 0)
        last = self._last_voice_at.get(umo, 0.0)
        if cooldown > 0 and time.time() - last < cooldown:
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

        timeout = self._cfg_int("timeout", 300)
        self._busy.add(umo)
        try:
            logger.info(
                f"[{PLUGIN_NAME}] 合成语音（{len(speech)} 字，来源={source}）：{speech[:50]}"
            )
            t0 = time.time()
            audio_path = await asyncio.wait_for(provider.get_audio(speech), timeout=timeout)
            if not audio_path:
                logger.error(f"[{PLUGIN_NAME}] 合成失败，保留文字")
                return
            keep = [c for c in result.chain if not isinstance(c, Plain)]
            result.chain = keep + [Record(file=audio_path)]
            event.set_result(result)
            self._last_voice_at[umo] = time.time()
            if source == "随机":
                self._last_random_at[umo] = time.time()
            logger.info(f"[{PLUGIN_NAME}] 语音就绪（{time.time() - t0:.1f}s）：{audio_path}")
        except asyncio.TimeoutError:
            logger.error(f"[{PLUGIN_NAME}] 合成超时（>{timeout}s），保留文字")
        except Exception as e:
            logger.error(f"[{PLUGIN_NAME}] 合成异常，保留文字：{e}")
        finally:
            self._busy.discard(umo)
