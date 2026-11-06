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

## Example

```bash
python examples/messaging_hub/autoresponder.py --demo          # rules only
python examples/messaging_hub/autoresponder.py --demo --llm    # + local LLM
python examples/messaging_hub/autoresponder.py --telegram      # live bot
```
