"""错误信息里的密钥不能漏到日志和聊天里。"""

from conftest import make_plugin


def test_dotted_secret_is_fully_masked():
    mod = make_plugin()[1]
    msg = mod._short(RuntimeError("401 unauthorized api_key=abc.def.ghi"))
    assert "abc" not in msg and "def" not in msg and "ghi" not in msg, msg


def test_sk_style_key_is_masked():
    mod = make_plugin()[1]
    msg = mod._short(RuntimeError("bad token sk-abcdefghijklmnop"))
    assert "abcdefghijklmnop" not in msg, msg


def test_plain_error_survives_for_diagnosis():
    mod = make_plugin()[1]
    msg = mod._short(RuntimeError("Connection refused on 127.0.0.1"))
    assert "Connection refused" in msg
