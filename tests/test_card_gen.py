"""Tests for bin/generators/card_gen.py — DSPy generator program."""
import dspy
import pytest

from tests.conftest import _scripted_lm

def test_card_model_validates_required_fields():
    from bin.generators.card_gen import Card
    Card(title="t", body="b")
    with pytest.raises(Exception):
        Card(body="b")
    with pytest.raises(Exception):
        Card(title="t")

def test_card_optional_source_ref():
    from bin.generators.card_gen import Card
    c = Card(title="t", body="b", source_ref="path:42")
    assert c.source_ref == "path:42"
    c2 = Card(title="t", body="b")
    assert c2.source_ref is None

def test_card_gen_loads_prompt_from_langfuse(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt(
        "card-generator:memory",
        text="Extract durable memories from this text.",
        version=1,
        labels=["production"],
    )
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin.generators.card_gen import CardGen
    g = CardGen(prompt_name="card-generator:memory")
    assert "memories" in g.signature.instructions.lower()

def test_card_gen_forward_returns_cards_list(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("card-generator:x", text="extract", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    dspy.settings.configure(lm=_scripted_lm({
        "cards": [
            {"title": "Card one", "body": "First finding", "source_ref": "file:1"},
            {"title": "Card two", "body": "Second finding"},
        ],
    }))
    from bin.generators.card_gen import CardGen, Card
    g = CardGen(prompt_name="card-generator:x")
    result = g(corpus_text="some corpus content")
    assert isinstance(result.cards, list)
    assert len(result.cards) == 2
    assert all(isinstance(c, Card) for c in result.cards)
    assert result.cards[0].title == "Card one"
    assert result.cards[1].source_ref is None

def test_card_gen_coerces_dict_items(fake_langfuse, monkeypatch):
    """If DSPy hands back plain dicts (older adapters), forward() coerces them
    into Card pydantic models."""
    fake_langfuse.add_prompt("card-generator:x", text="extract", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    dspy.settings.configure(lm=_scripted_lm({
        "cards": [
            {"title": "valid", "body": "ok"},
            {"title": "another", "body": "fine", "source_ref": "x:1"},
        ],
    }))
    from bin.generators.card_gen import CardGen, Card
    g = CardGen(prompt_name="card-generator:x")
    result = g(corpus_text="x")
    titles = [c.title for c in result.cards]
    assert titles == ["valid", "another"]
    assert all(isinstance(c, Card) for c in result.cards)

def test_card_gen_empty_returns_empty_list(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("card-generator:x", text="extract", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    dspy.settings.configure(lm=_scripted_lm({"cards": []}))
    from bin.generators.card_gen import CardGen
    g = CardGen(prompt_name="card-generator:x")
    result = g(corpus_text="nothing interesting")
    assert result.cards == []
