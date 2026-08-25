defmodule TournamentUiWeb.JudgeLiveUxTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  setup do
    previous_backend = Application.get_env(:tournament_ui, :prompt_backend)
    Application.put_env(:tournament_ui, :prompt_backend, "langfuse")

    on_exit(fn ->
      if previous_backend,
        do: Application.put_env(:tournament_ui, :prompt_backend, previous_backend),
        else: Application.delete_env(:tournament_ui, :prompt_backend)
    end)

    Req.Test.set_req_test_from_context(%{async: false})

    Req.Test.stub(TournamentUi.LangfusePrompts, fn conn ->
      Req.Test.json(conn, %{
        "prompt" => "Prefer durable, specific memories over transient details.",
        "version" => 1,
        "labels" => ["production"]
      })
    end)

    home = "/tmp/dt-judge-ux-#{System.unique_integer([:positive])}"
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
          d.create_domain(name='memory-extraction', description='Memory extraction', corpus_source={'kind':'inline','items':[]})
          db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
          cfg_id = db.execute("SELECT id FROM job_configuration WHERE rater_type='human'").fetchone()[0]
          payload1 = json.dumps({
            'label': 'R1-1',
            'card_a': {'title': 'first card', 'body': 'body1'},
            'card_b': {'title': 'second card', 'body': 'body2'}
          })
          payload2 = json.dumps({
            'label': 'R1-2',
            'card_a': {'title': 'third card', 'body': 'body3'},
            'card_b': {'title': 'fourth card', 'body': 'body4'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 0, payload1, 1))
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload, domain_id) VALUES (?, ?, ?, ?, ?)", (cfg_id, 'domain:1', 1, payload2, 1))
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

  test "judge sidebar shows domain name, not opaque domain:N path", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")

    assert html =~ "memory-extraction"
    refute html =~ "domain:1"
  end

  test "domain-filtered review preserves its domain when opening results", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge?domain=memory-extraction")

    assert has_element?(
             live,
             "a[href='/results?domain=memory-extraction']",
             "compare results →"
           )
  end

  test "shows the category-specific judging brief above the pair", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")

    assert has_element?(live, "#judging-brief[open]")
    assert html =~ "Memory extraction"
    assert html =~ "better satisfies the judging brief"

    # The initial fallback is immediate; the full Langfuse prompt arrives
    # asynchronously and must replace it without blocking the judge pane.
    assert render(live) =~ "Prefer durable, specific memories"
  end

  test "keeps the immediate category fallback when the prompt service is unavailable", %{
    conn: conn
  } do
    Req.Test.stub(TournamentUi.LangfusePrompts, fn conn ->
      Plug.Conn.send_resp(conn, 404, "not found")
    end)

    {:ok, live, html} = live(conn, "/judge")
    assert html =~ "Memory extraction"

    refreshed = wait_for_prompt_load(live)
    assert refreshed =~ "Memory extraction"
    refute refreshed =~ "loading full brief"
  end

  test "verdict buttons show number-key labels for keyboard nav", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")
    # 8 verdicts in card-prioritizer-v0 → numbers 1..8 appear next to the labels
    assert html =~ ~r/text-\[10px\].*?>\s*1\s*</
    assert html =~ ~r/text-\[10px\].*?>\s*2\s*</
  end

  test "pressing number key picks the corresponding verdict", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    # Get the verdict enum so we know what "1" maps to
    html = render(live)

    [first_verdict | _] =
      ~w(a-clearly-better a-marginally-better tie-both-strong tie-both-weak b-marginally-better b-clearly-better incoherent skip)

    assert html =~ first_verdict

    new_html =
      render_hook(live, "keydown", %{"key" => "1"})

    assert new_html =~ "btn-primary"
    # The verdict status line shows the picked verdict
    assert new_html =~ first_verdict
  end

  test "pressing J moves to next pending row", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")
    # initial active row
    assert html =~ "first card"

    new_html = render_hook(live, "keydown", %{"key" => "j"})
    assert new_html =~ "third card"
  end

  # ── wave-13 layout (contract §7): no sidebar, full-width queue bar ──

  test "judging surface has NO aside — the queue lives in a full-width bar", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")

    refute html =~ "<aside"
    assert html =~ ~s(id="judge-queue-bar")
    assert html =~ ~s(id="judge-queue-strip")
  end

  test "keyboard hook stays on the #judge-shell workspace_split root", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")

    assert html =~ ~s(id="judge-shell")
    assert html =~ ~s(phx-hook="JudgeShortcuts")
    # The hook rides the shell root itself, not some inner remnant.
    assert html =~
             ~r/id="judge-shell"[^>]*phx-hook="JudgeShortcuts"|phx-hook="JudgeShortcuts"[^>]*id="judge-shell"/
  end

  test "queue bar carries the domain filter, pending counts, and results link", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    bar = live |> element("#judge-queue-bar") |> render()

    # Domain filter dropdown with per-domain pending counts.
    assert bar =~ ~s(id="judge-domain-filter")
    assert bar =~ "All domains"
    assert bar =~ "memory-extraction (2 pending)"

    # Aggregate pending count + recorded-ratings counter.
    assert bar =~ "2 pending"
    assert bar =~ "ratings recorded"

    # Compare-results link relocated into the bar.
    assert bar =~ "compare results →"

    # Queue rows render inside the bar's horizontal strip.
    assert bar =~ ~s(id="judge-queue-strip")
    assert bar =~ "first card"
    assert bar =~ "third card"
  end

  defp wait_for_prompt_load(live, attempts \\ 50)

  defp wait_for_prompt_load(live, attempts) when attempts > 0 do
    html = render(live)

    if html =~ "loading full brief" do
      Process.sleep(10)
      wait_for_prompt_load(live, attempts - 1)
    else
      html
    end
  end

  defp wait_for_prompt_load(live, 0), do: render(live)
end
