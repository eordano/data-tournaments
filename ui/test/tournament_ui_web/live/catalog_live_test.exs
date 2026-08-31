defmodule TournamentUiWeb.CatalogLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # The catalog INDEX moved into /environment?tab=sources (wave-13 §2):
  # /catalog now push_navigates there on mount, and the index assertions
  # below run against the Environment sources tab. The DETAIL page
  # (/catalog/:project) stays a live page: one card per section
  # (components / sources / snapshots), digests shown as 12-char prefixes
  # with the full digest in title.

  defp seed!(home) do
    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root}')
          import bin.catalog as cat
          cat.init()
          cat.create_project(name='unity-explorer', description='Client platform')
          cat.create_component(project='unity-explorer', name='client', kind='app')
          src1 = cat.create_source(project='unity-explorer', name='repo', kind='git', locator='https://github.com/decentraland/unity-explorer', trust_tier=1)
          src3 = cat.create_source(project='unity-explorer', name='forum', kind='docs', locator='https://forum.example', trust_tier=3)
          from bin.landscape.evidence import EvidenceRef, TrustTier, SourceType
          ev1 = EvidenceRef(source_type=SourceType.GIT_REPO, canonical_uri='repo://unity-explorer@abc123', trust_tier=TrustTier.TIER1_SYSTEM, excerpt='commit abc123')
          ev3 = EvidenceRef(source_type=SourceType.DOC, canonical_uri='doc://forum/1', trust_tier=TrustTier.TIER3_EXTERNAL, excerpt='forum post')
          d1 = cat.insert_evidence_ref(ev1, source_id=src1)
          d3 = cat.insert_evidence_ref(ev3, source_id=src3)
          from bin.landscape.snapshot import LandscapeSnapshot
          snap = LandscapeSnapshot(project='unity-explorer', created_at='2026-08-17T00:00:00Z', evidence=(ev1, ev3))
          pid = cat.get_project('unity-explorer')['id']
          sd = cat.insert_landscape_snapshot(snap, project_id=pid)
          cat.link_snapshot_evidence(sd, d1)
          cat.link_snapshot_evidence(sd, d3)
          from bin.landscape.pack import build_pack, Role
          jp = cat.insert_context_pack(build_pack(snap, Role.JUDGE, created_at='2026-08-17T00:00:00Z'))
          print('SNAP=' + sd)
          print('PACK=' + jp)
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"

    for key <- ["SNAP", "PACK"], into: %{} do
      [_, value] = Regex.run(~r/^#{key}=([0-9a-f]{64})$/m, out)
      {key, value}
    end
  end

  describe "with a seeded catalog" do
    setup do
      home =
        "/tmp/dt-catalog-live-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

      File.mkdir_p!(home)
      System.put_env("DATA_TOURNAMENTS_HOME", home)
      digests = seed!(home)
      on_exit(fn -> File.rm_rf(home) end)
      {:ok, digests: digests}
    end

    test "/catalog redirects to the Environment sources tab", %{conn: conn} do
      assert {:error, {:live_redirect, %{to: "/environment?tab=sources"}}} =
               live(conn, "/catalog")
    end

    test "sources tab lists projects with counts, not dumps", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/environment?tab=sources")

      assert html =~ "unity-explorer"
      assert html =~ "Client platform"
      assert html =~ "1 components"
      assert html =~ "2 sources"
      assert html =~ "1 snapshots"
      # Counts only — no source locators or digests on the index surface.
      refute html =~ "forum.example"
    end

    test "detail shows sections, tier badges, and digest prefixes",
         %{conn: conn, digests: digests} do
      {:ok, _view, html} = live(conn, "/catalog/unity-explorer")

      # Components / sources / snapshots sections.
      assert html =~ "Components"
      assert html =~ "client"
      assert html =~ "Sources"
      assert html =~ "TIER1 · system"
      assert html =~ "TIER3 · UNTRUSTED"
      assert html =~ "Recent snapshots"
      assert html =~ "2 evidence refs"

      # 12-char digest prefix rendered, full digest in the title attribute.
      assert html =~ String.slice(digests["SNAP"], 0, 12)
      assert html =~ ~s(title="#{digests["SNAP"]}")
      assert html =~ "judge · #{String.slice(digests["PACK"], 0, 12)}"
      assert html =~ ~s(title="#{digests["PACK"]}")
    end

    test "unknown project shows a graceful empty detail", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/catalog/no-such-project")
      assert html =~ "No project named"
      assert html =~ "no-such-project"
    end
  end

  test "empty state renders when no catalog exists", %{conn: conn} do
    System.put_env(
      "DATA_TOURNAMENTS_HOME",
      "/tmp/dt-catalog-live-empty-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
    )

    {:ok, _view, html} = live(conn, "/environment?tab=sources")
    assert html =~ "No projects in the catalog yet"
  end

  test "nav carries the Environment entry that replaced Catalog" do
    html =
      render_component(&TournamentUiWeb.CoreComponents.workspace_nav/1, current: :environment)

    assert html =~ ~s(href="/environment")
    assert html =~ "Environment"
    assert html =~ ~r/href="\/environment"[^>]*is-active/
    refute html =~ ~s(href="/catalog")
  end
end
