defmodule TournamentUiWeb.JudgeLiveCardShapeTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home = "/tmp/dt-judge-cards-#{System.unique_integer([:positive])}"
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
          sys.path.insert(0, '#{repo_root}')
          sys.path.insert(0, '#{repo_root}/bin')
          import bin.prompts as p
          class _S:
              class api:
                  class prompts:
                      @staticmethod
                      def list(**kw):
                          M = type('M', (), {'total_pages': 1})
                          return type('R', (), {'data': [], 'meta': M})()
                  class prompt_version:
                      @staticmethod
                      def update(**kw): return None
              def get_prompt(self, name, label='production', version=None):
                  raise LookupError(name)
              def create_prompt(self, **kw):
                  return type('P', (), {'version': 1, 'prompt': kw['prompt'], 'name': kw['name'], 'labels': kw.get('labels') or []})()
          p._client_factory = lambda: _S()
          import judgement; judgement.init_db()
          import bin.domains as d
          d.create_domain(name='memory', description='Memory extraction', corpus_source={'kind':'inline','items':[]})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': {'title': 'Nix over pip', 'body': 'User prefers Nix over pip on aarch64-darwin.', 'source_ref': 'inline:0'},
            'card_b': {'title': 'TCC cache workaround', 'body': 'Use /tmp for HEX_HOME and MIX_HOME when macOS TCC blocks cache access.', 'source_ref': 'inline:1'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 0, payload, 1))
          legacy = json.dumps({'label': 'legacy', 'input_a': '/tmp/a.md', 'input_b': '/tmp/b.md', 'synthesis': 'legacy synthesis'})
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, '/tmp/legacy.db', 1, legacy, None))
          malformed = json.dumps({'label': 'malformed legacy', 'input_a': 42, 'input_b': None})
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, '/tmp/malformed.db', 2, malformed, None))
          db.commit()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "renders new generated card_a/card_b title, body, and source refs", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")

    assert html =~ "Nix over pip"
    assert html =~ "User prefers Nix over pip on aarch64-darwin."
    assert html =~ "inline:0"

    assert html =~ "TCC cache workaround"
    assert html =~ "Use /tmp for HEX_HOME and MIX_HOME"
    assert html =~ "inline:1"
  end

  test "legacy input_a/input_b rows still render when selected", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    html =
      live
      |> element("button[phx-value-id='2']")
      |> render_click()

    assert html =~ "a.md"
    assert html =~ "/tmp/a.md"
    assert html =~ "b.md"
    assert html =~ "/tmp/b.md"
    assert html =~ "legacy synthesis"
  end

  test "malformed legacy input values do not crash rendering", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    html =
      live
      |> element("button[phx-value-id='3']")
      |> render_click()

    assert html =~ "malformed legacy"
    assert html =~ "— bye / no input —"
  end

  test "submitting a verdict on a generated card pair records and advances", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")
    assert html =~ "Nix over pip"

    live |> element("button[phx-value-v='a-clearly-better']") |> render_click()
    html = live |> element("form#judge-form") |> render_submit(%{})

    refute html =~ "Nix over pip"
    assert html =~ "legacy"
  end
end
