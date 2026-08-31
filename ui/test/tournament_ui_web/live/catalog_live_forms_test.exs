defmodule TournamentUiWeb.CatalogLiveFormsTest do
  # Mutates process-global env (CATALOG_CLI_CMD, UNITY_CLOUD_BUILD_API_KEY,
  # DATA_TOURNAMENTS_HOME) — must not run alongside other tests.
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  # F-1 + F-9: landscape setup happens in the UI. Project creation and
  # source add/archive are inline forms shelling out to the catalog CLI
  # (stubbed here via CATALOG_CLI_CMD); source rows carry an HONEST
  # offline status (configured / unknown kind / credential needed) plus
  # a per-source evidence count. No network probes.

  defp repo_root, do: File.cwd!() |> Path.join("..") |> Path.expand()

  defp seed!(home, python_body) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root()}')
          import bin.catalog as cat
          cat.init()
          #{python_body}
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    out
  end

  # A stub CLI that records its argv, then forwards to the real
  # bin/catalog.py so the list genuinely refreshes after a write.
  defp stub_cli!(home, mode) do
    script = Path.join(home, "catalog_stub.sh")
    log = Path.join(home, "cli.log")

    body =
      case mode do
        :passthrough ->
          """
          #!/bin/sh
          echo "$@" >> #{log}
          exec python3 bin/catalog.py "$@"
          """

        :fail ->
          """
          #!/bin/sh
          echo "$@" >> #{log}
          echo "boom: schema says no" >&2
          exit 3
          """
      end

    File.write!(script, body)
    File.chmod!(script, 0o755)
    System.put_env("CATALOG_CLI_CMD", script)
    log
  end

  defp cli_log(log) do
    case File.read(log) do
      {:ok, content} -> content
      _ -> ""
    end
  end

  defp flash_info(view),
    do: Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info)

  setup do
    home =
      "/tmp/dt-catalog-forms-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    on_exit(fn ->
      System.delete_env("CATALOG_CLI_CMD")
      System.delete_env("UNITY_CLOUD_BUILD_API_KEY")
      File.rm_rf(home)
    end)

    {:ok, home: home}
  end

  describe "project creation (F-1, now on the Environment sources tab)" do
    test "empty state shows the inline form, not a CLI wall", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/environment?tab=sources")

      assert html =~ ~s(id="new-project-form")
      assert html =~ "Create project"
      refute html =~ "python3 bin/catalog.py"
    end

    test "create-project dispatches the CLI, flashes, and refreshes the list",
         %{conn: conn, home: home} do
      # catalog.py create-project does NOT auto-init the schema; apply it
      # first (the deployed DB always has it via judgement.init_db).
      seed!(home, "pass")
      log = stub_cli!(home, :passthrough)
      {:ok, view, _html} = live(conn, "/environment?tab=sources")

      html =
        view
        |> form("#new-project-form", %{
          "name" => "  atlas  ",
          "description" => "Mapping platform"
        })
        |> render_submit()

      assert cli_log(log) =~ "create-project --name atlas --description Mapping platform"
      assert flash_info(view) == "Project 'atlas' created."
      # Refreshed list: the project card now renders instead of the empty state.
      assert html =~ "atlas"
      assert html =~ "Mapping platform"
      refute html =~ ~s(id="catalog-empty")
    end

    test "nonzero exit surfaces stderr in an error banner", %{conn: conn, home: home} do
      stub_cli!(home, :fail)
      {:ok, view, _html} = live(conn, "/environment?tab=sources")

      html =
        view
        |> form("#new-project-form", %{"name" => "atlas", "description" => ""})
        |> render_submit()

      assert html =~ ~s(id="project-error")
      assert html =~ "create-project failed (exit 3)"
      assert html =~ "boom: schema says no"
      assert flash_info(view) == nil
    end

    test "blank name is rejected client-side without dispatching the CLI",
         %{conn: conn, home: home} do
      log = stub_cli!(home, :passthrough)
      {:ok, view, _html} = live(conn, "/environment?tab=sources")

      html =
        view
        |> form("#new-project-form", %{"name" => "   ", "description" => ""})
        |> render_submit()

      assert html =~ "Project name is required."
      assert cli_log(log) == ""
    end

    test "New project button reveals the form when projects exist",
         %{conn: conn, home: home} do
      seed!(home, "cat.create_project(name='existing', description='')")
      {:ok, view, html} = live(conn, "/environment?tab=sources")

      assert html =~ ~s(id="new-project-btn")
      refute html =~ ~s(id="new-project-form")

      html = view |> element("#new-project-btn") |> render_click()
      assert html =~ ~s(id="new-project-form")
    end
  end

  describe "source add / archive (F-1)" do
    setup %{home: home} do
      seed!(home, "cat.create_project(name='atlas', description='Mapping platform')")
      :ok
    end

    test "Add source reveals a form with kind vocabulary and labeled tiers",
         %{conn: conn} do
      {:ok, view, html} = live(conn, "/catalog/atlas")

      refute html =~ ~s(id="add-source-form")
      html = view |> element("#add-source-btn") |> render_click()

      assert html =~ ~s(id="add-source-form")

      # Kind select carries the full adapter_kinds() vocabulary.
      for kind <- ~w(bugsweep_corpus dedup_lists git_local github_api
                     github_autoclosed sentry_csv slack_csv unity_cloud) do
        assert html =~ ~s(value="#{kind}")
      end

      # Trust tiers labeled with their semantics.
      assert html =~ "Tier 1 — system-captured"
      assert html =~ "Tier 2 — team-authored"
      assert html =~ "Tier 3 — external-untrusted"
    end

    test "create-source dispatches every field to the CLI and refreshes",
         %{conn: conn, home: home} do
      log = stub_cli!(home, :passthrough)
      {:ok, view, _html} = live(conn, "/catalog/atlas")

      view |> element("#add-source-btn") |> render_click()

      html =
        view
        |> form("#add-source-form", %{
          "name" => "repo",
          "kind" => "git_local",
          "locator" => "/srv/checkouts/atlas",
          "trust_tier" => "1"
        })
        |> render_submit()

      assert cli_log(log) =~
               "create-source --project atlas --name repo --kind git_local " <>
                 "--locator /srv/checkouts/atlas --trust-tier 1"

      assert flash_info(view) == "Source 'repo' added."
      assert html =~ ~s(id="source-row-repo")
      assert html =~ "TIER1 · system"
    end

    test "create-source failure surfaces stderr, keeps the form open",
         %{conn: conn, home: home} do
      stub_cli!(home, :fail)
      {:ok, view, _html} = live(conn, "/catalog/atlas")

      view |> element("#add-source-btn") |> render_click()

      html =
        view
        |> form("#add-source-form", %{
          "name" => "repo",
          "kind" => "git_local",
          "locator" => "/srv/x",
          "trust_tier" => "2"
        })
        |> render_submit()

      assert html =~ "create-source failed (exit 3)"
      assert html =~ "boom: schema says no"
      assert html =~ ~s(id="add-source-form")
    end

    test "archive asks for confirmation and dispatches archive-source",
         %{conn: conn, home: home} do
      seed!(
        home,
        "cat.create_source(project='atlas', name='repo', kind='git_local', locator='/srv/x', trust_tier=1)"
      )

      log = stub_cli!(home, :passthrough)
      {:ok, view, html} = live(conn, "/catalog/atlas")

      assert html =~ ~s(id="source-row-repo")
      # Destructive action is gated behind a browser confirm.
      assert view
             |> element("#archive-source-repo")
             |> render() =~ "data-confirm=\"Archive source &#39;repo&#39;?"

      html = view |> element("#archive-source-repo") |> render_click()

      assert cli_log(log) =~ "archive-source --project atlas --name repo"
      assert flash_info(view) == "Source 'repo' archived."
      refute html =~ ~s(id="source-row-repo")
    end
  end

  describe "honest offline source status (F-9)" do
    setup %{home: home} do
      seed!(home, "cat.create_project(name='atlas', description='')")
      :ok
    end

    test "registered kind with a locator reads configured", %{conn: conn, home: home} do
      seed!(
        home,
        "cat.create_source(project='atlas', name='repo', kind='git_local', locator='/srv/x', trust_tier=1)"
      )

      {:ok, _view, html} = live(conn, "/catalog/atlas")
      assert html =~ ~s(data-status="configured")
      refute html =~ "credential needed"
    end

    test "unregistered kind reads unknown kind, with known kinds in the title",
         %{conn: conn, home: home} do
      seed!(
        home,
        "cat.create_source(project='atlas', name='forum', kind='docs', locator='https://forum.example', trust_tier=3)"
      )

      {:ok, _view, html} = live(conn, "/catalog/atlas")
      assert html =~ ~s(data-status="unknown kind")
      assert html =~ "known kinds: bugsweep_corpus, dedup_lists, git_local"
    end

    test "credential-gated kind reflects env-var PRESENCE, never its value",
         %{conn: conn, home: home} do
      seed!(
        home,
        "cat.create_source(project='atlas', name='builds', kind='unity_cloud', locator='org/proj', trust_tier=1)"
      )

      System.delete_env("UNITY_CLOUD_BUILD_API_KEY")
      {:ok, _view, html} = live(conn, "/catalog/atlas")
      assert html =~ ~s(data-status="credential needed: UNITY_CLOUD_BUILD_API_KEY")

      System.put_env("UNITY_CLOUD_BUILD_API_KEY", "sekret-value-123")
      {:ok, _view, html} = live(conn, "/catalog/atlas")
      assert html =~ ~s(data-status="configured")
      # Presence check only — the value must never leak into the page.
      refute html =~ "sekret-value-123"
    end

    test "per-source evidence count renders", %{conn: conn, home: home} do
      seed!(home, """
      src = cat.create_source(project='atlas', name='repo', kind='git_local', locator='/srv/x', trust_tier=1)
      from bin.landscape.evidence import EvidenceRef, TrustTier, SourceType
      ev1 = EvidenceRef(source_type=SourceType.GIT_REPO, canonical_uri='repo://atlas@abc', trust_tier=TrustTier.TIER1_SYSTEM, excerpt='commit abc')
      ev2 = EvidenceRef(source_type=SourceType.GIT_REPO, canonical_uri='repo://atlas@def', trust_tier=TrustTier.TIER1_SYSTEM, excerpt='commit def')
      cat.insert_evidence_ref(ev1, source_id=src)
      cat.insert_evidence_ref(ev2, source_id=src)
      """)

      {:ok, _view, html} = live(conn, "/catalog/atlas")
      assert html =~ "2 evidence"
    end
  end
end
