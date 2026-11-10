"""工具本身的契约：发得出去、发不出去要说清、别把同一句重复两遍。"""

import asyncio

import pytest

from conftest import (
    Event,
    FakeContext,
    FakeProvider,
    audio_files,
    call_tool,
    make_wav,
    make_plugin,
    sent_text,
)


def test_success_sends_audio(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    ev = Event()
    out = call_tool(p, ev, prov, text="晚安，早点睡。")
    assert audio_files(ev), "没有把音频发出去"
    assert prov.calls == ["晚安，早点睡。"]
    assert "语音" in out


def test_success_tells_model_not_to_repeat(tmp_path):
    """语音已经发了，模型不该再把同一句用文字说一遍。"""
    p, _ = make_plugin()
    ev = Event()
    out = call_tool(p, ev, FakeProvider(path=str(tmp_path / "a.wav")), text="晚安。")
    assert "不要" in out and ("重复" in out or "再说" in out), out


def test_audio_is_its_own_message(tmp_path):
    """语音是独立一条消息，不与文字混在同一条链里。"""
    p, _ = make_plugin()
    ev = Event()
    call_tool(p, ev, FakeProvider(path=str(tmp_path / "a.wav")))
    assert len(ev.sent_chains) == 1
    kinds = [type(c).__name__ for c in ev.sent_chains[0].chain]
    assert kinds == ["Record"], kinds


def test_text_is_stripped_before_synthesis(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    call_tool(p, Event(), prov, text="  晚安。\n")
    assert prov.calls == ["晚安。"]


def test_over_limit_is_rejected_with_numbers(tmp_path):
    """超限不截断，明确告诉模型实际长度和上限。"""
    p, _ = make_plugin(max_chars=10)
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    ev = Event()
    sentence = "这是一句明显超过十个字的话，用来测试长度上限。"
    out = call_tool(p, ev, prov, text=sentence)
    assert not prov.calls, "超限还去合成了"
    assert not audio_files(ev)
    assert str(len(sentence)) in out, out      # 实际长度要如实回给模型
    assert "10" in out, out                    # 上限
    assert "缩短" in out or "短一点" in out, out


def test_limit_is_inclusive(tmp_path):
    """正好等于上限要能发，别把边界算成超限。"""
    p, _ = make_plugin(max_chars=10)
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    ev = Event()
    out = call_tool(p, ev, prov, text="一二三四五六七八九十")
    assert prov.calls, out
    assert audio_files(ev)


@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_empty_text_is_rejected(bad):
    p, _ = make_plugin()
    prov = FakeProvider()
    ev = Event()
    out = call_tool(p, ev, prov, text=bad)
    assert not prov.calls
    assert not audio_files(ev)
    assert out.strip()


def test_disabled_plugin_refuses():
    p, _ = make_plugin(enable=False)
    prov = FakeProvider()
    ev = Event()
    out = call_tool(p, ev, prov)
    assert not prov.calls
    assert "关闭" in out or "未启用" in out, out


def test_group_chat_is_allowed(tmp_path):
    """私聊群聊都能用，群里不需要管理员。"""
    p, _ = make_plugin()
    ev = Event(gid="220001", admin=False)
    call_tool(p, ev, FakeProvider(path=str(tmp_path / "a.wav")))
    assert audio_files(ev)


def test_busy_conversation_refuses_without_synthesizing(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    ev = Event()
    p._busy.add(ev.unified_msg_origin)
    out = call_tool(p, ev, prov)
    assert not prov.calls
    assert not audio_files(ev)
    assert "合成" in out or "稍" in out, out


def test_busy_slot_is_released_after_success(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    call_tool(p, Event(), prov)
    assert not p._busy, "成功之后没释放会话位，后面全被挡住"
    call_tool(p, Event(), prov, text="第二句。")
    assert len(prov.calls) == 2


def test_busy_slot_is_released_after_failure(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(error=RuntimeError("boom"))
    ev = Event()
    call_tool(p, ev, prov)
    assert not p._busy, "失败之后没释放会话位"
    ok = FakeProvider(path=str(tmp_path / "a.wav"))
    ev2 = Event()
    call_tool(p, ev2, ok)
    assert audio_files(ev2)


def test_concurrent_calls_synthesize_once(tmp_path):
    """同一会话并发调用只合成一次：先占位再 await。"""
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "a.wav"), delay=0.05)
    p.context = FakeContext(prov)

    async def both():
        return await asyncio.gather(
            p.tool_send_voice(Event(), "第一句。"),
            p.tool_send_voice(Event(), "第二句。"),
        )

    results = asyncio.run(both())
    assert len(prov.calls) == 1, prov.calls
    assert len(results) == 2
    assert any("合成" in r or "稍" in r for r in results), results


def test_timeout_returns_failure(tmp_path):
    p, _ = make_plugin(timeout=1)
    prov = FakeProvider(path=str(tmp_path / "a.wav"), delay=1.5)
    ev = Event()
    out = call_tool(p, ev, prov)
    assert "超时" in out or "秒" in out, out
    assert not audio_files(ev)


def test_provider_error_is_reported_and_redacted(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(error=RuntimeError("401 unauthorized api_key=abc.def.ghi"))
    ev = Event()
    out = call_tool(p, ev, prov)
    assert "失败" in out, out
    assert "abc" not in out and "def.ghi" not in out, out
    assert not audio_files(ev)


def test_missing_audio_file_is_reported(tmp_path):
    p, _ = make_plugin()
    prov = FakeProvider(path=str(tmp_path / "nope.wav"), auto_create=False)
    ev = Event()
    out = call_tool(p, ev, prov)
    assert not audio_files(ev)
    assert "不存在" in out or "用不了" in out, out


def test_broken_wav_is_reported(tmp_path):
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all" * 50)
    p, _ = make_plugin()
    ev = Event()
    out = call_tool(p, ev, FakeProvider(path=str(bad), auto_create=False))
    assert not audio_files(ev)
    assert "WAV" in out or "用不了" in out, out


def test_send_failure_is_reported_not_raised(tmp_path):
    """发送本身失败也要变成工具结果，不能让异常炸到模型那一层。"""
    p, _ = make_plugin()
    ev = Event(send_error=RuntimeError("平台断线"))
    out = call_tool(p, ev, FakeProvider(path=str(tmp_path / "a.wav")))
    assert out.strip()
    assert "失败" in out or "没有发出去" in out, out
    assert not p._busy


def test_success_returns_no_extra_text_message(tmp_path):
    """工具只发语音，不再额外发一条文字消息（文字由模型自己决定）。"""
    p, _ = make_plugin()
    ev = Event()
    call_tool(p, ev, FakeProvider(path=str(tmp_path / "a.wav")))
    assert sent_text(ev) == ""


def test_long_text_below_limit_is_spoken_whole(tmp_path):
    """50 字以内整句念完，不许私自截断。"""
    p, _ = make_plugin(max_chars=50)
    sentence = "今天下雨了，你带伞了吗？路上慢一点，别急着回我。"
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    call_tool(p, Event(), prov, text=sentence)
    assert prov.calls == [sentence]
