"""/语音测试：不经模型，直接把一句固定话合成出来发给你。

这条命令的存在理由是**隔离故障**：声音没出来时，能分清是 TTS 服务坏了，还是模型没调用工具。
"""

import asyncio

from conftest import Event, FakeProvider, audio_files, make_wav, make_plugin


def run_cmd(p, ev, provider=None):
    if provider is not None:
        import conftest

        p.context = conftest.FakeContext(provider)

    async def collect():
        return [m async for m in p.tts_self_test(ev)]

    return asyncio.run(collect())


def test_success_sends_audio_and_reports(tmp_path):
    p, _ = make_plugin(provider_id="qwen3-tts", test_text="测试一下")
    ev = Event()
    out = run_cmd(p, ev, FakeProvider(path=str(make_wav(tmp_path / "ok.wav"))))
    assert audio_files(ev), "自检没把音频发回来，用户没法试听"
    assert "✅" in str(out[0]), out
    assert "qwen3-tts" in str(out[0]), out


def test_report_includes_size_and_duration(tmp_path):
    p, _ = make_plugin(test_text="测试一下")
    out = run_cmd(p, Event(), FakeProvider(path=str(make_wav(tmp_path / "ok.wav"))))
    text = str(out[0])
    assert "KB" in text, text
    assert "时长" in text, text


def test_invalid_audio_is_reported_as_failure(tmp_path):
    p, _ = make_plugin()
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio" * 50)
    ev = Event()
    out = run_cmd(p, ev, FakeProvider(path=str(bad), auto_create=False))
    assert "❌" in str(out[0]), out
    assert not audio_files(ev), "坏音频不该发出去"


def test_group_member_without_admin_is_refused(tmp_path):
    p, _ = make_plugin()
    ev = Event(gid="220001", admin=False)
    out = run_cmd(p, ev, FakeProvider(path=str(make_wav(tmp_path / "ok.wav"))))
    assert "管理员" in str(out[0]), out
    assert not audio_files(ev)


def test_group_admin_can_run_it(tmp_path):
    p, _ = make_plugin()
    ev = Event(gid="220001", admin=True)
    out = run_cmd(p, ev, FakeProvider(path=str(make_wav(tmp_path / "ok.wav"))))
    assert "✅" in str(out[0]), out
    assert audio_files(ev)


def test_busy_conversation_refuses(tmp_path):
    p, _ = make_plugin()
    ev = Event()
    p._busy.add(ev.unified_msg_origin)
    out = run_cmd(p, ev, FakeProvider(path=str(make_wav(tmp_path / "ok.wav"))))
    assert "合成" in str(out[0]) or "稍" in str(out[0]), out
    assert not audio_files(ev)


def test_test_text_is_truncated(tmp_path):
    p, _ = make_plugin(test_text="啊" * 500)
    assert len(p._test_text()) <= 200


def test_self_test_and_tool_do_not_synthesize_twice(tmp_path):
    """自检和工具共用一套占位/释放：并发时只合成一次。"""
    import conftest

    p, _ = make_plugin()
    prov = FakeProvider(path=str(make_wav(tmp_path / "ok.wav")), delay=0.05)
    p.context = conftest.FakeContext(prov)

    # 解析提供商慢一点，才能造出「自检已进入流程、工具又进来」的窗口
    original = p._resolve_provider

    async def slow(ev):
        await asyncio.sleep(0.02)
        return await original(ev)

    p._resolve_provider = slow

    async def both():
        async def as_cmd():
            return [m async for m in p.tts_self_test(Event())]

        return await asyncio.gather(as_cmd(), p.tool_send_voice(Event(), "同时来一条。"))

    asyncio.run(both())
    assert len(prov.calls) == 1, prov.calls
    assert not p._busy


def test_missing_provider_is_reported():
    p, _ = make_plugin()
    import conftest

    p.context = conftest.FakeContext(None, session_error=RuntimeError("没有可用的 TTS"))
    ev = Event()
    out = run_cmd(p, ev)
    assert "❌" in str(out[0]), out
