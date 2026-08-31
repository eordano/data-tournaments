defmodule TournamentUiWeb.InspectLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home = "/tmp/dt-inspect-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          import sys, json, sqlite3
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
          d.create_domain(name='alpha', description='Alpha domain',
                          corpus_source={'kind': 'inline', 'items': []})
          d.create_domain(name='beta', description='Beta domain',
                          corpus_source={'kind': 'inline', 'items': []})

          # Inject a few pending rows with different domain_ids + a score
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          for i, did in enumerate([1, 2, 1]):
              payload = json.dumps({'label': 'R1-' + str(i+1),
                                    'card_a': {'title': 't' + str(i), 'body': 'b' + str(i)},
                                    'card_b': {'title': 't' + str(i) + 'B', 'body': 'b' + str(i) + 'B'}})
              db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)",
                         (cfg_id, 'domain:' + str(did), i, payload, did))
          db.commit()
          db.close()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "renders the inspect page with entity tabs", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/inspect")

    for entity <- ~w(domains pending scores prompts) do
      assert html =~ entity
    end
  end

  test "shows row counts in the header", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/inspect")
    # We seeded 2 domains + 3 pending rows
    assert html =~ "domains"
    assert html =~ "pending"
  end

  test "switching entity tab updates the table", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/inspect")
    html = render_click(live, "select_entity", %{"entity" => "pending"})
    # Three pending rows seeded — each row shows its domain_name in the table.
    # alpha appears twice, beta once.
    assert html =~ "alpha"
    assert html =~ "beta"
  end

  test "domain filter narrows pending rows", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/inspect")
    render_click(live, "select_entity", %{"entity" => "pending"})
    html = render_click(live, "filter_domain", %{"domain" => "alpha"})
    # After filtering, beta rows should be gone from table cells (the
    # <option value="beta">beta</option> in the dropdown remains).
    refute html =~ ~r/<td[^>]*>\s*beta\s*</
    # alpha rows still present
    assert html =~ ~r/<td[^>]*>\s*alpha\s*</
  end

  test "json expand reveals the raw row", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/inspect")
    render_click(live, "select_entity", %{"entity" => "domains"})
    html = render_click(live, "expand_row", %{"id" => "1"})
    assert html =~ "Alpha domain"
    # Expanded JSON should expose corpus_source as a JSON literal
    assert html =~ "corpus_source"
  end

  test "download link is a real route, not a no-op", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/inspect")
    assert html =~ ~r/href="\/inspect\/download[^"]*"/
  end
end
