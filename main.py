"""说一句就念给你听 —— 把「这句话用语音说」交给模型，插件只负责可靠地发出去。

唯一入口：模型调用 `send_voice(text)`，插件合成并作为一条独立消息发出音频。

插件的职责只有一件事：**可靠地发出去或者准确地说清为什么没发**。
什么时候该念、念多长，由模型结合上下文判断，工具说明负责引导。

没有插件内开关（宿主禁用插件就会停用工具）、没有随机、没有标记、没有关键词、
不记忆偏好、不改写回复；也不清理旧配置（宿主加载配置时已经删掉 schema 之外的键）。
"""

import asyncio
import re
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_tts_force"

# 设置页直接填数字；这里只是防止手改配置文件写出离谱值时的兜底边界
MAX_CHARS_MIN = 10
MAX_CHARS_MAX = 300
DEFAULT_MAX_CHARS = 30
TIMEOUT_MIN = 5
TIMEOUT_MAX = 600
DEFAULT_TIMEOUT = 120   # 宿主工具调用默认 120 秒预算，插件不能等得比它久
TEST_TEXT_CEIL = 200


@dataclass(frozen=True)
class Spoken:
    """一次「合成 + 发送」的结果。"""

    ok: bool
    why: str = ""
    desc: str = ""
    seconds: float = 0.0
    path: str = ""
    unconfirmed: bool = False   # 发送阶段没拿到结果：可能已经发出去了


