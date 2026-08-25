defmodule TournamentUiWeb.JudgeLiveCitationsTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # WorkOrder payloads don't carry evidence digests yet (wave-3 wiring);
  # until then the judge pane resolves the payload key `cited_evidence`
  # (list of EvidenceRef digests) through TournamentUi.Catalog and renders
  # a small "Cited evidence" section: tier badge (tier 3 visibly marked
  # UNTRUSTED), source kind, first excerpt line, https-only source link.

  setup do
    home = "/tmp/dt-judge-cite-#{System.unique_integer([:positive])}"
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
          import bin.catalog as cat
          cat.create_project(name='unity-explorer')
          src1 = cat.create_source(project='unity-explorer', name='repo', kind='git', locator='https://github.com/decentraland/unity-explorer', trust_tier=1)
          src3 = cat.create_source(project='unity-explorer', name='forum', kind='docs', locator='https://forum.example', trust_tier=3)
          from bin.landscape.evidence import EvidenceRef, TrustTier, SourceType, BrowsableLink
          ev1 = EvidenceRef(source_type=SourceType.GIT_REPO, canonical_uri='repo://unity-explorer@8be52b3', trust_tier=TrustTier.TIER1_SYSTEM, excerpt='Retry helper unused since 2024\\nsecond line must not render', browsable_link=BrowsableLink(label='commit', url='https://github.com/decentraland/unity-explorer/commit/8be52b3', kind='commit'), why_selected='dead code evidence')
          ev3 = EvidenceRef(source_type=SourceType.DOC, canonical_uri='doc://forum/42', trust_tier=TrustTier.TIER3_EXTERNAL, excerpt='Anonymous forum post claims timeouts are fine', why_selected='external claim')
          d1 = cat.insert_evidence_ref(ev1, source_id=src1)
          d3 = cat.insert_evidence_ref(ev3, source_id=src3)
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          def wo(title):
              return {
                'kind': 'work-order',
                'title': title,
                'body': '## Goal\\n\\nFix it.',
                'work_order': {'priority': 'P1', 'work_type': 'bug-fix', 'title': title, 'links': []},
              }
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': wo('Retry logic is dead code'),
            'card_b': wo('Missing HTTP timeouts'),
            'cited_evidence': [d1, d3, 'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'],
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 0, payload, 1))
          payload2 = json.dumps({
            'label': 'R1-2',
            'card_a': wo('No citations here'),
            'card_b': wo('None here either'),
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 1, payload2, 1))
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

  test "cited evidence renders tier badges, kind, excerpt line, and safe link", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge")

    # Select the citing row explicitly (queue order among same-second rows
    # is not guaranteed).
    html =
      view
      |> element("button[phx-value-id]", "R1-1")
      |> render_click()

    assert html =~ "Cited evidence"

    # Tier badges: tier-1 plain, tier-3 visibly untrusted.
    assert html =~ "TIER1"
    assert html =~ "TIER3 · UNTRUSTED"

    # Source kinds from the evidence rows.
    assert html =~ "git_repo"
    assert html =~ "doc"

    # First excerpt line only.
    assert html =~ "Retry helper unused since 2024"
    refute html =~ "second line must not render"
    assert html =~ "Anonymous forum post claims timeouts are fine"

    # Browsable link is https and passes SafeMarkdown.safe_link?/1; the
    # tier-3 ref has no browsable link, so exactly one citation link chip.
    assert html =~
             ~s(href="https://github.com/decentraland/unity-explorer/commit/8be52b3")
  end

  test "row without citations renders no section", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge")

    # Advance to the second pending row (no cited_evidence key).
    html =
      view
      |> element("button[phx-value-id]", "R1-2")
      |> render_click()

    refute html =~ "Cited evidence"
  end
end
