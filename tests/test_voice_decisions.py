"""说一声就念给你听：三条路（用户点名 / 模型标记 / 随机）与念不念的判断。"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
ASTRBOT_ROOT = Path(r"C:\Users\Ray\astrbot")
for path in (str(PLUGIN_DIR), str(ASTRBOT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_mod():
    spec = importlib.util.spec_from_file_location("tts_force_main", PLUGIN_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_plugin(**attrs):
    mod = load_mod()
    plugin = object.__new__(mod.TTSForcePlugin)
    plugin.config = {}
    plugin._last_voice_at = {}
    plugin._last_random_at = {}
    plugin._busy = set()
    plugin.context = None
    for key, value in attrs.items():
        plugin.config[key] = value
    # 跟 __init__ 一样把清洗正则编出来
    import re

    pats = plugin._cfg_list("strip_patterns", mod.DEFAULT_STRIP)
    plugin._strip_res = [re.compile(x) for x in pats]
    return plugin, mod


class FakeEvent:
    def __init__(self, gid="", umo="bot_a:FriendMessage:1", text=""):
        self._gid = gid
        self.unified_msg_origin = umo
        self.message_str = text

    def get_group_id(self):
        return self._gid


# ---------------------------------------------------------------- 念不念
def test_plain_short_line_is_voice_worthy():
    p, _ = make_plugin()
    ok, reason = p._voice_worthy("累了就早点睡，别硬撑。")
    assert ok, reason


@pytest.mark.parametrize(
    "speech",
    [
        "看这里 https://example.com/abc 就行",
        "跑这个：```bash\nnpm i\n```",
        "- 第一步\n- 第二步\n- 第三步",
        "The quick brown fox jumps over the lazy dog",
        "订单号 2026110800012345 已经发出了",
        "嗯",
    ],
)
def test_noise_is_not_voice_worthy(speech):
    p, _ = make_plugin()
    ok, _reason = p._voice_worthy(speech)
    assert ok is False


def test_mixed_chinese_with_one_english_word_is_fine():
    p, _ = make_plugin()
    ok, reason = p._voice_worthy("那个 GitHub 仓库我看了，写得挺干净")
    assert ok, reason


# ---------------------------------------------------------------- 范围
def test_private_chat_is_allowed_by_default():
    p, _ = make_plugin(random_rate=100)
    ok, reason = p._random_scope_ok(FakeEvent())
    assert ok, reason


def test_group_needs_whitelist():
    p, _ = make_plugin(random_rate=100, random_groups=["111"])
    ok, reason = p._random_scope_ok(FakeEvent(gid="222"))
    assert ok is False and "白名单" in reason
    ok, _reason = p._random_scope_ok(FakeEvent(gid="111"))
    assert ok


def test_random_disabled_when_rate_zero():
    p, _ = make_plugin(random_rate=0)
    ok, reason = p._random_scope_ok(FakeEvent())
    assert ok is False and "关闭" in reason


# ---------------------------------------------------------------- 掷骰子
def test_roll_hits_when_probability_is_full(monkeypatch):
    p, _ = make_plugin(random_rate=100, random_min_gap=0)
    hit, reason = p._random_roll(FakeEvent(), "今天早点睡吧")
    assert hit, reason


def test_roll_misses_when_probability_is_zero(monkeypatch):
    p, _ = make_plugin(random_rate=0, random_min_gap=0)
    hit, _reason = p._random_roll(FakeEvent(), "今天早点睡吧")
    assert hit is False


def test_roll_respects_probability(monkeypatch):
    p, _ = make_plugin(random_rate=15, random_min_gap=0)
    rolls = iter([0.0, 50.0, 99.0])
    monkeypatch.setattr(random, "uniform", lambda a, b: next(rolls))
    assert p._random_roll(FakeEvent(), "今天早点睡吧")[0] is True
    assert p._random_roll(FakeEvent(), "今天早点睡吧")[0] is False
    assert p._random_roll(FakeEvent(), "今天早点睡吧")[0] is False


def test_random_gap_blocks_second_voice():
    p, _ = make_plugin(random_rate=100, random_min_gap=600)
    ev = FakeEvent()
    assert p._random_roll(ev, "今天早点睡吧")[0] is True
    p._last_random_at[ev.unified_msg_origin] = __import__("time").time()
    hit, reason = p._random_roll(ev, "今天早点睡吧")
    assert hit is False and "下限" in reason


def test_noise_text_never_gets_rolled_even_at_full_rate():
    p, _ = make_plugin(random_rate=100, random_min_gap=0)
    hit, reason = p._random_roll(FakeEvent(), "看 https://example.com 这个")
    assert hit is False and "链接" in reason


# ---------------------------------------------------------------- 提示词
def test_hint_mentions_marker_and_length_limit():
    p, _ = make_plugin(max_chars=40)
    hint = p._build_hint()
    assert "<语音>" in hint
    assert "40" in hint
    assert "别用语音" in hint


def test_custom_hint_overrides_default():
    p, _ = make_plugin(prompt_hint="只说 {marker}，最多 {max_chars} 字", max_chars=33)
    assert p._build_hint() == "只说 <语音>，最多 33 字"


def test_marker_can_be_customised():
    p, _ = make_plugin(voice_markers=["[VOICE]"])
    assert p._marker() == "[VOICE]"
    assert "[VOICE]" in p._build_hint()


# ---------------------------------------------------------------- 标记清洗
def test_marker_is_removed_from_text():
    p, _ = make_plugin()
    text, hit = p._strip_markers("<语音>早点睡吧")
    assert hit is True and text == "早点睡吧"


def test_parenthetical_actions_are_not_spoken():
    p, _ = make_plugin()
    assert "低下头" not in p._clean_for_speech("（她低下头）早点睡吧")


def test_code_block_is_not_spoken():
    p, _ = make_plugin()
    cleaned = p._clean_for_speech("看这个 ```bash\nnpm i\n``` 就好")
    assert "npm i" not in cleaned


# ---------------------------------------------------------------- 整条链路
class FakeResult:
    def __init__(self, text):
        from astrbot.api.message_components import Plain

        self.chain = [Plain(text=text)]

    def is_llm_result(self):
        return True


class FakeProvider:
    def __init__(self, path):
        self.path = path
        self.calls = []

    async def get_audio(self, text):
        self.calls.append(text)
        return self.path


class FakeContext:
    def __init__(self, provider):
        self._provider = provider

    def get_provider_by_id(self, pid):
        return self._provider


class FullEvent(FakeEvent):
    def __init__(self, text, reply, gid="", umo="bot_a:FriendMessage:1"):
        super().__init__(gid=gid, umo=umo, text=text)
        self.result = FakeResult(reply)

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result


def _voice_plugin(tmp_path, provider, **attrs):
    p, mod = make_plugin(**attrs)
    p.context = FakeContext(provider)
    plugin = p

    async def get_using_tts_provider_async(umo):
        return provider

    p.context.get_using_tts_provider_async = get_using_tts_provider_async
    return p


def _texts(result):
    from astrbot.api.message_components import Plain

    return [c.text for c in result.chain if isinstance(c, Plain)]


def _has_record(result):
    from astrbot.api.message_components import Record

    return any(isinstance(c, Record) for c in result.chain)


def test_marker_turns_reply_into_voice(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(tmp_path, provider, max_chars=50, random_rate=0)
    ev = FullEvent(text="晚安", reply="<语音>早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert _has_record(ev.result)
    assert _texts(ev.result) == [], "发语音时文字应该被替换掉"
    assert provider.calls == ["早点睡吧。"]


def test_user_keyword_forces_voice_without_marker(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(tmp_path, provider, max_chars=50, random_rate=0)
    ev = FullEvent(text="念给我听", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert _has_record(ev.result)


def test_random_hit_turns_reply_into_voice(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(tmp_path, provider, max_chars=50, random_rate=100, random_min_gap=0)
    ev = FullEvent(text="今天好累", reply="那就先别干了，躺一会儿。")
    asyncio.run(p.maybe_tts(ev))
    assert _has_record(ev.result)


def test_random_miss_leaves_text_alone(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(tmp_path, provider, max_chars=50, random_rate=0)
    ev = FullEvent(text="今天好累", reply="那就先别干了，躺一会儿。")
    asyncio.run(p.maybe_tts(ev))
    assert not _has_record(ev.result)
    assert _texts(ev.result) == ["那就先别干了，躺一会儿。"]
    assert provider.calls == []


def test_long_reply_is_not_spoken_even_if_marked(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(tmp_path, provider, max_chars=10, random_rate=0)
    ev = FullEvent(text="晚安", reply="<语音>这句话明显超过十个字了")
    asyncio.run(p.maybe_tts(ev))
    assert not _has_record(ev.result)
    assert provider.calls == []
    assert "<语音>" not in "".join(_texts(ev.result)), "标记要顺手删掉"


def test_group_not_in_whitelist_stays_text(tmp_path):
    import asyncio

    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    provider = FakeProvider(str(wav))
    p = _voice_plugin(
        tmp_path, provider, max_chars=50, random_rate=100, random_min_gap=0, random_groups=["999"]
    )
    ev = FullEvent(text="在吗", reply="在的。", gid="123", umo="bot_a:GroupMessage:123")
    asyncio.run(p.maybe_tts(ev))
    assert not _has_record(ev.result)


def test_tts_failure_keeps_text(tmp_path):
    import asyncio

    class Broken:
        async def get_audio(self, text):
            return None

    p = _voice_plugin(tmp_path, Broken(), max_chars=50, random_rate=0)
    ev = FullEvent(text="晚安", reply="<语音>早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert _has_record(ev.result) is False
    assert _texts(ev.result) == ["早点睡吧。"]
