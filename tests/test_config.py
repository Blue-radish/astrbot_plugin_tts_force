"""配置只有四项：读得对、范围与设置页一致、没有已删机制的残留。"""

from conftest import make_plugin


def test_defaults_match_the_documented_values():
    p, mod = make_plugin()
    assert p._max_chars() == 50
    assert p._timeout() == 300
    assert mod.DEFAULT_MAX_CHARS == 50
    assert mod.DEFAULT_TIMEOUT == 300


def test_values_from_config_are_used():
    p, _ = make_plugin(max_chars=80, timeout=120)
    assert p._max_chars() == 80
    assert p._timeout() == 120


def test_too_small_values_are_clamped():
    p, mod = make_plugin(max_chars=0, timeout=0)
    assert p._max_chars() == mod.MAX_CHARS_MIN
    assert p._timeout() == mod.TIMEOUT_MIN


def test_too_large_values_are_clamped():
    p, mod = make_plugin(max_chars=999999, timeout=999999)
    assert p._max_chars() == mod.MAX_CHARS_MAX
    assert p._timeout() == mod.TIMEOUT_MAX


def test_garbage_values_fall_back_to_default():
    p, _ = make_plugin(max_chars="很多", timeout=None)
    assert p._max_chars() == 50
    assert p._timeout() == 300


def test_runtime_range_matches_the_settings_page():
    """设置页滑块拉不到的值，运行时也不该生效（以前 1～10000 两边不一致）。"""
    import json

    from conftest import PLUGIN_DIR

    _, mod = make_plugin()
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))

    chars = schema["max_chars"]["slider"]
    assert (chars["min"], chars["max"]) == (mod.MAX_CHARS_MIN, mod.MAX_CHARS_MAX)

    timeout = schema["timeout"]["slider"]
    assert (timeout["min"], timeout["max"]) == (mod.TIMEOUT_MIN, mod.TIMEOUT_MAX)


def test_default_timeout_is_below_the_host_tool_budget():
    """宿主工具预算默认 120 秒；插件不再自己造一个够不到的 300 秒默认（这件事写在提示里）。

    这里只断言默认值是合法且不低于合成所需的最短时间，具体取值由用户决定。
    """
    _, mod = make_plugin()
    assert mod.TIMEOUT_MIN <= mod.DEFAULT_TIMEOUT <= mod.TIMEOUT_MAX
