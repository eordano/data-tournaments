defmodule TournamentUiWeb.JudgeLiveRoundTest do
  @moduledoc """
  The review queue serves ONE Swiss round at a time.

  `docs/design/priority-tournament.md` — later rounds pair on standing, so
  round N+1's pairings are computed from the table round N produced.
  Serving a round-3 pair while round 2 is still open corrupts the input to
  the next pairing, and a global FIFO gives the operator no way to see how
  far the round has left to go.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  alias TournamentUi.Judgement

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-judge-round-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

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
          for name in ('swiss-pool', 'closed-pool'):
              domains.create_domain(
                  name=name,
                  description=f'{name} description',
                  corpus_source={'kind': 'inline', 'items': []},
              )

          db = sqlite3.connect(os.path.join('#{home}', 'judgements.db'))
          db.row_factory = sqlite3.Row
          cfg_id = db.execute(
              "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.rater_type='human' AND c.status='active' AND t.name='pair-wheel-v2'"
          ).fetchone()['id']

          def domain_id(name):
              return db.execute('SELECT id FROM domain WHERE name=?', (name,)).fetchone()['id']

          def payload(marker, rnd, slot):
              return json.dumps({
                  'label': f'R{rnd}-{slot}',
                  'round': rnd,
                  'card_a': {'title': f'{marker}-{slot}-A', 'body': 'body a'},
                  'card_b': {'title': f'{marker}-{slot}-B', 'body': 'body b'},
              })

          def insert(pool, dom, match_id, body, status='pending'):
              db.execute(
                  "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id,status)"
                  " VALUES (?,?,?,?,?,?)",
                  (cfg_id, pool, match_id, body, dom, status))

          swiss = domain_id('swiss-pool')
          for slot in (1, 2, 3):
              insert('domain:swiss-pool', swiss, slot, payload('ROUNDONEMARK', 1, slot))
          for slot in (1, 2):
              insert('domain:swiss-pool', swiss, 10 + slot, payload('ROUNDTWOMARK', 2, slot))

          closed = domain_id('closed-pool')
          for slot in (1, 2):
              insert('domain:closed-pool', closed, 20 + slot,
                     payload('CLOSEDMARK', 1, slot), status='done')

          db.commit()
          db.close()
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

  defp round_one_ids do
    Judgement.open_round_queue(rater_type: "human", domain: "swiss-pool")
    |> Map.fetch!(:rows)
    |> Enum.map(& &1.id)
  end

  test "only round-1 pairings are offered while round 1 is open", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=swiss-pool")

    assert html =~ "ROUNDONEMARK-1-A"
    assert html =~ "ROUNDONEMARK-2-A"
    assert html =~ "ROUNDONEMARK-3-A"
    refute html =~ "ROUNDTWOMARK"
  end

  test "the round-2 pair cannot be selected or stepped onto from round 1", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge?domain=swiss-pool")

    [round_two_id] =
      Judgement.list_pending(rater_type: "human", domain: "swiss-pool", limit: 500)
      |> Enum.filter(&(Judgement.payload_round(&1.trace_payload) == 2))
      |> Enum.map(& &1.id)
      |> Enum.take(1)

    refute has_element?(view, "button[phx-value-id='#{round_two_id}']")

    for step <- 1..5 do
      html = render_hook(view, "keydown", %{"key" => "j"})

      refute html =~ "ROUNDTWOMARK",
             "step #{step} of the queue walk landed on a round-2 pair; the queue must wrap inside round 1"
    end
  end

  test "the queue bar states the round and how much of it is left", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=swiss-pool")

    assert html =~ "round 1, 3 of 3 remaining"

    [first | _] = round_one_ids()
    {:ok, _} = Judgement.submit_human(first, "a-wins-big", "mid")

    {:ok, _view, html} = live(conn, "/judge?domain=swiss-pool")
    assert html =~ "round 1, 2 of 3 remaining"
    refute html =~ "ROUNDTWOMARK"
  end

  test "round 2 opens only once every round-1 pending is resolved", %{conn: conn} do
    for id <- round_one_ids() do
      {:ok, _} = Judgement.submit_human(id, "a-wins-big", "mid")
    end

    {:ok, _view, html} = live(conn, "/judge?domain=swiss-pool")

    assert html =~ "ROUNDTWOMARK-1-A"
    assert html =~ "ROUNDTWOMARK-2-A"
    refute html =~ "ROUNDONEMARK"
    assert html =~ "round 2, 2 of 2 remaining"
  end

  test "a pool with no open round shows the empty state, not a later pair", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=closed-pool")

    assert html =~ "Inbox zero"
    assert html =~ "No pending reviews"
    refute html =~ "CLOSEDMARK"
    refute html =~ "round 1,"
  end

  test "the open round is per pool — one pool's round 2 does not gate another", %{conn: conn} do
    for id <- round_one_ids() do
      {:ok, _} = Judgement.submit_human(id, "a-wins-big", "mid")
    end

    {:ok, _view, html} = live(conn, "/judge")

    assert html =~ "ROUNDTWOMARK", "swiss-pool should have advanced to its round 2"

    refute html =~ "CLOSEDMARK",
           "closed-pool has no open round; another pool's progress must not reopen it"
  end
end
