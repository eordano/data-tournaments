defmodule TournamentUiWeb.DomainsLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home = "/tmp/dt-domains-live-#{System.unique_integer([:positive])}"
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
          import sys
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
          d.create_domain(name='memory-extraction', description='Extract memories',
                          corpus_source={'kind': 'inline', 'items': []})
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "renders existing domains", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/domains")
    assert html =~ "memory-extraction"
    assert html =~ "Extract memories"
  end

  test "shows fan-out action for each active domain", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/domains")
    assert html =~ "Generate pairs"
  end

  test "offers domain-scoped judge learning", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/domains")

    assert has_element?(
             live,
             "button[phx-click='optimize_judge'][phx-value-name='memory-extraction']",
             "Improve rubric"
           )
  end

  test "links to the category chooser for new-domain creation", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/domains")
    assert html =~ "/start"
  end
end
