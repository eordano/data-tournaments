defmodule TournamentUiWeb.JudgeLiveUxTest do
  @moduledoc """
  The judging screen as a person actually operates it.

  Two groups here are about the one irreversible action on the page.
  `discard-a`/`discard-b` eject an item from the pool forever and cancel
  its outstanding queue rows, while the five comparison verdicts can all
  be re-judged; `docs/design/priority-tournament.md`, "Discard is a
  verdict, not a loss, and it is PER SIDE". So the southern diagonals must
  not weigh the same as "a wins" (colour, glyph, and the consequence
  naming what happens to the OTHER side), and the keyboard path must not
  let a "digit, space" rhythm eject by muscle memory.

  The other group holds Skip to a single contract: selected like any other
  verdict, submitted through the same button. A second Skip affordance
  that submits on the spot throws away a rationale already typed.
  """
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

    home = "/tmp/dt-judge-ux-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
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

  test "the default rubric offers its wheel plus an off-wheel skip", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    for {pos, verdict} <- [
          {"nw", "a-wins"},
          {"n", "tie"},
          {"ne", "b-wins"},
          {"w", "a-wins-big"},
          {"e", "b-wins-big"},
          {"sw", "discard-a"},
          {"se", "discard-b"}
        ] do
      assert has_element?(live, "#wheel-#{pos}[phx-value-v='#{verdict}']"),
             "missing wheel button #{pos} -> #{verdict}"
    end

    refute has_element?(live, "#wheel-s"),
           "south stays empty: there is no 'both are bad' verdict"

    assert has_element?(live, "#operational-verdicts button#operational-skip"),
           "a rater who cannot call a pairing must have somewhere to say so"

    refute has_element?(live, "#verdict-wheel button[phx-value-v='skip']"),
           "skip establishes nothing, so it never occupies a wheel position"
  end

  test "pressing a numpad key picks the reversible verdict at that position", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    assert render_hook(live, "keydown", %{"key" => "8"}) =~ "tie"
    assert has_element?(live, "#wheel-n[aria-checked='true']")

    render_hook(live, "keydown", %{"key" => "4"})
    assert has_element?(live, "#wheel-w[aria-checked='true']")
    refute has_element?(live, "#wheel-n[aria-checked='true']")
  end

  test "the discard cells are dressed as destructive, the comparison cells are not", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")

    for pos <- ["sw", "se"] do
      assert has_element?(live, "#wheel-#{pos}[data-destructive='true']"),
             "#{pos} ejects an item from the pool for good and must not weigh the " <>
               "same as a verdict that can be re-judged"

      assert html =~ ~r/id="wheel-#{pos}"[^>]*class="[^"]*text-error/,
             "#{pos} must not render btn-ghost border app-hairline, byte-identical " <>
               "to 'a wins'"
    end

    for pos <- ["nw", "n", "ne", "w", "e"] do
      assert has_element?(live, "#wheel-#{pos}[data-destructive='false']")
    end

    assert html =~ "Ejects A permanently; B is not credited and is paired again."
    assert html =~ "Ejects B permanently; A is not credited and is paired again."
  end

  test "a digit-then-space rhythm cannot eject: the discard key must be pressed twice", %{
    conn: conn
  } do
    {:ok, live, _html} = live(conn, "/judge")

    armed = render_hook(live, "keydown", %{"key" => "1"})
    refute has_element?(live, "#wheel-sw[aria-checked='true']")
    assert has_element?(live, "#discard-armed")
    assert armed =~ "Ejects A permanently; B is not credited and is paired again."

    after_space = render_hook(live, "keydown", %{"key" => " "})

    assert after_space =~ "first card",
           "space is the submit key for every other verdict, so a judge in rhythm " <>
             "would have ejected A here"

    render_hook(live, "keydown", %{"key" => "1"})
    assert has_element?(live, "#wheel-sw[aria-checked='true']")
    refute has_element?(live, "#discard-armed")

    submitted = render_hook(live, "keydown", %{"key" => " "})
    refute submitted =~ "first card"
    assert submitted =~ "third card"
  end

  test "any other key disarms a half-pressed discard", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    render_hook(live, "keydown", %{"key" => "3"})
    assert has_element?(live, "#discard-armed")

    render_hook(live, "keydown", %{"key" => "2"})
    refute has_element?(live, "#discard-armed")
    refute has_element?(live, "#wheel-se[aria-checked='true']")

    render_hook(live, "keydown", %{"key" => "3"})
    render_hook(live, "keydown", %{"key" => "8"})
    refute has_element?(live, "#discard-armed")
    assert has_element?(live, "#wheel-n[aria-checked='true']")
  end

  test "the mouse path puts the consequence on the submit button itself", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    live |> element("#wheel-n") |> render_click()
    submit = live |> element("#judge-submit") |> render()
    refute submit =~ "data-confirm", "a tie must not stop to ask"
    assert submit =~ "Submit"

    live |> element("#wheel-sw") |> render_click()
    submit = live |> element("#judge-submit") |> render()

    assert submit =~ "data-confirm",
           "the app's only other data-confirm guards archiving a source whose " <>
             "evidence is kept; ejecting an item from the pool is not reversible"

    assert submit =~ "Ejects A permanently"
    assert submit =~ "Eject and continue"
  end

  test "skip is selected like every other verdict and never submits on its own", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")

    refute has_element?(live, "button[phx-click='skip']"),
           "a Skip that submits immediately is a second contract, and it throws " <>
             "away a rationale the judge already typed"

    assert has_element?(live, "#operational-skip"),
           "a rater who cannot call a pairing still needs somewhere to say so"

    render_hook(live, "keydown", %{"key" => "s"})
    assert has_element?(live, "#operational-skip[aria-pressed='true']")
    assert render(live) =~ "first card"

    assert render_hook(live, "keydown", %{"key" => " "}) =~ "third card"
  end

  test "the keyboard hint advertises only the digits that do something", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/judge")

    assert html =~ "1 3 4 6 7 8 9",
           "the shipped wheel leaves due south empty, so 2 and 5 are inert"

    refute html =~ "1-9",
           "2 sits physically between the two permanent verdicts; advertising it " <>
             "as a compass key is how a judge finds out by pressing it"

    assert html =~ "an eject key must be pressed twice"
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
