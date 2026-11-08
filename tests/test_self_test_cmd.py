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


def test_success_reports_provider_time_and_size(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts", test_text="测试一下")
    wav = tmp_path / "ok.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 4000)
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(wav)))
    assert "✅" in out[0]
    assert "qwen3-tts" in out[0]
    assert "KB" in out[0]


def test_missing_audio_file_is_reported(tmp_path):
    p, _ = make_plugin(tmp_path, provider_id="qwen3-tts")
    ev = Event()
    out = call_cmd(p, ev, FakeProvider(path=str(tmp_path / "nope.wav")))
    assert "❌" in out[0]
    assert "不是可用音频" in out[0]


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
