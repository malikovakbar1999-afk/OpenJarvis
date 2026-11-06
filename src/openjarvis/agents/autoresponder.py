"""AutoResponder — automatic replies for messenger channels.

Attaches to any :class:`~openjarvis.channels._stubs.BaseChannel` (Telegram,
WhatsApp, Slack, Discord, Messenger, ...) and answers incoming messages on
the owner's behalf — e.g. while they are away, asleep, or busy.

Decision pipeline for every inbound message (first match wins):

1. responder paused / disabled         -> no reply
2. empty message                       -> no reply
3. sender ignored, blocked, or not in the allowlist -> no reply
4. outside the configured active schedule           -> no reply
5. urgent keyword                      -> ``on_urgent`` callback + urgent reply
                                          (bypasses the cooldown)
6. conversation still in cooldown      -> no reply
7. keyword / regex rule                -> canned rule reply
8. agent configured                    -> LLM reply using the conversation
                                          history (falls back to the away
                                          message on error / empty output)
9. otherwise                           -> away message

The cooldown makes sure a chatty contact receives one auto-reply per
``cooldown_seconds`` instead of one per message.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dtime
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple

from openjarvis.channels._stubs import BaseChannel, ChannelMessage
from openjarvis.core.types import Conversation, Message, Role

logger = logging.getLogger(__name__)

DEFAULT_AWAY_MESSAGE = (
    "Hi! I'm away right now and will get back to you as soon as I can."
)
DEFAULT_URGENT_REPLY = (
    "Got it — this looks urgent, I've been notified and will reply ASAP."
)
DEFAULT_SYSTEM_PROMPT = (
    "You are an auto-responder replying on behalf of {owner} in a personal "
    "messenger while they are unavailable. Reply briefly (1-3 sentences), "
    "politely, in the same language as the sender. Make it clear that "
    "{owner} is currently unavailable and will reply later. Never make "
    "promises, commitments, payments, or share private information on "
    "{owner}'s behalf. If you cannot help, just say {owner} will reply later."
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AutoReplyRule:
    """A keyword / regex rule mapped to a canned reply.

    ``pattern`` is a regular expression matched case-insensitively with
    :func:`re.search` against the message text.
    """

    pattern: str
    reply: str
    name: str = ""
    _compiled: Optional[re.Pattern[str]] = field(default=None, repr=False)

    def matches(self, text: str) -> bool:
        if self._compiled is None:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)
        return bool(self._compiled.search(text))


def _parse_hhmm(value: str) -> dtime:
    try:
        hours, minutes = value.strip().split(":")
        return dtime(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid time {value!r}, expected 'HH:MM'") from exc


@dataclass(slots=True)
class AutoResponderConfig:
    """Settings for :class:`AutoResponderAgent`.

    Parameters
    ----------
    owner_name:
        How the responder refers to the owner in LLM replies.
    away_message:
        Default reply when no rule matches and no agent is configured.
    rules:
        Keyword rules checked before the LLM.
    active_hours:
        ``("HH:MM", "HH:MM")`` window during which the responder is active.
        Windows may wrap midnight (``("22:00", "08:00")``). ``None`` means
        always active.
    active_weekdays:
        Weekdays (0=Monday … 6=Sunday) on which the responder is active.
        ``None`` means every day. For windows wrapping midnight the weekday
        of the window *start* is used.
    cooldown_seconds:
        Minimum gap between two auto-replies in the same conversation.
    allowlist / blocklist / ignore_senders:
        Sender filters. ``ignore_senders`` is meant for the owner's own ids
        and other bots so the responder never talks to itself.
    urgent_keywords:
        Regexes that flag a message as urgent.
    history_size:
        Number of recent messages per conversation given to the LLM.
    reply_prefix / reply_suffix:
        Text added around every reply (e.g. ``"[auto-reply] "``).
    max_reply_chars:
        Replies longer than this are truncated.
    """

    enabled: bool = True
    owner_name: str = "the owner"
    away_message: str = DEFAULT_AWAY_MESSAGE
    urgent_reply: str = DEFAULT_URGENT_REPLY
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    rules: List[AutoReplyRule] = field(default_factory=list)
    active_hours: Optional[Tuple[str, str]] = None
    active_weekdays: Optional[Sequence[int]] = None
    cooldown_seconds: float = 3600.0
    allowlist: Sequence[str] = ()
    blocklist: Sequence[str] = ()
    ignore_senders: Sequence[str] = ()
    urgent_keywords: Sequence[str] = ()
    history_size: int = 10
    reply_prefix: str = ""
    reply_suffix: str = ""
    max_reply_chars: int = 1000

    def __post_init__(self) -> None:
        if self.active_hours is not None:
            start, end = self.active_hours
            # Validate eagerly so a bad config fails at startup.
            _parse_hhmm(start)
            _parse_hhmm(end)
        if self.active_weekdays is not None:
            for day in self.active_weekdays:
                if not 0 <= int(day) <= 6:
                    raise ValueError(f"Invalid weekday {day!r}, expected 0-6")
        for pattern in self.urgent_keywords:
            re.compile(pattern)
        for rule in self.rules:
            re.compile(rule.pattern)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AutoResponderConfig":
        """Build a config from a plain dict (e.g. a TOML ``[autoresponder]``
        table). Rules may be given as ``{"pattern": ..., "reply": ...}``."""
        data = dict(data)
        rules = [
            r if isinstance(r, AutoReplyRule) else AutoReplyRule(**r)
            for r in data.pop("rules", []) or []
        ]
        hours = data.pop("active_hours", None)
        if hours is not None:
            hours = tuple(hours)
            if len(hours) != 2:
                raise ValueError("active_hours must be [start, end]")
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown autoresponder options: {sorted(unknown)}")
        return cls(rules=rules, active_hours=hours, **data)


# ---------------------------------------------------------------------------
# Decision type
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AutoReplyDecision:
    """Outcome of :meth:`AutoResponderAgent.decide`."""

    reply: Optional[str]
    reason: str
    urgent: bool = False

    @property
    def should_reply(self) -> bool:
        return bool(self.reply)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class AutoResponderAgent:
    """Auto-reply bridge between a messenger channel and (optionally) an LLM.

    Parameters
    ----------
    channel:
        Channel to listen on and reply through.
    config:
        Behaviour settings; defaults to :class:`AutoResponderConfig()`.
    agent:
        Optional object exposing ``run(input, context=...)`` returning an
        :class:`~openjarvis.agents._stubs.AgentResult` (any ``BaseAgent``).
        Without it, only rules and the away message are used.
    on_urgent:
        Callback invoked with the :class:`ChannelMessage` when an urgent
        message arrives (e.g. send a push notification to the owner).
    on_reply:
        Callback invoked with ``(message, decision)`` after a reply is sent.
    clock / now:
        Injectable time sources (monotonic seconds / wall-clock datetime)
        for testing.
    synchronous:
        Process messages inline instead of in a thread pool.
    """

    def __init__(
        self,
        channel: BaseChannel,
        config: Optional[AutoResponderConfig] = None,
        *,
        agent: Any = None,
        on_urgent: Optional[Callable[[ChannelMessage], None]] = None,
        on_reply: Optional[Callable[[ChannelMessage, AutoReplyDecision], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        synchronous: bool = False,
        max_workers: int = 2,
    ) -> None:
        self._channel = channel
        self.config = config or AutoResponderConfig()
        self._agent = agent
        self._on_urgent = on_urgent
        self._on_reply = on_reply
        self._clock = clock
        self._now = now
        self._paused = False
        self._lock = threading.Lock()
        self._last_reply: Dict[str, float] = {}
        self._history: Dict[str, Deque[Message]] = {}
        self._pool = (
            None if synchronous else ThreadPoolExecutor(max_workers=max_workers)
        )
        self._urgent_res = [
            re.compile(p, re.IGNORECASE) for p in self.config.urgent_keywords
        ]
        channel.on_message(self._handle_message)

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """Temporarily stop auto-replying (e.g. the owner is back)."""
        self._paused = True

    def resume(self) -> None:
        """Resume auto-replying."""
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def reset_cooldown(self, conversation_id: Optional[str] = None) -> None:
        """Forget cooldowns for one conversation, or all of them."""
        with self._lock:
            if conversation_id is None:
                self._last_reply.clear()
            else:
                self._last_reply.pop(conversation_id, None)

    def history(self, conversation_id: str) -> List[Message]:
        """Return the recorded history for *conversation_id*."""
        with self._lock:
            return list(self._history.get(conversation_id, ()))

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    @staticmethod
    def _conversation_key(msg: ChannelMessage) -> str:
        return msg.conversation_id or msg.sender

    def is_active(self, when: Optional[datetime] = None) -> bool:
        """Whether the schedule allows auto-replies at *when*."""
        when = when or self._now()
        cfg = self.config
        weekday = when.weekday()
        if cfg.active_hours is None:
            return cfg.active_weekdays is None or weekday in cfg.active_weekdays

        start = _parse_hhmm(cfg.active_hours[0])
        end = _parse_hhmm(cfg.active_hours[1])
        current = when.time().replace(second=0, microsecond=0)
        if start == end:
            in_window = True
        elif start < end:
            in_window = start <= current < end
        else:  # wraps midnight
            in_window = current >= start or current < end
            if in_window and current < end:
                # Early-morning part belongs to the previous day's window.
                weekday = (weekday - 1) % 7
        if not in_window:
            return False
        return cfg.active_weekdays is None or weekday in cfg.active_weekdays

    def _sender_allowed(self, sender: str) -> bool:
        cfg = self.config
        if sender in cfg.ignore_senders or sender in cfg.blocklist:
            return False
        if cfg.allowlist and sender not in cfg.allowlist:
            return False
        return True

    def _is_urgent(self, text: str) -> bool:
        return any(r.search(text) for r in self._urgent_res)

    def decide(self, msg: ChannelMessage) -> AutoReplyDecision:
        """Decide how (and whether) to answer *msg* without sending anything.

        Does not update cooldown state; :meth:`process` does that.
        """
        cfg = self.config
        text = (msg.content or "").strip()
        if self._paused or not cfg.enabled:
            return AutoReplyDecision(None, "paused")
        if not text:
            return AutoReplyDecision(None, "empty")
        if not self._sender_allowed(msg.sender):
            return AutoReplyDecision(None, "sender_filtered")
        if not self.is_active():
            return AutoReplyDecision(None, "outside_schedule")

        if self._is_urgent(text):
            return AutoReplyDecision(cfg.urgent_reply, "urgent", urgent=True)

        key = self._conversation_key(msg)
        with self._lock:
            last = self._last_reply.get(key)
        if last is not None and self._clock() - last < cfg.cooldown_seconds:
            return AutoReplyDecision(None, "cooldown")

        for rule in cfg.rules:
            if rule.matches(text):
                return AutoReplyDecision(
                    rule.reply, f"rule:{rule.name or rule.pattern}"
                )

        if self._agent is not None:
            generated = self._generate_reply(msg)
            if generated:
                return AutoReplyDecision(generated, "agent")
            return AutoReplyDecision(cfg.away_message, "agent_fallback")

        return AutoReplyDecision(cfg.away_message, "away_message")

    def _generate_reply(self, msg: ChannelMessage) -> str:
        from openjarvis.agents._stubs import AgentContext

        cfg = self.config
        key = self._conversation_key(msg)
        conversation = Conversation()
        conversation.add(
            Message(
                role=Role.SYSTEM,
                content=cfg.system_prompt.format(owner=cfg.owner_name),
            )
        )
        # History already contains the current message as its last entry;
        # it is passed separately as the agent input.
        for past in self.history(key)[:-1]:
            conversation.add(past)
        context = AgentContext(
            conversation=conversation,
            metadata={
                "autoresponder": True,
                "channel": msg.channel,
                "sender": msg.sender,
                "conversation_id": key,
            },
        )
        try:
            result = self._agent.run(msg.content, context=context)
        except Exception:  # noqa: BLE001
            logger.exception("AutoResponder agent failed; using away message")
            return ""
        content = getattr(result, "content", result)
        return str(content or "").strip()

    def _format_reply(self, reply: str) -> str:
        cfg = self.config
        if len(reply) > cfg.max_reply_chars:
            reply = reply[: max(cfg.max_reply_chars - 1, 0)].rstrip() + "…"
        return f"{cfg.reply_prefix}{reply}{cfg.reply_suffix}"

    def _record(self, key: str, message: Message) -> None:
        with self._lock:
            hist = self._history.get(key)
            if hist is None:
                hist = deque(maxlen=max(self.config.history_size, 1))
                self._history[key] = hist
            hist.append(message)

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_message(self, msg: ChannelMessage) -> Optional[str]:
        if self._pool is None:
            self.process(msg)
        else:
            self._pool.submit(self.process, msg)
        return None

    def process(self, msg: ChannelMessage) -> AutoReplyDecision:
        """Decide on *msg*, send the reply, and update state."""
        key = self._conversation_key(msg)
        if msg.content and msg.sender not in self.config.ignore_senders:
            self._record(
                key, Message(role=Role.USER, content=msg.content, name=msg.sender)
            )

        decision = self.decide(msg)

        if decision.urgent and self._on_urgent is not None:
            try:
                self._on_urgent(msg)
            except Exception:  # noqa: BLE001
                logger.exception("AutoResponder on_urgent callback failed")

        if not decision.should_reply:
            logger.debug("AutoResponder skipped %s: %s", key, decision.reason)
            return decision

        reply = self._format_reply(decision.reply or "")
        decision.reply = reply
        try:
            sent = self._channel.send(
                msg.conversation_id or msg.sender,
                reply,
                conversation_id=msg.message_id,
                metadata={"autoresponder": True, "reason": decision.reason},
            )
        except Exception:  # noqa: BLE001
            logger.exception("AutoResponder failed to send reply")
            sent = False

        if sent:
            with self._lock:
                self._last_reply[key] = self._clock()
            self._record(key, Message(role=Role.ASSISTANT, content=reply))
            if self._on_reply is not None:
                try:
                    self._on_reply(msg, decision)
                except Exception:  # noqa: BLE001
                    logger.exception("AutoResponder on_reply callback failed")
        else:
            decision.reason = f"{decision.reason}:send_failed"
        return decision


__all__ = [
    "AutoReplyDecision",
    "AutoReplyRule",
    "AutoResponderAgent",
    "AutoResponderConfig",
    "DEFAULT_AWAY_MESSAGE",
    "DEFAULT_SYSTEM_PROMPT",
    "DEFAULT_URGENT_REPLY",
]
