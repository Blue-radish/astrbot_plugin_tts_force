"""配置页要让人看得懂：短标题 + 独立提示 + 该有的控件类型。"""

import json
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _schema():
    return json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))


def _visible(schema):
    return {k: v for k, v in schema.items() if not v.get("invisible")}


def test_every_visible_field_has_a_short_title():
    for key, meta in _visible(_schema()).items():
        title = meta.get("description", "")
        assert title, f"{key} 没有标题"
        assert len(title) <= 12, f"{key} 的标题太长（{len(title)} 字）：说明应该放 hint"


def test_every_visible_field_has_a_hint():
    for key, meta in _visible(_schema()).items():
        assert meta.get("hint"), f"{key} 缺少 hint，用户只能猜"


def test_rate_fields_use_a_slider():
    schema = _schema()
    for key in ("random_rate", "voice_mode_rate"):
        slider = schema[key].get("slider")
        assert slider, f"{key} 应该是滑块"
        assert slider["min"] == 0 and slider["max"] == 100


def test_prompt_hint_is_multiline():
    assert _schema()["prompt_hint"]["type"] == "text", "提示词是多行内容，单行输入框不好改"


def test_auto_fields_only_show_when_auto_enabled():
    schema = _schema()
    for key in ("random_rate", "auto_min_gap", "auto_groups"):
        assert schema[key].get("condition") == {"auto_enable": True}, f"{key} 应该跟随主动语音开关显示"


def test_test_text_is_the_last_visible_field():
    keys = list(_visible(_schema()))
    assert keys[-1] == "test_text", "自检文本是最后才需要动的东西"


def test_first_use_guidance_is_obvious():
    schema = _schema()
    assert schema["provider_id"].get("obvious_hint") is True
    hint = schema["provider_id"]["hint"]
    assert "关闭" in hint or "关掉" in hint, "首次使用必须提醒关掉宿主自带的 TTS"


def test_legacy_keys_survive_the_rewrite():
    schema = _schema()
    for legacy in ("random_min_gap", "random_groups"):
        assert schema[legacy].get("invisible") is True


def test_group_whitelist_hint_mentions_named_requests():
    hint = _schema()["auto_groups"]["hint"]
    assert "点名" in hint or "明确要求" in hint, "白名单只管主动语音，这句话必须写出来"
