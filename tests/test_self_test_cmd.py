"""/语音测试：真合一次，并按失败类型给出能指导操作的提示。"""

import asyncio

import pytest

from conftest import Event, FakeProvider, make_plugin


def call_cmd(p, event, provider):
    from conftest import FakeContext

    p.context = FakeContext(provider)

    async def collect():
        return [m async for m in p.tts_self_test(event)]

    return asyncio.run(collect())


def _real_wav(path, seconds=0.2):
    import wave

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def test_success_reports_provider_time_and_size(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts", test_text="测试一下")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "✅" in out[0]
    assert "qwen3-tts" in out[0]
    assert "KB" in out[0]
    assert "时长" in out[0], "能算出时长就说明文件真的是音频"


def test_success_returns_playable_audio(tmp_path):
    """自检要能真听见，不能只回一段文字。"""
    from astrbot.api.message_components import Record

    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event()
    call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert ev.sent_chain, "没把音频发回去，用户没法试听"
    chain = ev.sent_chain[-1]
    assert any(isinstance(c, Record) for c in chain), "发回去的链里要有语音"
    assert any(getattr(c, "text", "") .find("✅") >= 0 for c in chain), "说明文字也要带着"


def test_invalid_wav_is_reported_as_failure(tmp_path):
    """非空但不是音频的文件不能算成功（审计复现的那条）。"""
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all" * 50)
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(bad)))
    assert "❌" in out[0], out[0]
    assert not ev.sent_chain, "坏的音频不该发出去"


def test_disabled_plugin_refuses_self_test(tmp_path):
    p, _ = make_plugin(tmp_path, enable=False, provider_id="qwen3-tts")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "关闭" in out[0] or "未启用" in out[0], out[0]


def test_group_member_without_admin_is_refused(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event(gid="220001", admin=False)
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "管理员" in out[0], out[0]


def test_group_admin_can_run_it(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event(gid="220001", admin=True)
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "✅" in out[0], out[0]


def test_busy_conversation_refuses_self_test(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event()
    p._busy.add(ev.unified_msg_origin)
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "正在" in out[0] or "稍后" in out[0], out[0]


def test_test_text_is_truncated(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts", test_text="啊" * 500)
    wav = _real_wav(tmp_path / "ok.wav")
    ev = Event()
    call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert len(p._self_test_sample()) <= 200


def test_missing_audio_file_is_reported(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(tmp_path / "nope.wav"), auto_create=False))
    assert "❌" in out[0]
    assert "用不了" in out[0] or "不存在" in out[0]


def test_empty_result_is_reported(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(result=None))
    assert "❌" in out[0]
    assert "没返回音频" in out[0]


@pytest.mark.parametrize(
    "err, expect",
    [
        (RuntimeError("401 Unauthorized"), "鉴权失败"),
        (RuntimeError("invalid model: qwen3-tts-x"), "模型或音色无效"),
        (RuntimeError("Connection refused"), "连不上接口地址"),
        (RuntimeError("something odd"), "未知错误"),
    ],
)
def test_errors_are_classified(tmp_path, err, expect):
    class Boom:
        async def get_audio(self, text):
            raise err

    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    ev = Event()
    out = call_cmd(p, ev, Boom())
    assert "❌" in out[0]
    assert expect in out[0]


def test_timeout_is_reported(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts", timeout=1)
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(delay=1.5))
    assert "超时" in out[0]


def test_keys_are_masked_in_error_text():
    _mod = make_plugin()[1]

    msg = _mod._short(RuntimeError("auth failed with key=sk-abcdefghijklmnop"))
    assert "sk-abcdefghijklmnop" not in msg


def test_no_provider_is_reported(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")

    class NoProvider:
        def get_provider_by_id(self, pid):
            return None

        async def get_using_tts_provider_async(self, umo):
            return None

    p.context = NoProvider()
    ev = Event()

    async def collect():
        return [m async for m in p.tts_self_test(ev)]

    out = asyncio.run(collect())
    assert "❌" in out[0]
