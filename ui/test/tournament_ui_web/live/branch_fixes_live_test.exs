defmodule TournamentUiWeb.BranchFixesLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # /branch-fixes index (table with status chips + latest summaries) and
  # /branch-fixes/:id detail (validation/review history + decision panel).
  # Decision rules mirrored client-side for affordance: decidable only when
  # status=validated AND latest validation passed AND it tested the current
  # head; terminal branches hide the panel; failed/stale/registered get an
  # honest note. Decisions dispatch to the fix_branches CLI via
  # FIX_BRANCHES_CLI_CMD (stubbed here to record argv).

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

  # Branch 1: validated, latest validation PASSED the current head → decidable.
  # Branch 2: failed (latest validation failed the current head).
  # Branch 3: validated status BUT the validation tested a superseded sha → stale evidence.
  # Branch 4: approved (terminal).
  defp seed!(home) do
    create_tables!(home)

    sql!(home, """
    INSERT INTO fix_branch (id, finding_id, workorder_ref, repo_path, branch_name,
                            base_sha, head_sha, patch_digest, status)
    VALUES (1, 7, 'wo-42', '/repos/unity', 'fix/decidable',
            'base1111', 'head1111', 'digest111111', 'validated'),
           (2, NULL, NULL, '/repos/unity', 'fix/failing',
            'base2222', 'head2222', 'digest222222', 'failed'),
           (3, 8, 'wo-43', '/repos/unity', 'fix/stale-head',
            'base3333', 'head3333', 'digest333333', 'validated'),
           (4, 9, 'wo-44', '/repos/unity', 'fix/done',
            'base4444', 'head4444', 'digest444444', 'approved');

    INSERT INTO fix_branch_validation (fix_branch_id, tested_sha, red_intended,
                                       red_observed, green_total, green_passed,
                                       guard_total, guard_passed, passed)
    VALUES (1, 'head1111', 2, 2, 5, 5, 3, 3, 1),
           (2, 'head2222', 2, 1, 5, 3, 3, 3, 0),
           (3, 'oldsha33', 1, 1, 4, 4, 0, 0, 1),
           (4, 'head4444', 1, 1, 2, 2, 1, 1, 1);

    INSERT INTO fix_branch_review (fix_branch_id, tested_sha, reviewer, decision, rationale)
    VALUES (4, 'head4444', 'changeme', 'approve', 'shipped it');
    """)
  end

  defp install_stub!(home, exit_code) do
    stub = Path.join(home, "stub_fix_branches.sh")

    File.write!(stub, """
    #!/bin/sh
    echo "$@" >> #{home}/argv.log
    echo "stub says: review exploded" >&2
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("FIX_BRANCHES_CLI_CMD", stub)
  end

  # Ship gateway stub (BRANCH_SHIP_CLI_CMD contract): records argv, emits a
  # configurable stderr line, exits with the given code.
  defp install_ship_stub!(home, exit_code, stderr_line \\ "gateway ok: release queued") do
    stub = Path.join(home, "stub_branch_ship.sh")

    File.write!(stub, """
    #!/bin/sh
    echo "$@" >> #{home}/ship_argv.log
    echo "#{stderr_line}" >&2
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("BRANCH_SHIP_CLI_CMD", stub)
  end

  # Contract with bin/: unified diff for a branch lives at
  # $DATA_TOURNAMENTS_HOME/branch-diffs/<patch_digest>.patch.
  defp write_patch!(home, digest, text) do
    dir = Path.join(home, "branch-diffs")
    File.mkdir_p!(dir)
    File.write!(Path.join(dir, "#{digest}.patch"), text)
  end

  setup do
    home =
      "/tmp/dt-branchfixes-live-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    System.delete_env("DT_OPERATOR")

    on_exit(fn ->
      System.delete_env("DT_OPERATOR")
      System.delete_env("FIX_BRANCHES_CLI_CMD")
      System.delete_env("BRANCH_SHIP_CLI_CMD")
      File.rm_rf(home)
    end)

    {:ok, home: home}
  end

  describe "index" do
    test "empty state is helpful, not a crash", %{conn: conn} do
      {:ok, _view, html} = live(conn, "/branch-fixes")

      assert html =~ ~s(id="branches-empty")
      assert html =~ "No fix branches registered yet"
    end

    test "seeded rows render with color-coded status chips and summaries", %{
      conn: conn,
      home: home
    } do
      seed!(home)

      {:ok, view, html} = live(conn, "/branch-fixes")

      assert html =~ ~s(id="branches-table")
      assert html =~ "fix/decidable"
      assert html =~ "fix/failing"
      assert html =~ "fix/stale-head"
      assert html =~ "fix/done"
      assert html =~ "RED 2/2 GREEN 5/5 GUARD 3/3"
      assert html =~ "RED 1/2 GREEN 3/5 GUARD 3/3"

      # Chip color classes: failed=error, approved=success, validated=info.
      assert view |> element("#branch-2") |> render() =~ "text-error"
      assert view |> element("#branch-4") |> render() =~ "text-success"
      assert view |> element("#branch-1") |> render() =~ "text-info"

      # Latest reviewer decision shows for the reviewed branch.
      assert view |> element("#branch-4") |> render() =~ "approve"
    end

    test "stale branch status renders as a warning chip", %{conn: conn, home: home} do
      seed!(home)
      sql!(home, "UPDATE fix_branch SET status='stale' WHERE id=3;")

      {:ok, view, _html} = live(conn, "/branch-fixes")
      assert view |> element("#branch-3") |> render() =~ "text-warning"
    end
  end

  describe "show" do
    test "renders header, validation scorecard, and review history", %{conn: conn, home: home} do
      seed!(home)

      {:ok, view, html} = live(conn, "/branch-fixes/4")

      assert html =~ ~s(id="branch-header")
      assert html =~ "fix/done"
      assert html =~ "/repos/unity"
      assert html =~ "base4444"
      assert html =~ "head4444"
      assert html =~ "digest444444"
      assert html =~ ~s(id="validation-history")
      # Newest (only) validation renders as an expanded scorecard with
      # labeled RED/GREEN/GUARD cells parsed from the counts columns.
      assert html =~ ~s(id="score-cells-4")
      assert view |> element("#cell-red-4") |> render() =~ "1/1"
      assert view |> element("#cell-green-4") |> render() =~ "2/2"
      assert view |> element("#cell-guard-4") |> render() =~ "1/1"
      assert html =~ ~s(id="review-history")
      assert html =~ "shipped it"
    end

    test "unknown id renders a graceful missing state", %{conn: conn, home: home} do
      seed!(home)

      {:ok, _view, html} = live(conn, "/branch-fixes/999")
      assert html =~ ~s(id="branch-missing")
      assert html =~ "No fix branch with id"
    end

    test "decidable branch shows the decision panel with an enabled approve", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="decision-panel")
      assert html =~ "changeme"
      refute html =~ ~s(id="not-decidable-hint")
      refute view |> element("#approve-button") |> render() =~ "disabled"
      refute view |> element("#reject-button") |> render() =~ "disabled"
      refute view |> element("#needs-changes-button") |> render() =~ "disabled"
    end

    test "approve dispatches the stubbed CLI with the exact argv and flashes", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      install_stub!(home, 0)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      view
      |> form("#decision-form", %{"rationale" => "clean evidence"})
      |> render_submit(%{"decision" => "approve"})

      assert File.read!(Path.join(home, "argv.log")) =~
               "review --id 1 --reviewer changeme --decision approve --rationale clean evidence"

      assert Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info) ==
               "Review recorded: approve"
    end

    test "nonzero CLI exit surfaces stderr in an error banner", %{conn: conn, home: home} do
      seed!(home)
      install_stub!(home, 1)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      html =
        view
        |> form("#decision-form", %{"rationale" => ""})
        |> render_submit(%{"decision" => "reject"})

      assert html =~ ~s(id="decision-error")
      assert html =~ "exit 1"
      assert html =~ "review exploded"
    end

    test "failed branch has no decision panel, only an honest note", %{conn: conn, home: home} do
      seed!(home)

      {:ok, _view, html} = live(conn, "/branch-fixes/2")

      refute html =~ ~s(id="decision-panel")
      refute html =~ ~s(id="approve-button")
      assert html =~ ~s(id="decision-note")
      assert html =~ "cannot be approved"
    end

    test "validated branch with stale evidence disables approve and marks STALE", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      System.put_env("DT_OPERATOR", "changeme")

      # Branch 3 is status=validated but its validation tested oldsha33 while
      # head is head3333 — reject/needs-changes stay available, approve does not.
      {:ok, view, html} = live(conn, "/branch-fixes/3")

      assert html =~ ~s(id="decision-panel")
      assert html =~ "STALE"
      assert html =~ ~s(id="not-decidable-hint")
      assert view |> element("#approve-button") |> render() =~ "disabled"
      refute view |> element("#reject-button") |> render() =~ "disabled"
    end

    test "stale STATUS branch hides all decision controls with a note", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      sql!(home, "UPDATE fix_branch SET status='stale' WHERE id=3;")
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, _view, html} = live(conn, "/branch-fixes/3")

      refute html =~ ~s(id="decision-panel")
      assert html =~ ~s(id="decision-note")
      assert html =~ "Re-validate"
    end

    test "terminal branch hides the decision surface entirely", %{conn: conn, home: home} do
      seed!(home)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, _view, html} = live(conn, "/branch-fixes/4")

      refute html =~ ~s(id="decision-panel")
      refute html =~ ~s(id="decision-note")
      refute html =~ ~s(id="approve-button")
    end

    test "DT_OPERATOR unset disables the controls with a hint", %{conn: conn, home: home} do
      seed!(home)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="decision-panel")
      assert html =~ "set" and html =~ "DT_OPERATOR"
      assert view |> element("#approve-button") |> render() =~ "disabled"
      assert view |> element("#reject-button") |> render() =~ "disabled"
      assert view |> element("#needs-changes-button") |> render() =~ "disabled"
    end
  end

  describe "patch section" do
    # lib/foo.ex: +2 −1, README.md: +1 −0; carries HTML-special chars so the
    # escaping assertion has teeth.
    @sample_diff """
    diff --git a/lib/foo.ex b/lib/foo.ex
    index 1111111..2222222 100644
    --- a/lib/foo.ex
    +++ b/lib/foo.ex
    @@ -1,4 +1,5 @@
     defmodule Foo do
    -  def a, do: 1 < 2
    +  def a, do: 2 > 1
    +  def b, do: "<script>alert('x')</script>"
     end
    diff --git a/README.md b/README.md
    --- a/README.md
    +++ b/README.md
    @@ -1 +1,2 @@
     # readme
    +new line
    """

    test "renders the diff escaped with the changed-files summary", %{conn: conn, home: home} do
      seed!(home)
      write_patch!(home, "digest111111", @sample_diff)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="patch-section")
      # Parseable diff renders the per-file view, not the raw <pre>.
      assert html =~ ~s(id="diff-file-0")
      assert html =~ ~s(id="diff-file-1")
      refute html =~ ~s(id="branch-diff")
      # HTML-special characters in the diff appear escaped, never raw.
      assert html =~ "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;"
      refute html =~ "<script>alert('x')</script>"

      # Changed-files summary: 2 files, +3 −1 total; per-file counts right.
      summary = view |> element("#changed-files-summary") |> render()
      assert summary =~ "2 files, +3 −1"
      files = view |> element("#changed-files") |> render()
      assert files =~ "lib/foo.ex"
      assert files =~ "README.md"

      refute html =~ ~s(id="diff-truncated-chip")
      refute html =~ ~s(id="diff-not-captured")
    end

    test "missing patch file shows 'diff not captured', no crash", %{conn: conn, home: home} do
      seed!(home)

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="patch-section")
      assert html =~ ~s(id="diff-not-captured")
      assert html =~ "Diff not captured"
      refute html =~ ~s(id="branch-diff")
    end

    test "oversized diff shows the truncated chip", %{conn: conn, home: home} do
      seed!(home)
      big = "diff --git a/big.txt b/big.txt\n" <> String.duplicate("+xxxxxxxx\n", 30_000)
      write_patch!(home, "digest111111", big)

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="diff-truncated-chip")
      assert html =~ "TRUNCATED"
    end

    test "multi-file diff renders one card per file with counts and gutter numbers", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      write_patch!(home, "digest111111", @sample_diff)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      # Two file cards, index-keyed ids (never raw paths in DOM ids).
      card0 = view |> element("#diff-file-0") |> render()
      card1 = view |> element("#diff-file-1") |> render()

      # lib/foo.ex: +2 −1; README.md: +1 −0 — per-card badges are honest.
      assert card0 =~ "lib/foo.ex"
      assert card0 =~ "+2"
      assert card0 =~ "−1"
      assert card1 =~ "README.md"
      assert card1 =~ "+1"
      assert card1 =~ "−0"

      # M status chips on both modified files (letter may render padded).
      assert card0 =~ ~r/>\s*M\s*</
      assert card1 =~ ~r/>\s*M\s*</

      # Dual line-number gutters with correct numbers: the deleted line is
      # old-only (old 2), the adds are new-only (new 2, new 3).
      assert card0 =~ "diff-gutter-old"
      assert card0 =~ "diff-gutter-new"
      assert card0 =~ "@@ -1,4 +1,5 @@"
      # Row tints for add/del lines.
      assert card0 =~ "diff-add"
      assert card0 =~ "diff-del"
      # Hunk header row present in README card too.
      assert card1 =~ "@@ -1 +1,2 @@"

      # Collapse toggles + client-only viewed checkboxes exist per card.
      assert html =~ ~s(id="diff-collapse-0")
      assert html =~ ~s(id="diff-collapse-1")
      assert html =~ ~s(id="diff-viewed-0")
      assert html =~ ~s(id="diff-viewed-1")
    end

    test "file tree anchors are index-based and match the card ids", %{conn: conn, home: home} do
      seed!(home)
      write_patch!(home, "digest111111", @sample_diff)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      tree = view |> element("#changed-files") |> render()
      assert tree =~ ~s(href="#diff-file-0")
      assert tree =~ ~s(href="#diff-file-1")
      assert tree =~ ~s(id="file-tree-link-0")
      assert tree =~ ~s(id="file-tree-link-1")
      # Every anchor target exists as a card id.
      assert html =~ ~s(id="diff-file-0")
      assert html =~ ~s(id="diff-file-1")
      # No raw candidate-controlled path is ever interpolated into a DOM id.
      refute html =~ ~s(id="diff-file-lib/foo.ex")
    end

    test "duplicate paths in one diff get distinct index anchors — no collision", %{
      conn: conn,
      home: home
    } do
      seed!(home)

      dup = """
      diff --git a/dup.ex b/dup.ex
      --- a/dup.ex
      +++ b/dup.ex
      @@ -1 +1 @@
      -one
      +uno
      diff --git a/dup.ex b/dup.ex
      --- a/dup.ex
      +++ b/dup.ex
      @@ -5 +5 @@
      -five
      +cinco
      """

      write_patch!(home, "digest111111", dup)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      # Same path twice → two cards under different index ids, two anchors.
      assert html =~ ~s(id="diff-file-0")
      assert html =~ ~s(id="diff-file-1")
      tree = view |> element("#changed-files") |> render()
      assert tree =~ ~s(href="#diff-file-0")
      assert tree =~ ~s(href="#diff-file-1")
      assert view |> element("#diff-file-0") |> render() =~ "uno"
      assert view |> element("#diff-file-1") |> render() =~ "cinco"
    end

    test "every diff line HEEx-escapes candidate-authored markup", %{conn: conn, home: home} do
      seed!(home)

      hostile = """
      diff --git a/evil.html b/evil.html
      --- a/evil.html
      +++ b/evil.html
      @@ -1,2 +1,3 @@
       <div onclick="boom()">
      -<script>alert('pwned')</script>
      +<img src=x onerror="alert(1)">
      +<script>document.location='https://evil'</script>
      """

      write_patch!(home, "digest111111", hostile)

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      # Escaped forms present; raw executable forms absent — for add, del,
      # AND context lines alike.
      assert html =~ "&lt;script&gt;alert(&#39;pwned&#39;)&lt;/script&gt;"
      assert html =~ "&lt;script&gt;document.location=&#39;https://evil&#39;&lt;/script&gt;"
      assert html =~ "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
      assert html =~ "&lt;div onclick=&quot;boom()&quot;&gt;"
      refute html =~ "<script>alert('pwned')</script>"
      refute html =~ "<script>document.location"
      refute html =~ ~s(<img src=x onerror=)
      refute html =~ ~s(<div onclick=)
    end

    test "per-file line cap shows the large-file chip and collapses the body", %{
      conn: conn,
      home: home
    } do
      seed!(home)

      # Over the 2000-line per-file cap but under the 200KB diff byte cap:
      # the PER-FILE chip fires without the page-level TRUNCATED chip.
      lines = Enum.map_join(1..2_100, "\n", fn i -> "+l#{i}" end)

      capped =
        """
        diff --git a/huge.ex b/huge.ex
        --- /dev/null
        +++ b/huge.ex
        @@ -0,0 +1,2100 @@
        """ <> lines <> "\n"

      write_patch!(home, "digest111111", capped)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="diff-file-truncated-0")
      assert html =~ "large file truncated"
      # Honest full-file counts survive the cap.
      assert view |> element("#changed-files-summary") |> render() =~ "1 file, +2100 −0"
      # Body starts collapsed (hidden) with the honest cap note inside.
      assert view |> element("#diff-file-body-0") |> render() =~ "hidden"
      assert html =~ ~s(id="diff-file-truncated-note-0")
      # No page-level byte-cap chip — this is the per-file cap only.
      refute html =~ ~s(id="diff-truncated-chip")
    end

    test "unparseable diff falls back to the raw escaped block with a note", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      write_patch!(home, "digest111111", "not a diff at all <script>alert('raw')</script>\n")

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="diff-unparsed-note")
      assert html =~ "Could not parse this diff"
      assert html =~ ~s(id="branch-diff")
      assert html =~ "&lt;script&gt;alert(&#39;raw&#39;)&lt;/script&gt;"
      refute html =~ "<script>alert('raw')</script>"
      refute html =~ ~s(id="diff-file-0")
    end

    test "harness-tampered validation raises the refusal banner", %{conn: conn, home: home} do
      seed!(home)
      digest = "abad1dea" <> String.duplicate("cd", 28)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{digest}', passed=0 WHERE id=1;")

      dir = Path.join(home, "branch-logs")
      File.mkdir_p!(dir)

      File.write!(
        Path.join(dir, "#{digest}.log"),
        "HARNESS-TAMPERED: protected path tests/conftest.py changed in base..head\n"
      )

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="harness-tampered-banner")
      assert html =~ "Trusted harness file changed"
      assert html =~ "validation refused before"
    end

    test "no tamper banner when the latest log is a normal transcript", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      digest = "abad1dea" <> String.duplicate("cd", 28)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{digest}' WHERE id=1;")

      dir = Path.join(home, "branch-logs")
      File.mkdir_p!(dir)
      File.write!(Path.join(dir, "#{digest}.log"), "RED leg: 2/2 intended failures\n")

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      refute html =~ ~s(id="harness-tampered-banner")
    end
  end

  describe "ship panel" do
    test "approved branch shows the panel with the head sha, disabled without DT_OPERATOR", %{
      conn: conn,
      home: home
    } do
      seed!(home)

      {:ok, view, html} = live(conn, "/branch-fixes/4")

      assert html =~ ~s(id="ship-panel")
      assert view |> element("#ship-head-sha") |> render() =~ "head4444"
      assert view |> element("#ship-button") |> render() =~ "disabled"
      assert html =~ "DT_OPERATOR"
    end

    test "Start release dispatches the ship gateway with the exact argv and flashes", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      # Promote branch 1 to approved so the panel is live for id 1.
      sql!(home, "UPDATE fix_branch SET status='approved' WHERE id=1;")
      install_ship_stub!(home, 0)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      refute view |> element("#ship-button") |> render() =~ "disabled"
      view |> element("#ship-button") |> render_click()

      assert File.read!(Path.join(home, "ship_argv.log")) =~
               "ship --id 1 --requested-by changeme"

      assert Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info) =~
               "Release started:"
    end

    test "gateway refusal surfaces its stderr verbatim in an error banner", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      install_ship_stub!(home, 1, "REFUSED: stale")
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, _html} = live(conn, "/branch-fixes/4")

      html = view |> element("#ship-button") |> render_click()

      assert html =~ ~s(id="ship-error")
      assert html =~ "REFUSED: stale"
      assert html =~ "exit 1"
    end

    test "validated-but-not-approved branch has NO ship panel", %{conn: conn, home: home} do
      seed!(home)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      refute html =~ ~s(id="ship-panel")
      refute html =~ ~s(id="ship-button")
    end

    test "failed branch has NO ship panel", %{conn: conn, home: home} do
      seed!(home)
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, _view, html} = live(conn, "/branch-fixes/2")

      refute html =~ ~s(id="ship-panel")
      refute html =~ ~s(id="ship-button")
    end
  end

  # Contract with the catalyrst run: validation logs live at
  # $DATA_TOURNAMENTS_HOME/branch-logs/<digest>.log (CAS fallback:
  # cas/sha256/<2ch>/<digest>).
  defp write_log!(home, digest, text) do
    dir = Path.join(home, "branch-logs")
    File.mkdir_p!(dir)
    File.write!(Path.join(dir, "#{digest}.log"), text)
  end

  defp write_cas_log!(home, digest, text) do
    dir = Path.join([home, "cas", "sha256", String.slice(digest, 0, 2)])
    File.mkdir_p!(dir)
    File.write!(Path.join(dir, digest), text)
  end

  describe "validation scorecard" do
    @log_digest "feedc0de" <> String.duplicate("ab", 28)

    test "newest card is expanded with sha, CURRENT chip, and cells; older collapse", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      # Give branch 1 a second, newer validation of the current head.
      sql!(home, """
      INSERT INTO fix_branch_validation (id, fix_branch_id, tested_sha, red_intended,
                                         red_observed, green_total, green_passed,
                                         guard_total, guard_passed, passed)
      VALUES (50, 1, 'head1111', 2, 2, 6, 6, 3, 3, 1);
      """)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      # Newest first: card 50 before card 1.
      {pos_new, _} = :binary.match(html, ~s(id="validation-50"))
      {pos_old, _} = :binary.match(html, ~s(id="validation-1"))
      assert pos_new < pos_old

      # Newest card is the expanded scorecard: full tested sha + CURRENT chip.
      expanded = view |> element("#scorecard-50") |> render()
      assert expanded =~ "head1111"
      assert expanded =~ "CURRENT"
      assert expanded =~ "PASS"
      assert view |> element("#cell-red-50") |> render() =~ "2/2"
      assert view |> element("#cell-green-50") |> render() =~ "6/6"
      assert view |> element("#cell-guard-50") |> render() =~ "3/3"

      # Older card collapses to a summary line; clicking expands it.
      assert html =~ ~s(id="scorecard-summary-1")
      refute html =~ ~s(id="scorecard-1")
      html = view |> element("#scorecard-summary-1") |> render_click()
      assert html =~ ~s(id="scorecard-1")
      assert html =~ ~s(id="cell-red-1")
    end

    test "failing counts render error cells and a STALE chip vs head", %{conn: conn, home: home} do
      seed!(home)
      # Branch 2's validation: RED 1/2, GREEN 3/5, GUARD 3/3, tested head2222.
      sql!(home, "UPDATE fix_branch SET head_sha='moved9999' WHERE id=2;")

      {:ok, view, _html} = live(conn, "/branch-fixes/2")

      expanded = view |> element("#scorecard-2") |> render()
      assert expanded =~ "FAIL"
      assert expanded =~ "STALE"
      assert expanded =~ "head2222"
      assert view |> element("#cell-red-2") |> render() =~ "text-error"
      assert view |> element("#cell-green-2") |> render() =~ "text-error"
      assert view |> element("#cell-guard-2") |> render() =~ "text-success"
    end

    test "log toggle reads branch-logs/<digest>.log and renders it escaped", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{@log_digest}' WHERE id=1;")
      write_log!(home, @log_digest, "RED leg output <script>alert('x')</script>\nall good")

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      # Short digest + toggle affordance; log body not rendered until asked.
      assert html =~ "log #{String.slice(@log_digest, 0, 12)}"
      assert html =~ ~s(id="log-toggle-1")
      refute html =~ ~s(id="validation-log-1")

      html = view |> element("#log-toggle-1") |> render_click()
      assert html =~ ~s(id="validation-log-1")
      assert html =~ "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;"
      refute html =~ "<script>alert('x')</script>"
      refute html =~ ~s(id="log-truncated-1")

      # Toggle off hides it again.
      html = view |> element("#log-toggle-1") |> render_click()
      refute html =~ ~s(id="validation-log-1")
    end

    test "log falls back to the CAS path when branch-logs has no file", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{@log_digest}' WHERE id=1;")
      write_cas_log!(home, @log_digest, "cas-stored validation transcript")

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      html = view |> element("#log-toggle-1") |> render_click()
      assert html =~ ~s(id="validation-log-1")
      assert html =~ "cas-stored validation transcript"
    end

    test "oversized log is capped with an honest truncated note", %{conn: conn, home: home} do
      seed!(home)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{@log_digest}' WHERE id=1;")
      write_log!(home, @log_digest, String.duplicate("x", 150_000))

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      html = view |> element("#log-toggle-1") |> render_click()
      assert html =~ ~s(id="log-truncated-1")
      assert html =~ "TRUNCATED"
    end

    test "missing log file shows the honest not-found note", %{conn: conn, home: home} do
      seed!(home)
      sql!(home, "UPDATE fix_branch_validation SET log_digest='#{@log_digest}' WHERE id=1;")

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      html = view |> element("#log-toggle-1") |> render_click()
      assert html =~ ~s(id="log-not-found-1")
      assert html =~ "Log not found"
      refute html =~ ~s(id="validation-log-1")
    end

    test "validation without a log digest says so instead of a dead toggle", %{
      conn: conn,
      home: home
    } do
      seed!(home)

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="no-log-digest-1")
      refute html =~ ~s(id="log-toggle-1")
    end
  end

  describe "authoring provenance" do
    defp create_authoring_table!(home) do
      sql!(home, """
      CREATE TABLE IF NOT EXISTS branch_authoring (
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
      """)
    end

    test "fixture-backed branch shows the FIXTURE chip, workorder, and summary", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      create_authoring_table!(home)

      sql!(home, """
      INSERT INTO branch_authoring (fix_branch_id, backend, workorder_ref, base_sha,
                                    head_sha, patch_digest, provenance)
      VALUES (1, 'fixture', 'wo-42', 'base1111', 'head1111', 'digest111111',
              '{"label": "nre-fix", "files": ["a.cs", "b.cs"]}');
      """)

      {:ok, view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="authoring-provenance")
      prov = view |> element("#authoring-provenance") |> render()
      assert prov =~ "FIXTURE"
      assert prov =~ "text-warning"
      assert prov =~ "workorder wo-42"
      assert prov =~ "label=nre-fix"
      assert prov =~ "files=[2 items]"
    end

    test "command-backed branch gets the blue COMMAND chip", %{conn: conn, home: home} do
      seed!(home)
      create_authoring_table!(home)

      sql!(home, """
      INSERT INTO branch_authoring (fix_branch_id, backend, workorder_ref, base_sha,
                                    head_sha, patch_digest, provenance)
      VALUES (1, 'command', NULL, 'base1111', 'head1111', 'digest111111', NULL);
      """)

      {:ok, view, _html} = live(conn, "/branch-fixes/1")

      prov = view |> element("#authoring-provenance") |> render()
      assert prov =~ "COMMAND"
      assert prov =~ "text-info"
    end

    test "missing branch_authoring table still renders the page", %{conn: conn, home: home} do
      # seed! never creates branch_authoring — this IS the older-DB shape.
      seed!(home)

      {:ok, _view, html} = live(conn, "/branch-fixes/1")

      assert html =~ ~s(id="validation-history")
      refute html =~ ~s(id="authoring-provenance")
    end
  end

  describe "shipping / rolled-back statuses" do
    defp create_ship_table!(home) do
      sql!(home, """
      CREATE TABLE IF NOT EXISTS fix_branch_ship (
        id INTEGER PRIMARY KEY,
        fix_branch_id INTEGER NOT NULL,
        workflow_id TEXT NOT NULL,
        tested_sha TEXT NOT NULL,
        requested_by TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
      );
      """)
    end

    test "shipping branch shows the workflow id, pulsing chip, and NO ship button", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      create_ship_table!(home)

      sql!(home, """
      UPDATE fix_branch SET status='shipping' WHERE id=4;
      INSERT INTO fix_branch_ship (fix_branch_id, workflow_id, tested_sha, requested_by)
      VALUES (4, 'release:fix/done:head4444', 'head4444', 'changeme');
      """)

      System.put_env("DT_OPERATOR", "changeme")
      {:ok, view, html} = live(conn, "/branch-fixes/4")

      assert html =~ ~s(id="shipping-panel")

      assert view |> element("#shipping-workflow-id") |> render() =~
               "release:fix/done:head4444"

      # Info/pulsing status chip; no ship controls, no decision surface.
      assert html =~ "animate-pulse"
      refute html =~ ~s(id="ship-panel")
      refute html =~ ~s(id="ship-button")
      refute html =~ ~s(id="decision-panel")
      refute html =~ ~s(id="decision-note")
    end

    test "shipping branch without a ship row degrades honestly", %{conn: conn, home: home} do
      # No fix_branch_ship table at all — sibling contract may not have
      # landed in this DB; the page must render, never crash.
      seed!(home)
      sql!(home, "UPDATE fix_branch SET status='shipping' WHERE id=4;")

      {:ok, _view, html} = live(conn, "/branch-fixes/4")

      assert html =~ ~s(id="shipping-panel")
      assert html =~ ~s(id="shipping-no-record")
      refute html =~ ~s(id="ship-button")
    end

    test "rolled-back branch shows the honest note and NO controls at all", %{
      conn: conn,
      home: home
    } do
      seed!(home)
      sql!(home, "UPDATE fix_branch SET status='rolled-back' WHERE id=4;")
      System.put_env("DT_OPERATOR", "changeme")

      {:ok, view, html} = live(conn, "/branch-fixes/4")

      assert html =~ ~s(id="rolled-back-note")
      assert html =~ "rolled back"
      assert html =~ "requires fresh validation + approval"
      refute html =~ ~s(id="ship-panel")
      refute html =~ ~s(id="ship-button")
      refute html =~ ~s(id="decision-panel")
      refute html =~ ~s(id="decision-note")
      refute html =~ ~s(id="approve-button")

      # Error chip on the status.
      assert view |> element("#branch-header") |> render() =~ "text-error"
    end
  end
end
