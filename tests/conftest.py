"""共享的测试夹具：加载插件、造假事件/假 provider。

3.0 之后插件只剩一个工具，夹具也跟着变小：没有偏好、没有轮次、没有清洗正则。
"""

import importlib.util
import os
import sys
import tempfile
import wave
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def find_astrbot_root() -> Path | None:
    """找 AstrBot 主程序目录，别在仓库里写死个人路径。

    顺序：环境变量 ASTRBOT_ROOT -> 从本文件往上找含有 astrbot/api 的目录。
    插件要靠 `astrbot.api` 注册工具，所以整套测试都需要能导入 AstrBot；
    找不到时下面的导入检查会直接报出原因，不会静默跳过。
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

try:
    import astrbot.api  # noqa: F401
except ImportError as exc:  # 缺宿主时给出能直接照做的一句话，而不是一堆 ImportError
    raise RuntimeError(
        "测试需要能导入 AstrBot 本体（插件通过 astrbot.api 注册工具）。"
        "用 AstrBot 自己的虚拟环境运行，例如 <AstrBot>\\.venv\\Scripts\\python.exe -m pytest tests -q；"
        "或者把环境变量 ASTRBOT_ROOT 指向 AstrBot 主程序目录。"
        f"当前 ASTRBOT_ROOT = {ASTRBOT_ROOT!r}；导入失败原因：{exc}"
    ) from exc


def load_mod():
    spec = importlib.util.spec_from_file_location("tts_force_main", PLUGIN_DIR / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_plugin(**cfg):
    """造一个插件实例：只初始化工具真正会碰到的状态。"""
    mod = load_mod()
    p = object.__new__(mod.TTSForcePlugin)
    p.config = dict(cfg)
    p._busy = set()
    p.context = None
    return p, mod


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


UNSET = object()


class FakeProvider:
    """result=UNSET 正常返回路径；result=None 模拟合成失败；error=异常对象 模拟抛错。"""

    def __init__(self, path=None, delay=0.0, result=UNSET, auto_create=True, error=None):
        if path is None:
            path = str(make_wav(Path(tempfile.mkdtemp(prefix="ttsfake_")) / "fake.wav"))
        elif auto_create and result is UNSET and not Path(str(path)).exists():
            make_wav(path)  # 少写点样板：给个路径就自动补一个合法 wav
        self.path = str(path)
        self.delay = delay
        self.calls = []
        self.error = error
        self._result = result

    async def get_audio(self, text):
        import asyncio

        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.path if self._result is UNSET else self._result


class FakeContext:
    """provider：按 id 查或按会话取，默认都返回它；by_id 只影响按 id 那条路。"""

    def __init__(self, provider=None, by_id=None, session_error=None):
        self._provider = provider
        self._by_id = by_id
        self._session_error = session_error
        self.asked_ids = []

    def get_provider_by_id(self, pid):
        self.asked_ids.append(pid)
        if self._by_id is not None:
            return self._by_id
        return self._provider

    async def get_using_tts_provider_async(self, umo):
        if self._session_error is not None:
            raise self._session_error
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

    def __init__(
        self,
        gid="",
        umo="bot_a:FriendMessage:110001",
        sender="110001",
        admin=False,
        send_error=None,
    ):
        self.message_str = ""
        self.unified_msg_origin = umo
        self._gid = gid
        self._sender = sender
        self._admin = admin
        self._extras = {}
        self._send_error = send_error
        self.sent_chains = []  # event.send() 发出去的消息链
        self.sent_results = []  # 命令返回值

    # --- 事件接口 ---
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

    async def send(self, message):
        if self._send_error is not None:
            raise self._send_error
        self.sent_chains.append(message)

    def plain_result(self, text):
        self.sent_results.append(text)
        return text

    def chain_result(self, chain):
        self.sent_results.append(chain)
        return FakeChainResult(chain)


def audio_files(event: Event) -> list[str]:
    """event.send() 发出的语音文件列表。"""
    from astrbot.api.message_components import Record

    out = []
    for chain in event.sent_chains:
        for comp in getattr(chain, "chain", chain):
            if isinstance(comp, Record):
                out.append(comp.file)
    return out


def sent_text(event: Event) -> str:
    """event.send() 发出的文字（拼起来）。"""
    from astrbot.api.message_components import Plain

    parts = []
    for chain in event.sent_chains:
        for comp in getattr(chain, "chain", chain):
            if isinstance(comp, Plain):
                parts.append(comp.text or "")
    return "".join(parts)


def call_tool(p, event, provider=None, text="晚安，早点睡。"):
    """调用一次工具，返回模型看到的工具结果字符串。"""
    import asyncio

    if provider is not None:
        p.context = FakeContext(provider)
    return asyncio.run(p.tool_send_voice(event, text))
