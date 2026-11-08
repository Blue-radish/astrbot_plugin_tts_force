"""共享的测试夹具：加载插件、造假事件/假 provider。"""

import importlib.util
import os
import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def find_astrbot_root() -> Path | None:
    """找 AstrBot 主程序目录，别在仓库里写死个人路径。

    顺序：环境变量 ASTRBOT_ROOT -> 从本文件往上找含有 astrbot/api 的目录。
    找不到也没关系，需要宿主的测试会用 importorskip 跳过。
    """
    env = os.environ.get("ASTRBOT_ROOT")
    if env and (Path(env) / "astrbot" / "api").is_dir():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "astrbot" / "api").is_dir():
            return parent
    return None


ASTRBOT_ROOT = find_astrbot_root()
for path in (str(PLUGIN_DIR), str(ASTRBOT_ROOT) if ASTRBOT_ROOT else None):
    if path and path not in sys.path:
        sys.path.insert(0, path)


def load_mod():
    spec = importlib.util.spec_from_file_location("tts_force_main", PLUGIN_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_plugin(tmp_path=None, **attrs):
    mod = load_mod()
    p = object.__new__(mod.TTSForcePlugin)
    p.config = dict(attrs)
    p._last_voice_at = {}
    p._last_auto_at = {}
    p._busy = set()
    p._prefs = {}
    p._prefs_loaded = True
    p._pref_ver = {}
    p._turn_seq = {}
    p.context = None
    pats = p._cfg_list("strip_patterns", mod.DEFAULT_STRIP)
    p._strip_res = [re.compile(x) for x in pats]
    if tmp_path is not None:
        p._pref_file = lambda: Path(tmp_path) / "prefs.json"
        p._state_file = lambda: Path(tmp_path) / "state.json"  # 别写线上 data
    return p, mod


class FakeResult:
    def __init__(self, text):
        from astrbot.api.message_components import Plain

        self.chain = [Plain(text=text)]

    def is_llm_result(self):
        return True


UNSET = object()


class FakeProvider:
    """result=UNSET 正常返回路径；result=None 模拟合成失败。"""

    def __init__(self, path="C:/tmp/fake.wav", delay=0.0, result=UNSET):
        self.path = path
        self.delay = delay
        self.calls = []
        self._result = result

    async def get_audio(self, text):
        import asyncio

        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.path if self._result is UNSET else self._result


class FakeContext:
    def __init__(self, provider):
        self._provider = provider

    def get_provider_by_id(self, pid):
        return self._provider

    async def get_using_tts_provider_async(self, umo):
        return self._provider


class Event:
    """够用的假事件：只实现插件会碰到的方法。"""

    def __init__(self, text="", reply="", gid="", umo="bot_a:FriendMessage:110001", sender="110001"):
        self.message_str = text
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender
        self.sent = []
        self.result = FakeResult(reply)

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result

    def plain_result(self, text):
        """命令返回值：记下来方便断言。"""
        self.sent.append(text)
        return text


def run(p, event, provider=None):
    """跑一遍 maybe_tts，返回 (是否发了语音, provider 被念的内容)。"""
    import asyncio

    prov = provider or FakeProvider()
    p.context = FakeContext(prov)
    asyncio.run(p.maybe_tts(event))
    from astrbot.api.message_components import Record

    voiced = any(isinstance(c, Record) for c in event.result.chain)
    return voiced, prov.calls


def texts(result):
    from astrbot.api.message_components import Plain

    return [c.text for c in result.chain if isinstance(c, Plain)]
