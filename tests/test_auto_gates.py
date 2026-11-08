"""系统主动语音（模型标记 + 随机）共享同一套约束；点名另算。"""

import pytest

from conftest import Event, FakeProvider, make_plugin, run


def test_marker_with_link_is_not_spoken(tmp_path):
    """模型误加标记时，带链接的内容不该被念出来。"""
    p, _ = make_plugin(tmp_path, max_chars=200, random_rate=0)
    voiced, calls = run(
        p, Event(text="晚安", reply="<语音>参考 https://example.com/docs 这里说得很清楚")
    )
    assert voiced is False
    assert calls == []


def test_marker_with_code_is_kept_as_text(tmp_path):
    """带代码的回复不做主动语音：念掉就等于把命令弄丢了。"""
    p, _ = make_plugin(tmp_path, max_chars=200, random_rate=0)
    voiced, calls = run(p, Event(text="晚安", reply="<语音>跑这个 ```npm i``` 就行"))
    assert voiced is False
    assert calls == []


def test_marker_in_group_outside_whitelist_is_not_spoken(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0, auto_groups=["999"])
    voiced, _ = run(
        p, Event(text="在吗", reply="<语音>在的", gid="123", umo="bot_a:GroupMessage:123")
    )
    assert voiced is False


def test_marker_in_whitelisted_group_is_spoken(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0, auto_groups=["123"])
    voiced, _ = run(
        p, Event(text="在吗", reply="<语音>在的", gid="123", umo="bot_a:GroupMessage:123")
    )
    assert voiced is True


def test_marker_updates_auto_gap_so_random_is_blocked(tmp_path):
    """刚发过一次主动语音，紧接着的随机分支要被间隔挡住。"""
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=100, auto_min_gap=600)
    voiced, _ = run(p, Event(text="晚安", reply="<语音>早点睡吧"))
    assert voiced is True
    umo = Event().unified_msg_origin
    assert p._last_auto_at.get(umo), "模型标记也要记进主动语音时间"

    voiced2, _ = run(p, Event(text="今天好累", reply="那就先别干了，躺一会儿。"))
    assert voiced2 is False


def test_short_marker_reply_can_be_spoken(tmp_path):
    """模型主动说一句“嗯。”是它的判断，不该被字数门槛二次否决。"""
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0)
    voiced, calls = run(p, Event(text="晚安", reply="<语音>嗯。"))
    assert voiced is True
    assert calls == ["嗯。"]


@pytest.mark.parametrize("speech", ["晚安啦", "我在呢", "好的，晚安", "今天先到这吧，早点休息"])
def test_natural_short_replies_pass_content_check(speech):
    p, _ = make_plugin()
    ok, reason = p._voice_worthy(speech)
    assert ok, f"{speech} 被拒了：{reason}"


@pytest.mark.parametrize("speech", ["。", "……", "？？？", "嗯"])
def test_meaningless_fragments_are_rejected(speech):
    p, _ = make_plugin()
    ok, _reason = p._voice_worthy(speech)
    assert ok is False


@pytest.mark.parametrize("speech", ["The quick brown fox jumps", "订单 20261108001234 已发出", "12:30 发车 13:45 到站"])
def test_noise_is_rejected(speech):
    p, _ = make_plugin()
    ok, _reason = p._voice_worthy(speech)
    assert ok is False


@pytest.mark.parametrize("text", ["看这里 https://a.b/c 就行", "```x```", "- 一\n- 二"])
def test_copyable_content_blocks_auto_voice(text):
    p, _ = make_plugin()
    assert p._copyable(text)


def test_random_respects_group_whitelist(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=100, auto_min_gap=0, auto_groups=[])
    voiced, _ = run(
        p, Event(text="在吗", reply="在的，怎么了？", gid="123", umo="bot_a:GroupMessage:123")
    )
    assert voiced is False


def test_auto_timeout_keeps_text(tmp_path):
    """自动语音等太久就先发文字，别让对方干等。"""
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0, auto_timeout=1)
    slow = FakeProvider(delay=1.3)
    voiced, calls = run(p, Event(text="晚安", reply="<语音>早点睡吧。"), provider=slow)
    assert voiced is False
    assert calls == ["早点睡吧。"], "合成是发出去了，只是没等到"


def test_user_requested_voice_uses_long_timeout(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, max_chars_user=2000, auto_timeout=1, timeout=300)
    slow = FakeProvider(delay=1.3)
    voiced, _ = run(p, Event(text="念给我听", reply="早点睡吧。"), provider=slow)
    assert voiced is True, "对方明确要求的，值得多等一会儿"


def test_user_requested_long_text_is_spoken(tmp_path):
    """点名要念的长文本不该被 50 字上限打回文字。"""
    p, _ = make_plugin(tmp_path, max_chars=50, max_chars_user=2000, random_rate=0)
    long_reply = "这段代码的问题在于你用的是旧接口，" * 4
    voiced, calls = run(p, Event(text="念给我听", reply=long_reply))
    assert voiced is True
    assert calls and len(calls[0]) > 50


def test_user_requested_above_hard_ceiling_stays_text(tmp_path):
    """点名也还是有个硬上限，避免一次合成几千字。"""
    p, _ = make_plugin(tmp_path, max_chars=50, max_chars_user=20, random_rate=0)
    voiced, _ = run(p, Event(text="念给我听", reply="这句话明显超过二十个字的硬上限了所以还是会发文字"))
    assert voiced is False


def test_tts_failure_keeps_text(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0)
    voiced, _ = run(
        p, Event(text="晚安", reply="<语音>早点睡吧。"), provider=FakeProvider(result=None)
    )
    assert voiced is False
