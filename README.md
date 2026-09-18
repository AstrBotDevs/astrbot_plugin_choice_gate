# astrbot_plugin_choice_gate

Decides whether a message should reach the LLM at all, using
[TypeSafe Jev](https://docs.typesafe.ai/introduction) choice probabilities.
The pattern comes from
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast): one
request carries every choice head, the answer is strictly validated, and only
the head the decision points at is consumed.

A rejected message never enters the LLM request: no queue slot, no tokens, no
context. That is what separates this from a "do not reply" line in the system
prompt.

[中文说明](README_zh.md)

## How it works

```
message -> should it reach the LLM?
   |- command / mention / quote of the bot / admin  -> pass (bypass)
   |- reply budget for this burst already used      -> silent
   '- one TypeSafe request:
        decision:     {RESPOND, IGNORE}
        reply_target: {1: ..., 2: ...}     <- speculative head, same request
        validate: exact id set, sum 1 +/- 0.02, argmax matches the choice
        P(RESPOND) >= threshold ?  -> pass : stop_event() (never queued)
```

When a message passes, the selected `reply_target` is appended to that request
as a transient part, so the model knows which message to answer.

## Setup

A TypeSafe API key is required (typesafe.ai). Jev is cheap (about $0.042/MTok
input), which is the premise for asking it about every candidate message.

```bash
cd AstrBot/data/plugins
git clone https://github.com/AstrBotDevs/astrbot_plugin_choice_gate
```

Reload the plugin in the WebUI, then fill in `api_key`.

## Configuration

| Key | Default | Notes |
| --- | --- | --- |
| `enable` | `true` | Master switch |
| `language` | `auto` | `auto` follows AstrBot's `language` setting, else English |
| `api_key` | empty | TypeSafe API key |
| `model` | `jev-latest` | TypeSafe model |
| `base_url` | `https://api.typesafe.ai/v1/systemone` | TypeSafe endpoint |
| `timeout_sec` / `retries` | `20` / `1` | Timeout, plus retries on 429/503/529 |
| `respond_threshold` | `0.5` | Below this P(RESPOND) the message stays unanswered |
| `min_confidence` | `0.0` | Same, for the reported confidence |
| `fail_mode` | `open` | `open` replies as usual on failure, `closed` stays silent |
| `scope_private` / `scope_group` | `true` | Where the gate applies |
| `bypass_command` / `bypass_mention` / `bypass_quote_bot` / `bypass_admin` | `true` | Always pass when the bot is explicitly addressed |
| `debounce_enable` / `debounce_window_sec` / `debounce_max_replies` | `true` / `25` / `1` | One reply per burst |
| `reply_target` / `inject_hint` | `true` | Ask the extra head, and tell the model about it |
| `history_max_messages` | `12` | Messages handed to the gate |
| `log_decisions` | `false` | One log line per decision |

## Commands

```
/choicegate                       status, stats, recent decisions
/choicegate on|off                toggle
/choicegate reset                 clear the debounce window and stats
/choicegate test <text>           dry run through the real request and thresholds
/choicegate prompt                dump the next gate request body
```

`test` reuses the production path, writes nothing to the transcript, and spends
no debounce budget:

```
🧪 Dry run · input: 在吗 · messages: 4
Endpoint: https://api.typesafe.ai/v1/systemone · model jev-latest
P(RESPOND)=0.130 · confidence=0.900 · target=-
-> silent (p(RESPOND)=0.13 < 0.50)
Thresholds: >=0.30 silent / >=0.50 silent / >=0.70 silent / >=0.90 silent
Latency: 412ms
Heads:
  decision: RESPOND=0.130, IGNORE=0.870
Debounce: allowed (0/1 replies in window)
```

A wrong endpoint or key shows up as the exception text plus what chat would do
under the current `fail_mode`.

## Trade-offs

- Costs one TypeSafe call per gated message. For a private chat where you want a
  reply to everything, that is a loss; the value is in groups and shared bots.
- Only messages that would have triggered the LLM anyway pass through here.
  Ambient group messages never wake the bot and never reach the plugin.
- Only real inbound user messages are gated. Scheduled tasks, other plugins, and
  the bot's own messages are left alone.
- Chat text is untrusted input. Every model-produced id is matched against the
  offered set; anything else is refused and the message falls back to
  `fail_mode`.

## i18n

The plugin page and the chat replies are localised from
`.astrbot-plugin/i18n/<locale>.json`, the layout AstrBot documents in
`docs/zh/dev/star/guides/plugin-i18n.md`. `en-US` and `zh-CN` ship; the English
schema text is the fallback. Adding a locale means adding one file.

## Development

```bash
pip install -r requirements.txt pytest ruff
ruff check .
pytest -q          # pure logic and locale rules, no AstrBot required
```

`choice_gate_core.py` holds the decision logic, `i18n.py` the locale lookup, and
`main.py` only wires AstrBot events to those two.
