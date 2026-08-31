defmodule TournamentUiWeb.StandingsLiveTest do
  @moduledoc """
  /standings is the derived queue view the design asks for — off the judging
  surface, so the operator can act on the top group while the lower pairings
  are still being judged.

  The page renders what `bin/standings_view.py` computed and nothing else, so
  these tests assert the read model reaches the DOM intact and that an
  uncomputed view announces itself rather than looking like an empty corpus.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  alias TournamentUi.Standings

  @standing_marker "8675309"

  defp data_home_no_crashed_earlier_run_can_hand_back(prefix) do
    home =
      Path.join("/tmp", "#{prefix}-#{Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)}")

    File.rm_rf!(home)
    home
  end

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    home = data_home_no_crashed_earlier_run_can_hand_back("dt-standings-live")
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          os.environ['PROMPT_BACKEND'] = 'local'
          sys.path.insert(0, '#{repo_root}')
          sys.path.insert(0, '#{repo_root}/bin')

          import judgement
          judgement.init_db()
          import bin.domains as domains
          domains.create_domain(
              name='order-review',
              description='Items competing for a position in the queue',
              corpus_source={'kind': 'inline', 'items': []},
              rubric='pair-wheel-v2',
          )

          db_path = os.path.join('#{home}', 'judgements.db')
          db = sqlite3.connect(db_path)
          db.row_factory = sqlite3.Row
          dom = db.execute("SELECT id FROM domain WHERE name='order-review'").fetchone()['id']

          def human_cfg(template):
              return db.execute(
                  "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id = c.template_id "
                  "WHERE c.rater_type='human' AND c.status='active' AND t.name=?",
                  (template,)).fetchone()['id']

          def pair(a, b, rnd, slot):
              return json.dumps({
                  'label': f'R{rnd}-{slot}',
                  'round': rnd,
                  'card_a': {'title': a, 'body': f'body {a}', 'source_ref': f'{a.lower()}.md'},
                  'card_b': {'title': b, 'body': f'body {b}', 'source_ref': f'{b.lower()}.md'},
              })

          plan = [
              ('pair-wheel-v2', 'ALPHA', 'BETA', 1, 1, 'a-wins-big'),
              ('pair-wheel-v2', 'GAMMA', 'DELTA', 1, 2, 'tie'),
              ('pair-wheel-v2', 'ECHO', 'FOXTROT', 1, 3, 'a-wins'),
              ('pair-wheel-v2', 'ALPHA', 'GAMMA', 2, 1, 'a-wins'),
              ('pair-wheel-v2', 'BETA', 'DELTA', 2, 2, 'b-wins-big'),
              ('pair-wheel-v2', 'JUNK', 'ECHO', 2, 3, 'discard-a'),
              ('pair-idea-wheel-v2', 'IDEA1', 'IDEA2', 1, 1, 'a-wins'),
          ]

          written = []
          for match_id, (template, a, b, rnd, slot, verdict) in enumerate(plan, start=1):
              pid = db.execute(
                  "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id)"
                  " VALUES (?,?,?,?,?)",
                  (human_cfg(template), 'domain:order-review', match_id, pair(a, b, rnd, slot), dom),
              ).lastrowid
              written.append((pid, verdict))

          open_payload_left_unjudged_so_the_judge_page_has_a_pair_to_show = json.dumps({
              'label': 'R3-1',
              'round': 3,
              'points': #{@standing_marker},
              'standings': [{'item': 'OPENA', 'points': #{@standing_marker}}],
              'card_a': {'title': 'OPENA', 'body': 'body OPENA', 'rank': #{@standing_marker}},
              'card_b': {'title': 'OPENB', 'body': 'body OPENB'},
          })
          db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id)"
              " VALUES (?,?,?,?,?)",
              (human_cfg('pair-wheel-v2'), 'domain:order-review', 500,
               open_payload_left_unjudged_so_the_judge_page_has_a_pair_to_show, dom))
          db.commit()
          db.close()

          for pid, verdict in written:
              judgement.write_judgement(
                  pending_id=pid,
                  verdict=verdict,
                  confidence='high',
                  rationale='seeded for the standings table',
                  rater={'type': 'human', 'userId': 'reviewer'},
              )
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    :ok
  end

  defp materialised(conn, path) do
    :ok = Standings.materialise()
    live(conn, path)
  end

  defp row_points(view, rubric) do
    view
    |> element("#standings-table-#{rubric}")
    |> render()
    |> then(&Regex.scan(~r/<tr[^>]*id="standing-[^"]*"[^>]*data-points="(\d+)"/, &1))
    |> Enum.map(fn [_all, points] -> points end)
  end

  test "an uncomputed view says so instead of showing an empty corpus", %{conn: conn} do
    {:ok, view, html} = live(conn, "/standings?domain=order-review")

    assert has_element?(view, "#standings-empty")
    assert html =~ "has not been computed"
    refute html =~ "No settled positions yet"
  end

  test "Recompute materialises the table the page then renders", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/standings?domain=order-review")

    html = view |> element("#standings-recompute") |> render_click()

    assert html =~ "ALPHA"
    assert row_points(view, "pair-wheel-v2") == ~w(6 4 3 1 0 0)
  end

  test "the table is the engine's — points, order and W/D/L", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")

    assert has_element?(view, "#standings-table-pair-wheel-v2")
    assert row_points(view, "pair-wheel-v2") == ~w(6 4 3 1 0 0)
    assert row_points(view, "pair-idea-wheel-v2") == ~w(3 0)

    alpha = view |> element("tr[id^='standing-']", "ALPHA") |> render()
    assert alpha =~ ~s(data-points="6")
    assert alpha =~ "2 / 0 / 0"
  end

  test "marks the top group", %{conn: conn} do
    {:ok, view, html} = materialised(conn, "/standings?domain=order-review")

    assert has_element?(view, "tr[data-top-group='true']", "ALPHA")
    refute has_element?(view, "tr[data-top-group='true']", "BETA")
    assert html =~ "top group"
  end

  test "each pair rubric gets its own section", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")

    assert has_element?(view, "#standings-rubric-pair-wheel-v2")
    assert has_element?(view, "#standings-rubric-pair-idea-wheel-v2")
  end

  test "states the round and the comparison count behind each table", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")

    text =
      view |> element("#standings-rubric-pair-wheel-v2 [data-role='rubric-round']") |> render()

    assert text =~ "Round 2"
    assert text =~ "5 scored comparison"
  end

  test "a discard ejects one side and names the survivor that stayed", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")

    discards = view |> element("#discards-pair-wheel-v2") |> render()

    assert discards =~ "JUNK"
    assert discards =~ "discard-a"
    assert discards =~ "drawn against ECHO, which stayed in the pool"

    refute has_element?(view, "tr[id^='standing-']", "JUNK")

    assert has_element?(view, "tr[id^='standing-']", "ECHO"),
           "the survivor of a discarded pairing must still hold its position"
  end

  test "zero from losing is labelled apart from a survivor with no result", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")

    beta = view |> element("tr[id^='standing-']", "BETA") |> render()
    assert beta =~ "zero points"
    refute beta =~ "no result yet"
  end

  test "the freshness line reports verdicts recorded since the last compute", %{conn: conn} do
    {:ok, view, _html} = materialised(conn, "/standings?domain=order-review")
    assert view |> element("#standings-freshness") |> render() =~ "current"

    pending =
      TournamentUi.Judgement.list_pending(rater_type: "human", limit: 10)
      |> List.first()

    {:ok, _} = TournamentUi.Judgement.submit_human(pending.id, "a-wins", "mid")

    {:ok, view, _html} = live(conn, "/standings?domain=order-review")

    assert view |> element("#standings-freshness") |> render() =~ "recorded since"
  end

  test "points live here and never reach the judging surface", %{conn: conn} do
    {:ok, _view, standings_html} = materialised(conn, "/standings?domain=order-review")

    assert standings_html =~ "Points"
    assert standings_html =~ ~s(data-points="6")

    {:ok, _view, judge_html} = live(conn, "/judge?domain=order-review")

    assert judge_html =~ "OPENA",
           "the judge page must actually be showing the open pair for this to mean anything"

    lowered = String.downcase(judge_html)

    for forbidden <- ["top group", "points", "standing", "rank", " w / d / l"] do
      refute String.contains?(lowered, forbidden),
             "the judging surface leaked #{inspect(forbidden)} — showing standing anchors the next comparison"
    end

    refute judge_html =~ @standing_marker,
           "standing keys in a pending payload must be scrubbed before the judge sees them"
  end

  test "the nav does not highlight Results while the operator is on Standings",
       %{conn: conn} do
    {:ok, view, _html} = live(conn, "/standings?domain=order-review")

    assert has_element?(view, "nav[aria-label='Workspace'] a[href='/results']"),
           "the workspace nav must be rendering at all for the next assertion to mean anything"

    refute has_element?(view, "nav[aria-label='Workspace'] a.is-active[href='/results']"),
           "borrowing the Results nav key highlights a page the operator is not reading"
  end

  test "the domain card routes to the settled order and has one primary action",
       %{conn: conn} do
    {:ok, view, _html} = live(conn, "/domains")

    card = view |> element("article[id^='domain-']") |> render()

    assert card =~ "/standings?domain=order-review",
           "a judged domain must offer the priority order its comparisons produced"

    primaries = length(String.split(card, "btn-primary")) - 1

    assert primaries == 1,
           "the card offered #{primaries} primary actions; one card states one next step"
  end

  test "the domain filter patches the scope", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/standings")

    view
    |> element("#standings-domain-filter")
    |> render_change(%{"domain" => "order-review"})

    assert_patch(view, "/standings?domain=order-review")
  end

  test "a domain with no judgements renders the empty state, not a broken table",
       %{conn: conn} do
    {:ok, view, html} = materialised(conn, "/standings?domain=nothing-here")

    assert has_element?(view, "#standings-empty")
    refute html =~ ~s(id="standings-table-)
  end
end
