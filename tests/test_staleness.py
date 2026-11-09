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


def test_second_message_arriving_kills_the_running_voice(tmp_path):
    """审计复现的窗口：第二条消息已经进来，但它的回复还没进入装饰阶段。"""

    async def scenario():
        p, _ = make_plugin(tmp_path, max_chars=50, random_rate=0)
        prov = FakeProvider(path=str(tmp_path / "s.wav"), delay=0.3)
        p.context = type("Ctx", (), {
            "get_provider_by_id": lambda self, pid: prov,
            "get_using_tts_provider_async": lambda self, umo: _async(prov),
        })()
        first = Event(text="晚安", reply="<语音>早点睡吧。")
        second = Event(text="还没睡吗", reply="快了。")
        task = asyncio.create_task(p.maybe_tts(first))
        await asyncio.sleep(0.05)
        # 只走到「消息进来、模型还没生成」这一步，回复尚未进入装饰阶段
        await p.track_voice_pref(second, type("Req", (), {"system_prompt": ""})())
        await task
        return first

    first = asyncio.run(scenario())
    from astrbot.api.message_components import Record

    assert not any(isinstance(c, Record) for c in first.result.chain), \
        "对方已经继续说话了，旧语音不该再发"


def test_turn_is_bound_to_the_event_not_recomputed(tmp_path):
    """同一轮里 on_llm_request 和 on_decorating_result 要认同一个轮次，不能各加一次。"""
    p, _ = make_plugin(tmp_path, max_chars=50)
    ev = Event(text="晚安", reply="早点睡吧。")
    req = type("Req", (), {"system_prompt": ""})()
    import asyncio

    asyncio.run(p.track_voice_pref(ev, req))
    conv = ev.unified_msg_origin
    after_hook = p._turn_seq[conv]
    assert ev.get_extra("tts_turn") == after_hook
    assert p._turn_of(ev, conv) == after_hook


def test_turn_bumps_once_per_message(tmp_path):
    p, _ = make_plugin(tmp_path, max_chars=50)
    import asyncio

    req = type("Req", (), {"system_prompt": ""})()
    ev = Event(text="A", reply="x")
    asyncio.run(p.track_voice_pref(ev, req))
    conv = ev.unified_msg_origin
    first = p._turn_seq[conv]
    asyncio.run(p.track_voice_pref(ev, req))      # 同一条消息重复走钩子
    assert p._turn_seq[conv] == first, "同一条消息不能占两个轮次"
