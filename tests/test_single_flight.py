"""同一会话的合成必须是单飞：解析提供商这段异步等待不能留窗口。"""

import asyncio

from conftest import Event, FakeProvider, FakeContext, make_plugin


def test_slow_provider_resolution_does_not_double_synthesize(tmp_path):
    p, _ = make_plugin(tmp_path, random_rate=100)
    prov = FakeProvider(path=str(tmp_path / "a.wav"), delay=0.05)
    (tmp_path / "a.wav").write_bytes(b"x")
    p.context = FakeContext(prov)
    original = p._resolve_provider

    async def slow_resolve(event):
        await asyncio.sleep(0.05)      # 解析提供商要等一次网络/上下文查询
        return await original(event)

    p._resolve_provider = slow_resolve

    async def scenario():
        first = Event(text="用语音说", reply="第一条回复")
        second = Event(text="用语音说", reply="第二条回复")
        await asyncio.gather(p.maybe_tts(first), p.maybe_tts(second))

    asyncio.run(scenario())
    assert len(prov.calls) == 1, f"同一会话并发合成了 {len(prov.calls)} 次"


def test_busy_slot_is_released_even_when_resolution_fails(tmp_path):
    p, _ = make_plugin(tmp_path, random_rate=100)

    class Boom:
        def get_provider_by_id(self, pid):
            raise RuntimeError("查提供商炸了")

        async def get_using_tts_provider_async(self, umo):
            raise RuntimeError("会话 provider 也炸了")

    p.context = Boom()
    ev = Event(text="用语音说", reply="早点睡吧。")
    asyncio.run(p.maybe_tts(ev))
    assert not p._busy, "解析失败也必须把会话位放开，否则以后永远发不出语音"
