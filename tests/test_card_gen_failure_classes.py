"""Failure-class tests for bin/generators/card_gen.py.

Each test scripts *raw* LM completions (text + finish_reason) or provider
exceptions, exercising the real dspy adapter stack — no network, no real LM.
Covers the explicit failure taxonomy:

    success | timeout | parse-error | truncation (incl. the silent-repair
    regression: payload that parses after adapter "repair" but whose call
    exhausted the output budget must FAIL as truncation, never succeed).
"""
import dspy
import litellm
import pytest

class _RawLM(dspy.LM):
    """LM returning scripted raw completions.

    Each script item is one of:
      * str                     — completion text, finish_reason "stop"
      * (str, finish_reason)    — completion text with explicit finish_reason
      * Exception instance      — raised on that call

    dspy's ChatAdapter falls back to JSONAdapter on failure, so failure
    scenarios consume up to two script items per predict call.
    """

    def __init__(self, script):
        super().__init__(model="raw-test", cache=False)
        self._script = list(script)
        self.calls = 0

    def forward(self, prompt=None, messages=None, **kwargs):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            text, finish_reason = item
        else:
            text, finish_reason = item, "stop"
        return litellm.ModelResponse(
            model="raw-test",
            choices=[dict(finish_reason=finish_reason, index=0,
                          message=dict(content=text, role="assistant"))],
            usage={"total_tokens": 1},
        )

GOOD = (
    "[[ ## cards ## ]]\n"
    '[{"title": "T1", "body": "B1"}]\n'
    "[[ ## completed ## ]]"
)
TRUNCATED_MIDFIELD = (
    "[[ ## cards ## ]]\n"
    '[{"title": "T1", "body": "B1"}, {"title": "T2", "bo'
)
TRUNCATED_REPAIRABLE = (
    "[[ ## cards ## ]]\n"
    '[{"title": "T1", "body": "B1"}'
)
GARBAGE = "Let me think about the corpus. It discusses pandas, so maybe..."

@pytest.fixture
def gen(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt(
        "card-generator:x", text="extract", version=1, labels=["production"]
    )
    monkeypatch.setattr(
        "bin.prompts._client_factory", lambda: fake_langfuse.as_client()
    )
    from bin.generators.card_gen import CardGen
    return CardGen(prompt_name="card-generator:x")

def _use(script):
    lm = _RawLM(script)
    dspy.settings.configure(lm=lm)
    return lm

def test_success_path_with_trailing_commentary(gen):
    """A well-formed answer-first payload succeeds; commentary after the
    payload is ignored rather than breaking the parse."""
    _use([GOOD + "\n\nSome trailing commentary about the cards."])
    result = gen(corpus_text="x")
    assert [c.title for c in result.cards] == ["T1"]

def test_timeout_is_classified_as_timeout(gen):
    from bin.generators.card_gen import CardGenError, CardGenTimeout
    _use([
        litellm.Timeout("request timed out", "raw-test", "openai"),
        litellm.Timeout("request timed out", "raw-test", "openai"),
    ])
    with pytest.raises(CardGenTimeout) as exc:
        gen(corpus_text="x")
    assert isinstance(exc.value, CardGenError)
    assert exc.value.failure_class == "timeout"

def test_malformed_output_is_classified_as_parse_error(gen):
    from bin.generators.card_gen import CardGenError, CardGenParseError
    _use([GARBAGE, GARBAGE])
    with pytest.raises(CardGenParseError) as exc:
        gen(corpus_text="x")
    assert isinstance(exc.value, CardGenError)
    assert exc.value.failure_class == "parse-error"

def test_budget_exhausted_unparseable_is_classified_as_truncation(gen):
    from bin.generators.card_gen import CardGenError, CardGenTruncation
    _use([
        (TRUNCATED_MIDFIELD, "length"),
        (TRUNCATED_MIDFIELD, "length"),
    ])
    with pytest.raises(CardGenTruncation) as exc:
        gen(corpus_text="x")
    assert isinstance(exc.value, CardGenError)
    assert exc.value.failure_class == "truncation"

def test_truncated_but_repairable_payload_is_never_silently_repaired(gen):
    """Regression: dspy's JSON adapter can 'fix' a payload that is only
    missing its closing bracket, yielding a valid-looking but incomplete
    finding. When the winning call hit the token limit, the item must fail
    as truncation instead."""
    from bin.generators.card_gen import CardGenTruncation
    _use([
        (TRUNCATED_REPAIRABLE, "length"),
        (TRUNCATED_REPAIRABLE, "length"),
    ])
    with pytest.raises(CardGenTruncation):
        gen(corpus_text="x")

def test_truncated_attempt_then_clean_json_fallback_succeeds(gen):
    """Only the winning call's finish_reason matters on success: a truncated
    ChatAdapter attempt followed by a clean, complete JSONAdapter fallback
    is a success, not tainted by the earlier length-exhausted call."""
    _use([
        (TRUNCATED_MIDFIELD, "length"),
        ('{"cards": [{"title": "T1", "body": "B1"}]}', "stop"),
    ])
    result = gen(corpus_text="x")
    assert [c.title for c in result.cards] == ["T1"]

def test_truncated_attempt_then_failed_fallback_is_truncation(gen):
    """When every attempt fails and one of them exhausted the output budget,
    the item fails as truncation (the budget was the root cause)."""
    from bin.generators.card_gen import CardGenTruncation
    _use([
        (TRUNCATED_MIDFIELD, "length"),
        (GOOD, "stop"),
    ])
    with pytest.raises(CardGenTruncation):
        gen(corpus_text="x")

def test_invalid_card_item_fails_as_parse_error_not_silent_drop(gen):
    """A schema-invalid item must fail the finding, not be silently dropped
    into a smaller valid-looking payload."""
    from bin.generators.card_gen import CardGenParseError
    bad = (
        "[[ ## cards ## ]]\n"
        '[{"title": "ok", "body": "fine"}, {"title": "missing body"}]\n'
        "[[ ## completed ## ]]"
    )
    _use([bad, bad])
    with pytest.raises(CardGenParseError):
        gen(corpus_text="x")

def test_output_contract_is_appended_to_prompt(gen):
    """The generation contract demands the parseable payload first/bounded."""
    instructions = gen.signature.instructions
    assert "extract" in instructions
    assert "before any prose" in instructions
    assert "cards" in instructions
