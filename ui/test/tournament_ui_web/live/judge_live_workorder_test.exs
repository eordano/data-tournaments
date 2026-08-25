defmodule TournamentUiWeb.JudgeLiveWorkorderTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # WorkOrder payloads (kind=work-order) must render as markdown documents
  # with "Work order A/B" labels and priority/type badges — not as plain
  # pre-wrap card bodies.

  setup do
    home = "/tmp/dt-judge-wo-#{System.unique_integer([:positive])}"
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
          d.create_domain(name='release-mgmt', description='Release work orders', corpus_source={'kind':'inline','items':[],'artifact':'work-order'})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          def wo(title, prio):
              return {
                'kind': 'work-order',
                'title': title,
                'body': '**Domain:** release-mgmt · **Priority:** ' + prio + '\\n\\n## Goal\\n\\nHarden the release pipeline.\\n\\n<script>alert("xss")</script>\\n\\n<img src=x onerror=alert(1)>\\n\\n## Implementation plan\\n\\n1. Fix it\\n2. Test it',
                'source_ref': 'scripts/build.py',
                'work_order': {
                  'priority': prio, 'work_type': 'bug-fix', 'title': title,
                  'links': [
                    {'label': 'Repository', 'url': 'https://github.com/decentraland/unity-explorer', 'kind': 'repository'},
                    {'label': 'Base commit 8be52b3847f7', 'url': 'https://github.com/decentraland/unity-explorer/commit/8be52b3847f7', 'kind': 'commit'},
                    {'label': 'Evil chip', 'url': 'javascript:alert(1)', 'kind': 'repository'},
                    {'label': 'Plain http chip', 'url': 'http://insecure.example', 'kind': 'docs'},
                  ],
                },
              }
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': wo('Retry logic is dead code', 'P1'),
            'card_b': wo('Missing HTTP timeouts', 'P2'),
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 0, payload, 1))
          db.commit()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf(home) end)
    :ok
  end

  test "work-order pair renders markdown, badges, and Work order labels", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge")

    # Labels reflect the artifact kind.
    assert html =~ "Work order A"
    assert html =~ "Work order B"
    refute html =~ "Card A"

    # Priority + work-type badges.
    assert html =~ "P1"
    assert html =~ "P2"
    assert html =~ "bug-fix"

    # Body is rendered as markdown (headings become h2), not pre-wrap text.
    assert html =~ "<h2>"
    assert html =~ "Implementation plan"

    # Links render as real anchors (clickable chips), opening in a new tab.
    assert html =~ ~s(href="https://github.com/decentraland/unity-explorer")
    assert html =~ ~s(href="https://github.com/decentraland/unity-explorer/commit/8be52b3847f7")
    assert html =~ ~s(target="_blank")
    assert html =~ ~s(rel="noopener noreferrer")
    assert html =~ "Base commit 8be52b3847f7"
  end

  test "untrusted markdown body is sanitized but legit markdown still renders", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge")

    # The seeded body contains <script>alert("xss")</script> and an
    # <img onerror> payload: neither may survive as executable markup.
    # (The page layout has its own legitimate <script> tags for app.js and
    # the theme bootstrap, so we assert on the payload, not on <script>.)
    refute html =~ "<script>alert"
    refute html =~ "alert(&quot;xss&quot;)"
    refute html =~ "<img"
    refute html =~ "onerror"

    # The legitimate markdown surrounding the payload still renders.
    assert html =~ "<h2>"
    assert html =~ "Harden the release pipeline."
    assert html =~ "Implementation plan"
    assert html =~ "Test it"

    # Link chips: non-https URLs are dropped at the Elixir layer too.
    refute html =~ ~s(href="javascript:)
    refute html =~ ~s(href="http://insecure.example")
    refute html =~ "Evil chip"
    refute html =~ "Plain http chip"
    assert html =~ ~s(href="https://github.com/decentraland/unity-explorer")
  end
end
