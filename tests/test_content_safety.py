"""内容不丢：可复制信息（代码/链接/清单/参数）不能因为转语音而消失。"""

import pytest

from conftest import Event, FakeProvider, make_plugin, run, texts

COPYABLE = [
    "运行这个命令：```npm i```，然后重试。",
    "改一下 `package.json` 里的版本号。",
    "参考 https://example.com/docs 这里写得很清楚。",
    "照着做：\n- 先装依赖\n- 再跑测试",
    "订单号 20261108001234 已经发出了。",
]


@pytest.mark.parametrize("reply", COPYABLE)
def test_auto_voice_never_drops_copyable_content(tmp_path, reply):
    """主动语音遇到可复制信息：整条留文字，一个字都不能丢。"""
    p, _ = make_plugin(tmp_path, max_chars=200, random_rate=100, auto_min_gap=0)
    ev = Event(text="今天好累", reply=f"<语音>{reply}")
    voiced, calls = run(p, ev)
    assert voiced is False
    assert calls == []
    assert "".join(texts(ev.result)).strip(), "文字必须保留"


def test_explicit_request_keeps_text_alongside_voice(tmp_path):
    """点名要听、但内容里有一行命令：语音发出去，文字也留着。"""
    p, _ = make_plugin(tmp_path, max_chars=200, max_chars_user=2000, random_rate=0)
    reply = "运行这个命令：```npm i```，然后重试。"
    ev = Event(text="念给我听", reply=reply)
    voiced, calls = run(p, ev)
    assert voiced is True, "对方点名的还是要念"
    assert calls, "应该真的合成"
    assert "npm i" in "".join(texts(ev.result)), "命令必须还在文字里"


def test_explicit_request_without_copyable_still_replaces_text(tmp_path):
    """没有可复制信息时维持原行为：语音替掉文字，不多发一条。"""
    p, _ = make_plugin(tmp_path, max_chars=200, max_chars_user=2000, random_rate=0)
    ev = Event(text="念给我听", reply="早点睡吧，明天再说。")
    voiced, _ = run(p, ev)
    assert voiced is True
    assert texts(ev.result) == [], "纯口语内容可以直接替掉文字"


def test_copyable_detection_covers_the_usual_cases():
    p, _ = make_plugin()
    for reply in COPYABLE:
        assert p._copyable(reply), reply


@pytest.mark.parametrize("reply", ["早点睡吧。", "（她低下头）今天辛苦了。", "好呀，那你早点休息。"])
def test_plain_chat_is_not_copyable(reply):
    p, _ = make_plugin()
    assert p._copyable(reply) == ""
