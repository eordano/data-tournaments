from bin.context_playbook import (
    END_MARKER,
    START_MARKER,
    entries_for_domain,
    merge_entries,
    render_prompt,
    split_prompt,
)

def test_playbook_round_trip_preserves_seed_and_entries():
    entries = merge_entries(
        [],
        [
            {
                "section": "evidence",
                "content": "Prefer claims with an authoritative source reference over unsupported generalities.",
            }
        ],
    )
    rendered = render_prompt("Never discard this production rubric.", entries)
    base, parsed = split_prompt(rendered)

    assert base == "Never discard this production rubric."
    assert len(parsed) == 1
    assert parsed[0].section == "EVIDENCE CHECKS"
    assert START_MARKER in rendered and END_MARKER in rendered

def test_merge_is_deterministic_and_deduplicates_lessons():
    delta = {
        "section": "mistake",
        "content": "Do not confuse a longer card with a more actionable or higher-impact card.",
    }
    first = merge_entries([], [delta])
    second = merge_entries(first, [delta])

    assert len(second) == 1
    assert second[0].id == first[0].id
    assert second[0].helpful == 1

def test_merge_reinforce_and_weaken_update_counters_by_id_or_content():
    base = merge_entries(
        [],
        [{"section": "strategy", "content": "Weigh evidence specificity above rhetorical confidence in card bodies."}],
    )
    entry_id = base[0].id

    reinforced = merge_entries(base, [{"op": "reinforce", "id": entry_id}])
    assert reinforced[0].helpful == 1

    weakened = merge_entries(
        reinforced,
        [{"op": "weaken", "content": "Weigh evidence specificity above rhetorical confidence in card bodies."}],
    )
    assert weakened[0].harmful == 1
    assert weakened[0].helpful == 1

def test_merge_retire_removes_entry_and_unknown_targets_are_ignored():
    base = merge_entries(
        [],
        [{"section": "evidence", "content": "Require a concrete source reference before crediting a specific factual claim."}],
    )
    retired = merge_entries(base, [{"op": "retire", "id": base[0].id}])
    assert retired == []

    assert merge_entries(base, [{"op": "retire", "id": "str-0000000000"}]) == base
    assert merge_entries(base, [{"op": "reinforce", "id": "str-0000000000"}]) == base

def test_merge_prunes_entries_whose_harm_outweighs_help():
    base = merge_entries(
        [],
        [{"section": "strategy", "content": "Prefer findings whose reproduction steps are deterministic end to end."}],
    )
    weakened = merge_entries(base, [{"op": "weaken", "id": base[0].id}] * 2)
    assert len(weakened) == 1
    assert merge_entries(weakened, [{"op": "weaken", "id": base[0].id}]) == []

def test_domain_scoped_entry_round_trips_through_render_and_split():
    entries = merge_entries(
        [],
        [
            {
                "section": "strategy",
                "content": "Down-rank timing-window claims unless the window is quantified in the card.",
                "domain": "explorer bugs",
            }
        ],
        provenance="run-42",
    )
    assert entries[0].domain == "explorer-bugs"
    assert entries[0].provenance == "run-42"

    rendered = render_prompt("Seed rubric.", entries)
    assert "domain=explorer-bugs" in rendered
    base, parsed = split_prompt(rendered)
    assert base == "Seed rubric."
    assert parsed[0].domain == "explorer-bugs"
    assert parsed[0].content == entries[0].content
    assert "run-42" not in rendered
    assert parsed[0].provenance == ""

def test_unknown_ops_are_rejected_not_coerced_to_add():
    result = merge_entries(
        [],
        [
            {
                "op": "obliterate",
                "section": "strategy",
                "content": "This content is long enough that an add would normally accept it fine.",
            }
        ],
    )
    assert result == []

def test_entries_for_domain_scopes_lessons():
    entries = merge_entries(
        [],
        [
            {"section": "strategy", "content": "A global lesson that applies to every review domain equally."},
            {"section": "strategy", "content": "Explorer-specific lesson about deferred deep link lifetimes.", "domain": "explorer-bugs"},
            {"section": "strategy", "content": "Style-specific lesson about commit message imperative mood.", "domain": "style-review"},
        ],
    )
    explorer = entries_for_domain(entries, "explorer-bugs")
    assert {e.domain for e in explorer} == {"", "explorer-bugs"}
    unscoped = entries_for_domain(entries, "")
    assert [e.domain for e in unscoped] == [""]

def _seed_scoped_playbook():
    """One global entry plus one entry each in two domains."""
    entries = merge_entries(
        [],
        [{"section": "strategy", "content": "A global lesson that must survive any scoped optimization run."}],
    )
    entries = merge_entries(
        entries,
        [{"section": "strategy", "content": "An explorer lesson about deterministic deep link reproduction."}],
        domain="explorer-bugs",
    )
    return merge_entries(
        entries,
        [{"section": "strategy", "content": "A style lesson about imperative mood in commit subject lines."}],
        domain="style-review",
    )

def test_scoped_merge_cannot_touch_global_or_foreign_entries():
    entries = _seed_scoped_playbook()
    global_entry = next(e for e in entries if e.domain == "")
    style_entry = next(e for e in entries if e.domain == "style-review")

    attacked = merge_entries(
        entries,
        [
            {"op": "retire", "id": global_entry.id},
            {"op": "weaken", "id": style_entry.id},
            {"op": "reinforce", "content": global_entry.content},
        ],
        domain="explorer-bugs",
    )
    assert {e.id for e in attacked} == {e.id for e in entries}
    assert all(e.helpful == 0 and e.harmful == 0 for e in attacked)

def test_scoped_merge_can_manage_its_own_entries():
    entries = _seed_scoped_playbook()
    mine = next(e for e in entries if e.domain == "explorer-bugs")

    updated = merge_entries(
        entries, [{"op": "reinforce", "id": mine.id}], domain="explorer-bugs"
    )
    assert next(e for e in updated if e.id == mine.id).helpful == 1

    retired = merge_entries(
        entries, [{"op": "retire", "id": mine.id}], domain="explorer-bugs"
    )
    assert mine.id not in {e.id for e in retired}
    assert len(retired) == len(entries) - 1

def test_identical_content_coexists_across_domains_with_distinct_ids():
    content = "Prefer evidence anchored to a concrete source reference over prose."
    entries = merge_entries([], [{"section": "evidence", "content": content}])
    entries = merge_entries(
        entries, [{"section": "evidence", "content": content}], domain="explorer-bugs"
    )
    assert len(entries) == 2
    assert len({e.id for e in entries}) == 2
    assert {e.domain for e in entries} == {"", "explorer-bugs"}
    base, parsed = split_prompt(render_prompt("Seed.", entries))
    assert len(parsed) == 2

def test_scoped_merge_does_not_prune_untouched_foreign_entry():
    from bin.context_playbook import PlaybookEntry

    rotten = PlaybookEntry(
        id="str-aaaaaaaaaa",
        section="STRATEGIES & INSIGHTS",
        content="A harmful foreign lesson that only its own domain may prune.",
        helpful=0,
        harmful=3,
        domain="style-review",
    )
    result = merge_entries(
        [rotten],
        [{"section": "strategy", "content": "An unrelated new explorer lesson about lifetimes."}],
        domain="explorer-bugs",
    )
    assert rotten.id in {e.id for e in result}
    owned = merge_entries([rotten], [], domain="style-review")
    assert owned == []
