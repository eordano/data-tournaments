defmodule TournamentUiWeb.CampaignsLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # /campaigns index (cards + in-UI create form — never a CLI wall) and
  # /campaigns/:name ledger (state chips incl. NO_GO reason, lens/validation
  # summaries). The create form dispatches to the campaigns CLI via
  # CAMPAIGNS_CLI_CMD (stubbed here to record argv).

  defp repo_root, do: File.cwd!() |> Path.join("..") |> Path.expand()

  defp py!(home, code) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root()}')
          #{code}
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "python failed: #{out}"
    out
  end

  defp seed_project!(home) do
    py!(home, """
    import bin.catalog as cat
    cat.init()
    cat.create_project(name='unity-explorer')
    """)
  end

  defp seed_campaigns!(home) do
    py!(home, """
    import bin.campaigns as camp
    camp.create_campaign(project='unity-explorer', name='bugsweep-aug', kind='bugsweep',
                         objective='crash-class sweep', base_commit='abc123def456')
    camp.create_finding(campaign='bugsweep-aug', slug='a-crash', source_kind='sentry',
                        root_cause='NRE on stale handle')
    camp.create_finding(campaign='bugsweep-aug', slug='b-dupe', source_kind='slack')
    camp.set_finding_state('bugsweep-aug', 'b-dupe', 'no_go', no_go_reason='already-fixed')
    camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='root-cause', verdict='CONFIRM')
    rid = camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='lifecycle', verdict='REFUTE')
    camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='lifecycle', verdict='CONFIRM',
                          repair_of=rid)
    camp.add_validation_row('bugsweep-aug', 'a-crash', red_intended=2, red_observed=2,
                            green_total=5, green_passed=5)
    """)
  end

  defp install_stub!(home, exit_code) do
    stub = Path.join(home, "stub_campaigns.sh")

    File.write!(stub, """
    #!/bin/sh
    echo "$@" >> #{home}/argv.log
    echo "stub says: campaign create exploded" >&2
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("CAMPAIGNS_CLI_CMD", stub)
  end

  setup do
    home =
      "/tmp/dt-campaigns-live-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    on_exit(fn ->
      System.delete_env("CAMPAIGNS_CLI_CMD")
      File.rm_rf(home)
    end)

    {:ok, home: home}
  end

  describe "index" do
    test "empty state offers the create form, not a CLI wall", %{conn: conn, home: home} do
      seed_project!(home)
      {:ok, _view, html} = live(conn, "/campaigns")

      assert html =~ ~s(id="campaigns-empty")
      assert html =~ ~s(id="new-campaign-form")
      refute html =~ "python3 bin/campaigns.py"
    end

    test "without a project, the form is replaced by a catalog hint", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/campaigns")

      assert html =~ ~s(id="campaigns-empty")
      assert html =~ ~s(id="no-projects-hint")
      refute html =~ ~s(id="new-campaign-form")
    end

    test "seeded index shows one card per campaign with counts", %{conn: conn, home: home} do
      seed_project!(home)
      seed_campaigns!(home)

      {:ok, _view, html} = live(conn, "/campaigns")

      assert html =~ ~s(id="campaign-bugsweep-aug")
      assert html =~ "bugsweep-aug"
      assert html =~ "crash-class sweep"
      assert html =~ "2 findings"
      assert html =~ "candidate 1"
      assert html =~ "no_go 1"
    end

    test "create form dispatches the CLI with the exact argv and flashes", %{
      conn: conn,
      home: home
    } do
      seed_project!(home)
      install_stub!(home, 0)

      {:ok, view, _html} = live(conn, "/campaigns")

      view
      |> form("#new-campaign-form", %{
        "campaign" => %{
          "name" => "bugsweep-sep",
          "project" => "unity-explorer",
          "kind" => "bugsweep",
          "objective" => "next sweep",
          "time_window" => "sentry 7d",
          "base_commit" => "cafe1234"
        }
      })
      |> render_submit()

      argv = File.read!(Path.join(home, "argv.log"))

      assert argv =~
               "create-campaign --project unity-explorer --name bugsweep-sep " <>
                 "--kind bugsweep --objective next sweep --time-window sentry 7d " <>
                 "--base-commit cafe1234"

      assert Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info) ==
               "Campaign 'bugsweep-sep' created."
    end

    test "nonzero CLI exit surfaces stderr in an error banner", %{conn: conn, home: home} do
      seed_project!(home)
      install_stub!(home, 1)

      {:ok, view, _html} = live(conn, "/campaigns")

      html =
        view
        |> form("#new-campaign-form", %{
          "campaign" => %{
            "name" => "bugsweep-sep",
            "project" => "unity-explorer",
            "kind" => "bugsweep"
          }
        })
        |> render_submit()

      assert html =~ ~s(id="create-error")
      assert html =~ "exit 1"
      assert html =~ "campaign create exploded"
    end

    test "blank name is rejected before any CLI dispatch", %{conn: conn, home: home} do
      seed_project!(home)
      install_stub!(home, 0)

      {:ok, view, _html} = live(conn, "/campaigns")

      html =
        view
        |> form("#new-campaign-form", %{
          "campaign" => %{"name" => "  ", "project" => "unity-explorer", "kind" => "bugsweep"}
        })
        |> render_submit()

      assert html =~ ~s(id="create-error")
      refute File.exists?(Path.join(home, "argv.log"))
    end
  end

  describe "show" do
    test "renders the campaign header and ledger rows", %{conn: conn, home: home} do
      seed_project!(home)
      seed_campaigns!(home)

      {:ok, _view, html} = live(conn, "/campaigns/bugsweep-aug")

      assert html =~ ~s(id="campaign-header")
      assert html =~ "crash-class sweep"
      assert html =~ "abc123def456" |> String.slice(0, 12)

      assert html =~ ~s(id="campaign-ledger")
      assert html =~ ~s(id="finding-a-crash")
      assert html =~ "sentry"
      assert html =~ "NRE on stale handle"
      # REFUTE→repaired lens summary derived from the repair_of answer.
      assert html =~ "CONFIRM ×1 + REFUTE→repaired"
      assert html =~ "RED 2/2 GREEN 5/5"
    end

    test "NO_GO row renders a state chip carrying the reason", %{conn: conn, home: home} do
      seed_project!(home)
      seed_campaigns!(home)

      {:ok, view, html} = live(conn, "/campaigns/bugsweep-aug")

      assert html =~ ~s(id="finding-b-dupe")
      chip = view |> element("#finding-b-dupe td:nth-child(3)") |> render()
      assert chip =~ "no_go"
      assert chip =~ "already-fixed"
    end

    test "unknown campaign renders a graceful missing state", %{conn: conn, home: home} do
      seed_project!(home)

      {:ok, _view, html} = live(conn, "/campaigns/nope")
      assert html =~ ~s(id="campaign-missing")
      assert html =~ "No campaign named"
    end
  end
end
