"""合成结果要真的能用（存在、非空、WAV 可解析），否则保留文字。"""

import asyncio
import wave

from conftest import Event, FakeProvider, FakeContext, make_plugin, texts


def _make_wav(path, seconds=0.2):
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * int(8000 * seconds))


def _voiced(result):
    from astrbot.api.message_components import Record

    return any(isinstance(c, Record) for c in result.chain)


def test_missing_audio_file_keeps_text(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars_user=2000)
    p.context = FakeContext(
        FakeProvider(path=str(tmp_path / "没有这个文件.wav"), auto_create=False)
    )
    ev = Event(text="念给我听", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert not _voiced(ev.result), "文件不存在就不该换成语音"
    assert "早点睡吧。" in "".join(texts(ev.result))


def test_empty_audio_file_keeps_text(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars_user=2000)
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    p.context = FakeContext(FakeProvider(path=str(empty)))
    ev = Event(text="念给我听", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert not _voiced(ev.result)
    assert "早点睡吧。" in "".join(texts(ev.result))


def test_broken_wav_keeps_text(tmp_path):
    """扩展名是 wav 但内容不是音频：宿主发不出去，别把文字换掉。"""
    p, _ = make_plugin(tmp_path, max_chars_user=2000)
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio at all" * 20)
    p.context = FakeContext(FakeProvider(path=str(broken)))
    ev = Event(text="念给我听", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert not _voiced(ev.result)
    assert "早点睡吧。" in "".join(texts(ev.result))


def test_valid_audio_replaces_text(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars_user=2000)
    good = tmp_path / "good.wav"
    _make_wav(good)
    p.context = FakeContext(FakeProvider(path=str(good)))
    ev = Event(text="念给我听", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert _voiced(ev.result)
