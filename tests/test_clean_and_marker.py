"""清洗与标记：动作描写、代码、系统注记都不念；提示词给出判断方向。"""

from conftest import Event, FakeProvider, make_plugin, run, texts


def test_marker_is_removed_from_text():
    p, _ = make_plugin()
    text, hit = p._strip_markers("<语音>早点睡吧")
    assert hit is True and text == "早点睡吧"


def test_marker_removed_even_when_not_spoken():
    """标记无论如何不能留在聊天文本里。"""
    p, _ = make_plugin(max_chars=1)
    ev = Event(text="晚安", reply="<语音>这句话太长会被打回文字")
    run(p, ev)
    assert "<语音>" not in "".join(texts(ev.result))


def test_parenthetical_actions_are_not_spoken():
    p, _ = make_plugin()
    assert "低下头" not in p._clean_for_speech("（她低下头）早点睡吧")


def test_star_actions_are_not_spoken():
    p, _ = make_plugin()
    assert "叹气" not in p._clean_for_speech("*叹气* 行了，睡吧")


def test_code_block_is_not_spoken():
    p, _ = make_plugin()
    assert "npm i" not in p._clean_for_speech("看这个 ```bash\nnpm i\n``` 就好")


def test_system_reminder_is_not_spoken():
    p, _ = make_plugin()
    cleaned = p._clean_for_speech(
        "<system_reminder>User ID: 110001</system_reminder>早点睡吧"
    )
    assert "110001" not in cleaned
    assert "早点睡吧" in cleaned


def test_hint_prefers_asking_first_then_content():
    p, _ = make_plugin()
    hint = p._build_hint()
    assert "对方是否方便听" in hint
    assert "复制" in hint and "文字" in hint
    assert "默认是文字" in hint
    assert "<语音>" in hint


def test_custom_hint_overrides_default():
    p, _ = make_plugin(prompt_hint="只说 {marker}，最多 {max_chars} 字", max_chars=33)
    assert p._build_hint() == "只说 <语音>，最多 33 字"


def test_marker_can_be_customised():
    p, _ = make_plugin(voice_markers=["[VOICE]"])
    assert p._marker() == "[VOICE]"
    assert "[VOICE]" in p._build_hint()


def test_disabled_plugin_does_nothing():
    p, _ = make_plugin(enable=False, max_chars=50)
    ev = Event(text="念给我听", reply="早点睡吧。")
    run(p, ev)
    assert [c.text for c in ev.result.chain if hasattr(c, "text")] == ["早点睡吧。"]


def test_non_llm_result_is_ignored():
    class CommandResult:
        chain = []

        def is_llm_result(self):
            return False

    p, _ = make_plugin(max_chars=50)
    ev = Event(text="念给我听", reply="x")
    ev.result = CommandResult()
    run(p, ev)
    assert ev.result.chain == []


def test_provider_missing_keeps_text():
    p, _ = make_plugin(max_chars=50)
    ev = Event(text="念给我听", reply="早点睡吧。")

    class NoProvider:
        def get_provider_by_id(self, pid):
            return None

        async def get_using_tts_provider_async(self, umo):
            return None

    p.context = NoProvider()
    import asyncio

    asyncio.run(p.maybe_tts(ev))
    assert "早点睡吧。" in "".join(texts(ev.result))
