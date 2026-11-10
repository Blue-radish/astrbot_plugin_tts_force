"""说一句就念给你听 —— 把「这句话用语音说」交给模型，插件只负责可靠地发出去。

设计只有一个入口：模型调用 `send_voice(text)`，插件合成并**直接发送**音频。

- 什么时候该念、念多长，由模型结合上下文判断（工具说明负责引导）
- 插件负责的部分要绝对可靠：长度上限、超时预算、提供商解析、音频校验、
  同会话不并发合成、失败原因如实回给模型
- 语音一旦发出去，工具结果会提醒模型「别再用文字重复同一句」

没有随机概率、没有语音标记、没有关键词表、没有场景正则、不记忆长期偏好，
也不改写模型已经生成的回复。模型选表达方式，插件保证发送。
"""

import asyncio
import re
import time
import wave
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_tts_force"

DEFAULT_MAX_CHARS = 50
DEFAULT_TIMEOUT = 300
MAX_CHARS_CEIL = 10000
TIMEOUT_CEIL = 3600
TEST_TEXT_CEIL = 200

# 3.0 删掉的机制留下的键。升级时连值一起清掉，否则用户会以为它们还有效。
LEGACY_KEYS = (
    "random_rate",
    "random_min_gap",
    "random_groups",
    "auto_enable",
    "auto_min_gap",
    "auto_groups",
    "voice_mode_rate",
    "voice_mode_min_gap",
    "text_mode_ttl",
    "max_chars_user",
    "auto_timeout",
    "cooldown",
    "inject_prompt",
    "prompt_hint",
    "voice_markers",
    "force_keywords",
    "strip_patterns",
    "strip_markdown",
)


