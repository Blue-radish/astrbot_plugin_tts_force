"""工具说明和失败文案是这套设计的「策略层」，改动它们等于改模型行为，所以钉死几条不许退化的约束。"""

import asyncio

from conftest import Event, FakeProvider, call_tool, make_plugin


def doc():
    from conftest import load_mod

    return load_mod().TTSForcePlugin.tool_send_voice.__doc__ or ""


def test_prompt_leaves_the_decision_to_the_model():
    """场景判断权在模型（人设、上下文、用户要求），不是插件的硬规则。"""
    text = doc()
    assert "结合" in text and "上下文" in text, text
    assert "人设" in text, text


def test_prompt_has_no_absolute_always_call_rule():
    """「用户要求时一定调用」会让功能咨询也被念出来，已删除这种绝对措辞。"""
    text = doc()
    assert "一定调用" not in text, text
    assert "一定" not in text, text


def test_prompt_warns_about_repeating_before_sending():
    """先发文字再转语音时，提醒必须在调用前就给出——成功返回再提醒已经晚了。"""
    text = doc()
    assert "不要提前用文字重复" in text or "避免提前" in text, text


def test_prompt_says_it_sends_immediately_and_returns_status():
    """要分清「生成音频」和「发送音频」：调用即发送，返回值是执行状态。"""
    text = doc()
    assert "立即尝试发送" in text or "立即发送" in text, text
    assert "发送状态" in text, text


def test_prompt_forbids_immediate_retry_when_unconfirmed():
    text = doc()
    assert "未确认" in text and "不要立即重试" in text, text


def test_prompt_keeps_the_args_format_astrbot_needs():
    """AstrBot 从 docstring 的 Args 段生成参数 schema，格式坏了工具就注册不上。"""
    text = doc()
    assert "Args:" in text, text
    assert "text(string):" in text, text


def test_prompt_keeps_length_limit_guidance():
    text = doc()
    assert "长度限制" in text or "长度上限" in text, text
    assert "避免分段" in text, text


def test_failure_result_does_not_order_a_text_reply(tmp_path):
    """失败只给原因，回不回、怎么回由模型决定（这是需求里写明的）。"""
    p, _ = make_plugin()
    ev = Event()

    class Boom(FakeProvider):
        async def get_audio(self, text):
            self.calls.append(text)
            raise RuntimeError("boom")

    out = call_tool(p, ev, Boom(path=str(tmp_path / "a.wav")))
    assert "没有发出去" in out, out
    assert "boom" in out, out
    assert "请改用文字回复" not in out, out
    assert "决定" in out or "可以" in out, out


def test_unconfirmed_result_still_warns_against_retry(tmp_path):
    """唯一保留强指令的地方：发送超时可能已经发出去了，重试会重复。"""
    p, _ = make_plugin()
    ev = Event()

    async def boom(msg):
        raise asyncio.TimeoutError()

    ev.send = boom
    out = call_tool(p, ev, FakeProvider(path=str(tmp_path / "b.wav")))
    assert "未确认" in out, out
    assert "不要重复调用" in out, out


def test_empty_text_result_is_not_a_command(tmp_path):
    p, _ = make_plugin()
    ev = Event()
    prov = FakeProvider(path=str(tmp_path / "c.wav"))
    out = call_tool(p, ev, prov, text="   ")
    assert not prov.calls
    assert "空" in out, out
    assert "请直接" not in out, out
