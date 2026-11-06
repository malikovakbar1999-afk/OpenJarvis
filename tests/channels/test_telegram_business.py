"""Tests for Telegram Business support in TelegramChannel."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from openjarvis.channels.telegram import TelegramChannel

OWNER_ID = 500
CONTACT_ID = 777


@pytest.fixture(autouse=True)
def _no_business_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BUSINESS", raising=False)


def _business_update(
    *, sender=CONTACT_ID, chat=CONTACT_ID, text="hi", bot=None, conn="bc-1"
):
    return SimpleNamespace(
        message=None,
        business_message=SimpleNamespace(
            from_user=SimpleNamespace(id=sender),
            text=text,
            message_id=42,
            chat=SimpleNamespace(id=chat),
            business_connection_id=conn,
            sender_business_bot=bot,
        ),
    )


def _deliver(monkeypatch, channel, updates):
    """Run ``_poll_loop`` with a fake PTB and feed *updates* to its handler."""
    handlers = []
    app = SimpleNamespace(add_handler=handlers.append)
    builder = MagicMock()
    builder.token.return_value = builder
    builder.build.return_value = app
    monkeypatch.setitem(
        sys.modules,
        "telegram.ext",
        SimpleNamespace(
            ApplicationBuilder=lambda: builder,
            MessageHandler=lambda _f, callback: SimpleNamespace(callback=callback),
            filters=SimpleNamespace(TEXT=object()),
        ),
    )

    async def fake_lifecycle(_app, _stop):
        for update in updates:
            await handlers[0].callback(update, None)

    monkeypatch.setattr(channel, "_run_polling_lifecycle", fake_lifecycle)
    received = []
    channel.on_message(received.append)
    channel._poll_loop()
    return received


class TestBusinessReceive:
    def test_disabled_by_default_ignores_business_messages(self, monkeypatch):
        ch = TelegramChannel(bot_token="t")
        assert _deliver(monkeypatch, ch, [_business_update()]) == []

    def test_env_var_enables_business(self):
        with patch.dict(os.environ, {"TELEGRAM_BUSINESS": "true"}):
            assert TelegramChannel(bot_token="t")._business is True
        with patch.dict(os.environ, {"TELEGRAM_BUSINESS": "1"}):
            assert TelegramChannel(bot_token="t", business=False)._business is False

    def test_contact_message_dispatched(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True)
        [cm] = _deliver(monkeypatch, ch, [_business_update(text="Привет")])
        assert cm.content == "Привет"
        assert cm.sender == str(CONTACT_ID)
        assert cm.conversation_id == str(CONTACT_ID)
        assert cm.message_id == "42"
        assert cm.metadata == {"business_connection_id": "bc-1", "from_owner": False}

    def test_owner_message_flagged(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True)
        [cm] = _deliver(monkeypatch, ch, [_business_update(sender=OWNER_ID)])
        assert cm.metadata["from_owner"] is True

    def test_bot_echo_dropped(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True)
        update = _business_update(sender=OWNER_ID, bot=SimpleNamespace(id=1))
        assert _deliver(monkeypatch, ch, [update]) == []

    def test_regular_messages_still_work(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True)
        update = SimpleNamespace(
            message=SimpleNamespace(
                from_user=SimpleNamespace(id=1),
                text="direct",
                message_id=1,
                chat=SimpleNamespace(id=1),
            )
        )
        [cm] = _deliver(monkeypatch, ch, [update])
        assert cm.content == "direct"
        assert cm.metadata == {}

    def test_allowlist_applies_to_business_chats(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True, allowed_chat_ids="1")
        assert _deliver(monkeypatch, ch, [_business_update()]) == []


class TestBusinessSend:
    def _send(self, ch, *args, **kwargs):
        resp = MagicMock(status_code=200)
        with patch("httpx.post", return_value=resp) as post:
            assert ch.send(*args, **kwargs) is True
        return post.call_args[1]["json"]

    def test_send_uses_remembered_connection(self, monkeypatch):
        ch = TelegramChannel(bot_token="t", business=True)
        _deliver(monkeypatch, ch, [_business_update()])
        payload = self._send(ch, str(CONTACT_ID), "reply")
        assert payload["business_connection_id"] == "bc-1"

    def test_send_metadata_connection_wins(self):
        ch = TelegramChannel(bot_token="t", business=True)
        ch._business_chats["9"] = "old"
        payload = self._send(
            ch, "9", "reply", metadata={"business_connection_id": "new"}
        )
        assert payload["business_connection_id"] == "new"

    def test_plain_send_has_no_connection(self):
        payload = self._send(TelegramChannel(bot_token="t"), "9", "reply")
        assert "business_connection_id" not in payload


class TestBusinessPolling:
    def _polling_kwargs(self, business):
        ch = TelegramChannel(bot_token="t", business=business)
        seen = {}
        events = {}

        class Updater:
            running = False

            async def start_polling(self, **kwargs):
                seen.update(kwargs)

            async def stop(self):
                pass

        class App:
            updater = Updater()
            running = False

            async def initialize(self):
                pass

            async def start(self):
                events["stop"].set()

            async def shutdown(self):
                pass

        async def run():
            events["stop"] = asyncio.Event()
            await ch._run_polling_lifecycle(App(), events["stop"])

        asyncio.run(run())
        return seen

    def test_business_requests_business_updates(self):
        kwargs = self._polling_kwargs(True)
        assert "business_message" in kwargs["allowed_updates"]
        assert "message" in kwargs["allowed_updates"]

    def test_default_polling_unchanged(self):
        assert self._polling_kwargs(False) == {"drop_pending_updates": True}


def test_config_forwards_business_flag():
    from openjarvis.core.config import ChannelConfig
    from openjarvis.system._channel_kwargs import build_channel_kwargs

    cfg = ChannelConfig()
    assert "business" not in build_channel_kwargs(cfg, "telegram")
    cfg.telegram.business = True
    assert build_channel_kwargs(cfg, "telegram")["business"] is True
