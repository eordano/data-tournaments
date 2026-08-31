defmodule TournamentUiWeb.JudgeLiveDomainFilterTest do
  @moduledoc """
  The review queue is domain-scoped: the sidebar carries a domain
  dropdown; picking one patches to /judge?domain=<name> and the pending
  list/counts never mix rows from other domains.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-judge-domfilter-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

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
          alpha = d.create_domain(name='alpha-domain', description='A', corpus_source={'kind':'inline','items':[]})
          beta = d.create_domain(name='beta-domain', description='B', corpus_source={'kind':'inline','items':[]})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          def card(t):
              return {'title': t, 'body': 'body of ' + t, 'source_ref': 'x.md'}
          for dom_id, marker in ((alpha, 'ALPHA-ONLY-CARD'), (beta, 'BETA-ONLY-CARD')):
              payload = json.dumps({'label': 'R1-1', 'card_a': card(marker + ' a'), 'card_b': card(marker + ' b')})
              db.execute(
                  "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)",
                  (cfg_id, 'domain:' + str(dom_id), 0, payload, dom_id))
          db.commit()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
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

  test "unfiltered queue shows both domains and the dropdown lists them", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge")

    # Dropdown present, listing both domains with their pending counts.
    assert html =~ "judge-domain-filter"
    assert html =~ "alpha-domain (1 pending)"
    assert html =~ "beta-domain (1 pending)"
    # Unscoped queue counts BOTH rows (only the active row's card body
    # renders in the main pane, so we assert on counts + sidebar rows).
    assert html =~ "2 pending"
    assert html =~ "ALPHA-ONLY-CARD" or html =~ "BETA-ONLY-CARD"
  end

  test "?domain= scopes the queue — other domain's rows never render", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=alpha-domain")

    assert html =~ "ALPHA-ONLY-CARD"
    refute html =~ "BETA-ONLY-CARD"
    # Scoped count: 1 pending in alpha-domain.
    assert html =~ "1 pending"

    {:ok, _view, html_b} = live(conn, "/judge?domain=beta-domain")
    assert html_b =~ "BETA-ONLY-CARD"
    refute html_b =~ "ALPHA-ONLY-CARD"
  end

  test "picking a domain in the dropdown patches the URL and scopes the list", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge")

    html =
      view
      |> element("form[phx-change=filter_domain]")
      |> render_change(%{"domain" => "beta-domain"})

    assert_patch(view, "/judge?domain=beta-domain")
    assert html =~ "BETA-ONLY-CARD"
    refute html =~ "ALPHA-ONLY-CARD"
    assert html =~ "1 pending"

    # Clearing back to all domains: both rows return to the queue (the
    # main pane shows one active card at a time, so assert the count).
    html_all =
      view
      |> element("form[phx-change=filter_domain]")
      |> render_change(%{"domain" => ""})

    assert_patch(view, "/judge")
    assert html_all =~ "2 pending"
    assert html_all =~ "alpha-domain (1 pending)"
  end
end
