"""Unit tests for the pure decision path (no AstrBot runtime required)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from choice_gate_core import (  # noqa: E402
    IGNORE,
    RESPOND,
    BypassRules,
    BurstLimiter,
    ChatMessage,
    ChoiceValidationError,
    EventFacts,
    GatePolicy,
    build_questions,
    build_state,
    parse_answers,
    render_prompt,
    resolve_decisions,
    validate_choice,
)

IDS = {"RESPOND": "respond now", "IGNORE": "stay silent"}


def answer(choice="RESPOND", p=0.9, confidence=0.95, ids=None):
    ids = ids or IDS
    others = [key for key in ids if key != choice]
    rest = (1.0 - p) / len(others) if others else 0.0
    return {
        "choice": choice,
        "confidence": confidence,
        "probabilities": {choice: p, **{key: rest for key in others}},
    }


# --------------------------------------------------------------------------- #
# validate_choice
# --------------------------------------------------------------------------- #
def test_validate_choice_accepts_a_normalized_argmax_answer():
    choice, probabilities, confidence = validate_choice(answer(), IDS)
    assert choice == RESPOND
    assert probabilities[RESPOND] == pytest.approx(0.9)
    assert confidence == pytest.approx(0.95)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(choice="MAYBE"),
        lambda a: a["probabilities"].pop("IGNORE"),
        lambda a: a["probabilities"].update(EXTRA=0.0),
        lambda a: a.update(probabilities={"RESPOND": 0.5, "IGNORE": 0.1}),
        lambda a: a.update(probabilities={"RESPOND": 0.4, "IGNORE": 0.6}),
        lambda a: a.update(probabilities={"RESPOND": 1.4, "IGNORE": -0.4}),
        lambda a: a.update(probabilities={"RESPOND": float("nan"), "IGNORE": 0.1}),
        lambda a: a.update(confidence="high"),
        lambda a: a.pop("probabilities"),
        lambda a: a.pop("choice"),
    ],
)
def test_validate_choice_rejects_malformed_answers(mutate):
    payload = answer()
    mutate(payload)
    with pytest.raises(ChoiceValidationError):
        validate_choice(payload, IDS)


def test_validate_choice_tolerates_two_percent_of_probability_noise():
    payload = answer(p=0.9)
    payload["probabilities"] = {"RESPOND": 0.9, "IGNORE": 0.099}
    validate_choice(payload, IDS)
    payload["probabilities"] = {"RESPOND": 0.9, "IGNORE": 0.05}
    with pytest.raises(ChoiceValidationError):
        validate_choice(payload, IDS)


# --------------------------------------------------------------------------- #
# parse_answers / resolve_decisions
# --------------------------------------------------------------------------- #
def test_parse_answers_reads_both_wire_shapes():
    assert set(parse_answers({"answers": {"decision": answer()}})) == {"decision"}
    assert set(parse_answers({"decision": answer()})) == {"decision"}
    assert parse_answers({"unrelated": 1}) == {}


def test_resolve_decisions_ignores_the_unused_target_head():
    questions = {
        "decision": {"criteria": IDS},
        "reply_target": {"criteria": {"1": {}, "2": {}}},
    }
    answers = {"decision": answer(IGNORE), "reply_target": {"garbage": True}}
    resolved = resolve_decisions(answers, questions)
    assert resolved["decision"][0] == IGNORE
    assert "reply_target" not in resolved


def test_resolve_decisions_requires_a_valid_target_when_responding():
    questions = {
        "decision": {"criteria": IDS},
        "reply_target": {"criteria": {"1": {}, "2": {}}},
    }
    with pytest.raises(ChoiceValidationError):
        resolve_decisions({"decision": answer(RESPOND)}, questions)
    with pytest.raises(ChoiceValidationError):
        resolve_decisions(
            {
                "decision": answer(RESPOND),
                "reply_target": {
                    "choice": "9",
                    "confidence": 1.0,
                    "probabilities": {"1": 0.5, "2": 0.5},
                },
            },
            questions,
        )
    valid_target = {
        "choice": "2",
        "confidence": 0.8,
        "probabilities": {"1": 0.2, "2": 0.8},
    }
    resolved = resolve_decisions(
        {"decision": answer(RESPOND), "reply_target": valid_target}, questions
    )
    assert resolved["reply_target"][0] == "2"


def test_resolve_decisions_requires_a_decision_head():
    with pytest.raises(ChoiceValidationError):
        resolve_decisions({}, {"decision": {"criteria": IDS}})


# --------------------------------------------------------------------------- #
# state / questions / prompt
# --------------------------------------------------------------------------- #
def messages(count=3):
    return [
        ChatMessage(index=index + 1, sender=f"u{index}", text=f"m{index}")
        for index in range(count)
    ]


def test_build_state_reports_message_ages_from_arrival_time():
    items = messages(2)
    items[0].arrived_at = 100.0
    items[1].arrived_at = 105.0
    state = build_state("goal", items, now=110.0)
    assert [entry["seconds_ago"] for entry in state["messages"]] == [10.0, 5.0]
    assert state["messages"][0]["sender"] == "u0"


def test_build_questions_offers_message_indices_and_can_be_disabled():
    state = build_state("goal", messages(2))
    questions = build_questions(state)
    assert set(questions) == {"decision", "reply_target"}
    assert set(questions["decision"]["criteria"]) == {RESPOND, IGNORE}
    assert set(questions["reply_target"]["criteria"]) == {"1", "2"}
    assert set(build_questions(state, reply_target_enabled=False)) == {"decision"}
    assert set(build_questions(build_state("goal", []))) == {"decision"}


def test_render_prompt_returns_a_json_request_naming_every_head():
    state = build_state("goal", messages(1))
    questions = build_questions(state)
    system, user = render_prompt(state, questions)
    assert "decision" in system and "reply_target" in system
    parsed = json.loads(user)
    assert parsed["state"]["goal"] == "goal"
    assert set(parsed["questions"]) == {"decision", "reply_target"}


# --------------------------------------------------------------------------- #
# policy / bypass / debounce
# --------------------------------------------------------------------------- #
def test_policy_applies_threshold_and_confidence():
    policy = GatePolicy(respond_threshold=0.6, min_confidence=0.5)
    assert policy.check((RESPOND, {RESPOND: 0.6}, 0.9))[0] is True
    assert policy.check((RESPOND, {RESPOND: 0.59}, 0.9))[0] is False
    assert policy.check((RESPOND, {RESPOND: 0.9}, 0.4))[0] is False
    assert policy.check((IGNORE, {RESPOND: 0.9}, 0.9))[0] is False


def test_bypass_rules_cover_explicit_address_and_can_be_switched_off():
    rules = BypassRules()
    assert rules.reason(EventFacts(looks_like_command=True)) == "command"
    assert rules.reason(EventFacts(mentioned_bot=True)) == "mentioned the bot"
    assert rules.reason(EventFacts(quoted_bot=True)) == "quoted the bot"
    assert rules.reason(EventFacts(is_admin=True)) == "administrator"
    assert rules.reason(EventFacts(text="hello")) is None
    assert BypassRules(admin=False).reason(EventFacts(is_admin=True)) is None


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_burst_limiter_enforces_a_reply_budget_per_window():
    clock = Clock()
    limiter = BurstLimiter(10.0, 1, clock=clock)
    assert limiter.allow("s")[0] is True
    limiter.record("s")
    assert limiter.allow("s")[0] is False
    assert limiter.allow("other")[0] is True
    clock.now = 11.0
    assert limiter.allow("s")[0] is True


def test_burst_limiter_is_transparent_when_disabled_and_can_forget():
    limiter = BurstLimiter(0.0, 1)
    for _ in range(5):
        assert limiter.allow("s")[0] is True
        limiter.record("s")
    limiter = BurstLimiter(10.0, 1)
    limiter.record("s")
    assert limiter.allow("s")[0] is False
    limiter.forget("s")
    assert limiter.allow("s")[0] is True
    limiter.record("s")
    limiter.forget()
    assert limiter.allow("s")[0] is True
