"""三项设置：读得对、越界夹住、旧键清干净。"""

from conftest import make_plugin


def test_defaults_match_the_documented_values():
    p, mod = make_plugin()
    assert p._max_chars() == 50
    assert p._timeout() == 300
    assert p._enable() is True
    assert mod.DEFAULT_MAX_CHARS == 50
    assert mod.DEFAULT_TIMEOUT == 300


def test_values_from_config_are_used():
    p, _ = make_plugin(max_chars=80, timeout=120)
    assert p._max_chars() == 80
    assert p._timeout() == 120


def test_too_small_values_are_clamped():
    """0 或负数没有意义：字数至少 1，超时至少 1 秒。"""
    p, _ = make_plugin(max_chars=0, timeout=0)
    assert p._max_chars() == 1
    assert p._timeout() == 1


def test_too_large_values_are_clamped():
    p, _ = make_plugin(max_chars=999999, timeout=999999)
    assert p._max_chars() == 10000
    assert p._timeout() == 3600


def test_garbage_values_fall_back_to_default():
    p, _ = make_plugin(max_chars="很多", timeout=None)
    assert p._max_chars() == 50
    assert p._timeout() == 300


def test_enable_flag_is_read_as_bool():
    p, _ = make_plugin(enable=False)
    assert p._enable() is False


def test_legacy_key_list_covers_everything_we_removed():
    _, mod = make_plugin()
    legacy = set(mod.LEGACY_KEYS)
    for key in (
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
        "random_min_gap",
        "random_groups",
        "auto_enable",
    ):
        assert key in legacy, f"{key} 忘了列进 LEGACY_KEYS"


class FakeConfig(dict):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.saved = 0

    def save_config(self):
        self.saved += 1


def test_legacy_keys_are_deleted_and_saved():
    """升级后配置文件里的旧键要真的消失，不是「留着不管」。"""
    p, mod = make_plugin()
    p.config = FakeConfig(
        enable=True,
        provider_id="qwen3-tts",
        max_chars=50,
        timeout=300,
        test_text="测试",
        random_rate=15,
        voice_markers=["<语音>"],
        strip_patterns=["\\*[^*\\n]{0,120}\\*"],
    )
    p._prune_legacy_config()
    assert "random_rate" not in p.config
    assert "voice_markers" not in p.config
    assert "strip_patterns" not in p.config
    assert p.config["provider_id"] == "qwen3-tts"
    assert p.config["max_chars"] == 50
    assert p.config.saved == 1


def test_prune_does_nothing_when_clean():
    p, _ = make_plugin()
    p.config = FakeConfig(enable=True, max_chars=50, timeout=300)
    p._prune_legacy_config()
    assert p.config.saved == 0, "没有旧键就不该写盘"


def test_prune_survives_save_failure():
    p, _ = make_plugin()

    class Boom(FakeConfig):
        def save_config(self):
            raise RuntimeError("磁盘满了")

    p.config = Boom(enable=True, random_rate=15)
    p._prune_legacy_config()   # 不许抛
    assert "random_rate" not in p.config


def test_prune_leaves_unknown_host_keys_alone():
    """只删我们自己的旧键，别碰宿主可能塞进来的东西。"""
    p, _ = make_plugin()
    p.config = FakeConfig(enable=True, random_rate=15, __template_key="x")
    p._prune_legacy_config()
    assert p.config["__template_key"] == "x"
