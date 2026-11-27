import asyncio

from cogs.ping import PingCog
from tests.fakes import FakeBot, FakeInteraction, invoke


def ping(latency: float):
    cog = PingCog(FakeBot(latency=latency))
    return asyncio.run(invoke(cog, "ping", FakeInteraction()))


def test_ping_reports_latency_in_whole_milliseconds():
    assert "123 ms" in ping(0.1234).content


def test_ping_reply_is_only_shown_to_whoever_asked():
    assert ping(0.05).ephemeral


def test_ping_before_first_heartbeat_does_not_claim_a_latency():
    # discord.py reports latency as inf until the first heartbeat is acknowledged.
    reply = ping(float("inf")).content
    assert "ms" not in reply
    assert "not measured yet" in reply
