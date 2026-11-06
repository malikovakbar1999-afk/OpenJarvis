# Messenger Auto-Responder

`AutoResponderAgent` answers incoming messenger messages on your behalf while
you are away, asleep or busy. It works with **any** OpenJarvis channel —
Telegram, WhatsApp, Slack, Discord, Facebook Messenger, Signal, Viber, … —
because it only relies on the common `BaseChannel` interface.

## Features

- **Away message** — a fixed reply when nothing else applies.
- **LLM replies** — plug in any agent (e.g. `SimpleAgent` on a local model)
  to write short, context-aware replies in the sender's language.
  Recent conversation history is passed to the agent.
- **Keyword rules** — regex → canned reply, checked before the LLM.
- **Schedule** — active only during given hours (windows may wrap midnight,
  e.g. `22:00`–`08:00`) and/or weekdays.
- **Cooldown** — at most one auto-reply per conversation per
  `cooldown_seconds`, so a chatty contact is not spammed.
- **Urgent detection** — urgent keywords bypass the cooldown, send an urgent
  reply and call your `on_urgent` callback (push notification, SMS, …).
- **Sender filters** — `allowlist`, `blocklist`, `ignore_senders` (your own
  ids / other bots, so the responder never talks to itself).
- **Pause / resume** at runtime.

## Decision order

0. written by you (`metadata["from_owner"]`) → no reply; quiets that chat for the cooldown
1. paused / disabled → no reply
2. empty message → no reply
3. sender filtered → no reply
4. outside schedule → no reply
5. urgent keyword → urgent reply + `on_urgent` (ignores cooldown)
6. cooldown active → no reply
7. matching rule → rule reply
8. agent configured → LLM reply (falls back to the away message on error)
9. away message

## Usage

```python
from openjarvis.agents.autoresponder import (
    AutoReplyRule, AutoResponderAgent, AutoResponderConfig,
)
from openjarvis.channels.telegram import TelegramChannel

config = AutoResponderConfig(
    owner_name="Akbar",
    away_message="Привет! Сейчас не на связи, отвечу позже.",
    active_hours=("22:00", "08:00"),
    cooldown_seconds=1800,
    urgent_keywords=[r"срочно", r"urgent"],
    ignore_senders=["my-own-user-id"],
    rules=[AutoReplyRule(pattern=r"адрес|address", reply="ул. Примерная, 1")],
    reply_prefix="🤖 ",
)

channel = TelegramChannel()           # reads TELEGRAM_BOT_TOKEN
responder = AutoResponderAgent(
    channel,
    config,
    agent=my_agent,                   # optional, any BaseAgent
    on_urgent=lambda msg: print("URGENT:", msg.sender, msg.content),
)
channel.connect()
```

Config can also be loaded from a dict / TOML table:

```toml
[autoresponder]
owner_name = "Akbar"
active_hours = ["22:00", "08:00"]
cooldown_seconds = 1800
urgent_keywords = ["срочно", "urgent"]

[[autoresponder.rules]]
pattern = "адрес|address"
reply = "ул. Примерная, 1"
```

```python
config = AutoResponderConfig.from_dict(toml_data["autoresponder"])
```

Use `responder.decide(msg)` to preview a decision without sending, and
`responder.pause()` / `responder.resume()` to toggle it.

## Telegram Business: answer your personal chats

A regular bot only sees messages sent *to the bot*. With
[Telegram Business](https://core.telegram.org/bots/api#business-connection)
(requires Telegram Premium) you connect the bot to your own account and it
answers people who write to **you**, with replies sent from your account.

1. In [@BotFather](https://t.me/BotFather): `/mybots` → your bot →
   *Bot Settings* → *Business Mode* → turn it on.
2. In Telegram on your account: *Settings* → *Telegram Business* →
   *Chatbots* → enter the bot's username, choose which chats it may access,
   and allow it to reply to messages.
3. Enable business mode in the channel:

```python
channel = TelegramChannel(business=True)   # or TELEGRAM_BUSINESS=1
```

```toml
[channel.telegram]
business = true
```

How it behaves:

- Messages from your contacts arrive with
  `metadata["business_connection_id"]` and `metadata["from_owner"] = False`;
  replies automatically go out through the same business connection.
- Messages **you** write in those chats arrive with `from_owner = True`.
  The responder never answers them and, with `quiet_after_owner_reply`
  (on by default), stays quiet in that chat for `cooldown_seconds` so it
  doesn't talk over you. Urgent messages are still answered.
- Messages the bot itself sent on your behalf are dropped, so auto-replies
  never trigger more auto-replies.
- `allowed_chat_ids` also applies to business chats (the chat id is the
  contact's user id).

## Example

```bash
python examples/messaging_hub/autoresponder.py --demo          # rules only
python examples/messaging_hub/autoresponder.py --demo --llm    # + local LLM
python examples/messaging_hub/autoresponder.py --telegram      # live bot
```

To answer your personal Telegram chats as well (see Telegram Business above):

```bash
TELEGRAM_BOT_TOKEN=... python examples/messaging_hub/autoresponder.py --telegram --business
```
