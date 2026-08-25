defmodule TournamentUi.FixBranchesTest do
  use ExUnit.Case, async: false

  # Read-only adapter over the fix-branch loop tables. The schema is owned
  # by Python (bin/judgement_schema.sql + bin/fix_branches.py); these tests
  # seed via RAW SQL that mirrors it so they never depend on the Python
  # module. Covers list summaries, get with the full evidence trail, the
  # current?/stale distinction, and both grace paths (no DB, DB without the
  # tables).

  alias TournamentUi.FixBranches

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
    CREATE TABLE IF NOT EXISTS fix_branch (
      id INTEGER PRIMARY KEY,
      finding_id INTEGER,
      workorder_ref TEXT,
      repo_path TEXT NOT NULL,
      branch_name TEXT NOT NULL,
      base_sha TEXT NOT NULL,
      head_sha TEXT NOT NULL,
      patch_digest TEXT,
      status TEXT NOT NULL DEFAULT 'registered',
      created_at TEXT NOT NULL DEFAULT (datetime('now')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fix_branch_validation (
      id INTEGER PRIMARY KEY,
      fix_branch_id INTEGER NOT NULL,
      tested_sha TEXT NOT NULL,
      red_cmd TEXT,
      red_intended INTEGER NOT NULL DEFAULT 0,
      red_observed INTEGER NOT NULL DEFAULT 0,
      green_cmd TEXT,
      green_total INTEGER NOT NULL DEFAULT 0,
      green_passed INTEGER NOT NULL DEFAULT 0,
      guard_total INTEGER NOT NULL DEFAULT 0,
      guard_passed INTEGER NOT NULL DEFAULT 0,
      passed INTEGER NOT NULL DEFAULT 0,
      log_digest TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fix_branch_review (
      id INTEGER PRIMARY KEY,
      fix_branch_id INTEGER NOT NULL,
      tested_sha TEXT,
      reviewer TEXT NOT NULL,
      decision TEXT NOT NULL,
      rationale TEXT,
      approval_event_id INTEGER,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)
  end

  defp seed!(home) do
    create_tables!(home)

    sql!(home, """
    INSERT INTO fix_branch (id, finding_id, workorder_ref, repo_path, branch_name,
                            base_sha, head_sha, patch_digest, status)
    VALUES (1, 7, 'wo-42', '/repos/unity', 'fix/nre-stale-handle',
            'aaaa111122223333', 'bbbb444455556666', 'deadbeefcafe0123', 'validated'),
           (2, NULL, NULL, '/repos/unity', 'fix/never-validated',
            'cccc111122223333', 'dddd444455556666', NULL, 'registered'),
           (3, 8, 'wo-43', '/repos/unity', 'fix/gone-stale',
            'eeee111122223333', 'ffff444455556666', 'feedface00000000', 'stale');

    -- Branch 1: an older failing run, then a passing run of the CURRENT head.
    INSERT INTO fix_branch_validation (fix_branch_id, tested_sha, red_intended,
                                       red_observed, green_total, green_passed,
                                       guard_total, guard_passed, passed)
    VALUES (1, 'aaaa000000000000', 2, 1, 5, 4, 3, 3, 0),
           (1, 'bbbb444455556666', 2, 2, 5, 5, 3, 3, 1);

    -- Branch 3: its only validation tested a sha that is no longer head.
    INSERT INTO fix_branch_validation (fix_branch_id, tested_sha, red_intended,
                                       red_observed, green_total, green_passed,
                                       guard_total, guard_passed, passed)
    VALUES (3, 'oldsha0000000000', 1, 1, 4, 4, 0, 0, 1);

    INSERT INTO fix_branch_review (fix_branch_id, tested_sha, reviewer, decision, rationale)
    VALUES (1, 'aaaa000000000000', 'esteban', 'needs-changes', 'guard flake'),
           (1, 'bbbb444455556666', 'esteban', 'approve', 'clean run');
    """)
  end

  setup do
    home = "/tmp/dt-fixbranches-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    on_exit(fn -> File.rm_rf(home) end)
    {:ok, home: home}
  end

  # Contract with bin/: the unified diff for a branch lives at
  # $DATA_TOURNAMENTS_HOME/branch-diffs/<patch_digest>.patch.
  defp write_patch!(home, digest, text) do
    dir = Path.join(home, "branch-diffs")
    File.mkdir_p!(dir)
    File.write!(Path.join(dir, "#{digest}.patch"), text)
  end

  # lib/foo.ex: +2 −1, README.md: +1 −0. Includes HTML-special characters
  # so the LiveView escaping test has something to bite on.
  @sample_diff """
  diff --git a/lib/foo.ex b/lib/foo.ex
  index 1111111..2222222 100644
  --- a/lib/foo.ex
  +++ b/lib/foo.ex
  @@ -1,4 +1,5 @@
   defmodule Foo do
  -  def a, do: 1 < 2
  +  def a, do: 2 > 1
  +  def b, do: "<script>&amp;</script>"
   end
  diff --git a/README.md b/README.md
  --- a/README.md
  +++ b/README.md
  @@ -1 +1,2 @@
   # readme
  +new line
  """

  describe "list_branches/0" do
    test "returns branches newest first with latest summaries", %{home: home} do
      seed!(home)

      branches = FixBranches.list_branches()
      assert length(branches) == 3
      assert Enum.map(branches, & &1.id) == [3, 2, 1]

      one = Enum.find(branches, &(&1.id == 1))
      assert one.branch_name == "fix/nre-stale-handle"
      assert one.repo_path == "/repos/unity"
      assert one.status == "validated"
      assert one.head_sha == "bbbb444455556666"
      assert one.base_sha == "aaaa111122223333"
      assert one.finding_id == 7
      assert one.workorder_ref == "wo-42"
      # Summary comes from the LATEST validation row (the passing one).
      assert one.validation_summary == "RED 2/2 GREEN 5/5 GUARD 3/3"
      assert one.validation_passed
      # Decision comes from the LATEST review row.
      assert one.review_decision == "approve"
    end

    test "branch without validations or reviews gets em-dash and nils", %{home: home} do
      seed!(home)

      two = FixBranches.list_branches() |> Enum.find(&(&1.id == 2))
      assert two.validation_summary == "—"
      refute two.validation_passed
      assert two.review_decision == nil
    end

    test "graceful empty when the tables are missing", %{home: home} do
      # DB exists but has none of the fix_branch tables.
      sql!(home, "CREATE TABLE unrelated (id INTEGER PRIMARY KEY);")
      assert FixBranches.list_branches() == []
    end

    test "graceful empty when the DB does not exist" do
      assert FixBranches.list_branches() == []
    end
  end

  describe "get_branch/1" do
    test "returns branch with ALL validation and review rows", %{home: home} do
      seed!(home)

      branch = FixBranches.get_branch(1)
      assert branch.branch_name == "fix/nre-stale-handle"
      assert branch.patch_digest == "deadbeefcafe0123"

      assert [failing, passing] = branch.validations
      assert failing.passed == 0
      assert failing.tested_sha == "aaaa000000000000"
      assert passing.passed == 1
      assert passing.tested_sha == "bbbb444455556666"
      assert passing.guard_passed == 3

      assert [first, second] = branch.reviews
      assert first.decision == "needs-changes"
      assert first.rationale == "guard flake"
      assert second.decision == "approve"
    end

    test "current? is true when the latest validation tested the head", %{home: home} do
      seed!(home)
      assert FixBranches.get_branch(1).current?
    end

    test "current? is false when the head moved past the tested sha", %{home: home} do
      seed!(home)
      refute FixBranches.get_branch(3).current?
    end

    test "current? is false with no validations at all", %{home: home} do
      seed!(home)
      branch = FixBranches.get_branch(2)
      refute branch.current?
      assert branch.validations == []
      assert branch.reviews == []
    end

    test "accepts a string id (route param)", %{home: home} do
      seed!(home)
      assert FixBranches.get_branch("1").id == 1
      assert FixBranches.get_branch("nope") == nil
    end

    test "nil for unknown ids and missing tables", %{home: home} do
      seed!(home)
      assert FixBranches.get_branch(999) == nil

      File.rm_rf!(home)
      File.mkdir_p!(home)
      sql!(home, "CREATE TABLE unrelated (id INTEGER PRIMARY KEY);")
      assert FixBranches.get_branch(1) == nil
    end
  end

  describe "get_branch/1 diff evidence" do
    test "reads the diff from the contract path and parses per-file counts", %{home: home} do
      seed!(home)
      write_patch!(home, "deadbeefcafe0123", @sample_diff)

      branch = FixBranches.get_branch(1)
      assert branch.diff == @sample_diff
      refute branch.diff_truncated?

      assert branch.changed_files == [
               %{path: "lib/foo.ex", additions: 2, deletions: 1},
               %{path: "README.md", additions: 1, deletions: 0}
             ]
    end

    test "missing patch file yields nil diff and empty summary, no crash", %{home: home} do
      seed!(home)

      branch = FixBranches.get_branch(1)
      assert branch.diff == nil
      assert branch.changed_files == []
      refute branch.diff_truncated?
    end

    test "nil patch_digest yields nil diff", %{home: home} do
      seed!(home)

      branch = FixBranches.get_branch(2)
      assert branch.diff == nil
      assert branch.changed_files == []
    end

    test "oversized diff is capped with the truncated flag but honest counts", %{home: home} do
      seed!(home)
      big = "diff --git a/big.txt b/big.txt\n" <> String.duplicate("+x\n", 100_000)
      write_patch!(home, "deadbeefcafe0123", big)

      branch = FixBranches.get_branch(1)
      assert branch.diff_truncated?
      assert byte_size(branch.diff) <= 200_000
      # Counts come from the FULL file, not the capped render.
      assert branch.changed_files == [%{path: "big.txt", additions: 100_000, deletions: 0}]
    end
  end

  describe "parse_changed_files/1" do
    test "nil and non-diff text are empty summaries" do
      assert FixBranches.parse_changed_files(nil) == []
      assert FixBranches.parse_changed_files("not a diff at all\n+stray plus\n") == []
    end
  end

  describe "validation_summary/1" do
    test "formats RED/GREEN/GUARD from a row" do
      assert FixBranches.validation_summary(%{
               red_observed: 2,
               red_intended: 2,
               green_passed: 5,
               green_total: 6,
               guard_passed: 0,
               guard_total: 1
             }) == "RED 2/2 GREEN 5/6 GUARD 0/1"
    end

    test "em-dash for nil" do
      assert FixBranches.validation_summary(nil) == "—"
    end
  end

  describe "read_log/1" do
    @digest "feedc0de" <> String.duplicate("ab", 28)

    test "reads from branch-logs/<digest>.log first", %{home: home} do
      dir = Path.join(home, "branch-logs")
      File.mkdir_p!(dir)
      File.write!(Path.join(dir, "#{@digest}.log"), "primary log body")

      assert {:ok, "primary log body", false} = FixBranches.read_log(@digest)
    end

    test "falls back to the CAS fan-out path", %{home: home} do
      dir = Path.join([home, "cas", "sha256", String.slice(@digest, 0, 2)])
      File.mkdir_p!(dir)
      File.write!(Path.join(dir, @digest), "cas log body")

      assert {:ok, "cas log body", false} = FixBranches.read_log(@digest)
    end

    test "branch-logs wins when both paths exist", %{home: home} do
      File.mkdir_p!(Path.join(home, "branch-logs"))
      File.write!(Path.join([home, "branch-logs", "#{@digest}.log"]), "primary")
      cas_dir = Path.join([home, "cas", "sha256", String.slice(@digest, 0, 2)])
      File.mkdir_p!(cas_dir)
      File.write!(Path.join(cas_dir, @digest), "fallback")

      assert {:ok, "primary", false} = FixBranches.read_log(@digest)
    end

    test "caps oversized logs at 100KB with the truncated flag", %{home: home} do
      dir = Path.join(home, "branch-logs")
      File.mkdir_p!(dir)
      File.write!(Path.join(dir, "#{@digest}.log"), String.duplicate("x", 150_000))

      assert {:ok, text, true} = FixBranches.read_log(@digest)
      assert byte_size(text) == 100_000
    end

    test ":not_found for missing files, nil, and blank digests" do
      assert FixBranches.read_log(@digest) == :not_found
      assert FixBranches.read_log(nil) == :not_found
      assert FixBranches.read_log("") == :not_found
    end
  end

  describe "get_branch/1 authoring + ship (sibling tables)" do
    test "nil authoring/ship when the tables are missing — never a crash", %{home: home} do
      seed!(home)

      branch = FixBranches.get_branch(1)
      assert branch.authoring == nil
      assert branch.ship == nil
    end

    test "carries the LATEST branch_authoring row when present", %{home: home} do
      seed!(home)

      sql!(home, """
      CREATE TABLE branch_authoring (
        id INTEGER PRIMARY KEY,
        fix_branch_id INTEGER NOT NULL,
        backend TEXT NOT NULL,
        workorder_ref TEXT,
        base_sha TEXT NOT NULL,
        head_sha TEXT NOT NULL,
        patch_digest TEXT NOT NULL,
        provenance TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      INSERT INTO branch_authoring (fix_branch_id, backend, workorder_ref, base_sha,
                                    head_sha, patch_digest, provenance)
      VALUES (1, 'fixture', 'wo-42', 'aaaa111122223333', 'oldhead', 'olddigest',
              '{"label": "v1"}'),
             (1, 'command', 'wo-42', 'aaaa111122223333', 'bbbb444455556666',
              'deadbeefcafe0123', '{"argv": ["make", "fix"]}');
      """)

      authoring = FixBranches.get_branch(1).authoring
      assert authoring.backend == "command"
      assert authoring.workorder_ref == "wo-42"
      assert authoring.provenance =~ "argv"
    end

    test "carries the LATEST fix_branch_ship row when present", %{home: home} do
      seed!(home)

      sql!(home, """
      CREATE TABLE fix_branch_ship (
        id INTEGER PRIMARY KEY,
        fix_branch_id INTEGER NOT NULL,
        workflow_id TEXT NOT NULL,
        tested_sha TEXT NOT NULL,
        requested_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      INSERT INTO fix_branch_ship (fix_branch_id, workflow_id, tested_sha, requested_by)
      VALUES (1, 'release:old:1', 'oldhead', 'esteban'),
             (1, 'release:new:2', 'bbbb444455556666', 'esteban');
      """)

      ship = FixBranches.get_branch(1).ship
      assert ship.workflow_id == "release:new:2"
      assert ship.tested_sha == "bbbb444455556666"
      assert ship.requested_by == "esteban"
    end
  end

  describe "ship_for_workflow/1" do
    test "nil when the DB has tables but NOT fix_branch_ship — never a crash", %{home: home} do
      # seed! creates fix_branch/validation/review only: this IS the older
      # data home that predates the ship contract.
      seed!(home)

      assert FixBranches.ship_for_workflow("release:unity:abc") == nil
    end

    test "nil when the data home has no DB at all" do
      # setup created an empty home dir — no judgements.db exists.
      assert FixBranches.ship_for_workflow("release:unity:abc") == nil
    end

    test "returns the LATEST ship row for the workflow id", %{home: home} do
      seed!(home)

      sql!(home, """
      CREATE TABLE fix_branch_ship (
        id INTEGER PRIMARY KEY,
        fix_branch_id INTEGER NOT NULL,
        workflow_id TEXT NOT NULL,
        tested_sha TEXT NOT NULL,
        requested_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      INSERT INTO fix_branch_ship (fix_branch_id, workflow_id, tested_sha, requested_by)
      VALUES (1, 'release:unity:abc', 'firstsha', 'esteban'),
             (1, 'release:unity:abc', 'retrysha', 'esteban'),
             (2, 'release:unity:other', 'othersha', NULL);
      """)

      ship = FixBranches.ship_for_workflow("release:unity:abc")
      assert ship.fix_branch_id == 1
      assert ship.tested_sha == "retrysha"
      assert ship.requested_by == "esteban"

      assert FixBranches.ship_for_workflow("release:unknown:zzz") == nil
    end

    test "nil for non-binary and empty workflow ids" do
      assert FixBranches.ship_for_workflow(nil) == nil
      assert FixBranches.ship_for_workflow("") == nil
      assert FixBranches.ship_for_workflow(123) == nil
    end
  end
end
