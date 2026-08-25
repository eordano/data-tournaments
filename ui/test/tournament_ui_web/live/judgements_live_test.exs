defmodule TournamentUiWeb.JudgementsLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home = "/tmp/dt-results-live-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import json, os, sqlite3, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          os.environ['PROMPT_BACKEND'] = 'local'
          sys.path.insert(0, '#{repo_root}')
          sys.path.insert(0, '#{repo_root}/bin')

          import judgement
          judgement.init_db()
          import bin.domains as domains
          domains.create_domain(
              name='quality-review',
              description='Find concrete quality risks',
              corpus_source={'kind': 'inline', 'items': []},
          )
          domains.create_domain(
              name='unrelated-review',
              description='Keep pending counts out of filtered views',
              corpus_source={'kind': 'inline', 'items': []},
          )

          db_path = os.path.join('#{home}', 'judgements.db')
          db = sqlite3.connect(db_path)
          db.row_factory = sqlite3.Row
          domain_id = db.execute("SELECT id FROM domain WHERE name='quality-review'").fetchone()['id']
          # Scope to the legacy rubric's configs — wave-12 init also seeds
          # wheel templates (pair-wheel-v1, …) whose enums reject these
          # legacy verdicts.
          configs = db.execute(
              "SELECT c.id, c.rater_type, c.rater_config FROM job_configuration c "
              "JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.status='active' AND t.name='card-prioritizer-v0' ORDER BY c.id").fetchall()
          payload = json.dumps({
              'label': 'Pair 1',
              'card_a': {
                  'title': 'Partial file read loses bytes',
                  'body': 'A single ReadAsync result is ignored.',
                  'source_ref': 'Assets/DiskCache.cs:114',
              },
              'card_b': {
                  'title': 'Rename local variable',
                  'body': 'A stylistic cleanup with no runtime impact.',
                  'source_ref': 'Assets/DiskCache.cs:88',
              },
          })
          pending = []
          for config in configs:
              pid = db.execute(
                  "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id) VALUES (?,?,?,?,?)",
                  (config['id'], 'domain:quality-review', 1, payload, domain_id),
              ).lastrowid
              pending.append((pid, config['rater_type'], json.loads(config['rater_config'])))
          db.commit()

          unrelated_id = db.execute("SELECT id FROM domain WHERE name='unrelated-review'").fetchone()['id']
          human_cfg = next(config['id'] for config in configs if config['rater_type'] == 'human')
          db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id) VALUES (?,?,?,?,?)",
              (human_cfg, 'domain:unrelated-review', 2, payload, unrelated_id),
          )
          db.commit()
          db.close()

          model_index = 0
          for pid, rater_type, config in pending:
              if rater_type == 'human':
                  verdict = 'a-clearly-better'
                  rater = {'type': 'human', 'userId': 'reviewer'}
                  rationale = 'The partial read is a concrete correctness bug.'
              else:
                  verdict = 'a-clearly-better' if model_index < 2 else 'b-marginally-better'
                  model_index += 1
                  rater = {'type': 'llm', 'model': config['model']}
                  rationale = 'Compared impact and source specificity.'
              judgement.write_judgement(
                  pending_id=pid,
                  verdict=verdict,
                  confidence='high',
                  rationale=rationale,
                  rater=rater,
              )
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "groups a match with candidates, sources, human and model ratings", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/results?domain=quality-review")

    assert html =~ "Results"
    assert html =~ "quality-review"
    assert html =~ "Partial file read loses bytes"
    assert html =~ "Assets/DiskCache.cs:114"
    assert html =~ "Rename local variable"
    assert html =~ "Human · reviewer"
    assert html =~ "Kimi K3"
    assert html =~ "GLM 5.2"
    assert html =~ "Claude Opus 5"
    assert html =~ "Human and panel agree"
    assert html =~ "Configure domain"
    assert html =~ "domain=quality-review"
    assert html =~ "0 human and 0 model reviews pending"
    refute html =~ "Review 1 pending"
  end

  test "filters the grouped comparison by rater without losing match context", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results")

    html = live |> element("button[phx-value-v='llm']") |> render_click()
    refute html =~ "Human · reviewer"
    assert html =~ "Partial file read loses bytes"
    assert html =~ "Kimi K3"
    assert html =~ "3 ratings across 1 matches"
  end

  test "legacy judgements URL retains the coherent Results view", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judgements")
    assert html =~ "Results"
    assert html =~ "Rater comparison"
  end

  test "filtered results keeps Review links scoped to the active domain", %{conn: conn} do
    {:ok, live, html} = live(conn, "/results?domain=unrelated-review")

    # unrelated-review has exactly 1 pending human review — the count must be
    # domain-scoped and the Review CTA must carry the domain.
    assert html =~ "1 human and 0 model reviews pending"

    assert has_element?(
             live,
             ~s(a[href="/judge?domain=unrelated-review"]),
             "Review 1 pending"
           )

    refute has_element?(live, ~s(a[href="/judge"]), "Review 1 pending")
  end

  test "empty filtered results shows the empty state without dropping the scope", %{conn: conn} do
    {:ok, live, html} = live(conn, "/results?domain=unrelated-review")

    # No judgements recorded for unrelated-review → empty state, but the
    # Review queue link must still point at the filtered queue.
    assert html =~ "No results for this view"
    refute html =~ "Partial file read loses bytes"

    assert has_element?(
             live,
             ~s(a[href="/judge?domain=unrelated-review"]),
             "Open Review queue"
           )
  end

  test "filtered results shows domain-scoped pending counts, not global", %{conn: conn} do
    # quality-review is fully judged: its scoped view must show 0 pending even
    # though the global queue still holds unrelated-review's pending row.
    {:ok, live, html} = live(conn, "/results?domain=quality-review")

    assert html =~ "0 human and 0 model reviews pending"
    refute html =~ "1 human and 0 model reviews pending"
    refute has_element?(live, ~s(a[href*="/judge"]), "Review 1 pending")
  end

  test "global results keeps global counts and unscoped Review links", %{conn: conn} do
    {:ok, live, html} = live(conn, "/results")

    # Global view: pending count covers every domain, links carry no filter.
    assert html =~ "1 human and 0 model reviews pending"
    assert has_element?(live, ~s(a[href="/judge"]), "Review 1 pending")
    refute has_element?(live, ~s(a[href*="/judge?domain="]))
  end

  test "picking a domain from the filter patches to the scoped URL", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results")

    live
    |> element(~s(select[name="domain"]))
    |> render_change(%{"domain" => "quality-review"})

    assert_patch(live, "/results?domain=quality-review")

    live
    |> element(~s(select[name="domain"]))
    |> render_change(%{"domain" => ""})

    assert_patch(live, "/results")
  end

  # ── wave-13 slice A: append-only judgement revision ────────────────────

  defp db_query(sql, params \\ []) do
    db = Path.join(System.get_env("DATA_TOURNAMENTS_HOME"), "judgements.db")
    {:ok, conn} = Exqlite.Sqlite3.open(db)
    {:ok, stmt} = Exqlite.Sqlite3.prepare(conn, sql)
    :ok = Exqlite.Sqlite3.bind(stmt, params)
    rows = db_collect(conn, stmt, [])
    Exqlite.Sqlite3.release(conn, stmt)
    Exqlite.Sqlite3.close(conn)
    Enum.reverse(rows)
  end

  defp db_collect(conn, stmt, acc) do
    case Exqlite.Sqlite3.step(conn, stmt) do
      {:row, row} -> db_collect(conn, stmt, [row | acc])
      :done -> acc
    end
  end

  defp human_rating do
    [[rating_id, pending_id]] =
      db_query("""
      SELECT rating_id, pending_id FROM score
      WHERE name='judgement.verdict'
        AND json_extract(metadata, '$.rater.type') = 'human'
      """)

    {rating_id, pending_id}
  end

  test "Revise button renders for the human rating only; LLM rows have none", %{conn: conn} do
    {rating_id, _pending_id} = human_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    assert has_element?(live, "#revise-#{rating_id}", "Revise")

    llm_ids =
      db_query("""
      SELECT rating_id FROM score
      WHERE name='judgement.verdict'
        AND json_extract(metadata, '$.rater.type') = 'llm'
      """)

    assert llm_ids != []

    for [llm_rating_id] <- llm_ids do
      refute has_element?(live, "#revise-#{llm_rating_id}")
    end
  end

  test "revision panel opens prefilled with the current verdict and rationale", %{conn: conn} do
    {rating_id, _pending_id} = human_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    html = live |> element("#revise-#{rating_id}") |> render_click()

    assert html =~ "revision-panel"
    assert html =~ "Reason for revision (required)"
    # Prefilled: the current verdict button is highlighted (btn-primary)
    # and the prior rationale populates the textarea.
    assert live
           |> element("#revise-verdict-a-clearly-better")
           |> render() =~ "btn-primary"

    assert html =~ "The partial read is a concrete correctness bug."

    # Cancel closes the panel.
    html = live |> element("#cancel-revise") |> render_click()
    refute html =~ "revision-panel"
  end

  test "submitting a revision writes a new rating + revision row; old rows intact", %{conn: conn} do
    {rating_id, pending_id} = human_rating()

    original_rows =
      db_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id])

    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#revise-verdict-b-clearly-better") |> render_click()
    live |> element("#revise-confidence-high") |> render_click()

    html =
      live
      |> form("#revise-form", %{
        "reason" => "the B card is the real bug",
        "rationale" => "second look: partial read is stylistic here"
      })
      |> render_submit()

    refute html =~ "revise-error"

    # Raw SQL: 2 ratings for this pending, exactly 1 revision row.
    assert [[2]] =
             db_query(
               "SELECT COUNT(DISTINCT rating_id) FROM score WHERE pending_id=?",
               [pending_id]
             )

    assert [[1]] =
             db_query("SELECT COUNT(*) FROM judgement_revision WHERE pending_id=?", [pending_id])

    [[^pending_id, ^rating_id, new_rating_id, _by, "the B card is the real bug"]] =
      db_query(
        "SELECT pending_id, previous_rating_id, new_rating_id, revised_by, reason FROM judgement_revision WHERE pending_id=?",
        [pending_id]
      )

    assert new_rating_id != rating_id

    # Old score rows byte-identical.
    assert db_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id]) ==
             original_rows

    # Pending stays done — no status churn.
    assert [["done"]] =
             db_query("SELECT status FROM pending_judgement WHERE id=?", [pending_id])
  end

  test "after a revision the history expander shows the superseded rating dimmed", %{conn: conn} do
    {rating_id, pending_id} = human_rating()

    # Simulate downstream use: the pair already advanced a bracket
    # (winner recorded) before the revision happens.
    db_query(
      "UPDATE pending_judgement SET trace_payload = json_set(trace_payload, '$.winner_id', 1) WHERE id=?",
      [pending_id]
    )

    {:ok, live, _html} = live(conn, "/results?domain=quality-review")
    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#revise-verdict-b-clearly-better") |> render_click()

    html =
      live
      |> form("#revise-form", %{"reason" => "flip to B"})
      |> render_submit()

    # Effective row: revised chip + reason caption; superseded original is
    # inside the collapsed history expander with struck-through styling.
    assert html =~ "data-role=\"revised-chip\""
    assert html =~ "flip to B"
    assert html =~ "history (1)"
    assert has_element?(live, "#superseded-#{rating_id}[data-superseded]")
    assert live |> element("#superseded-#{rating_id}") |> render() =~ "line-through"

    # Honest caption: the pair was already used downstream.
    assert html =~ "revised after use — downstream outcomes unaffected"

    # The new tip renders normally (not struck through) with a Revise button.
    [[new_rating_id]] =
      db_query(
        "SELECT new_rating_id FROM judgement_revision WHERE pending_id=?",
        [pending_id]
      )

    assert has_element?(live, "#rating-#{new_rating_id}")
    assert has_element?(live, "#revise-#{new_rating_id}", "Revise")
    refute has_element?(live, "#revise-#{rating_id}")
  end

  test "submitting a revision without a reason is refused", %{conn: conn} do
    {rating_id, pending_id} = human_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#revise-verdict-b-clearly-better") |> render_click()

    html = live |> form("#revise-form", %{"reason" => "   "}) |> render_submit()

    assert html =~ "a reason is required to revise"

    assert [[0]] =
             db_query("SELECT COUNT(*) FROM judgement_revision WHERE pending_id=?", [pending_id])
  end
end