@register(
    PLUGIN_NAME,
    "Ray",
    "模型决定这句话用语音说，插件负责合成并可靠地发出去。"
    "闲聊、问候、情绪表达适合简短语音；技术讨论和要复制的内容用文字。",
    "3.3.0",
)
class TTSForcePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._busy: set[str] = set()   # 同一会话同一时间只合成一条

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

    def _max_chars(self) -> int:
        return self._cfg_int(
            "max_chars", DEFAULT_MAX_CHARS, lo=MAX_CHARS_MIN, hi=MAX_CHARS_MAX
        )

    def _timeout(self) -> int:
        return self._cfg_int("timeout", DEFAULT_TIMEOUT, lo=TIMEOUT_MIN, hi=TIMEOUT_MAX)

    def _test_text(self) -> str:
        return self._cfg_str("test_text", "语音测试成功，这是一句示例。")[:TEST_TEXT_CEIL]

    # ------------------------------------------------------------------ 并发
    def _claim(self, conv: str) -> bool:
        """占住会话。必须在第一个 await 之前调用，否则并发调用会各合成一次。"""
        if conv in self._busy:
            return False
        self._busy.add(conv)
        return True

    # ------------------------------------------------------------------ 提供商
    async def _resolve_provider(
        self, event: AstrMessageEvent
    ) -> tuple[object | None, str]:
        """留空＝跟随会话；指定了就必须用指定的那个，坏了如实报错，不偷偷换成别的。"""
        pid = self._cfg_str("provider_id").strip()
        if pid:
            try:
                provider = self.context.get_provider_by_id(pid)
            except Exception as e:
                return None, f"查询配置的语音提供商 {pid} 失败：{_short(e)}"
            if provider is None:
                return None, f"配置的语音提供商 {pid} 不存在，请到插件设置里重新选择"
            if not hasattr(provider, "get_audio"):
                return None, f"配置的提供商 {pid} 不是 TTS（没有 get_audio）"
            return provider, pid

        try:
            fetcher = self.context.get_using_tts_provider_async
            provider = await fetcher(getattr(event, "unified_msg_origin", ""))
        except Exception as e:
            return None, f"取当前会话的 TTS 失败：{_short(e)}"
        if provider is None:
            return None, "当前会话没有配置 TTS 提供商"
        if not hasattr(provider, "get_audio"):
            return None, "当前会话的 TTS 提供商不可用（没有 get_audio）"
        return provider, "会话默认"

    # ------------------------------------------------------------------ 执行
    async def _speak(self, event: AstrMessageEvent, sentence: str) -> Spoken:
        """解析提供商 → 合成 → 校验 → 发送。

        超时只罩在合成外面：发送阶段的超时是另一回事（结果未确认），不能混成「合成超时」。
        调用方必须先用 `_claim()` 占住会话。
        """
        provider, desc = await self._resolve_provider(event)
        if provider is None:
            return Spoken(False, desc)

        timeout = self._timeout()
        started = time.monotonic()
        try:
            audio = await asyncio.wait_for(provider.get_audio(sentence), timeout=timeout)
        except asyncio.TimeoutError:
            return Spoken(
                False,
                f"合成超过 {timeout} 秒还没完成（提供商 {desc}）",
                desc,
                time.monotonic() - started,
            )
        except Exception as e:
            return Spoken(
                False,
                f"合成失败（{_classify_error(e)}）：{_short(e)}（提供商 {desc}）",
                desc,
                time.monotonic() - started,
            )
        seconds = time.monotonic() - started

        if not audio:
            return Spoken(False, f"提供商没有返回音频（提供商 {desc}）", desc, seconds)
        usable, why_not = _audio_ok(audio)
        if not usable:
            return Spoken(False, f"合成结果不可用：{why_not}", desc, seconds)
        path = str(audio)

        try:
            await event.send(MessageChain([Record(file=path)]))
        except asyncio.TimeoutError:
            return Spoken(
                False,
                "发送阶段超时",
                desc,
                seconds,
                path,
                unconfirmed=True,
            )
        except Exception as e:
            return Spoken(
                False,
                f"发送失败（{_classify_error(e)}）：{_short(e)}",
                desc,
                seconds,
                path,
            )
        return Spoken(True, "", desc, seconds, path)

    # ------------------------------------------------------------------ 工具
    @filter.llm_tool(name="send_voice")
    async def tool_send_voice(self, event: AstrMessageEvent, text: str) -> str:
        """将文本合成为语音，并作为一条独立消息发送到当前会话。

        结合用户当前要求、人设和上下文决定是否调用。
        生活闲聊、早晚问候、道谢和情绪表达适合简短语音；
        技术长文、代码、命令、路径、链接，以及需要复制或反复查看的信息优先用文字。
        用户明确要求语音时优先满足，仍需遵守长度限制。

        可以先发送其他文字，再用语音补充一句。准备通过语音说出的内容，
        不要提前用文字重复一遍；调用成功后也无需再用文字复述相同内容。

        调用会立即尝试发送，返回值是发送状态。超出长度上限时会给出实际长度，
        可缩短后重试，避免分段连续发送。其他失败由你结合原因决定后续回复；
        如果返回「发送结果未确认」，不要立即重试，以免重复发送。

        Args:
            text(string): 实际要朗读的简短文本，用自然口语，避免 Markdown、表情符号、代码和动作描写。
        """
        sentence = (text or "").strip()
        if not sentence:
            return "text 是空的，没有可念的内容，所以没有发送。用文字回复或重新准备一句都可以。"

        limit = self._max_chars()
        if len(sentence) > limit:
            return (
                f"这段话 {len(sentence)} 字，超过单次语音上限 {limit} 字，没有发送。"
                f"请缩短到 {limit} 字以内再调用一次；或者直接用文字回复，不要分段连发。"
            )

        conv = getattr(event, "unified_msg_origin", "") or ""
        if not self._claim(conv):
            return "上一条语音还在合成，这条没有发出。可以稍等再调用一次，也可以先用文字回复。"
        try:
            spoken = await self._speak(event, sentence)
        finally:
            self._busy.discard(conv)

        if spoken.ok:
            logger.info(
                f"[{PLUGIN_NAME}] 语音已发送（{spoken.seconds:.1f}s，"
                f"提供商={spoken.desc}）：{sentence[:40]}"
            )
            return (
                "语音已经作为一条独立消息发给用户了。"
                "不要把同一句话再用文字重复一遍；如果还有别的信息要补充，用文字说新的内容。"
            )

        logger.error(f"[{PLUGIN_NAME}] 语音没发出去（提供商={spoken.desc}）：{spoken.why}")
        if spoken.unconfirmed:
            return (
                f"语音发送结果未确认（{spoken.why}），可能已经发出去了。"
                "不要重复调用本工具；如果用户说没收到，再用文字回复。"
            )
        return f"语音没有发出去：{spoken.why}。可以根据这个原因决定接下来用文字回复还是做其他处理。"

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
        if not self._self_test_allowed(event):
            yield event.plain_result("这个命令在群里只给管理员用（私聊里谁都可以测）。")
            return

        conv = getattr(event, "unified_msg_origin", "") or ""
        if not self._claim(conv):
            yield event.plain_result("正在合成别的语音，稍后再测。")
            return

        sample = self._test_text()
        try:
            spoken = await self._speak(event, sample)
        finally:
            self._busy.discard(conv)

        if not spoken.ok:
            yield event.plain_result(f"❌ {spoken.why}")
            return

        path = Path(spoken.path)
        try:
            size_text = f"{path.stat().st_size / 1024:.0f} KB"
        except OSError:
            size_text = "未知"
        yield event.plain_result(
            f"✅ 合成成功（上面那条语音就是试听）\n提供商：{spoken.desc}\n"
            f"耗时：{spoken.seconds:.1f}s\n格式：{path.suffix or '未知'}　大小：{size_text}"
            f"{_duration_hint(path)}\n文本：{sample}"
        )


