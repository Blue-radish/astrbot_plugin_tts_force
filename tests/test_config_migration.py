"""配置迁移要在真实加载器下也生效：旧键在 schema 里留了隐藏字段，值不能丢。"""

import json
from pathlib import Path

import pytest

from conftest import make_plugin

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def test_legacy_keys_are_hidden_but_present_in_schema():
    """宿主只保留 schema 里有的键，所以旧键必须以隐藏字段留着。"""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    for legacy in ("random_min_gap", "random_groups"):
        assert legacy in schema, f"{legacy} 不在 schema 里，升级时会被宿主删掉"
        assert schema[legacy].get("invisible") is True, f"{legacy} 应该隐藏"


def test_migration_keeps_old_custom_values(tmp_path):
    p, _ = make_plugin(tmp_path, random_min_gap=23, random_groups=["999"])
    p._migrate_config()
    assert p.config["auto_min_gap"] == 23
    assert p.config["auto_groups"] == ["999"]
    assert "random_min_gap" not in p.config


def test_migration_removes_bare_voice_keyword(tmp_path):
    p, _ = make_plugin(tmp_path, force_keywords=["发语音", "voice"])
    p._migrate_config()
    assert p.config["force_keywords"] == ["发语音"]


def test_first_migration_prefers_the_old_value(tmp_path):
    """首次迁移时新键里那份是宿主补的默认值，旧值才是用户设的。"""
    p, _ = make_plugin(tmp_path, auto_min_gap=600, random_min_gap=999)
    p._migrate_config()
    assert p.config["auto_min_gap"] == 999


def test_migration_runs_once(tmp_path):
    """迁移过的用户，之后手动改的值不能被再次覆盖。"""
    p, _ = make_plugin(tmp_path, random_min_gap=23, auto_groups=[])
    p._migrate_config()
    assert p.config["auto_min_gap"] == 23
    p.config["auto_min_gap"] = 5
    p.config["random_min_gap"] = 23
    p._migrate_config()
    assert p.config["auto_min_gap"] == 5, "第二次不该再迁移"


def test_migration_is_a_noop_for_fresh_config(tmp_path):
    p, _ = make_plugin(tmp_path)
    before = dict(p.config)
    p._migrate_config()
    assert p.config == before


def test_real_loader_keeps_legacy_values(tmp_path):
    """走一遍宿主的 AstrBotConfig，确认旧值还在（就是审计复现的那条路径）。"""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    pytest.importorskip("astrbot")
    from astrbot.core.config.astrbot_config import AstrBotConfig

    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(
        json.dumps(
            {"random_min_gap": 23, "random_groups": ["999"], "force_keywords": ["发语音", "voice"]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = AstrBotConfig(config_path=str(cfg_path), schema=schema)
    assert loaded.get("random_groups") == ["999"], "旧值必须在加载后还在，否则迁移就是死代码"

    p, _ = make_plugin(tmp_path)
    p.config = loaded
    p._migrate_config()
    assert loaded.get("auto_groups") == ["999"]
    assert loaded.get("auto_min_gap") == 23
    assert "voice" not in (loaded.get("force_keywords") or [])


def test_failed_save_does_not_mark_migration_done(tmp_path):
    """配置没落盘就不能写迁移标记，否则重启后那次的改动凭空消失。"""
    p, _ = make_plugin(tmp_path, random_min_gap=23)

    class Boom(dict):
        def save_config(self):
            raise RuntimeError("磁盘写不进去")

    p.config = Boom(p.config)
    p._migrate_config()
    assert p.config["auto_min_gap"] == 23
    assert not (tmp_path / "state.json").exists() or json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8")
    ).get("config_migrated_v", 0) < 3, "保存失败还写标记，重启后会被跳过"


def test_successful_save_marks_migration_done(tmp_path):
    p, _ = make_plugin(tmp_path, random_min_gap=23)
    saved = {"n": 0}

    class Cfg(dict):
        def save_config(self):
            saved["n"] += 1

    p.config = Cfg(p.config)
    p._migrate_config()
    assert saved["n"] == 1
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["config_migrated_v"] >= 3


def test_old_default_strip_patterns_get_replaced(tmp_path):
    """从没改过清洗规则的用户，升级后不该继续吞括号内容。"""
    mod = make_plugin()[1]
    p, _ = make_plugin(tmp_path)
    p.config["strip_patterns"] = list(mod.LEGACY_DEFAULT_STRIP)
    p._migrate_config()
    assert p.config["strip_patterns"] == list(mod.DEFAULT_STRIP)
    assert "（[^（）]{0,120}）" not in p.config["strip_patterns"]


def test_custom_strip_patterns_are_left_alone(tmp_path):
    p, _ = make_plugin(tmp_path)
    p.config["strip_patterns"] = [r"【[^【】]*】"]
    p._migrate_config()
    assert p.config["strip_patterns"] == [r"【[^【】]*】"], "用户自己写的规则不许动"
