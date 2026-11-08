"""会话偏好：拒绝后主动语音被挡住，想用语音时倾向提高，临时偏好会过期。"""

import json
import time

from conftest import Event, FakeProvider, make_plugin, run


def test_refusal_blocks_model_marker(tmp_path):
    """说过“别发语音”之后，模型打标记也不能念。"""
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=100, auto_min_gap=0)
    voiced, calls = run(p, Event(text="别发语音，我在开会", reply="<语音>好的，用文字慢慢聊。"))
    assert voiced is False
    assert calls == []


def test_refusal_blocks_random(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=100, auto_min_gap=0)
    run(p, Event(text="别发语音", reply="嗯。"))  # 先落地偏好
    voiced, _ = run(p, Event(text="今天好累", reply="那就先别干了，躺一会儿。"))
    assert voiced is False


def test_refusal_does_not_block_explicit_request(tmp_path):
    """对方后来又点名要语音，那是更新的明确指令，应该覆盖旧的拒绝。"""
    p, _ = make_plugin(tmp_path, max_chars=50, max_chars_user=2000, random_rate=0)
    run(p, Event(text="别发语音", reply="好。"))
    voiced, calls = run(p, Event(text="这句念给我听", reply="那就说一句。"))
    assert voiced is True, "点名优先级最高"
    assert calls == ["那就说一句。"]


def test_voice_mode_raises_auto_tendency(tmp_path):
    p, _ = make_plugin(
        tmp_path, max_chars=50, random_rate=0, voice_mode_rate=100, auto_min_gap=0
    )
    run(p, Event(text="接下来用语音聊吧", reply="嗯。"))
    voiced, _ = run(p, Event(text="在吗", reply="我在呢。"))
    assert voiced is True


def test_temporary_preference_expires(tmp_path, monkeypatch):
    p, _ = make_plugin(tmp_path, max_chars=50, random_rate=100, auto_min_gap=0, text_mode_ttl=60)
    run(p, Event(text="我在开会，不方便听", reply="好。"))
    assert p._pref_mode(Event().unified_msg_origin)[0] == "text"

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 61)
    assert p._pref_mode(Event().unified_msg_origin)[0] == "auto", "临时偏好要能自己过期"


def test_back_to_auto_clears_preference(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="别发语音", reply="好。"))
    assert p._pref_mode(Event().unified_msg_origin)[0] == "text"
    run(p, Event(text="算了随便你", reply="行。"))
    assert p._pref_mode(Event().unified_msg_origin)[0] == "auto"


def test_long_term_preference_is_persisted(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="以后都用文字", reply="好。"))
    saved = json.loads((tmp_path / "prefs.json").read_text(encoding="utf-8"))
    umo = Event().unified_msg_origin
    assert saved[umo]["mode"] == "text"
    assert saved[umo]["until"] is None, "长期偏好没有过期时间"


def test_short_term_preference_is_not_persisted(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="我在开会", reply="好。"))
    assert not (tmp_path / "prefs.json").exists()


def test_persisted_preference_is_reloaded(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="以后都用文字", reply="好。"))
    q, _ = make_plugin(tmp_path)
    q._prefs_loaded = False
    assert q._pref_mode(Event().unified_msg_origin)[0] == "text"


def test_preference_is_per_conversation(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="别发语音", reply="好。", umo="bot_a:FriendMessage:1"))
    assert p._pref_mode("bot_a:FriendMessage:1")[0] == "text"
    assert p._pref_mode("bot_a:FriendMessage:2")[0] == "auto"


def test_hint_tells_model_about_text_mode(tmp_path):
    p, _ = make_plugin(tmp_path, text_mode_ttl=600)
    run(p, Event(text="别发语音", reply="好。"))
    hint = p._build_hint("text", "对方明确说别发语音")
    assert "请用文字回复" in hint
