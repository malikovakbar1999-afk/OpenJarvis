#!/usr/bin/env python3
"""Messenger auto-responder — answer messages while you are away.

Usage:
    python examples/messaging_hub/autoresponder.py --demo            # rules only
    python examples/messaging_hub/autoresponder.py --demo --llm      # + local LLM
    python examples/messaging_hub/autoresponder.py --telegram        # live bot

``--telegram`` needs ``TELEGRAM_BOT_TOKEN`` in the environment.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

import click

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

CONFIG = AutoResponderConfig(
    owner_name="Akbar",
    away_message="Привет! Сейчас не на связи, отвечу позже.",
    urgent_reply="Понял, что срочно — я уже получил уведомление и отвечу ASAP.",
    urgent_keywords=[r"срочно", r"urgent", r"asap"],
    rules=[
        AutoReplyRule(
            name="address",
            pattern=r"адрес|address",
            reply="Адрес: ул. Примерная, 1. Отвечу подробнее позже.",
        ),
    ],
    cooldown_seconds=1800,
    reply_prefix="🤖 ",
)

DEMO = [
    ("alice", "Привет! Как дела?"),
    ("alice", "Ты тут?"),  # cooldown -> no reply
    ("bob", "Подскажи адрес офиса"),
    ("carol", "СРОЧНО перезвони!"),
]


class ConsoleChannel(BaseChannel):
    """In-process channel that prints replies to stdout."""

    channel_id = "console"

    def __init__(self) -> None:
        self._handlers: List[ChannelHandler] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def send(self, channel, content, *, conversation_id="", metadata=None) -> bool:
        click.echo(f"    -> reply to {channel}: {content}")
        return True

    def status(self) -> ChannelStatus:
        return ChannelStatus.CONNECTED

    def list_channels(self) -> List[str]:
        return ["console"]

    def on_message(self, handler: ChannelHandler) -> None:
        self._handlers.append(handler)

    def inject(self, msg: ChannelMessage) -> None:
        for handler in self._handlers:
            handler(msg)


class JarvisAdapter:
    """Adapts :class:`openjarvis.Jarvis` to the ``agent.run`` interface."""

    def __init__(self, model: str, engine_key: str) -> None:
        from openjarvis import Jarvis

        self._jarvis = Jarvis(model=model, engine_key=engine_key)

    def run(self, input: str, context: Any = None, **_: Dict[str, Any]) -> AgentResult:
        lines = [m.content for m in context.conversation.messages] if context else []
        prompt = "\n".join([*lines, f"New message: {input}", "Your reply:"])
        return AgentResult(content=self._jarvis.ask(prompt, context=False))


@click.command()
@click.option("--demo", is_flag=True, help="Run on sample messages.")
@click.option("--telegram", is_flag=True, help="Run as a live Telegram bot.")
@click.option("--llm", is_flag=True, help="Generate replies with an LLM.")
@click.option("--model", default="qwen3:8b", show_default=True)
@click.option("--engine", "engine_key", default="ollama", show_default=True)
def main(demo: bool, telegram: bool, llm: bool, model: str, engine_key: str) -> None:
    agent = JarvisAdapter(model, engine_key) if llm else None

    def notify(msg: ChannelMessage) -> None:
        click.echo(f"    !! URGENT from {msg.sender}: {msg.content}")

    if telegram:
        from openjarvis.channels.telegram import TelegramChannel

        channel = TelegramChannel()
        responder = AutoResponderAgent(channel, CONFIG, agent=agent, on_urgent=notify)
        channel.connect()
        click.echo("Auto-responder running on Telegram. Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            responder.shutdown()
            channel.disconnect()
        return

    if not demo:
        raise click.UsageError("Pass --demo or --telegram")

    channel = ConsoleChannel()
    AutoResponderAgent(channel, CONFIG, agent=agent, on_urgent=notify, synchronous=True)
    for i, (sender, text) in enumerate(DEMO):
        click.echo(f"[{sender}] {text}")
        channel.inject(
            ChannelMessage(
                channel="console",
                sender=sender,
                content=text,
                message_id=str(i),
                conversation_id=sender,
            )
        )


if __name__ == "__main__":
    main()
