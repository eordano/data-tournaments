defmodule TournamentUiWeb.JudgeLiveWheelTest do
  @moduledoc """
  Wave-12 semantic wheel / single axis / subject stepper coverage
  (docs/design/judgement-wheel-v2.md). Seeds three templates alongside
  the legacy card-prioritizer-v0 rubric:

    * pair-wheel-v1        — kind pair, 8-position wheel, 1 subject
    * single-execution-v1  — kind single, 4-position axis
    * pair-wheel-duo-v1    — kind pair, wheel, subjects [idea, execution]
  """
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  @wheel %{
    "n" => "tie-both-important",
    "ne" => "b-slightly-better",
    "e" => "b-strongly-better",
    "se" => "b-lean-both-invalid",
    "s" => "neither-good",
    "sw" => "a-lean-both-invalid",
    "w" => "a-strongly-better",
    "nw" => "a-slightly-better"
  }

  @wheel_verdicts [
    "a-strongly-better",
    "a-slightly-better",
    "tie-both-important",
    "b-slightly-better",
    "b-strongly-better",
    "a-lean-both-invalid",
    "b-lean-both-invalid",
    "neither-good"
  ]

  @axis %{
    "n" => "strong-yes",
    "ne" => "yes",
    "se" => "weak",
    "s" => "invalid"
  }

  setup do
    home = "/tmp/dt-judge-wheel-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    pair_outdef =
      Jason.encode!(%{
        "verdict_enum" => @wheel_verdicts ++ ["incoherent", "skip"],
        "confidence_enum" => ["low", "mid", "high"],
        "rationale_required" => false,
        "judgement_kind" => "pair",
        "subjects" => ["execution"],
        "wheel" => @wheel
      })

    single_outdef =
      Jason.encode!(%{
        "verdict_enum" => Map.values(@axis) ++ ["needs-evidence", "skip"],
        "confidence_enum" => ["low", "mid", "high"],
        "rationale_required" => false,
        "judgement_kind" => "single",
        "wheel" => @axis
      })

    duo_outdef =
      Jason.encode!(%{
        "verdict_enum" => @wheel_verdicts ++ ["skip"],
        "confidence_enum" => ["low", "mid", "high"],
        "rationale_required" => false,
        "judgement_kind" => "pair",
        "subjects" => ["idea", "execution"],
        "wheel" => @wheel
      })

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
          # Scope to the legacy template — init may seed other human configs.
          legacy_cfg = db.execute(
              "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.rater_type='human' AND t.name='card-prioritizer-v0'").fetchone()[0]

          def add_template(name, outdef):
              cur = db.execute(
                  "INSERT INTO eval_template(name, version, output_definition) VALUES (?, 1, ?)",
                  (name, outdef))
              tid = cur.lastrowid
              cur = db.execute(
                  "INSERT INTO job_configuration(template_id, rater_type) VALUES (?, 'human')",
                  (tid,))
              return cur.lastrowid

          # Unique names: init_db already seeds pair-wheel-v1 etc.
          wheel_cfg = add_template('ui-wheel-pair-v1', '''#{pair_outdef}''')
          single_cfg = add_template('ui-wheel-single-v1', '''#{single_outdef}''')
          duo_cfg = add_template('ui-wheel-duo-v1', '''#{duo_outdef}''')

          # id 1 — legacy flat-row pair
          legacy_payload = json.dumps({
            'label': 'L1',
            'card_a': {'title': 'legacy left', 'body': 'left body'},
            'card_b': {'title': 'legacy right', 'body': 'right body'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?, ?, ?, ?)", (legacy_cfg, '/tmp/w.db', 0, legacy_payload))

          # id 2 — pair wheel
          wheel_payload = json.dumps({
            'label': 'W1',
            'card_a': {'title': 'wheel left', 'body': 'wl body'},
            'card_b': {'title': 'wheel right', 'body': 'wr body'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?, ?, ?, ?)", (wheel_cfg, '/tmp/w.db', 1, wheel_payload))

          # id 3 — single judgement ('card' key, not card_a/card_b)
          single_payload = json.dumps({
            'label': 'S1',
            'card': {'title': 'lone artifact', 'body': 'branch diff summary'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?, ?, ?, ?)", (single_cfg, '/tmp/w.db', 2, single_payload))

          # id 4 — two-subject pair wheel
          duo_payload = json.dumps({
            'label': 'D1',
            'card_a': {'title': 'duo left', 'body': 'dl body'},
            'card_b': {'title': 'duo right', 'body': 'dr body'}
          })
          db.execute("INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?, ?, ?, ?)", (duo_cfg, '/tmp/w.db', 3, duo_payload))
          db.commit()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    %{home: home}
  end

  defp select(live, pending_id) do
    live |> element("button[phx-click='select'][phx-value-id='#{pending_id}']") |> render_click()
  end

  test "wheel template renders 8 positioned buttons with grid classes + center legend", %{
    conn: conn
  } do
    {:ok, live, _html} = live(conn, "/judge")
    html = select(live, 2)

    assert html =~ ~s(id="verdict-wheel")
    assert html =~ ~s(role="radiogroup")

    # 8 positioned buttons, each keeping the set_verdict contract.
    for {pos, verdict} <- @wheel do
      assert has_element?(
               live,
               "#wheel-#{pos}[phx-click='set_verdict'][phx-value-v='#{verdict}']"
             ),
             "missing wheel button #{pos} → #{verdict}"
    end

    # Grid placement IS the semantics: corners + edges land in their cells.
    assert html =~ ~r/id="wheel-nw"[^>]*class="[^"]*col-start-1 row-start-1/
    assert html =~ ~r/id="wheel-n"[^>]*class="[^"]*col-start-2 row-start-1/
    assert html =~ ~r/id="wheel-e"[^>]*class="[^"]*col-start-3 row-start-2/
    assert html =~ ~r/id="wheel-s"[^>]*class="[^"]*col-start-2 row-start-3/

    # Center cell: A vs B legend + subject badge + selected label slot.
    assert has_element?(live, "#wheel-center.col-start-2.row-start-2", "A vs B")
    assert has_element?(live, "#wheel-center [data-subject='execution']")
    assert has_element?(live, "#wheel-selected-label", "—")
  end

  test "legacy template keeps the flat verdict row exactly (regression)", %{conn: conn} do
    {:ok, live, html} = live(conn, "/judge")
    # pending id 1 is active by default (oldest first)
    assert html =~ "legacy left"

    refute has_element?(live, "#verdict-wheel")
    refute has_element?(live, "#operational-verdicts")
    refute has_element?(live, "#subject-stepper")

    # Old selectors intact: number-key labels + flat buttons per enum entry.
    assert has_element?(live, "button[phx-click='set_verdict'][phx-value-v='a-clearly-better']")
    assert has_element?(live, "button[phx-click='set_verdict'][phx-value-v='skip']")
    assert html =~ ~r/1-8/
    # Legacy number-key behavior: "1" indexes verdict_enum.
    keyed = render_hook(live, "keydown", %{"key" => "1"})
    assert keyed =~ "a-clearly-better"
    assert keyed =~ "btn-primary"
  end

  test "skip and incoherent render off-wheel as operational buttons", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")
    select(live, 2)

    assert has_element?(
             live,
             "#operational-verdicts button#operational-skip[phx-click='set_verdict'][phx-value-v='skip']"
           )

    assert has_element?(
             live,
             "#operational-verdicts button#operational-incoherent[phx-value-v='incoherent']"
           )

    # And they are NOT wheel cells.
    refute has_element?(live, "#verdict-wheel button[phx-value-v='skip']")
    refute has_element?(live, "#verdict-wheel button[phx-value-v='incoherent']")
  end

  test "numpad keys follow wheel geometry: 7 → nw, 2 → s", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")
    select(live, 2)

    html = render_hook(live, "keydown", %{"key" => "7"})
    assert has_element?(live, "#wheel-nw[aria-checked='true']")
    assert html =~ "a-slightly-better"

    render_hook(live, "keydown", %{"key" => "2"})
    assert has_element?(live, "#wheel-s[aria-checked='true']")
    refute has_element?(live, "#wheel-nw[aria-checked='true']")
  end

  test "digit 5 does nothing on a wheel template", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")
    select(live, 2)

    render_hook(live, "keydown", %{"key" => "5"})
    refute has_element?(live, "#verdict-wheel [aria-checked='true']")
  end

  test "selected wheel button carries aria-checked=true, others false", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")
    select(live, 2)

    live |> element("#wheel-w") |> render_click()

    assert has_element?(live, "#wheel-w[aria-checked='true']")
    assert has_element?(live, "#wheel-e[aria-checked='false']")
    assert has_element?(live, "#wheel-selected-label", "a strongly better")
  end

  test "single-kind template renders one card + 4-position vertical axis", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/judge")
    html = select(live, 3)

    # One artifact card from the 'card' payload key, no side-by-side grid.
    assert html =~ "lone artifact"
    assert has_element?(live, "#single-artifact")
    refute html =~ "Card B"
    refute has_element?(live, "#verdict-wheel")

    # Vertical axis: n/ne/se/s, top = best.
    assert has_element?(live, "#verdict-axis[role='radiogroup']")

    for {pos, verdict} <- @axis do
      assert has_element?(live, "#axis-#{pos}[phx-click='set_verdict'][phx-value-v='#{verdict}']")
    end

    # No A/B legend; center shows the subject badge + selected label.
    refute has_element?(live, "#verdict-axis", "A vs B")
    assert has_element?(live, "#axis-center [data-subject='execution']")

    # Off-axis verdicts render as operational buttons.
    assert has_element?(live, "#operational-verdicts button[phx-value-v='needs-evidence']")
    assert has_element?(live, "#operational-verdicts button[phx-value-v='skip']")

    # Numpad geometry still applies on the axis: 8 → n.
    render_hook(live, "keydown", %{"key" => "8"})
    assert has_element?(live, "#axis-n[aria-checked='true']")
  end

  test "2-subject template steps idea → execution and submits both", %{conn: conn, home: home} do
    {:ok, live, _html} = live(conn, "/judge")
    html = select(live, 4)

    # Stepper header with idea active first.
    assert has_element?(live, "#subject-stepper #subject-step-idea[data-active='true']")
    assert has_element?(live, "#subject-stepper #subject-step-execution[data-active='false']")
    assert has_element?(live, "#wheel-center [data-subject='idea']")

    # Next subject (not Submit) on the first subject, disabled until a verdict.
    assert has_element?(live, "#next-subject[disabled]")
    refute html =~ ~r/type="submit"[^>]*>\s*Submit/

    # Judge the idea.
    live |> element("#wheel-n") |> render_click()
    refute has_element?(live, "#next-subject[disabled]")
    live |> element("#next-subject") |> render_click()

    # Now on execution: stepper advanced, wheel reset, Submit present.
    assert has_element?(live, "#subject-step-execution[data-active='true']")
    assert has_element?(live, "#wheel-center [data-subject='execution']")
    refute has_element?(live, "#verdict-wheel [aria-checked='true']")
    refute has_element?(live, "#next-subject")
    assert has_element?(live, "#judge-form button[type='submit'][disabled]")

    # Judge the execution and submit.
    live |> element("#wheel-w") |> render_click()
    refute has_element?(live, "#judge-form button[type='submit'][disabled]")
    html = live |> element("form#judge-form") |> render_submit()

    # Advanced away from the duo pair.
    refute html =~ "duo left"

    # Payload carried BOTH subjects: per-subject score rows under one rating.
    {:ok, conn_db} = Exqlite.Sqlite3.open(Path.join(home, "judgements.db"), mode: :readonly)

    {:ok, stmt} =
      Exqlite.Sqlite3.prepare(
        conn_db,
        "SELECT name, value FROM score WHERE name LIKE 'judgement.%' ORDER BY name"
      )

    rows = collect_rows(conn_db, stmt)
    :ok = Exqlite.Sqlite3.close(conn_db)

    assert [
             "judgement.execution.confidence",
             "judgement.execution.verdict",
             "judgement.idea.confidence",
             "judgement.idea.verdict"
           ] ==
             rows |> Enum.map(&Enum.at(&1, 0)) |> Enum.sort()

    verdicts = Map.new(rows, fn [name, value] -> {name, value} end)
    assert verdicts["judgement.idea.verdict"] == "tie-both-important"
    assert verdicts["judgement.execution.verdict"] == "a-strongly-better"
  end

  defp collect_rows(conn, stmt) do
    case Exqlite.Sqlite3.step(conn, stmt) do
      {:row, row} -> [row | collect_rows(conn, stmt)]
      :done -> []
    end
  end
end
