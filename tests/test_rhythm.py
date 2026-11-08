"""会话节奏：语音模式该更连贯、群里的偏好按人分开、总开关要真的管住两个分支。"""

from conftest import Event, make_plugin, run


def test_voice_mode_uses_shorter_gap(tmp_path):
    """说了想用语音聊之后，不该还被十分钟间隔挡住。"""
    p, _ = make_plugin(
        tmp_path, random_rate=0, voice_mode_rate=100, auto_min_gap=600, voice_mode_min_gap=0
    )
    run(p, Event(text="接下来用语音聊吧", reply="嗯。"))
    first, _ = run(p, Event(text="在吗", reply="我在呢。"))
    second, _ = run(p, Event(text="吃了吗", reply="刚吃完。"))
    assert first is True
    assert second is True, "语音模式下连续短句应该还能发"


def test_normal_mode_gap_still_applies(tmp_path):
    p, _ = make_plugin(tmp_path, random_rate=100, auto_min_gap=600, voice_mode_min_gap=0)
    first, _ = run(p, Event(text="在吗", reply="我在呢。"))
    second, _ = run(p, Event(text="吃了吗", reply="刚吃完。"))
    assert first is True
    assert second is False, "平时还是要有间隔"


def test_group_preference_is_per_speaker(tmp_path):
    """一个人在群里说不方便听，不该让整个群静音。"""
    gid = "220001"
    umo = f"bot_a:GroupMessage:{gid}"
    p, _ = make_plugin(tmp_path, random_rate=0, auto_min_gap=0, auto_groups=[gid])
    run(p, Event(text="我在开会，不方便听", reply="好。", gid=gid, umo=umo, sender="111"))
    voiced, _ = run(p, Event(text="晚安", reply="<语音>早点睡吧。", gid=gid, umo=umo, sender="222"))
    assert voiced is True, "另一个人没被波及"
    blocked, _ = run(p, Event(text="晚安", reply="<语音>早点睡吧。", gid=gid, umo=umo, sender="111"))
    assert blocked is False, "说过不方便听的那个人仍然受约束"


def test_preference_is_per_conversation(tmp_path):
    p, _ = make_plugin(tmp_path, random_rate=0, auto_min_gap=0)
    run(p, Event(text="别发语音", reply="好。"))
    muted, _ = run(p, Event(text="晚安", reply="<语音>早点睡吧。"))
    other, _ = run(
        p,
        Event(text="晚安", reply="<语音>早点睡吧。", umo="bot_a:FriendMessage:999999"),
    )
    assert muted is False
    assert other is True


def test_auto_enable_off_blocks_marker_and_random(tmp_path):
    p, _ = make_plugin(tmp_path, auto_enable=False, random_rate=100, auto_min_gap=0)
    marked, _ = run(p, Event(text="晚安", reply="<语音>早点睡吧。"))
    random_, _ = run(p, Event(text="今天好累", reply="那就早点休息。"))
    assert marked is False
    assert random_ is False


def test_auto_enable_off_still_honours_explicit_request(tmp_path):
    p, _ = make_plugin(tmp_path, auto_enable=False, max_chars_user=2000)
    voiced, _ = run(p, Event(text="这句念给我听", reply="那就说一句。"))
    assert voiced is True
