defmodule TournamentUi.StandingsTest do
  @moduledoc """
  The UI reads the points table; it does not compute one.

  Every number below was produced by `bin/standings_view.py` through
  `bin/swiss.py`. What this file pins is that the read side reports the
  engine's answer faithfully, and that it distinguishes "nobody has
  recomputed the view" from "nothing has been judged" — an uncomputed table
  rendered as an empty one is exactly how a person mistakes a stale page for
  data loss.
  """
  use ExUnit.Case, async: false

  alias TournamentUi.Standings

  defp data_home_no_crashed_earlier_run_can_hand_back(prefix) do
    home =
      Path.join("/tmp", "#{prefix}-#{Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)}")

    File.rm_rf!(home)
    home
  end

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    home = data_home_no_crashed_earlier_run_can_hand_back("dt-standings")
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    {:ok, home: home, repo_root: repo_root}
  end

  @plan """
  plan = [
      ('pair-wheel-v2', 'ALPHA', 'BETA', 1, 1, 'a-wins-big'),
      ('pair-wheel-v2', 'GAMMA', 'DELTA', 1, 2, 'tie'),
      ('pair-wheel-v2', 'ECHO', 'FOXTROT', 1, 3, 'a-wins'),
      ('pair-wheel-v2', 'ALPHA', 'GAMMA', 2, 1, 'a-wins'),
      ('pair-wheel-v2', 'BETA', 'DELTA', 2, 2, 'b-wins-big'),
      ('pair-wheel-v2', 'JUNK', 'ECHO', 2, 3, 'discard-a'),
  ]
  """

  defp seed(ctx, plan \\ @plan) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{ctx.home}'
          os.environ['PROMPT_BACKEND'] = 'local'
          sys.path.insert(0, '#{ctx.repo_root}')
          sys.path.insert(0, '#{ctx.repo_root}/bin')

          import judgement
          judgement.init_db()
          import bin.domains as domains
          domains.create_domain(
              name='order-review',
              description='Items competing for a position in the queue',
              corpus_source={'kind': 'inline', 'items': []},
              rubric='pair-wheel-v2',
          )

          db_path = os.path.join('#{ctx.home}', 'judgements.db')
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

          #{plan}

          written = []
          for match_id, (template, a, b, rnd, slot, verdict) in enumerate(plan, start=1):
              pid = db.execute(
                  "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id)"
                  " VALUES (?,?,?,?,?)",
                  (human_cfg(template), 'domain:order-review', match_id, pair(a, b, rnd, slot), dom),
              ).lastrowid
              written.append((pid, verdict))
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
        env: [{"DATA_TOURNAMENTS_HOME", ctx.home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    :ok
  end

  defp only_table(view) do
    assert [table] = view.tables
    table
  end

  defp entry(table, title), do: Enum.find(table.standings, &(&1.title == title))
  defp titles(table), do: Enum.map(table.standings, & &1.title)

  test "a scope that has never been materialised says so instead of showing an empty table",
       ctx do
    seed(ctx)

    view = Standings.table(domain: "order-review")

    refute view.materialised?
    assert view.tables == []
    assert view.totals == %{rubrics: 0, items: 0, matches: 0, discarded: 0}
    refute Standings.view_present?()
  end

  test "materialise/0 populates the view the page reads", ctx do
    seed(ctx)

    assert Standings.materialise() == :ok
    assert Standings.view_present?()

    view = Standings.table(domain: "order-review")
    assert view.materialised?
    assert view.computed_at
    assert view.behind_by == 0
  end

  test "the table is the engine's, points and order included", ctx do
    seed(ctx)
    :ok = Standings.materialise()
    table = only_table(Standings.table(domain: "order-review"))

    assert titles(table) == ~w(ALPHA DELTA ECHO GAMMA BETA FOXTROT)

    assert %{points: 6, played: 2, wins: 2, draws: 0, losses: 0, rank: 1} = entry(table, "ALPHA")
    assert %{points: 4, played: 2, wins: 1, draws: 1, losses: 0, rank: 2} = entry(table, "DELTA")
    assert %{points: 1, played: 2, wins: 0, draws: 1, losses: 1} = entry(table, "GAMMA")
    assert %{points: 0, played: 2, wins: 0, draws: 0, losses: 2} = entry(table, "BETA")
    assert table.rubric == "pair-wheel-v2"
    assert table.matches == 5
    assert table.round == 2
  end

  test "the top group is every item on the highest total", ctx do
    seed(ctx)
    :ok = Standings.materialise()
    table = only_table(Standings.table(domain: "order-review"))

    assert table.top_group_points == 6
    assert Enum.filter(table.standings, & &1.top_group) |> Enum.map(& &1.title) == ["ALPHA"]
  end

  test "a discard ejects the named side only; the survivor stays in the table", ctx do
    seed(ctx)
    :ok = Standings.materialise()
    table = only_table(Standings.table(domain: "order-review"))

    assert Enum.map(table.discards, & &1.title) == ["JUNK"]
    refute "JUNK" in titles(table)

    assert "ECHO" in titles(table),
           "ECHO was drawn against a discarded card; ejecting it too is the collateral bug"

    assert Enum.find(table.discards, &(&1.title == "JUNK")).survivor_title == "ECHO"
    assert Enum.find(table.discards, &(&1.title == "JUNK")).verdict == "discard-a"
    assert Enum.all?(table.discards, &(&1.pool == "order-review"))
  end

  test "zero from losing and zero from a survived discard are different positions", ctx do
    seed(ctx, """
    plan = [
        ('pair-wheel-v2', 'WINNER', 'LOSER', 1, 1, 'a-wins'),
        ('pair-wheel-v2', 'JUNK', 'SURVIVOR', 1, 2, 'discard-a'),
    ]
    """)

    :ok = Standings.materialise()
    table = only_table(Standings.table(domain: "order-review"))

    assert entry(table, "LOSER").lost_honestly
    refute entry(table, "LOSER").awaiting_first_result
    assert entry(table, "SURVIVOR").awaiting_first_result
    refute entry(table, "SURVIVOR").lost_honestly
    assert entry(table, "SURVIVOR").played == 0
    assert entry(table, "SURVIVOR").rank == 0
  end

  test "a verdict the engine does not score is reported, not folded into the table", ctx do
    seed(ctx, """
    plan = [('pair-wheel-v2', 'ALPHA', 'BETA', 1, 1, 'a-wins')]
    """)

    {_out, 0} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sqlite3, json
          db = sqlite3.connect(os.path.join('#{ctx.home}', 'judgements.db'))
          db.row_factory = sqlite3.Row
          tpl = db.execute("SELECT id FROM eval_template WHERE name='pair-wheel-v2'").fetchone()['id']
          pid = db.execute("SELECT id FROM pending_judgement LIMIT 1").fetchone()['id']
          db.execute(
              "INSERT INTO score(rating_id,pending_id,template_id,rubric_version,name,data_type,"
              "value,metadata,tournament_db_path,match_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
              ('retired-1', pid, tpl, 2, 'judgement.verdict', 'CATEGORICAL', 'incoherent',
               json.dumps({'rater': {'type': 'human'}}), 'domain:order-review', 900))
          db.execute(
              "INSERT INTO score(rating_id,pending_id,template_id,rubric_version,name,data_type,"
              "value,metadata,tournament_db_path,match_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
              ('retired-1', pid, tpl, 2, 'judgement.confidence', 'CATEGORICAL', 'high',
               json.dumps({'rater': {'type': 'human'}}), 'domain:order-review', 900))
          db.commit()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", ctx.home}],
        stderr_to_stdout: true
      )

    :ok = Standings.materialise()
    view = Standings.table(domain: "order-review")

    assert view.unscored_verdicts == [%{verdict: "incoherent", count: 1}]
    assert only_table(view).matches == 1
  end

  test "a verdict recorded after the view was computed makes it report as behind", ctx do
    seed(ctx, """
    plan = [('pair-wheel-v2', 'ALPHA', 'BETA', 1, 1, 'a-wins')]
    """)

    :ok = Standings.materialise()
    assert Standings.table(domain: "order-review").behind_by == 0

    seed_extra_verdict(ctx)

    assert Standings.table(domain: "order-review").behind_by > 0,
           "an unrefreshed table must say it is behind rather than look current"
  end

  defp seed_extra_verdict(ctx) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{ctx.home}'
          os.environ['PROMPT_BACKEND'] = 'local'
          sys.path.insert(0, '#{ctx.repo_root}')
          sys.path.insert(0, '#{ctx.repo_root}/bin')
          import judgement, sqlite3
          db = sqlite3.connect(os.path.join('#{ctx.home}', 'judgements.db'))
          db.row_factory = sqlite3.Row
          dom = db.execute("SELECT id FROM domain WHERE name='order-review'").fetchone()['id']
          cfg = db.execute(
              "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id=c.template_id "
              "WHERE c.rater_type='human' AND c.status='active' AND t.name='pair-wheel-v2'"
          ).fetchone()['id']
          payload = json.dumps({'label': 'R9-1', 'round': 9,
                                'card_a': {'title': 'NEWA', 'body': 'body NEWA'},
                                'card_b': {'title': 'NEWB', 'body': 'body NEWB'}})
          pid = db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id)"
              " VALUES (?,?,?,?,?)", (cfg, 'domain:order-review', 777, payload, dom)).lastrowid
          db.commit(); db.close()
          judgement.write_judgement(pending_id=pid, verdict='a-wins', confidence='high',
                                    rationale='later', rater={'type': 'human', 'userId': 'r'})
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", ctx.home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "extra verdict failed: #{out}"
  end

  test "an unjudged domain materialises an empty scope rather than an error", ctx do
    seed(ctx)
    :ok = Standings.materialise()

    view = Standings.table(domain: "nothing-here")

    refute view.materialised?
    assert view.tables == []
  end

  test "only human verdicts order the default scope", ctx do
    seed(ctx)
    :ok = Standings.materialise()

    human = Standings.table(domain: "order-review")
    every = Standings.table(domain: "order-review", rater_type: nil)

    assert human.scope.rater_type == "human"
    assert every.scope.rater_type == nil
    assert Standings.only_a_persons_verdict_orders_the_queue_by_default() =~ "model"
  end

  test "a failed refresh is reported, and the last good table keeps rendering", ctx do
    seed(ctx)
    :ok = Standings.materialise()
    good = Standings.table(domain: "order-review")

    System.put_env("STANDINGS_VIEW_CMD", "false")
    on_exit(fn -> System.delete_env("STANDINGS_VIEW_CMD") end)

    assert {:error, message} = Standings.materialise()
    assert message =~ "exited"
    assert Standings.table(domain: "order-review") == good
  end
end
