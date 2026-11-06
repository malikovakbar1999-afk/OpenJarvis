"""Tests for the messenger AutoResponderAgent."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import pytest

from openjarvis.agents._stubs import AgentResult
from openjarvis.agents.autoresponder import (
    AutoReplyRule,
    AutoResponderAgent,
    AutoResponderConfig,
)
from openjarvis.channels._stubs import (
    BaseChannel,
    ChannelHandler,
    ChannelMessage,
    ChannelStatus,
)
from openjarvis.core.types import Role


class FakeChannel(BaseChannel):
    channel_id = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self._handlers: List[ChannelHandler] = []
        self.sent: List[Dict[str, Any]] = []
        self.fail = fail

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def send(self, channel, content, *, conversation_id="", metadata=None) -> bool:
        if self.fail:
            return False
        self.sent.append(
            {
                "channel": channel,
                "content": content,
                "conversation_id": conversation_id,
                "metadata": metadata,
            }
        )
        return True

    def status(self) -> ChannelStatus:
        return ChannelStatus.CONNECTED

    def list_channels(self) -> List[str]:
        return ["fake"]

    def on_message(self, handler: ChannelHandler) -> None:
        self._handlers.append(handler)

    def simulate(self, msg: ChannelMessage) -> None:
        for h in self._handlers:
            h(msg)


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class RecordingAgent:
    def __init__(self, reply: str = "LLM reply", exc: Exception | None = None):
        self.reply = reply
        self.exc = exc
        self.calls: List[Dict[str, Any]] = []

    def run(self, input: str, context=None, **kwargs) -> AgentResult:
        self.calls.append({"input": input, "context": context})
        if self.exc:
            raise self.exc
        return AgentResult(content=self.reply)


def _msg(text="hello", sender="alice", conv="chat-1", mid="m1") -> ChannelMessage:
    return ChannelMessage(
        channel="telegram",
        sender=sender,
        content=text,
        message_id=mid,
        conversation_id=conv,
    )


def _make(config=None, **kwargs):
    channel = FakeChannel(fail=kwargs.pop("fail", False))
    clock = kwargs.pop("clock", FakeClock())
    responder = AutoResponderAgent(
        channel, config, clock=clock, synchronous=True, **kwargs
    )
    return channel, responder, clock


class TestBasicReplies:
    def test_away_message_sent_via_channel(self):
        channel, _, _ = _make(AutoResponderConfig(away_message="Away!"))
        channel.simulate(_msg())
        assert channel.sent == [
            {
                "channel": "chat-1",
                "content": "Away!",
                "conversation_id": "m1",
                "metadata": {"autoresponder": True, "reason": "away_message"},
            }
        ]

    def test_falls_back_to_sender_when_no_conversation_id(self):
        channel, _, _ = _make()
        channel.simulate(_msg(conv=""))
        assert channel.sent[0]["channel"] == "alice"

    def test_prefix_suffix_and_truncation(self):
        cfg = AutoResponderConfig(
            away_message="x" * 50,
            reply_prefix="[auto] ",
            reply_suffix=" -bot",
            max_reply_chars=10,
        )
        channel, _, _ = _make(cfg)
        channel.simulate(_msg())
        content = channel.sent[0]["content"]
        assert content.startswith("[auto] ")
        assert content.endswith("… -bot")
        assert len(content) == len("[auto] ") + 10 + len(" -bot")

    def test_empty_message_ignored(self):
        channel, responder, _ = _make()
        decision = responder.process(_msg(text="   "))
        assert decision.reason == "empty"
        assert channel.sent == []


class TestCooldown:
    def test_one_reply_per_cooldown(self):
        channel, _, clock = _make(AutoResponderConfig(cooldown_seconds=60))
        channel.simulate(_msg(mid="1"))
        channel.simulate(_msg(mid="2"))
        assert len(channel.sent) == 1
        clock.t += 61
        channel.simulate(_msg(mid="3"))
        assert len(channel.sent) == 2

    def test_cooldown_is_per_conversation(self):
        channel, _, _ = _make()
        channel.simulate(_msg(conv="a"))
        channel.simulate(_msg(conv="b"))
        assert len(channel.sent) == 2

    def test_reset_cooldown(self):
        channel, responder, _ = _make()
        channel.simulate(_msg())
        responder.reset_cooldown("chat-1")
        channel.simulate(_msg())
        assert len(channel.sent) == 2

    def test_failed_send_does_not_start_cooldown(self):
        channel, responder, _ = _make(fail=True)
        decision = responder.process(_msg())
        assert decision.reason == "away_message:send_failed"
        channel.fail = False
        assert responder.process(_msg()).reason == "away_message"


class TestFilters:
    def test_ignore_own_messages(self):
        channel, _, _ = _make(AutoResponderConfig(ignore_senders=["me"]))
        channel.simulate(_msg(sender="me"))
        assert channel.sent == []

    def test_blocklist(self):
        channel, _, _ = _make(AutoResponderConfig(blocklist=["spam"]))
        channel.simulate(_msg(sender="spam"))
        assert channel.sent == []

    def test_allowlist(self):
        channel, _, _ = _make(AutoResponderConfig(allowlist=["mom"]))
        channel.simulate(_msg(sender="stranger", conv="x"))
        channel.simulate(_msg(sender="mom", conv="y"))
        assert [s["channel"] for s in channel.sent] == ["y"]

    def test_pause_resume(self):
        channel, responder, _ = _make()
        responder.pause()
        channel.simulate(_msg())
        assert channel.sent == []
        responder.resume()
        channel.simulate(_msg())
        assert len(channel.sent) == 1

    def test_disabled(self):
        channel, _, _ = _make(AutoResponderConfig(enabled=False))
        channel.simulate(_msg())
        assert channel.sent == []


class TestSchedule:
    def _resp(self, hours=None, weekdays=None, now=None):
        cfg = AutoResponderConfig(active_hours=hours, active_weekdays=weekdays)
        return _make(cfg, now=lambda: now)[1]

    def test_daytime_window(self):
        r = self._resp(("09:00", "18:00"))
        assert r.is_active(datetime(2026, 11, 6, 12, 0))
        assert not r.is_active(datetime(2026, 11, 6, 18, 0))
        assert not r.is_active(datetime(2026, 11, 6, 8, 59))

    def test_overnight_window(self):
        r = self._resp(("22:00", "08:00"))
        assert r.is_active(datetime(2026, 11, 6, 23, 30))
        assert r.is_active(datetime(2026, 11, 6, 3, 0))
        assert not r.is_active(datetime(2026, 11, 6, 12, 0))

    def test_overnight_uses_start_weekday(self):
        # Friday 22:00 -> Saturday 08:00 active only for Friday (4).
        r = self._resp(("22:00", "08:00"), weekdays=[4])
        assert r.is_active(datetime(2026, 11, 6, 23, 0))  # Friday night
        assert r.is_active(datetime(2026, 11, 7, 2, 0))  # Saturday early
        assert not r.is_active(datetime(2026, 11, 5, 2, 0))  # Thursday early

    def test_weekdays_only(self):
        r = self._resp(weekdays=[5, 6])
        assert r.is_active(datetime(2026, 11, 7, 12, 0))  # Saturday
        assert not r.is_active(datetime(2026, 11, 6, 12, 0))  # Friday

    def test_outside_schedule_no_reply(self):
        cfg = AutoResponderConfig(active_hours=("22:00", "08:00"))
        channel, responder, _ = _make(cfg, now=lambda: datetime(2026, 11, 6, 12))
        assert responder.process(_msg()).reason == "outside_schedule"
        assert channel.sent == []

    def test_invalid_config(self):
        with pytest.raises(ValueError):
            AutoResponderConfig(active_hours=("25", "08:00"))
        with pytest.raises(ValueError):
            AutoResponderConfig(active_weekdays=[7])


class TestRulesAndUrgent:
    def test_rule_reply(self):
        cfg = AutoResponderConfig(
            rules=[AutoReplyRule(pattern=r"\bprice\b|цена", reply="See site", name="p")]
        )
        channel, responder, _ = _make(cfg)
        decision = responder.process(_msg("Какая ЦЕНА?"))
        assert decision.reason == "rule:p"
        assert channel.sent[0]["content"] == "See site"

    def test_rule_beats_agent(self):
        agent = RecordingAgent()
        cfg = AutoResponderConfig(rules=[AutoReplyRule("hours", "9-18")])
        channel, _, _ = _make(cfg, agent=agent)
        channel.simulate(_msg("your hours?"))
        assert channel.sent[0]["content"] == "9-18"
        assert agent.calls == []

    def test_urgent_bypasses_cooldown_and_notifies(self):
        urgent: List[ChannelMessage] = []
        cfg = AutoResponderConfig(urgent_keywords=[r"urgent|срочно"])
        channel, _, _ = _make(cfg, on_urgent=urgent.append)
        channel.simulate(_msg("hi"))
        channel.simulate(_msg("СРОЧНО перезвони"))
        assert len(channel.sent) == 2
        assert channel.sent[1]["content"] == cfg.urgent_reply
        assert len(urgent) == 1

    def test_on_reply_callback(self):
        seen = []
        channel, _, _ = _make(on_reply=lambda m, d: seen.append(d.reason))
        channel.simulate(_msg())
        assert seen == ["away_message"]


class TestAgent:
    def test_agent_reply_with_history(self):
        agent = RecordingAgent("I'll pass it on")
        cfg = AutoResponderConfig(owner_name="Akbar", cooldown_seconds=0)
        channel, responder, _ = _make(cfg, agent=agent)
        channel.simulate(_msg("first"))
        channel.simulate(_msg("second"))
        assert channel.sent[-1]["content"] == "I'll pass it on"

        ctx = agent.calls[-1]["context"]
        assert agent.calls[-1]["input"] == "second"
        msgs = ctx.conversation.messages
        assert msgs[0].role == Role.SYSTEM and "Akbar" in msgs[0].content
        assert [m.content for m in msgs[1:]] == ["first", "I'll pass it on"]
        assert ctx.metadata["autoresponder"] is True
        assert len(responder.history("chat-1")) == 4

    def test_agent_error_falls_back(self):
        agent = RecordingAgent(exc=RuntimeError("boom"))
        channel, responder, _ = _make(
            AutoResponderConfig(away_message="later"), agent=agent
        )
        decision = responder.process(_msg())
        assert decision.reason == "agent_fallback"
        assert channel.sent[0]["content"] == "later"

    def test_agent_empty_falls_back(self):
        channel, _, _ = _make(agent=RecordingAgent(""))
        channel.simulate(_msg())
        assert channel.sent[0]["metadata"]["reason"] == "agent_fallback"

    def test_history_capped(self):
        cfg = AutoResponderConfig(history_size=3, cooldown_seconds=0)
        channel, responder, _ = _make(cfg)
        for i in range(5):
            channel.simulate(_msg(str(i)))
        assert len(responder.history("chat-1")) == 3


class TestFromDict:
    def test_from_dict(self):
        cfg = AutoResponderConfig.from_dict(
            {
                "owner_name": "Akbar",
                "active_hours": ["22:00", "08:00"],
                "rules": [{"pattern": "hi", "reply": "hello"}],
                "cooldown_seconds": 10,
            }
        )
        assert cfg.active_hours == ("22:00", "08:00")
        assert cfg.rules[0].reply == "hello"

    def test_unknown_key(self):
        with pytest.raises(ValueError):
            AutoResponderConfig.from_dict({"bogus": 1})


def test_threaded_mode_replies():
    channel = FakeChannel()
    responder = AutoResponderAgent(channel)
    channel.simulate(_msg())
    responder.shutdown()
    assert len(channel.sent) == 1


class TestOwnerMessages:
    def _owner(self, text="I'm here", conv="chat-1"):
        msg = _msg(text, sender="owner", conv=conv)
        msg.metadata["from_owner"] = True
        return msg

    def test_owner_message_never_answered_and_quiets_chat(self):
        channel, responder, clock = _make(AutoResponderConfig(cooldown_seconds=60))
        assert responder.process(self._owner()).reason == "owner_message"
        assert responder.process(_msg()).reason == "cooldown"
        assert channel.sent == []
        clock.t += 61
        assert responder.process(_msg()).reason == "away_message"

    def test_owner_message_recorded_as_assistant(self):
        _, responder, _ = _make()
        responder.process(self._owner("on my way"))
        [entry] = responder.history("chat-1")
        assert entry.role == Role.ASSISTANT and entry.content == "on my way"

    def test_quiet_after_owner_reply_disabled(self):
        cfg = AutoResponderConfig(quiet_after_owner_reply=False)
        channel, responder, _ = _make(cfg)
        responder.process(self._owner())
        assert responder.process(_msg()).reason == "away_message"

    def test_urgent_still_answered_after_owner_message(self):
        cfg = AutoResponderConfig(urgent_keywords=["urgent"])
        channel, responder, _ = _make(cfg)
        responder.process(self._owner())
        assert responder.process(_msg("urgent!")).reason == "urgent"


def test_business_connection_forwarded_to_reply():
    channel, responder, _ = _make()
    msg = _msg()
    msg.metadata.update({"business_connection_id": "bc-9", "from_owner": False})
    responder.process(msg)
    assert channel.sent[0]["metadata"]["business_connection_id"] == "bc-9"
