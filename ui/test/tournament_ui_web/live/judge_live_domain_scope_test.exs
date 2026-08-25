defmodule TournamentUiWeb.JudgeLiveDomainScopeTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # Asymmetric seed so scoped/global mix-ups cannot pass by accident:
  #   alpha-review → 1 pending, beta-review → 2 pending, empty-review → 0.
  # Global pending count is therefore 3 and never equals any scoped count.
  setup do
    previous_backend = Application.get_env(:tournament_ui, :prompt_backend)
    Application.put_env(:tournament_ui, :prompt_backend, "local")

    on_exit(fn ->
      if previous_backend,
        do: Application.put_env(:tournament_ui, :prompt_backend, previous_backend),
        else: Application.delete_env(:tournament_ui, :prompt_backend)
    end)

    home = "/tmp/dt-judge-domain-scope-#{System.unique_integer([:positive])}"
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
          for name in ('alpha-review', 'beta-review', 'empty-review'):
              domains.create_domain(
                  name=name,
                  description=f'{name} description',
                  corpus_source={'kind': 'inline', 'items': []},
              )

          db = sqlite3.connect(os.path.join('#{home}', 'judgements.db'))
          db.row_factory = sqlite3.Row
          cfg_id = db.execute(
              "SELECT id FROM job_configuration WHERE rater_type='human' AND status='active'"
          ).fetchone()['id']

          def domain_id(name):
              return db.execute('SELECT id FROM domain WHERE name=?', (name,)).fetchone()['id']

          def payload(label):
              return json.dumps({
                  'label': label,
                  'card_a': {'title': f'{label} card A', 'body': 'body a'},
                  'card_b': {'title': f'{label} card B', 'body': 'body b'},
              })

          rows = [
              ('alpha-review', 1, 'Alpha pair'),
              ('beta-review', 2, 'Beta pair one'),
              ('beta-review', 3, 'Beta pair two'),
          ]
          for name, match_id, label in rows:
              db.execute(
                  "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id) VALUES (?,?,?,?,?)",
                  (cfg_id, f'domain:{name}', match_id, payload(label), domain_id(name)),
              )
          db.commit()
          db.close()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "filtered review shows only that domain's queue and keeps the Results link scoped",
       %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge?domain=alpha-review")

    assert html =~ "1 pending"
    assert html =~ "in alpha-review"
    assert html =~ "Alpha pair"
    refute html =~ "Beta pair one"
    refute html =~ "Beta pair two"

    assert has_element?(live, ~s(a[href="/results?domain=alpha-review"]), "compare results →")
    refute has_element?(live, ~s(a[href="/results"]), "compare results →")
  end

  test "scoped pending count reflects the active domain, not the global queue", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge?domain=beta-review")

    assert html =~ "2 pending"
    assert html =~ "in beta-review"
    refute html =~ "3 pending"
    refute html =~ "Alpha pair"
  end

  test "empty filtered domain shows the empty state without dropping the scope", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge?domain=empty-review")

    assert html =~ "0 pending"
    assert html =~ "in empty-review"
    assert html =~ "Inbox zero"
    assert html =~ "No pending reviews"
    refute html =~ "Alpha pair"

    # Both the sidebar footer and the empty-state body keep the domain.
    assert has_element?(live, ~s(a[href="/results?domain=empty-review"]), "compare results →")
    assert has_element?(live, ~s(a[href="/results?domain=empty-review"]), "results")
  end

  test "global review stays unfiltered: full queue and unscoped Results link", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")

    assert html =~ "3 pending"
    refute html =~ "in alpha-review"
    assert html =~ "Alpha pair"
    assert html =~ "Beta pair one"
    assert html =~ "Beta pair two"

    assert has_element?(live, ~s(a[href="/results"]), "compare results →")
    refute has_element?(live, ~s(a[href*="/results?domain="]))
  end
end
