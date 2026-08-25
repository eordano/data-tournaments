defmodule TournamentUiWeb.JudgeLiveTitleTest do
  @moduledoc """
  Judge pane information hierarchy (user: 'what is someone expected to
  learn from that title?'):

  * H1 = the two candidate titles ('A vs B') — what the pair is ABOUT.
  * Pair label (R1-1), domain, rubric/version demoted to the metadata line.
  * Artifact badge says 'Work orders' or 'Legacy cards' explicitly.
  * Each candidate has a 'Read full' control expanding the full document
    (inspection is separate from voting — expanding never submits).
  * Sidebar rows lead with the candidate title, not the pair label.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    home = "/tmp/dt-judge-title-#{System.unique_integer([:positive])}"
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
          d.create_domain(name='wo-dom', description='work orders', corpus_source={'kind':'inline','items':[],'artifact':'work-order'})
          d.create_domain(name='card-dom', description='legacy cards', corpus_source={'kind':'inline','items':[]})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          def wo(title):
              return {
                'kind': 'work-order', 'title': title,
                'body': '## Goal\\n\\nFix the thing.\\n\\n## Implementation plan\\n\\n1. step one\\n2. step two\\n\\n## Acceptance criteria\\n\\n- criterion',
                'source_ref': 'x.py',
                'work_order': {'priority': 'P1', 'work_type': 'bug-fix', 'title': title},
              }
          wo_payload = json.dumps({'label': 'R1-1',
            'card_a': wo('Retry logic is dead code'),
            'card_b': wo('Missing HTTP timeouts everywhere')})
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, 1)",
                     (cfg_id, 'domain:1', 0, wo_payload))
          card_payload = json.dumps({'label': 'R2-9',
            'card_a': {'title': 'Plain legacy card', 'body': 'card body', 'source_ref': 'y.md'},
            'card_b': {'title': 'Another legacy card', 'body': 'card body 2', 'source_ref': 'z.md'}})
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, 2)",
                     (cfg_id, 'domain:2', 1, card_payload))
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

  test "h1 is the candidate titles; R1-1 is demoted to metadata", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=wo-dom")

    assert html =~ "Retry logic is dead code  vs  Missing HTTP timeouts everywhere"
    # The pair label survives, but only as metadata prose.
    assert html =~ "Pair R1-1"
  end

  test "artifact badge distinguishes work orders from legacy cards", %{conn: conn} do
    {:ok, _view, wo_html} = live(conn, "/judge?domain=wo-dom")
    assert wo_html =~ "Work orders"

    {:ok, _view, card_html} = live(conn, "/judge?domain=card-dom")
    assert card_html =~ "Legacy cards"
  end

  test "Read full expands one candidate to a full-width document and back", %{conn: conn} do
    {:ok, view, html} = live(conn, "/judge?domain=wo-dom")

    assert has_element?(view, "#expand-left")
    assert has_element?(view, "#expand-right")
    refute html =~ "full document"

    html = view |> element("#expand-left") |> render_click()
    assert html =~ "full document"
    assert html =~ "back to side-by-side"
    assert html =~ "Implementation plan"
    # Inspection is not a vote: the verdict form is still unsubmitted.
    refute html =~ "Recorded"

    html = view |> element("#candidate-full button[phx-click=expand_candidate]") |> render_click()
    refute html =~ "full document"
  end

  test "sidebar rows lead with the candidate title, label demoted", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge")

    # Both rows' headlines are titles, with labels in the secondary line.
    assert html =~ "Retry logic is dead code"
    assert html =~ "Plain legacy card"
    assert html =~ "R2-9"
  end

  test "work-order markdown renders as structured document", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=wo-dom")

    # SafeMarkdown output: real headings/lists, not pre-wrap text.
    assert html =~ "<h2>"
    assert html =~ "Acceptance criteria"
    assert html =~ "<ol>"
  end
end
