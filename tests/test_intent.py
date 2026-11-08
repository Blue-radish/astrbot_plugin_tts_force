"""把用户这句话读成什么意思：拒绝、点名、想用语音、咨询。"""

import pytest

from conftest import make_plugin


def intent(msg, **cfg):
    p, _ = make_plugin(**cfg)
    return p._parse_intent(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "别发语音，我在开会",
        "不要语音，文字就行",
        "不用念了",
        "以后都别发语音",
        "别再发语音了",
        "这段话别读出来",
        "不能用语音",
        "我这边不能听语音",
        "先别放声音",
    ],
)
def test_refusal_makes_text_mode(msg):
    out = intent(msg)
    assert out["mode"] == "text", msg
    assert out["force_round"] is False, "拒绝不能被当成点名"


def test_refusal_after_long_term_word_becomes_permanent():
    assert intent("以后都别发语音")["ttl"] == 0
    assert intent("以后都用文字")["ttl"] == 0
    assert intent("别发语音").get("ttl", 0) > 0, "偶发的拒绝只临时生效"


@pytest.mark.parametrize(
    "msg",
    [
        "我在开会，不方便听",
        "现在不方便听",
        "听不了，旁边有人",
        "在地铁上，静音",
        "我在开会",
        "我在外面，不方便",
    ],
)
def test_cannot_listen_switches_to_text(msg):
    out = intent(msg)
    assert out["mode"] == "text", msg
    assert out["why"], "要能解释为什么转文字"


@pytest.mark.parametrize(
    "msg",
    [
        "今天晚饭吃什么都行",
        "我没在开会，可以用语音",
        "我没在开会",
        "你随便聊点什么都行",
        "在路上不方便，不过现在到了",
    ],
)
def test_scene_words_need_first_person_and_no_negation(msg):
    """“吃什么随便”“我没在开会”都不该动交流偏好。"""
    out = intent(msg)
    assert out["mode"] != "text", msg


@pytest.mark.parametrize(
    "msg",
    [
        "voice 这个单词是什么意思",
        "发语音功能怎么设置",
        "语音功能的配置在哪",
        "语音念出来是什么原理",
        "语音按钮在哪个选项里",
    ],
)
def test_questions_are_not_commands(msg):
    out = intent(msg)
    assert out["force_round"] is False, msg
    assert out["mode"] is None, "咨询不改会话偏好"


@pytest.mark.parametrize(
    "msg",
    [
        "这句念给我听",
        "发条语音吧",
        "刚才那段语音回我",
        "念给我听",
        "用语音说",
        "用语音解释一下为什么会这样",
        "读一下这段",
    ],
)
def test_explicit_request_for_this_round(msg):
    out = intent(msg)
    assert out["force_round"] is True, msg


@pytest.mark.parametrize("msg", ["接下来用语音聊吧", "以后都用语音", "别打字了，语音聊", "后面用语音"])
def test_voice_mode_request(msg):
    out = intent(msg)
    assert out["mode"] == "voice", msg
    assert out["force_round"] is False


@pytest.mark.parametrize("msg", ["语音文字都行", "恢复默认", "恢复默认回复方式", "语音文字都可以"])
def test_back_to_auto(msg):
    assert intent(msg)["mode"] == "auto"


def test_negated_meeting_is_read_as_permission():
    """“我没在开会，可以用语音”是许可，不是“不方便听”。"""
    out = intent("我没在开会，可以用语音")
    assert out["mode"] != "text"
    assert out["force_round"] is True, "明确说可以用语音，这一条就念"


@pytest.mark.parametrize("msg", ["随便你", "你看着办", "正常发就行", "都行"])
def test_vague_words_do_not_touch_preference(msg):
    """“随便”“都行”这种必须指向交流方式才算，不能见词就清偏好。"""
    assert intent(msg)["mode"] is None


def test_plain_chat_changes_nothing():
    out = intent("今天好累，先睡了")
    assert out["mode"] is None
    assert out["force_round"] is False


def test_custom_force_keywords_still_work():
    out = intent("读一下这段", force_keywords=["读一下"])
    assert out["force_round"] is True


def test_bare_voice_is_not_a_keyword_by_default():
    """不能再让 “voice 是什么意思” 这类触发语音。"""
    p, mod = make_plugin()
    assert "voice" not in mod.DEFAULT_FORCE
