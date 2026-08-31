defmodule TournamentUiWeb.JudgeLiveWorkorderTest do
  @moduledoc """
  WorkOrder payloads (kind=work-order) render as markdown documents with
  "Work order A/B" labels and a work-type badge — not as plain pre-wrap
  card bodies, and NOT with a priority badge.

  The card bodies are NOT hand-written fixtures: the seed imports
  `bin/workorder.py` and renders them with the real `to_markdown`, from a
  WorkOrder whose `priority` field IS populated (P0 + rationale). The
  payload keeps that key too, so the tests prove both layers: the composer
  never writes an absolute score into the prose, and the judge screen
  scrubs the key even when the payload carries it.

  The priority assertion is a refutation on purpose. `WorkOrder.priority`
  is the model's self-assessed absolute score, produced by a model that
  saw one item and could not see the other thirty-two;
  `docs/design/priority-tournament.md` calls it the weakest field on the
  artifact. Rendered on the judging card in the loudest colour on the page
  (P0 as bg-error/20 text-error) it anchors the comparison exactly as hard
  as points would, which is the one thing the judging screen may never do.
  This file previously asserted the badge stayed.

  `work_type` is asserted to STAY: it is a routing fact — only the
  mechanically-authorable types reach `branch_author` — not a rank.
  """
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  @meta_line "**Domain:** release-mgmt · **Created:** 2026-08-28 · **Type:** bug-fix  "

  setup do
    home = "/tmp/dt-judge-wo-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
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
          from bin.workorder import WorkOrderDraft, WorkOrderLink, finalize_work_order, to_markdown
          def wo(title, rationale):
              draft = WorkOrderDraft(
                  title=title,
                  goal='Harden the release pipeline.\\n\\n<script>alert("xss")</script>\\n\\n<img src=x onerror=alert(1)>',
                  plan='1. Fix it\\n2. Test it',
                  work_type='bug-fix',
                  priority='P0',
                  priority_rationale=rationale,
                  files=['scripts/build.py'],
              )
              w = finalize_work_order(
                  draft,
                  domain='release-mgmt',
                  created_at='2026-08-28',
                  models=[],
                  repos=[],
                  source_ref='scripts/build.py',
                  extra_links=[
                      WorkOrderLink(label='Repository', url='https://github.com/decentraland/unity-explorer', kind='repository'),
                      WorkOrderLink(label='Base commit 8be52b3847f7', url='https://github.com/decentraland/unity-explorer/commit/8be52b3847f7', kind='commit'),
                  ],
              )
              assert w.priority == 'P0' and w.priority_rationale
              wod = w.model_dump()
              wod['links'] = wod['links'] + [
                  {'label': 'Evil chip', 'url': 'javascript:alert(1)', 'kind': 'repository'},
                  {'label': 'Plain http chip', 'url': 'http://insecure.example', 'kind': 'docs'},
              ]
              return {
                  'kind': 'work-order',
                  'title': title,
                  'body': to_markdown(w),
                  'source_ref': 'scripts/build.py',
                  'work_order': wod,
              }
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': wo('Retry logic is dead code', 'release-blocking: every retry path is unreachable'),
            'card_b': wo('Missing HTTP timeouts', 'release-blocking: deploys hang forever on a dead mirror'),
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

  test "the self-assessed priority never reaches the judging card", %{conn: conn} do
    {:ok, view, html} = live(conn, "/judge")

    for card <- ["#judge-card-left", "#judge-card-right"] do
      rendered = view |> element(card) |> render()

      refute rendered =~ "P0",
             "the judge must never see P0: a self-assessed absolute score beside " <>
               "the item it is being compared against anchors the comparison"

      refute rendered =~ "Priority",
             "no Priority field may reach the judging card in any form, " <>
               "badge or prose"

      refute rendered =~ "release-blocking",
             "the priority rationale restates the score in words and must " <>
               "stay off the judging card with it"

      assert rendered =~ "bug-fix",
             "work_type is a routing fact and stays — the refutations above must " <>
               "not be passing because the whole badge row vanished"

      assert rendered =~ "Domain:" and rendered =~ "release-mgmt",
             "the meta header must still ship: a card with no meta line would " <>
               "make the priority refutations vacuous"

      assert rendered =~ "Created:" and rendered =~ "2026-08-28"
      assert rendered =~ "Type:"
    end

    refute html =~ "bg-error/20 text-error",
           "priority_class/1 paints P0 in the loudest colour on the page; " <>
             "it must not be reachable from the comparison surface at all"

    refute html =~ "bg-warning/20 text-warning",
           "priority_class/1 paints P1 as a warning badge on the judging card"

    payload = :sys.get_state(view.pid).socket.assigns.active.trace_payload

    for side <- ["card_a", "card_b"] do
      refute Map.has_key?(payload[side]["work_order"], "priority"),
             "#{side} still carries priority in the judge assigns — " <>
               "unrendered-but-present is a leak waiting for the next template edit"

      assert payload[side]["work_order"]["work_type"] == "bug-fix"
    end
  end

  test "fixture body mirrors bin/workorder.py to_markdown and must be regenerated with it", %{
    conn: conn
  } do
    {:ok, view, _html} = live(conn, "/judge")

    payload = :sys.get_state(view.pid).socket.assigns.active.trace_payload

    for side <- ["card_a", "card_b"] do
      [meta_line | _] = String.split(payload[side]["body"], "\n")

      assert meta_line == @meta_line,
             "#{side}'s meta line is no longer what to_markdown emits — " <>
               "update @meta_line and re-check every assertion in this file " <>
               "against the new shape"

      refute payload[side]["body"] =~ "Priority",
             "to_markdown wrote an absolute score into the judge-facing " <>
               "prose again; no Elixir-side scrub can reach it there"
    end
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
