defmodule TournamentUiWeb.JudgementsLiveTest do
  @moduledoc """
  /results under the pair-wheel-v2 vocabulary.

  Five regressions this file exists to catch: the page reporting a narrower
  rubric set than the fabric holds, the export reporting a different set from
  the page it sits on, a Revise control that does nothing when its queue row
  has been swept away by a discard, work-order markdown printed as literal
  text instead of rendered, and foreground colours that a reviewer cannot
  read.

  The contrast test is not a class-name spot check: it parses the oklch
  values out of `assets/css/app.css`, converts them to sRGB, composites the
  tint each chip actually paints, and computes the WCAG ratio. Lightening a
  `--fg-*` token back toward the stock `--color-*` value fails it, which is
  the only kind of colour test worth having — asserting that a class name is
  present proves nothing about whether the pixels underneath it are legible.
  """
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  defp data_home_no_crashed_earlier_run_can_hand_back(prefix) do
    home =
      Path.join("/tmp", "#{prefix}-#{Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)}")

    File.rm_rf!(home)
    home
  end

  setup do
    home = data_home_no_crashed_earlier_run_can_hand_back("dt-results-live")
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
              rubric='pair-wheel-v2',
          )
          domains.create_domain(
              name='unrelated-review',
              description='Keep pending counts out of filtered views',
              corpus_source={'kind': 'inline', 'items': []},
              rubric='pair-wheel-v2',
          )

          db_path = os.path.join('#{home}', 'judgements.db')
          db = sqlite3.connect(db_path)
          db.row_factory = sqlite3.Row
          domain_id = db.execute("SELECT id FROM domain WHERE name='quality-review'").fetchone()['id']
          # The seeded machine panel is one model wide on purpose; this page is
          # about rendering a MULTI-rater comparison, so widen it here rather
          # than depend on a seed that exists to be narrow.
          template_id = db.execute(
              "SELECT id FROM eval_template WHERE name='pair-wheel-v2'").fetchone()['id']
          seeded = {
              json.loads(row['rater_config'])['model']
              for row in db.execute(
                  "SELECT rater_config FROM job_configuration "
                  "WHERE template_id=? AND rater_type='llm' AND status='active'",
                  (template_id,)).fetchall()
          }
          for model in judgement.FRONTIER_OPENROUTER_MODELS:
              if model in seeded:
                  continue
              db.execute(
                  "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
                  "VALUES (?, 'llm', ?)",
                  (template_id, json.dumps(judgement._openrouter_config(model))))
          db.commit()
          configs = db.execute(
              "SELECT c.id, c.rater_type, c.rater_config FROM job_configuration c "
              "JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.status='active' AND t.name='pair-wheel-v2' ORDER BY c.id").fetchall()
          payload = json.dumps({
              'label': 'Pair 1',
              'card_a': {
                  'title': 'Partial file read loses bytes',
                  'body': (
                      '**Domain:** unity-explorer-security \u00b7 '
                      '**Priority:** P1 \u2014 a single ReadAsync result is ignored.\\n\\n'
                      '- truncated reads corrupt the cache\\n'
                      '- <script>alert(1)</script>\\n'
                  ),
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

          discard_payload = json.dumps({
              'label': 'Pair 9',
              'card_a': {
                  'title': 'DISCARDONE placeholder finding',
                  'body': 'Not a real finding.',
                  'source_ref': 'Assets/Nowhere.cs:1',
              },
              'card_b': {
                  'title': 'SURVIVOR concrete finding',
                  'body': 'A real null-deref, pointed at a line.',
                  'source_ref': 'Assets/Nowhere.cs:2',
              },
          })
          discard_pid = db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id) VALUES (?,?,?,?,?)",
              (human_cfg, 'domain:quality-review', 9, discard_payload, domain_id),
          ).lastrowid
          db.commit()
          db.close()

          model_index = 0
          for pid, rater_type, config in pending:
              if rater_type == 'human':
                  verdict = 'a-wins'
                  rater = {'type': 'human', 'userId': 'reviewer'}
                  rationale = 'The partial read is a concrete correctness bug.'
              else:
                  verdict = 'a-wins' if model_index < 2 else 'b-wins'
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

          judgement.write_judgement(
              pending_id=discard_pid,
              verdict='discard-a',
              confidence='high',
              rationale='The A card is not a finding at all.',
              rater={'type': 'human', 'userId': 'reviewer'},
          )

          db = sqlite3.connect(db_path)
          db.row_factory = sqlite3.Row
          idea_cfg = db.execute(
              "SELECT c.id FROM job_configuration c "
              "JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.status='active' AND c.rater_type='human' "
              "AND t.name='pair-idea-wheel-v2'").fetchone()['id']
          idea_payload = json.dumps({
              'label': 'Pair 11',
              'card_a': {
                  'title': 'IDEAONE speculative refactor',
                  'body': 'No evidence anything is wrong.',
                  'source_ref': 'Assets/Nowhere.cs:11',
              },
              'card_b': {
                  'title': 'IDEATWO cache invalidation proposal',
                  'body': 'A real gap in the invalidation path.',
                  'source_ref': 'Assets/Nowhere.cs:12',
              },
          })
          idea_pid = db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id) VALUES (?,?,?,?,?)",
              (idea_cfg, 'domain:quality-review', 11, idea_payload, domain_id),
          ).lastrowid
          db.commit()
          db.close()

          judgement.write_judgement(
              pending_id=idea_pid,
              verdict='b-wins',
              confidence='high',
              rationale='The invalidation gap is the better idea.',
              rater={'type': 'human', 'userId': 'reviewer'},
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

  defp db_exec(sql, params) do
    db = Path.join(System.get_env("DATA_TOURNAMENTS_HOME"), "judgements.db")
    {:ok, conn} = Exqlite.Sqlite3.open(db)
    {:ok, stmt} = Exqlite.Sqlite3.prepare(conn, sql)
    :ok = Exqlite.Sqlite3.bind(stmt, params)
    :done = Exqlite.Sqlite3.step(conn, stmt)
    Exqlite.Sqlite3.release(conn, stmt)
    Exqlite.Sqlite3.close(conn)
    :ok
  end

  defp human_comparison_rating do
    [[rating_id, pending_id]] =
      db_query("""
      SELECT rating_id, pending_id FROM score
      WHERE name='judgement.verdict'
        AND json_extract(metadata, '$.rater.type') = 'human'
        AND match_id = 1
      """)

    {rating_id, pending_id}
  end

  defp rating_with_verdict(verdict) do
    [[rating_id] | _] =
      db_query(
        "SELECT rating_id FROM score WHERE name='judgement.verdict' AND value=?",
        [verdict]
      )

    rating_id
  end

  # ── Grouped comparison ─────────────────────────────────────────────────

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
    assert html =~ "0 human and 0 model reviews pending"
  end

  test "filters the grouped comparison by rater without losing match context", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results")

    html = live |> element("button[phx-value-v='llm']") |> render_click()
    refute html =~ "Human · reviewer"
    assert html =~ "Partial file read loses bytes"
    assert html =~ "Kimi K3"
  end

  test "legacy judgements URL retains the coherent Results view", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judgements")
    assert html =~ "Results"
    assert html =~ "Rater comparison"
  end

  test "filtered results keeps Review links scoped to the active domain", %{conn: conn} do
    {:ok, live, html} = live(conn, "/results?domain=unrelated-review")

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

    assert html =~ "No results for this view"
    refute html =~ "Partial file read loses bytes"

    assert has_element?(
             live,
             ~s(a[href="/judge?domain=unrelated-review"]),
             "Open Review queue"
           )
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

  # ── Rubric scope: the page and its export report the same set ──────────

  test "the page reports every pair rubric, not one name", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/results?domain=quality-review")

    assert html =~ "Partial file read loses bytes",
           "a pair-wheel-v2 judgement must be on the page"

    assert html =~ "IDEATWO cache invalidation proposal",
           "scoping this page to one rubric hides every judgement written under " <>
             "the other pair rubric the fabric holds"
  end

  test "the export link exports exactly what the page displays", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    href =
      live
      |> element("a", "Export JSONL")
      |> render()
      |> then(&Regex.run(~r/href="([^"]+)"/, &1))
      |> Enum.at(1)
      |> String.replace("&amp;", "&")

    body = get(build_conn(), href).resp_body
    exported = body |> String.split("\n", trim: true) |> Enum.map(&Jason.decode!/1)

    on_page =
      db_query("""
      SELECT s.rating_id FROM score s
      JOIN pending_judgement p ON p.id = s.pending_id
      JOIN domain d ON d.id = p.domain_id
      WHERE s.name='judgement.verdict' AND d.name='quality-review'
      """)
      |> Enum.map(fn [id] -> id end)
      |> Enum.sort()

    assert Enum.sort(Enum.map(exported, & &1["ratingId"])) == on_page

    rubrics = exported |> Enum.map(& &1["rubric"]) |> Enum.uniq() |> Enum.sort()
    assert rubrics == ~w(pair-idea-wheel-v2 pair-wheel-v2)
  end

  test "an export narrowed to one rubric still names that rubric in the filename",
       %{conn: conn} do
    response = get(conn, "/api/judgements/export?rubric=pair-idea-wheel-v2")

    [disposition] = Plug.Conn.get_resp_header(response, "content-disposition")
    assert disposition =~ "judgements-pair-idea-wheel-v2.jsonl"

    rubrics =
      response.resp_body
      |> String.split("\n", trim: true)
      |> Enum.map(&Jason.decode!(&1)["rubric"])
      |> Enum.uniq()

    assert rubrics == ["pair-idea-wheel-v2"]
  end

  # ── Discards eject one side ────────────────────────────────────────────

  test "the discard section lists the ejected side only, and names the survivor",
       %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    section = live |> element("#results-discards") |> render()

    assert section =~ "DISCARDONE placeholder finding"
    assert section =~ "Discard A"
    assert section =~ "quality-review"
    assert section =~ "drawn against SURVIVOR concrete finding, which stayed in the pool"

    refute section =~ "Discarded — 2"
    assert section =~ "Discarded — 1"
  end

  test "the survivor of a discard is not listed as discarded", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    entries =
      live
      |> element("#results-discards")
      |> render()
      |> then(&Regex.scan(~r/<li[^>]*id="discarded-[^"]*"/, &1))

    assert length(entries) == 1,
           "listing both sides is the collateral-ejection bug: a malformed A used " <>
             "to take a good B with it"
  end

  test "a discard is excluded from the win/loss aggregation, not from the page", %{conn: conn} do
    {:ok, live, html} = live(conn, "/results?domain=quality-review")

    assert html =~ "DISCARDONE placeholder finding",
           "the discarded pair must stay on the page, not be dropped from it"

    assert html =~ "One side left the pool"
    assert html =~ "the other stayed with nothing recorded"

    assert html =~ "Human and panel agree",
           "the scored comparison must be untouched by the discard"

    refute html =~ "Awaiting human baseline",
           "dropping the discard out of the aggregation used to fake a missing human verdict"

    assert has_element?(live, "#results-discards")
  end

  test "a domain with no discards renders no discards section", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=unrelated-review")

    refute has_element?(live, "#results-discards")
  end

  # ── Revision ───────────────────────────────────────────────────────────

  test "Revise button renders for the human rating only; LLM rows have none", %{conn: conn} do
    {rating_id, _pending_id} = human_comparison_rating()
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

  test "a swept queue row shows why it cannot be revised instead of a dead button",
       %{conn: conn} do
    {rating_id, pending_id} = human_comparison_rating()
    db_exec("UPDATE pending_judgement SET status='cancelled' WHERE id=?", [pending_id])

    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    refute has_element?(live, "#revise-#{rating_id}")
    assert has_element?(live, "#revise-unavailable-#{rating_id}")

    assert live |> element("#revise-unavailable-#{rating_id}") |> render() =~
             "queue row is cancelled"

    assert live |> element("#revise-unavailable-#{rating_id}") |> render() =~ "discard sweep"
  end

  test "opening a revision on a swept row flashes the reason, never nothing", %{conn: conn} do
    {rating_id, pending_id} = human_comparison_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    db_exec("UPDATE pending_judgement SET status='cancelled' WHERE id=?", [pending_id])

    html =
      render_click(live, "open_revise", %{
        "pending-id" => "#{pending_id}",
        "rating-id" => rating_id
      })

    refute html =~ "revision-panel",
           "a swept queue row must not open a panel whose submit is doomed"

    refute has_element?(live, "#revise-#{rating_id}")
    assert has_element?(live, "#revise-unavailable-#{rating_id}")
    assert html =~ "discard sweep"
  end

  test "revision panel opens prefilled with the current verdict and rationale", %{conn: conn} do
    {rating_id, _pending_id} = human_comparison_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    html = live |> element("#revise-#{rating_id}") |> render_click()

    assert html =~ "revision-panel"
    assert html =~ "Reason for revision (required)"
    assert live |> element("#wheel-nw") |> render() =~ "btn-primary"
    assert live |> element("#wheel-selected-label") |> render() =~ "a wins"
    assert html =~ "The partial read is a concrete correctness bug."

    html = live |> element("#cancel-revise") |> render_click()
    refute html =~ "revision-panel"
  end

  test "submitting a revision writes a new rating + revision row; old rows intact",
       %{conn: conn} do
    {rating_id, pending_id} = human_comparison_rating()

    original_rows =
      db_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id])

    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#wheel-e") |> render_click()
    live |> element("#revise-confidence-high") |> render_click()

    html =
      live
      |> form("#revise-form", %{
        "reason" => "the B card is the real bug",
        "rationale" => "second look: partial read is stylistic here"
      })
      |> render_submit()

    refute html =~ "revise-error"

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

    assert db_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id]) ==
             original_rows

    assert [["done"]] =
             db_query("SELECT status FROM pending_judgement WHERE id=?", [pending_id])
  end

  test "after a revision the history expander shows the superseded rating dimmed",
       %{conn: conn} do
    {rating_id, pending_id} = human_comparison_rating()

    db_exec(
      "UPDATE pending_judgement SET trace_payload = json_set(trace_payload, '$.winner_id', 1) WHERE id=?",
      [pending_id]
    )

    {:ok, live, _html} = live(conn, "/results?domain=quality-review")
    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#wheel-e") |> render_click()

    html =
      live
      |> form("#revise-form", %{"reason" => "flip to B"})
      |> render_submit()

    assert html =~ "data-role=\"revised-chip\""
    assert html =~ "flip to B"
    assert html =~ "history (1)"
    assert has_element?(live, "#superseded-#{rating_id}[data-superseded]")
    assert live |> element("#superseded-#{rating_id}") |> render() =~ "line-through"
    assert html =~ "revised after use — downstream outcomes unaffected"

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
    {rating_id, pending_id} = human_comparison_rating()
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#wheel-ne") |> render_click()

    html = live |> form("#revise-form", %{"reason" => "   "}) |> render_submit()

    assert html =~ "a reason is required to revise"

    assert [[0]] =
             db_query("SELECT COUNT(*) FROM judgement_revision WHERE pending_id=?", [pending_id])
  end

  test "the revision wheel offers both per-side discards and no both-are-bad seat",
       %{conn: conn} do
    rating_id = rating_with_verdict("discard-a")
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    html = live |> element("#revise-#{rating_id}") |> render_click()
    assert html =~ "revision-panel"

    assert has_element?(live, "#wheel-sw[phx-value-v='discard-a']")
    assert has_element?(live, "#wheel-se[phx-value-v='discard-b']")

    refute has_element?(live, "#wheel-s[phx-click='set_verdict']"),
           "there is deliberately no 'both are bad' verdict: a judge facing two bad " <>
             "items discards one and the other returns to the pool"
  end

  test "a per-side discard submits and is stored as the effective rating", %{conn: conn} do
    rating_id = rating_with_verdict("discard-a")
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    live |> element("#revise-#{rating_id}") |> render_click()
    live |> element("#wheel-se") |> render_click()
    live |> element("#revise-confidence-high") |> render_click()

    html =
      live
      |> form("#revise-form", %{
        "reason" => "I ejected the wrong side",
        "rationale" => "B is the one that should never have been generated"
      })
      |> render_submit()

    refute html =~ "revise-error"

    [[verdict]] =
      db_query(
        """
        SELECT s.value FROM score s
        JOIN judgement_revision r ON r.new_rating_id = s.rating_id
        WHERE s.name='judgement.verdict' AND r.previous_rating_id=?
        """,
        [rating_id]
      )

    assert verdict == "discard-b"
  end

  # ── Work-order markdown is rendered, not printed ───────────────────────

  test "a work-order body renders as markup, with no literal asterisks left",
       %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")
    html = render(live)

    assert html =~ "<strong>Domain:</strong>",
           "the card body is work-order markdown; SafeMarkdown must turn " <>
             "**Domain:** into a strong element"

    assert html =~ "<strong>Priority:</strong>"
    assert html =~ "<li>"
    assert html =~ "truncated reads corrupt the cache"

    refute html =~ "**Domain:**",
           "printing the body as text is the leak: 70 leaf elements on this " <>
             "page showed their own markdown syntax"

    refute html =~ "**Priority:**"
  end

  test "a card body is sanitized on its way to markup, not merely unescaped",
       %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")
    html = render(live)

    refute html =~ "<script>alert(1)",
           "reaching for raw/1 instead of SafeMarkdown would hand generator " <>
             "output to the browser as live HTML"

    assert html =~ "&lt;script&gt;alert(1)&lt;/script&gt;",
           "the sanitizer escapes what it cannot allow, so the reviewer still " <>
             "sees what the generator actually wrote"
  end

  # ── Contrast ───────────────────────────────────────────────────────────

  @wcag_aa 4.5

  test "every foreground token clears WCAG AA on the surfaces it paints on" do
    css = File.read!("assets/css/app.css")

    for theme <- ~w(dark light) do
      tokens = theme_tokens(css, theme)
      bases = for n <- ~w(100 200 300), do: srgb(fetch_token(tokens, theme, "--color-base-#{n}"))

      for role <- ~w(primary info success warning error) do
        fg = srgb(fetch_token(tokens, theme, "--fg-#{role}"))
        fill = srgb(fetch_token(tokens, theme, "--color-#{role}"))

        surfaces =
          Enum.flat_map(bases, fn base ->
            [
              {"plain", base},
              {"/15 tint", over(fill, base, 0.15)},
              {"/10 tint", over(fill, base, 0.10)}
            ]
          end)

        for {kind, surface} <- surfaces do
          measured = contrast(fg, surface)

          assert measured >= @wcag_aa,
                 "#{theme}: text-#{role} on a #{kind} surface measures " <>
                   "#{Float.round(measured, 2)}:1, under the #{@wcag_aa}:1 AA floor. " <>
                   "--fg-#{role} exists precisely so --color-#{role} can stay a fill."
        end
      end

      muted = srgb(fetch_token(tokens, theme, "--fg-muted"))

      for {base, n} <- Enum.zip(bases, ~w(100 200 300)) do
        measured = contrast(muted, base)

        assert measured >= @wcag_aa,
               "#{theme}: text-muted on base-#{n} measures " <>
                 "#{Float.round(measured, 2)}:1, under the #{@wcag_aa}:1 AA floor"
      end
    end
  end

  test "the audited tokens are the ones the text utilities actually resolve to" do
    css = File.read!("assets/css/app.css")

    for role <- ~w(primary info success warning error muted) do
      assert css =~ ".text-#{role} { color: var(--fg-#{role}",
             "an audited --fg-#{role} that no utility reads is a token nobody sees"
    end
  end

  test "results metadata is muted by the audited token, not by opacity", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/results?domain=quality-review")

    for region <- ~w(#result-groups #results-discards) do
      markup = live |> element(region) |> render()

      assert markup =~ "text-muted"

      for level <- ~w(50 55 60 65) do
        refute markup =~ "opacity-#{level}",
               "#{region}: opacity-#{level} on base-content measures 4.37:1 or " <>
                 "worse, and it multiplies into any coloured child it wraps"
      end
    end
  end

  # ── oklch → sRGB → WCAG, so the assertions above measure rather than guess ──

  defp theme_tokens(css, name) do
    ~r/@plugin\s+"\.\.\/vendor\/daisyui-theme"\s*\{(.*?)\n\}/s
    |> Regex.scan(css)
    |> Enum.map(fn [_all, body] -> body end)
    |> Enum.find(&(&1 =~ ~r/name:\s*"#{name}"/))
    |> then(fn
      nil -> flunk("app.css declares no daisyUI theme named #{name}")
      block -> block
    end)
    |> then(fn block ->
      ~r/(--[a-z0-9-]+):\s*oklch\(\s*([0-9.]+)%\s+([0-9.]+)\s+([0-9.]+)\s*\)/
      |> Regex.scan(block)
      |> Map.new(fn [_all, key, l, c, h] -> {key, {num(l) / 100, num(c), num(h)}} end)
    end)
  end

  defp fetch_token(tokens, theme, key) do
    case Map.fetch(tokens, key) do
      {:ok, value} -> value
      :error -> flunk("the #{theme} theme in app.css defines no #{key}")
    end
  end

  defp num(text) do
    {value, ""} = Float.parse(text)
    value
  end

  defp srgb({l, c, h}) do
    radians = h * :math.pi() / 180
    a = c * :math.cos(radians)
    b = c * :math.sin(radians)

    long = l + 0.3963377774 * a + 0.2158037573 * b
    medium = l - 0.1055613458 * a - 0.0638541728 * b
    short = l - 0.0894841775 * a - 1.2914855480 * b

    {lc, mc, sc} = {long * long * long, medium * medium * medium, short * short * short}

    {
      gamma(4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc),
      gamma(-1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc),
      gamma(-0.0041960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc)
    }
  end

  defp gamma(value) do
    clamped = value |> max(0.0) |> min(1.0)

    if clamped > 0.0031308,
      do: 1.055 * :math.pow(clamped, 1 / 2.4) - 0.055,
      else: 12.92 * clamped
  end

  defp linear(value) do
    if value <= 0.04045, do: value / 12.92, else: :math.pow((value + 0.055) / 1.055, 2.4)
  end

  defp over({fr, fg, fb}, {br, bg, bb}, alpha) do
    {
      fr * alpha + br * (1 - alpha),
      fg * alpha + bg * (1 - alpha),
      fb * alpha + bb * (1 - alpha)
    }
  end

  defp luminance({r, g, b}) do
    0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
  end

  defp contrast(fg, bg) do
    a = luminance(fg)
    b = luminance(bg)
    (max(a, b) + 0.05) / (min(a, b) + 0.05)
  end
end