@register(
    PLUGIN_NAME,
    "Ray",
    "模型决定这句话用语音说，插件负责合成并可靠地发出去。"
    "闲聊、问候、情绪表达适合简短语音；技术讨论和要复制的内容用文字。",
    "3.0.0",
)
class TTSForcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._busy: set[str] = set()   # 同一会话同一时间只合成一条
        try:
            self._prune_legacy_config()
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 旧配置清理跳过：{_short(e)}")

    # ------------------------------------------------------------------ 配置
    def _cfg_int(
        self, key: str, default: int, *, lo: int | None = None, hi: int | None = None
    ) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if lo is not None and value < lo:
            value = lo
        if hi is not None and value > hi:
            value = hi
        return value

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return str(value) if value is not None else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    def _enable(self) -> bool:
        return self._cfg_bool("enable", True)

    def _max_chars(self) -> int:
        return self._cfg_int("max_chars", DEFAULT_MAX_CHARS, lo=1, hi=MAX_CHARS_CEIL)

    def _timeout(self) -> int:
        return self._cfg_int("timeout", DEFAULT_TIMEOUT, lo=1, hi=TIMEOUT_CEIL)

    def _test_text(self) -> str:
        return self._cfg_str("test_text", "语音测试成功，这是一句示例。")[:TEST_TEXT_CEIL]

    def _prune_legacy_config(self) -> None:
        """3.0 删掉的键，从配置文件里也删掉——留着只会让人以为它们还有效。"""
        removed = [key for key in LEGACY_KEYS if key in self.config]
        if not removed:
            return
        for key in removed:
            self.config.pop(key, None)
        logger.info(f"[{PLUGIN_NAME}] 清理已移除的配置项：{'、'.join(removed)}")
        saver = getattr(self.config, "save_config", None)
        if not callable(saver):
            return
        try:
            saver()
            logger.info(f"[{PLUGIN_NAME}] 旧配置已从配置文件删除")
        except Exception as e:
            logger.warning(
                f"[{PLUGIN_NAME}] 旧配置删除后保存失败，下次启动再试：{_short(e)}"
            )

    # ------------------------------------------------------------------ 提供商
    async def _resolve_provider(self, event: AstrMessageEvent) -> tuple[object | None, str]:
        """配置里指定了就用它；留空或指定错了，就用当前会话的 TTS。"""
        pid = self._cfg_str("provider_id").strip()
        fallback_note = ""
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception as e:
                logger.warning(f"[{PLUGIN_NAME}] get_provider_by_id({pid}) 失败：{_short(e)}")
                provider = None
            if provider is not None:
                return provider, pid
            logger.warning(f"[{PLUGIN_NAME}] 配置的提供商 {pid} 不存在，改用当前会话的 TTS")
            fallback_note = f"配置的 {pid} 不存在，已回退到会话默认"
        try:
            fetcher = self.context.get_using_tts_provider_async
            umo = getattr(event, "unified_msg_origin", "")
            provider = await fetcher(umo)
            return provider, (fallback_note or f"会话默认（{pid or '未指定'}）")
        except Exception as e:
            return None, f"按会话取 TTS 失败：{_short(e)}"

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool(name="send_voice")
    async def tool_send_voice(self, event: AstrMessageEvent, text: str) -> str:
        """把一段话合成语音，直接发给用户（独立一条消息，用户马上能听见）。

        什么时候用：生活闲聊、早晚问候、道谢或道歉、情绪表达、哄人、叫对方早点休息、
        一句短回应——这些内容听起来比读起来舒服。可以先用文字说一句，再调用本工具补一段语音。

        什么时候不用：技术讨论、长篇解释、代码、命令、路径、链接、清单、数字，
        以及任何需要用户复制或回看的内容，一律用文字。

        用户明确要求时（「念给我听」「用语音说」「语音回我」）一定要调用本工具。
        一次只发一句：口语、自然，不要带 markdown、表情符号、代码或括号里的动作说明。
        超过字数上限会被拒绝，那时缩短再调用一次，不要分段连发。

        Args:
            text(string): 要念出来的话，纯口语句子，不含 markdown、表情、代码和括号动作说明
        """
        if not self._enable():
            return "语音功能已关闭（配置里 enable=false），这次没有发送。请直接用文字回复。"

        sentence = (text or "").strip()
        if not sentence:
            return "text 是空的，没有可念的内容。请直接用文字回复。"

        limit = self._max_chars()
        if len(sentence) > limit:
            return (
                f"这段话 {len(sentence)} 字，超过单次语音上限 {limit} 字，没有发送。"
                f"请缩短到 {limit} 字以内再调用一次；或者直接用文字回复，不要分段连发。"
            )

        conv = getattr(event, "unified_msg_origin", "") or ""
        if conv in self._busy:
            return "上一条语音还在合成，这条没有发出。稍等再调用一次，或者先用文字回复。"

        # 先占位再 await：否则两条并发调用都能通过上面的检查，各合成一次
        self._busy.add(conv)
        timeout = self._timeout()
        t0 = time.time()
        try:
            provider, desc = await self._resolve_provider(event)
            if provider is None or not hasattr(provider, "get_audio"):
                return f"没拿到可用的语音提供商：{desc}。请改用文字回复。"

            logger.info(
                f"[{PLUGIN_NAME}] 合成语音（{len(sentence)} 字，提供商={desc}，"
                f"超时 {timeout}s）：{sentence[:40]}"
            )
            audio = await asyncio.wait_for(provider.get_audio(sentence), timeout=timeout)
            if not audio:
                return "提供商没有返回音频。请改用文字回复。"

            usable, why_not = _audio_ok(audio)
            if not usable:
                logger.error(f"[{PLUGIN_NAME}] 合成结果不能用：{why_not}")
                return f"合成结果是不可用的音频（{why_not}），没有发送。请改用文字回复。"

            await event.send(MessageChain([Record(file=str(audio))]))
            logger.info(f"[{PLUGIN_NAME}] 语音已发送（{time.time() - t0:.1f}s）：{audio}")
            # 配置错了不该只在日志里：顺带告诉模型，它可以提醒用户去检查
            note = f"（另外：{desc}，可以提醒用户检查语音提供商配置。）" if "已回退" in desc else ""
            return (
                "语音已经发给用户了（这是一条独立消息，用户能听见这句话）。"
                "不要把同一句话再用文字重复一遍；如果还有别的信息要补充，用文字说新的内容。"
                + note
            )
        except asyncio.TimeoutError:
            logger.warning(f"[{PLUGIN_NAME}] 合成超过 {timeout}s，这条语音没发出去")
            return (
                f"合成超过 {timeout} 秒还没完成，这条语音没有发送。"
                "可以改用文字回复，或者把句子缩短后再试。"
            )
        except Exception as e:
            logger.error(
                f"[{PLUGIN_NAME}] 合成/发送失败（{_classify_error(e)}）：{_short(e)}"
            )
            return f"语音没有发出去（{_classify_error(e)}）：{_short(e)}。请改用文字回复。"
        finally:
            self._busy.discard(conv)

    # ------------------------------------------------------------------ 自检
    def _self_test_allowed(self, event: AstrMessageEvent) -> bool:
        """私聊谁都能测；群里只给管理员，免得谁都能拿它烧额度。"""
        try:
            if not event.get_group_id():
                return True
        except Exception:
            return True
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    @filter.command("语音测试")
    async def tts_self_test(self, event: AstrMessageEvent):
        """不经模型，直接把一句固定话合成发给你：分清是 TTS 坏了还是模型没调用工具。"""
        if not self._enable():
            yield event.plain_result("插件已关闭（enable=false），先在配置里启用再测。")
            return
        if not self._self_test_allowed(event):
            yield event.plain_result("这个命令在群里只给管理员用（私聊里谁都可以测）。")
            return

        conv = getattr(event, "unified_msg_origin", "") or ""
        if conv in self._busy:
            yield event.plain_result("正在合成别的语音，稍后再测。")
            return

        provider, desc = await self._resolve_provider(event)
        if provider is None or not hasattr(provider, "get_audio"):
            yield event.plain_result(f"❌ 没拿到语音提供商：{desc}")
            return

        sample = self._test_text()
        timeout = self._timeout()
        self._busy.add(conv)
        t0 = time.time()
        try:
            audio = await asyncio.wait_for(provider.get_audio(sample), timeout=timeout)
        except asyncio.TimeoutError:
            yield event.plain_result(f"❌ 合成超时（>{timeout}s），提供商 {desc}")
            return
        except Exception as e:
            yield event.plain_result(
                f"❌ 合成失败：{_classify_error(e)}\n提供商 {desc}\n{_short(e)}"
            )
            return
        finally:
            self._busy.discard(conv)

        elapsed = time.time() - t0
        if not audio:
            yield event.plain_result(f"❌ 提供商没返回音频（提供商 {desc}，{elapsed:.1f}s）")
            return
        usable, why_not = _audio_ok(audio)
        if not usable:
            yield event.plain_result(f"❌ 收到文件但用不了：{why_not}（提供商 {desc}）")
            return

        path = Path(str(audio))
        try:
            await event.send(MessageChain([Record(file=str(path))]))
        except Exception as e:
            yield event.plain_result(f"❌ 合成成功但发送失败：{_short(e)}")
            return

        yield event.plain_result(
            f"✅ 合成成功（下面这条语音就是试听）\n提供商：{desc}\n耗时：{elapsed:.1f}s\n"
            f"格式：{path.suffix or '未知'}　大小：{path.stat().st_size / 1024:.0f} KB"
            f"{_duration_hint(path)}\n文本：{sample}"
        )