# ---------------------------------------------------------------------- 工具函数
_SECRET_KEYS = (
    r"api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|token"
    r"|authorization|password|passwd|secret|bearer|密码|令牌|密钥"
)

# 键名 + 分隔符（可带引号）+ 可选的 Bearer/Basic + 值。
# 值允许 + / = 这类 base64 字符；引号与空白都算值的边界。
_SECRET_RX = re.compile(
    rf"(?i)(?P<key>{_SECRET_KEYS})"
    r"(?P<sep>\s*(?:[:=：]|是|为)?\s*[\"']?)"
    r"(?P<scheme>(?:bearer|basic)\s+)?"
    r"(?P<val>[A-Za-z0-9._~+/=\-]{4,})"
    r"(?P<tail>[\x22\x27]?)"
)
_BARE_SK_RX = re.compile(r"(?i)\bsk-[A-Za-z0-9_.\-]{4,}")


def _redact(text: str) -> str:
    """把凭据整段抹掉（统一的脱敏入口）。

    Authorization 头、带引号的值、带 + / = 的 base64 密钥、中文的「密码是」都在这里处理；
    `sk-` 开头的裸密钥单独兜一层。
    """
    masked = _SECRET_RX.sub(lambda m: f"{m.group('key')}=***", text)
    return _BARE_SK_RX.sub("sk-***", masked)


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


def _wav_ok(path: Path) -> tuple[bool, str]:
    """按 RIFF 块走一遍，确认 data 块真的带着它声称的字节数。

    `wave.getnframes()` 读的是头部声明，砍掉一半的文件照样「合法」——所以看实际长度。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return False, f"不是 RIFF/WAVE 头：{path.name}"
            pos = 12
            while pos + 8 <= size:
                fh.seek(pos)
                meta = fh.read(8)
                if len(meta) < 8:
                    break
                chunk_id, chunk_size = struct.unpack("<4sI", meta)
                body = pos + 8
                if chunk_id == b"data":
                    if chunk_size <= 0:
                        return False, f"音频数据块是空的：{path.name}"
                    if body + chunk_size > size:
                        return False, (
                            f"音频数据被截断：{path.name} 声明 {chunk_size} 字节，"
                            f"实际只有 {max(size - body, 0)} 字节"
                        )
                    return True, ""
                pos = body + chunk_size + (chunk_size & 1)   # RIFF 块按偶数字节对齐
            return False, f"没找到音频数据块：{path.name}"
    except Exception as e:
        return False, f"读取 WAV 失败：{path.name}（{_short(e)}）"


def _audio_ok(audio) -> tuple[bool, str]:
    """合成结果能不能真的当语音发出去（存在、非空、wav 的头与数据都对得上）。"""
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
        return _wav_ok(path)
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
