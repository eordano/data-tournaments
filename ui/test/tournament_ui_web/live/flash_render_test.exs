defmodule TournamentUiWeb.FlashRenderTest do
  @moduledoc """
  The application must be able to speak — on its production routes.

  Every judging action ends in a `put_flash/3` — "Judgement recorded.",
  "Points table recomputed.", and the failure strings. Until the shared
  shells rendered a flash group, none of those reached a browser, so a
  judge could not tell a recorded verdict from a silently dropped one.

  Every test here drives a REAL route through the router. A fixture
  LiveView is banned in this file: an earlier revision mounted a
  route-less fixture that passed `flash={@flash}` itself, and it stayed
  green while all 13 shipped LiveViews were mute — the exact failure this
  file exists to lock out. A flash proven on a fixture proves the shell
  can speak; only a flash proven on a route proves the application does.

  Coverage is a put_flash round-trip on both shells (`workspace_split`
  via /judge, `workspace_page` via /standings and /domains) plus a sweep
  asserting the flash container on every mountable live route. Two routes
  are deliberately absent from the sweep: /prompts and /catalog
  push_navigate to /environment on mount and never render a shell (their
  redirects are asserted instead), and the /runs/:workflow_id path form
  duplicates /runs/show with colon-bearing ids the path router rejects by
  design.
  """
  use TournamentUiWeb.ConnCase, async: false

  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-flash-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

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
          d.create_domain(name='flash-dom', description='Flash coverage', corpus_source={'kind':'inline','items':[]})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          payload = json.dumps({
            'label': 'R1-1',
            'card_a': {'title': 'Left card', 'body': 'left body', 'source_ref': 'inline:0'},
            'card_b': {'title': 'Right card', 'body': 'right body', 'source_ref': 'inline:1'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 0, payload, 1))
          db.commit()
          import bin.catalog as cat
          cat.init()
          cat.create_project(name='flash-proj')
          import bin.campaigns as camp
          camp.create_campaign(project='flash-proj', name='flash-sweep', kind='bugsweep',
                               objective='flash coverage', base_commit='abc123def456')
          from bin import workflow_runs as wr
          wr.start(temporal_workflow_id='release:unity:flash', temporal_run_id='run-flash')
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

  test "submitting a verdict on /judge announces itself through workspace_split", %{conn: conn} do
    {:ok, view, html} = live(conn, "/judge")

    refute html =~ "Judgement recorded.",
           "nothing has been submitted yet, so the toast must be absent"

    view |> element("button[phx-value-v='a-wins-big']") |> render_click()
    html = view |> element("form#judge-form") |> render_submit(%{})

    assert html =~ "Judgement recorded.",
           "put_flash(:info, ...) in JudgeLive's submit path must reach the page " <>
             "the judge is looking at — workspace_split is the one route that must never be mute"
  end

  test "Recompute on /standings announces itself through workspace_page", %{conn: conn} do
    {:ok, view, html} = live(conn, "/standings")

    refute html =~ "Points table recomputed."

    html = view |> element("#standings-recompute") |> render_click()

    assert html =~ "Points table recomputed.",
           "put_flash(:info, ...) in StandingsLive's recompute path must reach the operator"
  end

  test "an error flash on /domains is not silent either", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/domains")

    html = render_click(view, "optimize_judge", %{"name" => "no-such-domain"})

    assert html =~ "Unknown domain no-such-domain."
    assert html =~ "alert-error"
  end

  test "the toast sits clear of the queue strip and the verdict controls", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge")

    view |> element("button[phx-value-v='a-wins-big']") |> render_click()
    html = view |> element("form#judge-form") |> render_submit(%{})

    assert html =~ "toast-bottom",
           "the top edge of the judging screen is the queue strip, the domain filter and " <>
             "the round counter; a toast anchored there covers the queue"

    assert html =~ "toast-start",
           "Skip and Submit are on the right edge; a toast anchored end-side covers them"

    refute html =~ "toast-top",
           "a judge submits every few seconds -- a toast landing on the wheel is worse than none"

    assert html =~ "pointer-events-none",
           "even off in the corner the fixed container must never swallow a click meant " <>
             "for the wheel underneath it"
  end

  test "the aria-live region is present so the announcement is not sighted-only", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge")

    assert html =~ ~s(aria-live="polite")
    assert html =~ ~s(id="workspace-flash")
  end

  test "/prompts and /catalog redirect to /environment, which the sweep covers", %{conn: conn} do
    assert {:error, {:live_redirect, %{to: "/environment?tab=prompts"}}} =
             live(conn, "/prompts")

    assert {:error, {:live_redirect, %{to: "/environment?tab=sources"}}} =
             live(conn, "/catalog")
  end

  @sweep [
    "/",
    "/start",
    "/judge",
    "/candidates/1/a",
    "/results",
    "/judgements",
    "/standings",
    "/environment",
    "/domains",
    "/domains/new",
    "/domains/flash-dom/edit",
    "/catalog/flash-proj",
    "/campaigns",
    "/campaigns/flash-sweep",
    "/designer",
    "/runs",
    "/runs/show?id=release:unity:flash",
    "/branch-fixes",
    "/branch-fixes/1",
    "/inspect"
  ]

  for route <- @sweep do
    @route route
    test "#{@route} renders the workspace flash container", %{conn: conn} do
      {:ok, _view, html} = live(conn, @route)

      assert html =~ ~s(id="workspace-flash"),
             "#{@route} mounted without the shell's flash group — every put_flash " <>
               "on this page would be invisible"
    end
  end
end
