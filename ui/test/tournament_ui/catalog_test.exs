defmodule TournamentUi.CatalogTest do
  use ExUnit.Case, async: false

  # Read-only adapter over the catalog tables (ADR 0001 §2): Python seeds
  # via bin/catalog.py; Elixir must never write or run DDL. These tests
  # exercise the whole read surface plus the older-DB grace path.

  alias TournamentUi.Catalog

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
          cat.create_project(name='side-project')
          cat.create_component(project='unity-explorer', name='client', kind='app')
          cat.create_component(project='unity-explorer', name='renderer', kind='library')
          src1 = cat.create_source(project='unity-explorer', name='repo', kind='git', locator='https://github.com/decentraland/unity-explorer', trust_tier=1)
          src3 = cat.create_source(project='unity-explorer', name='forum', kind='docs', locator='https://forum.example', trust_tier=3)
          from bin.landscape.evidence import EvidenceRef, TrustTier, SourceType, BrowsableLink
          ev1 = EvidenceRef(source_type=SourceType.GIT_REPO, canonical_uri='repo://unity-explorer@abc123', trust_tier=TrustTier.TIER1_SYSTEM, excerpt='commit abc123: fix retry logic\\nsecond line of excerpt', browsable_link=BrowsableLink(label='commit', url='https://github.com/decentraland/unity-explorer/commit/abc123', kind='commit'), why_selected='most recent commit')
          ev3 = EvidenceRef(source_type=SourceType.DOC, canonical_uri='doc://forum/1', trust_tier=TrustTier.TIER3_EXTERNAL, excerpt='Forum post claims crash on load', why_selected='user report')
          d1 = cat.insert_evidence_ref(ev1, source_id=src1)
          d3 = cat.insert_evidence_ref(ev3, source_id=src3)
          from bin.landscape.snapshot import LandscapeSnapshot
          snap = LandscapeSnapshot(project='unity-explorer', created_at='2026-08-17T00:00:00Z', evidence=(ev1, ev3))
          pid = cat.get_project('unity-explorer')['id']
          sd = cat.insert_landscape_snapshot(snap, project_id=pid)
          cat.link_snapshot_evidence(sd, d1)
          cat.link_snapshot_evidence(sd, d3)
          from bin.landscape.pack import build_pack, Role
          for role in (Role.CREATOR, Role.JUDGE, Role.EXECUTOR):
              cat.insert_context_pack(build_pack(snap, role, created_at='2026-08-17T00:00:00Z'))
          print('SNAP=' + sd)
          print('EV1=' + d1)
          print('EV3=' + d3)
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"

    for key <- ["SNAP", "EV1", "EV3"], into: %{} do
      [_, value] = Regex.run(~r/^#{key}=([0-9a-f]{64})$/m, out)
      {key, value}
    end
  end

  describe "with a python-seeded catalog" do
    setup do
      home =
        "/tmp/dt-catalog-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

      File.mkdir_p!(home)
      System.put_env("DATA_TOURNAMENTS_HOME", home)
      digests = seed!(home)
      on_exit(fn -> File.rm_rf(home) end)
      {:ok, digests: digests}
    end

    test "list_projects returns counts per project" do
      projects = Catalog.list_projects()
      assert length(projects) == 2

      ue = Enum.find(projects, &(&1.name == "unity-explorer"))
      assert ue.description == "Client platform"
      assert ue.status == "active"
      assert ue.component_count == 2
      assert ue.source_count == 2
      assert ue.snapshot_count == 1
      assert is_binary(ue.updated_at)

      side = Enum.find(projects, &(&1.name == "side-project"))
      assert side.component_count == 0
      assert side.source_count == 0
      assert side.snapshot_count == 0
    end

    test "get_project returns components, sources with tiers, and snapshots with packs",
         %{digests: digests} do
      project = Catalog.get_project("unity-explorer")
      assert project.name == "unity-explorer"

      assert Enum.map(project.components, &{&1.name, &1.kind}) ==
               [{"client", "app"}, {"renderer", "library"}]

      forum = Enum.find(project.sources, &(&1.name == "forum"))
      assert forum.kind == "docs"
      assert forum.trust_tier == 3
      repo = Enum.find(project.sources, &(&1.name == "repo"))
      assert repo.trust_tier == 1
      assert repo.locator == "https://github.com/decentraland/unity-explorer"

      assert [snap] = project.snapshots
      assert snap.digest == digests["SNAP"]
      assert snap.evidence_count == 2
      assert Enum.map(snap.packs, & &1.role) == ["creator", "executor", "judge"]
      assert Enum.all?(snap.packs, &(byte_size(&1.digest) == 64))
    end

    test "get_project returns nil for unknown names" do
      assert Catalog.get_project("nope") == nil
    end

    test "get_evidence resolves all columns plus excerpt and browsable_url",
         %{digests: digests} do
      ev = Catalog.get_evidence(digests["EV1"])
      assert ev.digest == digests["EV1"]
      assert ev.kind == "git_repo"
      assert ev.locator == "repo://unity-explorer@abc123"
      assert ev.trust_tier == 1
      assert ev.summary == "most recent commit"
      assert ev.excerpt =~ "commit abc123: fix retry logic"
      assert ev.browsable_url == "https://github.com/decentraland/unity-explorer/commit/abc123"
      assert is_binary(ev.captured_at)

      ev3 = Catalog.get_evidence(digests["EV3"])
      assert ev3.trust_tier == 3
      assert ev3.browsable_url == nil

      assert Catalog.get_evidence(
               "0000000000000000000000000000000000000000000000000000000000000000"
             ) ==
               nil
    end

    test "list_packs_for_snapshot returns one pack per role", %{digests: digests} do
      packs = Catalog.list_packs_for_snapshot(digests["SNAP"])
      assert Enum.map(packs, & &1.role) == ["creator", "executor", "judge"]
      assert Enum.all?(packs, &(&1.schema_version == 1))
      assert Catalog.list_packs_for_snapshot("no-such-digest") == []
    end
  end

  describe "older DB without catalog tables" do
    setup do
      home =
        "/tmp/dt-catalog-old-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

      File.mkdir_p!(home)
      System.put_env("DATA_TOURNAMENTS_HOME", home)
      # An empty SQLite file: valid DB, zero tables — the pre-catalog shape.
      {:ok, conn} = Exqlite.Sqlite3.open(Path.join(home, "judgements.db"))
      :ok = Exqlite.Sqlite3.close(conn)
      on_exit(fn -> File.rm_rf(home) end)
      :ok
    end

    test "all readers degrade to empty results, not crashes" do
      assert Catalog.list_projects() == []
      assert Catalog.get_project("unity-explorer") == nil
      assert Catalog.get_evidence(String.duplicate("ab", 32)) == nil
      assert Catalog.list_packs_for_snapshot(String.duplicate("ab", 32)) == []
    end
  end

  test "missing DB file yields empty results" do
    System.put_env(
      "DATA_TOURNAMENTS_HOME",
      "/tmp/dt-catalog-absent-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
    )

    assert Catalog.list_projects() == []
    assert Catalog.get_project("x") == nil
  end
end
