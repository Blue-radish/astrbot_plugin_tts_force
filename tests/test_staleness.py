"""合成期间对方改主意 / 已经继续说话：旧语音不能补发出去。"""

import asyncio

from conftest import Event, FakeProvider, make_plugin


def test_refusal_during_synthesis_drops_the_voice(tmp_path):
    """复现审计场景：第一轮合成中，对方说“别发语音，我在开会”。"""

    async def scenario():
        p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0)
        prov = FakeProvider(delay=0.3)
        p.context = type("Ctx", (), {
            "get_provider_by_id": lambda self, pid: prov,
            "get_using_tts_provider_async": lambda self, umo: _async(prov),
        })()
        first = Event(text="晚安", reply="<语音>早点睡吧。")
        second = Event(text="别发语音，我在开会", reply="好。")
        task = asyncio.create_task(p.maybe_tts(first))
        await asyncio.sleep(0.05)
        await p.maybe_tts(second)     # 合成还没结束，对方改主意了
        await task
        return first, second

    first, _ = asyncio.run(scenario())
    from astrbot.api.message_components import Plain, Record

    chain = first.result.chain
    assert not any(isinstance(c, Record) for c in chain), "拒绝之后不该再发语音"
    assert [c.text for c in chain if isinstance(c, Plain)], "文字要留着"


def test_newer_turn_drops_older_voice(tmp_path):
    """对方已经继续说下一句了，上一条的语音就别补发了。"""

    async def scenario():
        p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0)
        prov = FakeProvider(delay=0.3)
        p.context = type("Ctx", (), {
            "get_provider_by_id": lambda self, pid: prov,
            "get_using_tts_provider_async": lambda self, umo: _async(prov),
        })()
        first = Event(text="晚安", reply="<语音>早点睡吧。")
        second = Event(text="还没睡吗", reply="快了。")
        task = asyncio.create_task(p.maybe_tts(first))
        await asyncio.sleep(0.05)
        await p.maybe_tts(second)
        await task
        return first

    first = asyncio.run(scenario())
    from astrbot.api.message_components import Record

    assert not any(isinstance(c, Record) for c in first.result.chain)


def test_explicit_request_is_not_dropped_by_old_text_preference(tmp_path):
    """先前的文字偏好不该把后来的点名请求也挡掉。"""
    from conftest import run

    p, _ = make_plugin(tmp_path, max_chars=50, max_chars_user=2000, random_rate=0)
    run(p, Event(text="别发语音", reply="好。"))
    voiced, _ = run(p, Event(text="这句念给我听", reply="那就说一句。"))
    assert voiced is True


async def _async(value):
    return value
