defmodule TournamentUiWeb.JudgeLiveShortcutFocusTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    home =
      "/tmp/dt-judge-shortcuts-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

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
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': {'title': 'first card', 'body': 'body1'},
            'card_b': {'title': 'second card', 'body': 'body2'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?, ?, ?, ?)", (cfg_id, '/tmp/r.db', 0, payload))
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

  test "keyboard shortcut hook is mounted on judge shell", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")
    assert html =~ ~s(phx-hook="JudgeShortcuts")
    assert html =~ ~s(id="judge-shell")
  end
end
