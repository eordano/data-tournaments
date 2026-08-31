defmodule TournamentUiWeb.CandidateLiveTest do
  @moduledoc """
  /candidates/:id/:side — standalone shareable candidate permalinks
  (user request: candidates need their own URL to inspect and share).

  Contract under test:
  * side a renders ONLY candidate A; side b ONLY candidate B
  * work-order markdown renders as a structured document; legacy cards
    render as plain text with a Legacy card badge
  * the permalink KEEPS WORKING after the pair is judged (status done)
  * unknown id / invalid side -> friendly not-found, never a crash
  * /judge candidate cards carry Open links pointing at the right URLs
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-candidate-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

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
          d.create_domain(name='cand-dom', description='x', corpus_source={'kind':'inline','items':[],'artifact':'work-order'})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          payload = json.dumps({'label': 'R1-1',
            'card_a': {'kind': 'work-order', 'title': 'CANDIDATE-A-TITLE unique',
                       'body': '## Goal\\n\\nA-BODY-MARKER fix retries.\\n\\n## Plan\\n\\n1. one\\n2. two',
                       'source_ref': 'a.py',
                       'work_order': {'priority': 'P1', 'work_type': 'bug-fix', 'title': 'CANDIDATE-A-TITLE unique',
                                      'links': [{'label': 'Repository', 'url': 'https://github.com/org/repo', 'kind': 'repository'},
                                                {'label': 'Evil', 'url': 'javascript:alert(1)', 'kind': 'docs'}]}},
            'card_b': {'title': 'CANDIDATE-B-TITLE unique', 'body': 'B-BODY-MARKER plain legacy text', 'source_ref': 'b.md'}})
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, 1)",
                     (cfg_id, 'domain:1', 0, payload))
          db.commit()
          print(db.execute('SELECT id FROM pending_judgement').fetchone()[0])
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    pending_id = out |> String.trim() |> String.to_integer()

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    {:ok, home: home, pending_id: pending_id}
  end

  test "side a renders only candidate A as a work-order document", %{
    conn: conn,
    pending_id: id
  } do
    {:ok, _view, html} = live(conn, "/candidates/#{id}/a")

    assert html =~ "CANDIDATE-A-TITLE"
    refute html =~ "CANDIDATE-B-TITLE"
    refute html =~ "B-BODY-MARKER"
    # Structured markdown + badges + safe links only.
    assert html =~ "<h2>"
    assert html =~ "A-BODY-MARKER"
    assert html =~ "Work order"
    assert html =~ "P1"
    assert html =~ "https://github.com/org/repo"
    refute html =~ "javascript:alert"
    # Provenance footer names the pair and permalink.
    assert html =~ "R1-1"
    assert html =~ "/candidates/#{id}/a"
  end

  test "side b renders only candidate B as a legacy card", %{conn: conn, pending_id: id} do
    {:ok, _view, html} = live(conn, "/candidates/#{id}/b")

    assert html =~ "CANDIDATE-B-TITLE"
    assert html =~ "B-BODY-MARKER"
    refute html =~ "CANDIDATE-A-TITLE"
    assert html =~ "Legacy card"
  end

  test "permalink keeps working after the pair is judged", %{
    conn: conn,
    home: home,
    pending_id: id
  } do
    {_, 0} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import sqlite3
          db = sqlite3.connect('#{home}/judgements.db')
          db.execute("UPDATE pending_judgement SET status='done' WHERE id=#{id}")
          db.commit()
          """
        ],
        stderr_to_stdout: true
      )

    {:ok, _view, html} = live(conn, "/candidates/#{id}/a")

    assert html =~ "CANDIDATE-A-TITLE"
    # The page says the pair is already judged, without dying.
    assert html =~ "pair done"
  end

  test "unknown id and invalid side give friendly not-found", %{conn: conn, pending_id: id} do
    {:ok, _view, html} = live(conn, "/candidates/999999/a")
    assert html =~ "No such candidate"

    {:ok, _view, html} = live(conn, "/candidates/#{id}/z")
    assert html =~ "No such candidate"

    {:ok, _view, html} = live(conn, "/candidates/not-a-number/a")
    assert html =~ "No such candidate"
  end

  test "/judge cards link to the correct candidate URLs", %{conn: conn, pending_id: id} do
    {:ok, view, html} = live(conn, "/judge?domain=cand-dom")

    assert has_element?(view, ~s(#open-left[href="/candidates/#{id}/a"]))
    assert has_element?(view, ~s(#open-right[href="/candidates/#{id}/b"]))
    assert html =~ "Open ↗"
  end
end
