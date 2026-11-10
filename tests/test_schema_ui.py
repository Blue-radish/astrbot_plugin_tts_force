"""设置页只有三项主设置：标题短、有独立提示、没有已删机制的残留。"""

import json

from conftest import PLUGIN_DIR

VISIBLE = ["enable", "provider_id", "max_chars", "timeout", "test_text"]


def schema():
    return json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))


def visible_keys():
    return [k for k, v in schema().items() if not v.get("invisible")]


def test_only_the_five_visible_settings():
    assert visible_keys() == VISIBLE, visible_keys()


def test_no_removed_mechanism_survives_in_the_schema():
    keys = set(schema())
    for gone in (
        "random_rate",
        "voice_markers",
        "force_keywords",
        "auto_groups",
        "auto_min_gap",
        "voice_mode_rate",
        "voice_mode_min_gap",
        "text_mode_ttl",
        "max_chars_user",
        "auto_timeout",
        "cooldown",
        "inject_prompt",
        "prompt_hint",
        "strip_patterns",
        "strip_markdown",
    ):
        assert gone not in keys, f"{gone} 应该已经删了"


def test_titles_are_short():
    for key in visible_keys():
        title = schema()[key].get("description", "")
        assert title, key
        assert len(title) <= 12, f"{key} 标题太长（{len(title)} 字）：{title}"


def test_every_visible_field_has_a_hint():
    for key in visible_keys():
        item = schema()[key]
        assert item.get("hint"), f"{key} 缺少 hint（标题装不下解释）"


def test_defaults_in_schema_match_the_code():
    s = schema()
    assert s["max_chars"]["default"] == 50
    assert s["timeout"]["default"] == 300
    assert s["provider_id"]["default"] == ""


def test_provider_field_explains_host_tts_conflict():
    """最容易踩的坑：宿主自带 TTS 也开着，结果念两遍。"""
    item = schema()["provider_id"]
    hint = item.get("hint", "")
    assert "关闭" in hint or "关掉" in hint, hint
    assert item.get("obvious_hint") is True


def test_max_chars_hint_says_it_is_a_hard_limit():
    hint = schema()["max_chars"].get("hint", "")
    assert "上限" in hint or "超过" in hint, hint
