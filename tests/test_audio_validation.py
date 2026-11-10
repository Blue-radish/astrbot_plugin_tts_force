"""合成结果能不能真的当语音发出去：存在、非空、wav 能解析。"""

from conftest import make_wav


def test_valid_wav_passes(tmp_path):
    from conftest import load_mod

    mod = load_mod()
    ok, why = mod._audio_ok(str(make_wav(tmp_path / "a.wav")))
    assert ok, why


def test_missing_file_is_rejected(tmp_path):
    from conftest import load_mod

    mod = load_mod()
    ok, why = mod._audio_ok(str(tmp_path / "nope.wav"))
    assert not ok
    assert "不存在" in why, why


def test_empty_file_is_rejected(tmp_path):
    from conftest import load_mod

    mod = load_mod()
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    ok, why = mod._audio_ok(str(empty))
    assert not ok
    assert "空" in why, why


def test_broken_wav_is_rejected(tmp_path):
    from conftest import load_mod

    mod = load_mod()
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"RIFF" + b"\x00" * 4000)
    ok, why = mod._audio_ok(str(bad))
    assert not ok
    assert "WAV" in why, why


def test_header_only_wav_is_rejected(tmp_path):
    """只有 44 字节头、没有真实音频数据的文件不能算成功。"""
    from conftest import load_mod

    full = make_wav(tmp_path / "full.wav", seconds=0.3).read_bytes()
    head = tmp_path / "head.wav"
    head.write_bytes(full[:44])
    ok, why = load_mod()._audio_ok(str(head))
    assert not ok, "只有头部的 WAV 被当成合法音频了"
    assert "截断" in why or "数据" in why, why


def test_half_truncated_wav_is_rejected(tmp_path):
    from conftest import load_mod

    full = make_wav(tmp_path / "full2.wav", seconds=0.3).read_bytes()
    half = tmp_path / "half.wav"
    half.write_bytes(full[: len(full) // 2])
    ok, why = load_mod()._audio_ok(str(half))
    assert not ok, "被砍掉一半的 WAV 被当成合法音频了"
    assert why


def test_directory_is_rejected(tmp_path):
    from conftest import load_mod

    mod = load_mod()
    ok, why = mod._audio_ok(str(tmp_path))
    assert not ok
    assert why


def test_non_wav_non_empty_is_accepted(tmp_path):
    """mp3/silk 这类我们没法解析的格式，非空就交给宿主去发。"""
    from conftest import load_mod

    mod = load_mod()
    mp3 = tmp_path / "a.mp3"
    mp3.write_bytes(b"ID3" + b"\x00" * 200)
    ok, why = mod._audio_ok(str(mp3))
    assert ok, why


def test_none_is_rejected():
    from conftest import load_mod

    mod = load_mod()
    ok, why = mod._audio_ok(None)
    assert not ok
    assert why
