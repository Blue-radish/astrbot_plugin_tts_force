"""配置只有四项：读得对、范围与设置页一致、没有已删机制的残留。"""

from conftest import make_plugin


def test_defaults_match_the_documented_values():
    p, mod = make_plugin()
    assert p._max_chars() == 30
    assert p._timeout() == 120
    assert mod.DEFAULT_MAX_CHARS == 30
    assert mod.DEFAULT_TIMEOUT == 120


def test_values_from_config_are_used():
    p, _ = make_plugin(max_chars=80, timeout=60)
    assert p._max_chars() == 80
    assert p._timeout() == 60


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
    assert p._max_chars() == 30
    assert p._timeout() == 120


def test_settings_are_plain_numbers_not_sliders():
    """设置页直接填数字，不再用滑块。"""
    import json

    from conftest import PLUGIN_DIR

    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    for key in ("max_chars", "timeout"):
        assert "slider" not in schema[key], f"{key} 不该再带滑块"
        assert schema[key]["type"] == "int", schema[key]["type"]


def test_default_timeout_fits_inside_the_host_tool_budget():
    """宿主工具调用默认 120 秒预算：插件自己的等待不能比它长，否则永远轮不到插件报超时。"""
    _, mod = make_plugin()
    assert mod.DEFAULT_TIMEOUT <= 120, mod.DEFAULT_TIMEOUT
    assert mod.TIMEOUT_MIN <= mod.DEFAULT_TIMEOUT <= mod.TIMEOUT_MAX