# ---------------------------------------------------------------------- 工具函数
_SECRET_RX = re.compile(
    r"(?i)("
    r"sk-[A-Za-z0-9_.\-]{4,}"
    r"|(?:api[_-]?key|apikey|access[_-]?token|token|authorization|password|passwd|secret|bearer)"
    r"[\"']?\s*[=:]?\s*[\"']?[A-Za-z0-9_.\-]{4,}"
    r")"
)


def _redact(text: str) -> str:
    """把可能的密钥整段抹掉：点号、下划线一起吃，否则会留下 `.def` 这种尾巴。"""

    def _sub(m: re.Match) -> str:
        token = m.group(1)
        if token.lower().startswith("sk-"):
            return "sk-***"
        head = re.split(r"[=:\s]", token, maxsplit=1)[0]
        return f"{head}=***"

    return _SECRET_RX.sub(_sub, text)


def _short(e: Exception, limit: int = 200) -> str:
    """错误信息脱敏再截断：工具结果会回给模型，别把 key 带出去。"""
    text = re.sub(r"\s+", " ", _redact(str(e))).strip()
    return text[:limit]


def _classify_error(e: Exception) -> str:
    text = str(e).lower()
    if any(k in text for k in ("401", "403", "unauthorized", "invalid api key", "auth")):
        return "鉴权失败"
    if any(k in text for k in ("voice", "音色", "model not found", "invalid model")):
        return "模型或音色无效"
    if any(k in text for k in ("timeout", "timed out", "超时")):
        return "请求超时"
    if any(k in text for k in ("connect", "refused", "dns", "resolve", "unreachable")):
        return "连不上接口地址"
    return "其他错误"


def _audio_ok(audio) -> tuple[bool, str]:
    """合成结果能不能真的当语音发出去（存在、非空、wav 可解析）。"""
    if not audio:
        return False, "没有返回音频路径"
    try:
        path = Path(str(audio))
    except Exception as e:
        return False, f"返回的不是文件路径（{_short(e)}）"
    if not path.is_file():
        return False, f"音频文件不存在：{path.name}"
    try:
        size = path.stat().st_size
    except OSError as e:
        return False, f"读不到音频文件：{path.name}（{_short(e)}）"
    if size <= 0:
        return False, f"音频文件是空的：{path.name}"
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                if wf.getnframes() <= 0 or wf.getframerate() <= 0:
                    return False, f"WAV 里没有音频帧：{path.name}"
        except Exception:
            return False, f"不是可解析的 WAV：{path.name}"
    return True, ""


def _duration_hint(path: Path) -> str:
    """能读出时长就报一下：读不出来说明文件本身可疑。"""
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            return f"　时长：{frames / rate:.1f}s"
    except Exception:
        return ""
