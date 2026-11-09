"""配置里的数字要么被夹在合理范围，要么在运行时按边界处理。"""

from conftest import make_plugin


def test_rates_are_clamped_to_0_100():
    p, _ = make_plugin(random_rate=999, voice_mode_rate=-5)
    assert p._rate_for("auto") == 100
    assert p._rate_for("voice") == 0


def test_negative_gap_does_not_bypass_limit():
    p, _ = make_plugin(auto_min_gap=-100)
    p._last_auto_at["c"] = 0.0
    ok, why = p._auto_gap_ok("c", "auto")
    assert ok is True, "负间隔按 0 处理（等于不限制），但别留下奇怪的行为"


def test_timeout_floor_is_one_second():
    p, _ = make_plugin(timeout=0, auto_timeout=-3)
    assert p._cfg_int("timeout", 300, lo=1, hi=3600) == 1
    assert p._cfg_int("auto_timeout", 40, lo=1, hi=3600) == 1


def test_char_limits_never_negative():
    p, _ = make_plugin(max_chars=-10, max_chars_user=-10)
    assert p._cfg_int("max_chars", 50, lo=0, hi=10000) == 0
    assert p._cfg_int("max_chars_user", 2000, lo=0, hi=10000) == 0


def test_ttl_must_be_positive_but_long_term_still_possible():
    p, _ = make_plugin(text_mode_ttl=0)
    assert p._cfg_int("text_mode_ttl", 1800, lo=1, hi=7 * 86400) == 1, "0 秒有效期没有意义，夹到 1"
    assert p._parse_intent("以后都用文字")["ttl"] == 0, "长期偏好仍然走 ttl=0 这条路径"
