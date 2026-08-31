defmodule TournamentUiWeb.EnvironmentLiveTest do
  # Mutates process-global env (DATA_TOURNAMENTS_HOME) — must not run
  # alongside other tests.
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  # /environment (wave-13 §2): one LiveView, five tabs. Sources/prompts
  # absorbed the old /catalog index and /prompts surfaces (tested in their
  # relocated suites); this file covers the three NEW tabs (rubrics /
  # pipelines / policies), the honest empty/unavailable notes, and the
  # legacy-route redirects.

  defp sql!(home, statements) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import sqlite3
          db = sqlite3.connect('#{home}/judgements.db')
          db.executescript('''#{statements}''')
          db.commit()
          """
        ],
        stderr_to_stdout: true
      )

    assert status == 0, "sql failed: #{out}"
  end

  defp create_tables!(home) do
    sql!(home, """
    CREATE TABLE IF NOT EXISTS eval_template (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      version INTEGER NOT NULL,
      output_definition TEXT NOT NULL,
      langfuse_prompt_name TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      is_draft INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS domain (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      description TEXT NOT NULL DEFAULT '',
      generator_prompt TEXT NOT NULL,
      judge_prompt TEXT NOT NULL,
      rubric TEXT NOT NULL DEFAULT 'pair-wheel-v2',
      corpus_source TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS pipeline (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      version INTEGER NOT NULL,
      definition TEXT NOT NULL,
      definition_digest TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS domain_pipeline (
      id INTEGER PRIMARY KEY,
      domain_id INTEGER NOT NULL,
      pipeline_id INTEGER NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS policy (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      kind TEXT NOT NULL,
      rule TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
  end

  setup do
    home =
      "/tmp/dt-environment-live-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    on_exit(fn -> File.rm_rf(home) end)
    {:ok, home: home}
  end

  describe "rubrics tab" do
    test "renders PAIR/SINGLE chips, subjects, wheel, and verdict enums",
         %{conn: conn, home: home} do
      create_tables!(home)

      sql!(home, """
      INSERT INTO eval_template (name, version, output_definition) VALUES
        ('pair-rubric', 1,
         '{"judgement_kind": "pair", "subjects": ["execution", "plan"],
           "verdict_enum": ["A", "B"], "wheel": {"CORRECTNESS": {}}}'),
        ('single-rubric', 1,
         '{"judgement_kind": "single", "subjects": ["plan"],
           "verdict_enum": ["GO", "NO_GO"]}');
      """)

      {:ok, view, html} = live(conn, "/environment?tab=rubrics")

      assert html =~ ~s(id="rubric-pair-rubric-v1")
      assert html =~ ~s(id="rubric-single-rubric-v1")

      pair = view |> element("#rubric-pair-rubric-v1") |> render()
      assert pair =~ "PAIR"
      assert pair =~ "execution"
      assert pair =~ "plan"
      assert pair =~ "wheel"
      assert pair =~ "A / B"

      single = view |> element("#rubric-single-rubric-v1") |> render()
      assert single =~ "SINGLE"
      assert single =~ "GO / NO_GO"
      refute single =~ "wheel"
    end

    test "legacy template with no judgement_kind defaults to PAIR / execution",
         %{conn: conn, home: home} do
      create_tables!(home)

      sql!(home, """
      INSERT INTO eval_template (name, version, output_definition) VALUES
        ('legacy-rubric', 1, '{"verdict_enum": ["LEFT", "RIGHT"]}');
      """)

      {:ok, view, _html} = live(conn, "/environment?tab=rubrics")

      legacy = view |> element("#rubric-legacy-rubric-v1") |> render()
      assert legacy =~ "PAIR"
      assert legacy =~ "execution"
      refute legacy =~ "SINGLE"
    end
  end

  describe "pipelines tab" do
    test "renders the registry with stage flow and the domain binding",
         %{conn: conn, home: home} do
      create_tables!(home)

      sql!(home, """
      INSERT INTO pipeline (id, name, version, definition, definition_digest) VALUES
        (1, 'release', 1,
         '{"stages": [{"key": "judge", "judgement": "pair", "subject": "execution"},
                      {"key": "ship", "action": "release"}]}',
         'aabbccddeeff00112233');
      INSERT INTO domain (id, name, generator_prompt, judge_prompt, corpus_source)
        VALUES (1, 'wearables-lane', 'gen', 'judge', '{"kind": "static"}');
      INSERT INTO domain_pipeline (domain_id, pipeline_id) VALUES (1, 1);
      """)

      {:ok, _view, html} = live(conn, "/environment?tab=pipelines")

      assert html =~ ~s(id="pipeline-release-v1")
      assert html =~ "aabbccddeeff"
      assert html =~ "judge · pair execution"
      assert html =~ "ship · release"

      assert html =~ ~s(id="binding-wearables-lane")
      assert html =~ "release v1"
    end
  end

  describe "policies tab" do
    test "renders approver names and scope but NEVER the rule JSON body",
         %{conn: conn, home: home} do
      create_tables!(home)

      sql!(home, """
      INSERT INTO policy (name, kind, rule, status) VALUES
        ('release-approval', 'approval',
         '{"scope": "release", "approvers": ["changeme"],
           "secret_token": "sekret-rule-value-123"}',
         'active');
      """)

      {:ok, _view, html} = live(conn, "/environment?tab=policies")

      assert html =~ ~s(id="policy-release-approval")
      assert html =~ "changeme"
      assert html =~ "release"
      assert html =~ "approval"
      # Rule bodies are parsed for names only and dropped — no JSON dump,
      # no secret values, ever.
      refute html =~ "sekret-rule-value-123"
      refute html =~ "secret_token"
    end
  end

  describe "empty and unavailable data homes" do
    test "tables exist but are empty: honest empty notes", %{conn: conn, home: home} do
      create_tables!(home)

      {:ok, _view, html} = live(conn, "/environment?tab=rubrics")
      assert html =~ ~s(id="env-rubrics-empty")

      {:ok, _view, html} = live(conn, "/environment?tab=pipelines")
      assert html =~ ~s(id="env-pipelines-empty")
      assert html =~ ~s(id="env-bindings-empty")

      {:ok, _view, html} = live(conn, "/environment?tab=policies")
      assert html =~ ~s(id="env-policies-empty")
    end

    test "no DB at all: honest 'not initialized' note, never a fake empty list",
         %{conn: conn} do
      {:ok, _view, html} = live(conn, "/environment?tab=rubrics")
      assert html =~ ~s(id="env-not-initialized-rubrics")
      assert html =~ "not initialized in this data home"

      # The pipelines tab carries TWO of these notes, so they cannot share one
      # DOM id: LiveView refuses to patch a duplicate.
      {:ok, _view, html} = live(conn, "/environment?tab=pipelines")
      assert html =~ ~s(id="env-not-initialized-pipelines")
      assert html =~ ~s(id="env-not-initialized-bindings")

      {:ok, _view, html} = live(conn, "/environment?tab=policies")
      assert html =~ ~s(id="env-not-initialized-policies")
    end
  end

  describe "tabs and legacy routes" do
    test "unknown tab falls back to sources", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/environment?tab=bogus")
      assert html =~ ~s(id="env-sources")
    end

    test "all five tab links render", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/environment")

      for tab <- ~w(sources prompts rubrics pipelines policies) do
        assert html =~ ~s(id="env-tab-#{tab}")
      end
    end

    test "/catalog redirects to the sources tab", %{conn: conn} do
      assert {:error, {:live_redirect, %{to: "/environment?tab=sources"}}} =
               live(conn, "/catalog")
    end

    test "/prompts redirects to the prompts tab", %{conn: conn} do
      assert {:error, {:live_redirect, %{to: "/environment?tab=prompts"}}} =
               live(conn, "/prompts")
    end

    test "/catalog/:project stays a live detail page (no redirect)", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/catalog/some-project")
      assert html =~ "No project named"
      assert html =~ "some-project"
    end
  end
end
