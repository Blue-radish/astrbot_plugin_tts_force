"""共享的测试夹具：加载插件、造假事件/假 provider。"""

import importlib.util
import os
import re
import sys
import tempfile
import wave
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
    p._last_test_at = {}
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


def make_wav(path, seconds=0.2):
    """写一个真能解析的 WAV：合成结果是要校验的，假字节糊不过去。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


class FakeProvider:
    """result=UNSET 正常返回路径；result=None 模拟合成失败。"""

    def __init__(self, path=None, delay=0.0, result=UNSET, auto_create=True):
        if path is None:
            path = str(make_wav(Path(tempfile.mkdtemp(prefix="ttsfake_")) / "fake.wav"))
        elif auto_create and result is UNSET and not Path(str(path)).exists():
            make_wav(path)   # 少写点样板：给个路径就自动补一个合法 wav
        self.path = str(path)
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


class FakeChainResult:
    def __init__(self, chain):
        self.chain = chain

    @property
    def text(self):
        return "".join(getattr(c, "text", "") or "" for c in self.chain)

    def __contains__(self, item):
        return item in self.text

    def __str__(self):
        return self.text


class Event:
    """够用的假事件：只实现插件会碰到的方法。"""

    def __init__(self, text="", reply="", gid="", umo="bot_a:FriendMessage:110001",
                 sender="110001", admin=False):
        self.message_str = text
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender
        self._admin = admin
        self._extras = {}
        self.sent = []
        self.sent_chain = []   # chain_result 发出去的东西（自检命令回音频用）
        self.result = FakeResult(reply)

    def get_group_id(self):
        return self._gid

    def get_sender_id(self):
        return self._sender

    def is_admin(self):
        return self._admin

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result

    def plain_result(self, text):
        """命令返回值：记下来方便断言。"""
        self.sent.append(text)
        return text

    def chain_result(self, chain):
        self.sent_chain.append(chain)
        return FakeChainResult(chain)


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
