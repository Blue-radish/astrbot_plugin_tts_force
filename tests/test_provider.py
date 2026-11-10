"""提供商解析：留空跟随会话、指定就按指定、找不到要说出回退。"""

from conftest import Event, FakeContext, FakeProvider, audio_files, call_tool, make_plugin


def test_configured_provider_is_used(tmp_path):
    p, _ = make_plugin(provider_id="qwen3-tts")
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    p.context = FakeContext(prov)
    ev = Event()
    call_tool(p, ev, prov)
    assert p.context.asked_ids == ["qwen3-tts"], p.context.asked_ids
    assert audio_files(ev)


def test_blank_provider_follows_the_session(tmp_path):
    """留空＝用当前会话的 TTS，不去查 id。"""
    p, _ = make_plugin(provider_id="")
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    p.context = FakeContext(prov)
    ev = Event()
    call_tool(p, ev, prov)
    assert p.context.asked_ids == []
    assert audio_files(ev)


def test_unknown_provider_id_falls_back_and_says_so(tmp_path):
    """配的提供商不存在时退回会话默认，并在工具结果里说明。"""
    p, _ = make_plugin(provider_id="不存在的服务")
    session = FakeProvider(path=str(tmp_path / "a.wav"))

    class Ctx(FakeContext):
        """按 id 查不到，按会话能拿到。"""

        def get_provider_by_id(self, pid):
            self.asked_ids.append(pid)
            return None

    p.context = Ctx(session)
    ev = Event()
    out = call_tool(p, ev)
    assert audio_files(ev), "回退之后应该还能发"
    assert "回退" in out, out


def test_missing_provider_is_reported():
    p, _ = make_plugin()
    p.context = FakeContext(None, by_id=None, session_error=RuntimeError("没有可用的 TTS"))
    ev = Event()
    out = call_tool(p, ev)
    assert "提供商" in out or "TTS" in out, out
    assert not audio_files(ev)


def test_object_without_get_audio_is_rejected():
    class NotATTS:
        pass

    p, _ = make_plugin()
    p.context = FakeContext(NotATTS())
    ev = Event()
    out = call_tool(p, ev)
    assert not audio_files(ev)
    assert out.strip()
