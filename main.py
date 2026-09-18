# ruff: noqa: E402 - the plugin directory is added to sys.path before the
# sibling module is imported, so those imports intentionally follow that setup.
"""Reply Gate: decide whether a message should reach the LLM at all.

Inspired by browser-use/jev-ultrafast. One request carries every choice
question about the chat state (speculative heads); the answer is strictly
validated and only the head the decision points at is consumed. Messages the
gate rejects are stopped before the LLM request is ever queued, which is what
makes this a debounce rather than another prompt instruction.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    # Plugins are loaded from a directory that is not guaranteed to be
    # importable, so the pure logic module is resolved relative to this file.
    sys.path.insert(0, _PLUGIN_DIR)

import httpx
from reply_gate_core import (
    RESPOND,
    BurstLimiter,
    BypassRules,
    ChatMessage,
    ChoiceValidationError,
    EventFacts,
    GatePolicy,
    build_questions,
    build_state,
    parse_answers,
    resolve_decisions,
    threshold_sensitivity,
)
from reply_gate_i18n import Translator, load_i18n, resolve_locale

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

DEFAULT_TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
RETRY_STATUS = {429, 503, 529}
DEFAULT_THRESHOLDS = (0.3, 0.5, 0.7, 0.9)

GOAL = (
    "Decide whether the assistant should answer the newest message in this chat, "
    "and which message the reply should focus on."
)


class ReplyGate(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._i18n = load_i18n(Path(__file__).resolve().parent)
        self._transcripts: dict[str, deque[ChatMessage]] = defaultdict(
            lambda: deque(maxlen=max(2, int(self._cfg("history_max_messages", 12))))
        )
        self._next_index: dict[str, int] = defaultdict(lambda: 1)
        self._limiter = BurstLimiter(
            self._cfg("debounce_window_sec", 25),
            self._cfg("debounce_max_replies", 1),
        )
        self._stats = defaultdict(int)
        self._recent: deque[dict] = deque(maxlen=10)
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ #
    # config helpers
    # ------------------------------------------------------------------ #
    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _locale(self) -> str:
        try:
            bot_language = self.context.get_config().get("language")
        except Exception:
            bot_language = None
        return resolve_locale(self._cfg("language", "auto"), bot_language)

    def _t(self, key: str, **kwargs) -> str:
        """Localise a chat-facing string; falls back to English."""
        return Translator(self._locale(), self._i18n).t(key, **kwargs)

    def _policy(self) -> GatePolicy:
        return GatePolicy(
            respond_threshold=float(self._cfg("respond_threshold", 0.5)),
            min_confidence=float(self._cfg("min_confidence", 0.0)),
            fail_open=str(self._cfg("fail_mode", "open")).lower() != "closed",
        )

    def _rules(self) -> BypassRules:
        return BypassRules(
            command=bool(self._cfg("bypass_command", True)),
            mention=bool(self._cfg("bypass_mention", True)),
            quote_bot=bool(self._cfg("bypass_quote_bot", True)),
            admin=bool(self._cfg("bypass_admin", True)),
        )

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=float(self._cfg("timeout_sec", 20)),
                http2=True,
            )
        return self._client

    # ------------------------------------------------------------------ #
    # transcript bookkeeping
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def remember(self, event: AstrMessageEvent) -> None:
        """Keep a bounded transcript per session; this handler never sends."""
        if not self._cfg("enable", True):
            return
        self._record(event)

    def _record(self, event: AstrMessageEvent) -> None:
        umo = event.unified_msg_origin
        transcript = self._transcripts[umo]
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        text = (event.message_str or "").strip()
        if transcript and message_id and transcript[-1].message_id == message_id:
            return
        index = self._next_index[umo]
        self._next_index[umo] = index + 1
        transcript.append(
            ChatMessage(
                index=index,
                sender=event.get_sender_name() or str(event.get_sender_id()),
                text=text[:500],
                is_self=str(event.get_sender_id()) == str(event.get_self_id()),
                at_bot=bool(self._mentioned(event)),
                seconds_ago=0.0,
                message_id=message_id,
                arrived_at=time.monotonic(),
            )
        )

    @staticmethod
    def _mentioned(event: AstrMessageEvent) -> bool:
        try:
            from astrbot.api.message_components import At

            return any(
                isinstance(component, At)
                and str(component.qq) in {str(event.get_self_id()), "all"}
                for component in event.get_messages()
            )
        except Exception:
            return False

    @staticmethod
    def _quoted_bot(event: AstrMessageEvent) -> bool:
        try:
            from astrbot.api.message_components import Reply

            return any(
                isinstance(component, Reply)
                and str(getattr(component, "sender_id", "")) == str(event.get_self_id())
                for component in event.get_messages()
            )
        except Exception:
            return False

    @staticmethod
    def _is_user_message(event: AstrMessageEvent) -> bool:
        """Whether this request came from a real inbound user message."""
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        return bool((event.message_str or "").strip() or event.get_messages())

    def _facts(self, event: AstrMessageEvent) -> EventFacts:
        prefixes = self.context.get_config(umo=event.unified_msg_origin).get(
            "wake_prefix", ["/"]
        )
        text = (event.message_str or "").strip()
        return EventFacts(
            is_private=event.is_private_chat(),
            is_group=bool(event.get_group_id()),
            is_admin=bool(event.is_admin()),
            looks_like_command=any(
                text.startswith(prefix) for prefix in prefixes if prefix
            ),
            mentioned_bot=bool(event.is_at_or_wake_command and self._mentioned(event)),
            quoted_bot=self._quoted_bot(event),
            text=text,
        )

    # ------------------------------------------------------------------ #
    # the gate itself
    # ------------------------------------------------------------------ #
    @filter.on_waiting_llm_request()
    async def gate(self, event: AstrMessageEvent) -> None:
        """Ask the model whether this message should reach the LLM.

        Runs before the LLM lock is taken: a rejected message is stopped here,
        so no request, no queue slot and no tokens are spent on it.
        """
        if not self._cfg("enable", True):
            return
        if not self._is_user_message(event):
            # Scheduled or plugin-triggered requests must never be gated.
            return
        facts = self._facts(event)
        if facts.is_private and not self._cfg("scope_private", True):
            return self._pass(event, "private chat out of scope")
        if facts.is_group and not self._cfg("scope_group", True):
            return self._pass(event, "group chat out of scope")
        if reason := self._rules().reason(facts):
            return self._pass(event, f"bypass: {reason}")

        umo = event.unified_msg_origin
        if bool(self._cfg("debounce_enable", True)):
            allowed, detail = self._limiter.allow(umo)
            if not allowed:
                return self._reject(event, f"debounce: {detail}")

        self._record(event)
        transcript = list(self._transcripts[umo])
        state = build_state(GOAL, transcript)
        questions = build_questions(
            state, reply_target_enabled=bool(self._cfg("reply_target", True))
        )

        try:
            resolved, _answers, _latency_ms = await self._probe(state, questions)
        except ChoiceValidationError as exc:
            self._stats["invalid"] += 1
            return self._fail(event, f"rejected malformed answer: {exc}")
        except Exception as exc:  # noqa: BLE001 - TypeSafe failures must never break chat
            self._stats["error"] += 1
            logger.warning(
                f"reply gate TypeSafe call failed: {type(exc).__name__}: {exc}"
            )
            return self._fail(event, f"TypeSafe error: {type(exc).__name__}")

        allowed, reason = self._policy().check(resolved["decision"])
        target = None
        if allowed and "reply_target" in resolved:
            target = resolved["reply_target"][0]
        event.set_extra(
            "_reply_gate",
            {"respond": allowed, "reason": reason, "target": target},
        )
        self._note(event, resolved, allowed, reason, target)
        if allowed:
            self._stats["respond"] += 1
            if bool(self._cfg("debounce_enable", True)):
                self._limiter.record(umo)
            return
        self._reject(event, reason)

    def _pass(self, event: AstrMessageEvent, reason: str) -> None:
        self._stats["bypassed"] += 1
        if self._cfg("log_decisions", False):
            logger.info(f"reply gate passed ({reason})")

    def _reject(self, event: AstrMessageEvent, reason: str) -> None:
        self._stats["ignored"] += 1
        event.set_extra("_reply_gate", {"respond": False, "reason": reason})
        event.stop_event()
        if self._cfg("log_decisions", False):
            logger.info(f"reply gate ignored message ({reason})")

    def _fail(self, event: AstrMessageEvent, reason: str) -> None:
        if self._policy().fail_open:
            self._stats["fail_open"] += 1
            if self._cfg("log_decisions", False):
                logger.info(f"reply gate failing open ({reason})")
            return
        self._stats["fail_closed"] += 1
        self._reject(event, f"fail closed: {reason}")

    def _note(self, event, resolved, allowed, reason, target) -> None:
        self._recent.append(
            {
                "time": time.strftime("%H:%M:%S"),
                "session": event.unified_msg_origin,
                "p": resolved["decision"][1].get(RESPOND, 0.0),
                "confidence": resolved["decision"][2],
                "target": target,
                "respond": allowed,
                "reason": reason,
            }
        )

    # ------------------------------------------------------------------ #
    # hint injection for the message the gate picked
    # ------------------------------------------------------------------ #
    @filter.on_llm_request()
    async def inject_hint(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Tell the model which message the gate decided to answer."""
        if not self._cfg("inject_hint", True):
            return
        outcome = event.get_extra("_reply_gate") or {}
        target = outcome.get("target")
        if not target:
            return
        transcript = list(self._transcripts.get(event.unified_msg_origin, ()))
        focus = next((m for m in transcript if str(m.index) == str(target)), None)
        if focus is None:
            return
        req.extra_user_content_parts.append(
            TextPart(
                text=(
                    "<system_reminder>"
                    f"The gate selected message #{focus.index} from {focus.sender} as the one to answer."
                    "</system_reminder>"
                )
            ).mark_as_temp()
        )

    # ------------------------------------------------------------------ #
    # TypeSafe backend
    # ------------------------------------------------------------------ #
    async def _probe(self, state, questions):
        """One real TypeSafe round trip: ask, then strictly validate.

        Shared by the gate and the ``test`` command so a dry run exercises the
        exact same request, parsing and validation as production.
        """
        started = time.monotonic()
        answers = await self._ask_typesafe(state, questions)
        resolved = resolve_decisions(answers, questions)
        return resolved, answers, int((time.monotonic() - started) * 1000)

    async def _post_json(self, url: str, body: dict, headers: dict) -> dict:
        """POST with bounded retries; mirrors Jev's 429/503/529 retry policy."""
        retries = max(0, int(self._cfg("retries", 1)))
        for attempt in range(retries + 1):
            response = await self._http().post(url, json=body, headers=headers)
            if response.status_code in RETRY_STATUS and attempt < retries:
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise RuntimeError("model unavailable")

    async def _ask_typesafe(self, state, questions) -> dict:
        """One request answers every head (speculative fan-out)."""
        body = {
            "model": str(self._cfg("model", "jev-latest")),
            "state": state,
            "questions": questions,
        }
        payload = await self._post_json(
            str(self._cfg("base_url", DEFAULT_TYPESAFE_URL)),
            body,
            {"Authorization": f"Bearer {self._cfg('api_key', '')}"},
        )
        return parse_answers(payload)

    # ------------------------------------------------------------------ #
    # status command
    # ------------------------------------------------------------------ #
    @filter.command("replygate")
    async def replygate(self, event: AstrMessageEvent):
        """Reply Gate status and tuning. Usage: /replygate [status|on|off|reset|test <text>|prompt]"""
        argument = self._command_argument(event)
        action = argument.split(maxsplit=1)[0].lower() if argument else "status"
        rest = (
            argument.split(maxsplit=1)[1].strip()
            if len(argument.split(maxsplit=1)) > 1
            else ""
        )

        if action in {"on", "off"}:
            self.config["enable"] = action == "on"
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            yield event.plain_result(
                self._t("cmd.enabled", state=self._t(f"state.{action}"))
            )
            return
        if action == "reset":
            self._limiter.forget()
            self._stats.clear()
            self._recent.clear()
            yield event.plain_result(self._t("cmd.reset"))
            return
        if action == "prompt":
            yield event.plain_result(self._prompt_text(event))
            return
        if action == "test":
            yield event.plain_result(await self._test_text(event, rest))
            return
        yield event.plain_result(self._status_text())

    @staticmethod
    def _command_argument(event: AstrMessageEvent) -> str:
        """Return the text after the command name.

        ``message_str`` may or may not still carry the wake prefix and the
        command name, so both are stripped when present.
        """
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            text = text[1:].lstrip()
        lowered = text.lower()
        for token in ("replygate",):
            if lowered.startswith(token):
                return text[len(token) :].strip()
        return text

    def _prompt_text(self, event: AstrMessageEvent) -> str:
        """Show exactly what the next gate call would send."""
        transcript = list(self._transcripts.get(event.unified_msg_origin, ()))
        state = build_state(GOAL, transcript)
        questions = build_questions(
            state, reply_target_enabled=bool(self._cfg("reply_target", True))
        )
        body = {
            "model": str(self._cfg("model", "jev-latest")),
            "state": state,
            "questions": questions,
        }
        preview = json.dumps(body, ensure_ascii=False)
        if len(preview) > 1500:
            preview = preview[:1500] + self._t("truncated")
        header = self._t(
            "prompt.header",
            endpoint=self._cfg("base_url", DEFAULT_TYPESAFE_URL),
            model=self._cfg("model", "jev-latest"),
            key_state=self._t(
                "prompt.key_set" if self._cfg("api_key", "") else "prompt.key_unset"
            ),
            count=len(transcript),
        )
        return f"📤 {header}\n\n{preview}"

    async def _test_text(self, event: AstrMessageEvent, text: str) -> str:
        """Dry run of the gate against a text, without touching real state."""
        if not text:
            return self._t("test.usage")
        umo = event.unified_msg_origin
        transcript = list(self._transcripts.get(umo, ()))
        probe = ChatMessage(
            index=(transcript[-1].index + 1) if transcript else 1,
            sender=event.get_sender_name() or str(event.get_sender_id()),
            text=text[:500],
            at_bot=False,
            arrived_at=time.monotonic(),
        )
        transcript.append(probe)
        state = build_state(GOAL, transcript)
        questions = build_questions(
            state, reply_target_enabled=bool(self._cfg("reply_target", True))
        )
        policy = self._policy()
        header = "\n".join(
            (
                "🧪 " + self._t("test.header", text=text[:100], count=len(transcript)),
                self._t(
                    "test.endpoint",
                    endpoint=self._cfg("base_url", DEFAULT_TYPESAFE_URL),
                    model=self._cfg("model", "jev-latest"),
                ),
            )
        )
        try:
            resolved, _answers, latency_ms = await self._probe(state, questions)
        except ChoiceValidationError as exc:
            return f"{header}\n❌ {self._t('test.validation_failed', error=exc)}"
        except Exception as exc:  # noqa: BLE001 - the report must survive any TypeSafe failure
            return f"{header}\n❌ " + self._t(
                "test.call_failed",
                error=f"{type(exc).__name__}: {exc}",
                fail_mode="open" if policy.fail_open else "closed",
                effect=self._t(
                    "test.effect_reply" if policy.fail_open else "test.effect_silent"
                ),
            )

        decision = resolved["decision"]
        target = resolved.get("reply_target")
        probability = decision[1].get(RESPOND, 0.0)
        allowed, reason = policy.check(decision)
        verdict_word = self._t("verdict.respond" if allowed else "verdict.silent")
        lines = [
            header,
            self._t(
                "test.decision",
                p=f"{probability:.3f}",
                confidence=f"{decision[2]:.3f}",
                target=target[0] if target else "-",
            ),
            self._t("test.verdict", verdict=verdict_word, reason=reason),
            self._t(
                "test.thresholds",
                rows=" / ".join(
                    self._t(
                        "test.threshold_row",
                        threshold=f"{threshold:.2f}",
                        verdict=self._t(
                            "verdict.respond" if verdict else "verdict.silent"
                        ),
                    )
                    for threshold, verdict in threshold_sensitivity(
                        probability, DEFAULT_THRESHOLDS
                    )
                ),
            ),
            self._t("test.latency", ms=latency_ms),
            self._t("test.heads"),
            self._t(
                "test.head_row",
                head="decision",
                values=", ".join(f"{k}={v:.3f}" for k, v in decision[1].items()),
            ),
        ]
        if target:
            lines.append(
                self._t(
                    "test.head_row",
                    head="reply_target",
                    values=", ".join(f"{k}={v:.3f}" for k, v in target[1].items()),
                )
            )
        if bool(self._cfg("debounce_enable", True)):
            allowed_now, detail = self._limiter.allow(umo)
            lines.append(self._t("test.debounce", detail=detail))
        return "\n".join(lines)

    def _status_text(self) -> str:
        lines = [
            self._t(
                "status.header",
                state=self._t("state.on" if self._cfg("enable", True) else "state.off"),
                model=self._cfg("model", "jev-latest"),
            ),
            self._t(
                "status.policy",
                threshold=self._cfg("respond_threshold", 0.5),
                confidence=self._cfg("min_confidence", 0.0),
                fail_mode="fail-open" if self._policy().fail_open else "fail-closed",
            ),
        ]
        if self._stats:
            lines.append(
                self._t(
                    "status.stats",
                    stats=", ".join(
                        f"{key}={value}" for key, value in sorted(self._stats.items())
                    ),
                )
            )
        else:
            lines.append(self._t("status.stats_empty"))
        if self._recent:
            lines.append(self._t("status.recent"))
            for item in list(self._recent)[-5:]:
                lines.append(
                    self._t(
                        "status.recent_item",
                        time=item["time"],
                        p=f"{item['p']:.2f}",
                        verdict=self._t(
                            "verdict.respond" if item["respond"] else "verdict.silent"
                        ),
                        reason=item["reason"],
                    )
                )
        return "\n".join(lines)

    async def terminate(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
