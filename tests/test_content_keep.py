"""清洗不能悄悄吞掉限定条件：括号默认保留，真的删了就同时保留文字。"""

import asyncio

from conftest import Event, FakeProvider, FakeContext, make_plugin, texts


def _voiced(result):
    from astrbot.api.message_components import Record

    return any(isinstance(c, Record) for c in result.chain)


def test_parentheses_are_kept_by_default():
    p, _ = make_plugin()
    cleaned = p._clean_for_speech("剂量为5毫克（每天最多一次），请遵医嘱。")
    assert "每天最多一次" in cleaned, "括号里可能是限定条件，默认不能静音掉"


def test_action_asterisk_still_stripped():
    p, _ = make_plugin()
    assert "叹气" not in p._clean_for_speech("*叹气* 行了，睡吧")


def test_meaningful_loss_keeps_text_alongside_voice(tmp_path):
    """用户自定义的清洗规则要是删掉了实质内容，文字必须留着。"""
    p, _ = make_plugin(tmp_path, strip_patterns=[r"（[^（）]*）"], max_chars_user=2000)
    prov = FakeProvider(path=str(tmp_path / "a.wav"))
    p.context = FakeContext(prov)
    ev = Event(text="念给我听", reply="剂量为5毫克（每天最多一次），请遵医嘱。")
    asyncio.run(p.maybe_tts(ev))
    assert _voiced(ev.result), "点名了，语音要有"
    assert "每天最多一次" in "".join(texts(ev.result)), "清洗删掉了限定条件，文字得留着"


def test_trivial_loss_does_not_duplicate_text(tmp_path):
    """只删掉 *动作* 这种无实质内容的东西，不该再多留一份文字。"""
    p, _ = make_plugin(tmp_path, max_chars_user=2000)
    wav = tmp_path / "b.wav"
    p.context = FakeContext(FakeProvider(path=str(wav)))
    ev = Event(text="念给我听", reply="*叹气* 早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert _voiced(ev.result)
    assert not texts(ev.result), "没丢信息就不该重复发文字"


def test_lossy_auto_path_also_keeps_text(tmp_path):
    """主动语音也一样：清洗有损就不许把原文吞掉。"""
    p, _ = make_plugin(tmp_path, strip_patterns=[r"（[^（）]*）"], random_rate=100, max_chars=200)
    wav = tmp_path / "c.wav"
    p.context = FakeContext(FakeProvider(path=str(wav)))
    ev = Event(text="随便聊聊", reply="记得（饭后）吃药。")
    asyncio.run(p.maybe_tts(ev))
    assert _voiced(ev.result)
    assert "饭后" in "".join(texts(ev.result))
