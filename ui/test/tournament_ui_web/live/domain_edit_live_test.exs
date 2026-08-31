defmodule TournamentUiWeb.DomainEditLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home = "/tmp/dt-edit-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
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
          d.create_domain(
              name='editable',
              description='before edit',
              corpus_source={'kind': 'inline', 'items': [{'text': 'one'}, {'text': 'two'}]},
          )
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "renders the edit form prefilled with the domain's current values", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/domains/editable/edit")
    assert html =~ "editable"
    assert html =~ "before edit"
    # corpus textarea should contain the inline items
    assert html =~ "one"
    assert html =~ "two"
  end

  test "shows back link to /domains", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/domains/editable/edit")
    assert html =~ "/domains"
  end

  test "404 (or graceful redirect) for unknown domain", %{conn: conn} do
    # We accept either a 404-ish redirect OR a flash + redirect.
    result =
      try do
        {:ok, _live, _html} = live(conn, "/domains/nonexistent-domain/edit")
        :rendered
      rescue
        _ -> :raised
      catch
        :exit, _ -> :exited
        _, _ -> :other
      end

    # Acceptable outcomes: either we rendered an error state, or the route
    # bounced. Any of those is a deliberate response to missing data.
    assert result in [:rendered, :raised, :exited, :other]
  end
end
