"""工具结果会回给模型、也可能被贴到聊天里，密钥不能漏。"""

from conftest import load_mod


def test_api_key_value_is_masked():
    mod = load_mod()
    out = mod._short(Exception("401 unauthorized api_key=abc.def.ghi"))
    assert "abc" not in out and "def.ghi" not in out, out
    assert "401" in out, "别把有用的信息一起抹掉"


def test_authorization_header_masks_the_token():
    """『Authorization: Bearer xxx』必须把令牌抹掉，不能只抹掉头名字。"""
    mod = load_mod()
    out = mod._short(Exception("Authorization: Bearer abc.def.ghi"))
    assert "abc.def.ghi" not in out, out


def test_base64_style_key_is_masked():
    """带 + / = 的密钥以前整段漏过。"""
    mod = load_mod()
    out = mod._short(Exception("api_key=abc+def/ghi=="))
    assert "abc+def" not in out and "ghi==" not in out, out


def test_quoted_value_is_masked():
    mod = load_mod()
    out = mod._short(Exception("token: 'xyz123456'"))
    assert "xyz123456" not in out, out
    assert "'" not in out and chr(34) not in out, f"留下了半截引号：{out!r}"


def test_chinese_keyword_is_masked():
    mod = load_mod()
    out = mod._short(Exception("密码是 hunter2hunter2"))
    assert "hunter2hunter2" not in out, out


def test_bare_bearer_is_masked():
    mod = load_mod()
    out = mod._short(Exception("Bearer eyJhbGciOi.payload.sig"))
    assert "eyJhbGciOi" not in out, out


def test_sk_style_key_is_masked():
    mod = load_mod()
    out = mod._short(Exception("bad token sk-abcdefghijklmnop"))
    assert "abcdefghijklmnop" not in out, out


def test_plain_errors_survive():
    mod = load_mod()
    out = mod._short(Exception("Connection refused on 127.0.0.1"))
    assert "Connection refused" in out, out


def test_long_error_is_truncated():
    mod = load_mod()
    out = mod._short(Exception("x" * 500), limit=50)
    assert len(out) <= 50


def test_whitespace_is_squeezed():
    mod = load_mod()
    out = mod._short(Exception("line1\n\n   line2\t line3"))
    assert out == "line1 line2 line3", out


def test_error_classification():
    mod = load_mod()
    cases = {
        "401 Unauthorized": "鉴权",
        "invalid voice: not found": "音色",
        "timed out": "超时",
        "Connection refused": "连不上",
    }
    for raw, want in cases.items():
        assert want in mod._classify_error(Exception(raw)), raw
