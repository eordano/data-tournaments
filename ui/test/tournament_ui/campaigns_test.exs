defmodule TournamentUi.CampaignsTest do
  use ExUnit.Case, async: false

  # Elixir mirror of the bin/campaigns.py ledger rollup: list/get, lens
  # summary derivation (CONFIRM ×N / REFUTE→repaired / open), latest-row
  # validation summary, and graceful empties on missing tables/DBs.
  # Seeding goes through Python (bin.campaigns owns the schema — ADR 0001).

  alias TournamentUi.Campaigns

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

  defp seed_base!(home) do
    py!(home, """
    import bin.catalog as cat
    import bin.campaigns as camp
    cat.init()
    cat.create_project(name='unity-explorer')
    camp.create_campaign(project='unity-explorer', name='bugsweep-aug', kind='bugsweep',
                         objective='crash-class sweep', time_window='sentry 7d',
                         base_commit='abc123def456')
    camp.create_campaign(project='unity-explorer', name='release-r1', kind='release')
    """)
  end

  setup do
    home =
      "/tmp/dt-campaigns-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    on_exit(fn -> File.rm_rf(home) end)

    {:ok, home: home}
  end

  describe "graceful empties" do
    test "missing DB file -> empty list / nil" do
      assert Campaigns.list_campaigns() == []
      assert Campaigns.get_campaign("bugsweep-aug") == nil
    end

    test "DB present but campaign tables missing -> empty, never a crash", %{home: home} do
      File.write!(Path.join(home, "judgements.db"), "")
      assert Campaigns.list_campaigns() == []
      assert Campaigns.get_campaign("bugsweep-aug") == nil
    end
  end

  describe "list_campaigns/0" do
    test "returns campaigns with metadata and per-state finding counts", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash', source_kind='sentry')
      camp.create_finding(campaign='bugsweep-aug', slug='b-leak', source_kind='slack')
      camp.set_finding_state('bugsweep-aug', 'b-leak', 'no_go', no_go_reason='already-fixed')
      """)

      assert [bugsweep, release] = Campaigns.list_campaigns()

      assert bugsweep.name == "bugsweep-aug"
      assert bugsweep.kind == "bugsweep"
      assert bugsweep.status == "active"
      assert bugsweep.objective == "crash-class sweep"
      assert bugsweep.time_window == "sentry 7d"
      assert bugsweep.base_commit == "abc123def456"
      assert bugsweep.counts == %{"candidate" => 1, "no_go" => 1}
      assert bugsweep.finding_count == 2

      assert release.name == "release-r1"
      assert release.kind == "release"
      assert release.counts == %{}
      assert release.finding_count == 0
    end
  end

  describe "get_campaign/1" do
    test "unknown name -> nil", %{home: home} do
      seed_base!(home)
      assert Campaigns.get_campaign("nope") == nil
    end

    test "ledger rows carry state, no_go_reason, root cause, summaries", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash', source_kind='sentry',
                          root_cause='NRE on stale handle')
      camp.create_finding(campaign='bugsweep-aug', slug='b-dupe', source_kind='slack')
      camp.set_finding_state('bugsweep-aug', 'b-dupe', 'no_go', no_go_reason='already-fixed')
      """)

      camp = Campaigns.get_campaign("bugsweep-aug")
      assert camp.name == "bugsweep-aug"
      assert camp.base_commit == "abc123def456"

      assert [a, b] = camp.findings
      assert a.slug == "a-crash"
      assert a.source_kind == "sentry"
      assert a.state == "candidate"
      assert a.no_go_reason == nil
      assert a.root_cause == "NRE on stale handle"
      assert a.lens_summary == "—"
      assert a.validation_summary == "—"

      assert b.slug == "b-dupe"
      assert b.state == "no_go"
      assert b.no_go_reason == "already-fixed"
    end

    test "lens summary: CONFIRM ×N", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash')
      camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='root-cause', verdict='CONFIRM')
      camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='lifecycle', verdict='CONFIRM')
      """)

      assert [%{lens_summary: "CONFIRM ×2"}] =
               Campaigns.get_campaign("bugsweep-aug").findings
    end

    test "lens summary: REFUTE with a repair_of answer -> REFUTE→repaired", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash')
      camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='root-cause', verdict='CONFIRM')
      rid = camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='lifecycle', verdict='REFUTE')
      camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='lifecycle', verdict='CONFIRM',
                            repair_of=rid)
      """)

      assert [%{lens_summary: "CONFIRM ×1 + REFUTE→repaired"}] =
               Campaigns.get_campaign("bugsweep-aug").findings
    end

    test "lens summary: unanswered REFUTE stays open", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash')
      camp.add_lens_verdict('bugsweep-aug', 'a-crash', lens='perf', verdict='REFUTE')
      """)

      assert [%{lens_summary: "REFUTE ×1 open"}] =
               Campaigns.get_campaign("bugsweep-aug").findings
    end

    test "validation summary comes from the LATEST row, with guards", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-crash')
      camp.add_validation_row('bugsweep-aug', 'a-crash', red_intended=5, red_observed=3,
                              green_total=4, green_passed=4)
      camp.add_validation_row('bugsweep-aug', 'a-crash', red_intended=2, red_observed=2,
                              green_total=5, green_passed=5, guards=2)
      """)

      assert [%{validation_summary: "RED 2/2 GREEN 5/5 + 2 guards"}] =
               Campaigns.get_campaign("bugsweep-aug").findings
    end

    test "validation summary renders the PERF segment like python does", %{home: home} do
      seed_base!(home)

      py!(home, """
      import bin.campaigns as camp
      camp.create_finding(campaign='bugsweep-aug', slug='a-perf')
      camp.add_validation_row('bugsweep-aug', 'a-perf', red_intended=1, red_observed=1,
                              green_total=3, green_passed=3,
                              perf=[{'metric': 'allocs', 'measured': 0, 'budget': 0},
                                    {'metric': 'p95_ms', 'measured': 20.0, 'budget': 16.6}])
      """)

      assert [%{validation_summary: "RED 1/1 GREEN 3/3 PERF 1/2"}] =
               Campaigns.get_campaign("bugsweep-aug").findings
    end
  end
end
