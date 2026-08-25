defmodule TournamentUiWeb.RunsLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # /runs list + /runs/show?id= detail (colon-safe query form; the legacy
  # /runs/:workflow_id path route stays for old bookmarks). Approval panel rules:
  # visible ONLY for awaiting-approval runs; buttons disabled with a hint
  # when DT_OPERATOR is unset; approve click goes through the fail-closed
  # gateway (audit row + stub client delivery + flash).

  alias TournamentUi.Approvals

  @wf "release:unity:abc123"

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

  defp seed_runs!(home) do
    py!(home, """
    from bin import workflow_runs as wr
    a = wr.start(temporal_workflow_id='#{@wf}', temporal_run_id='run-a')
    wr.record_stage(a, stage='assemble', status='ok')
    wr.record_stage(a, stage='canary', status='ok')
    wr.record_stage(a, stage='approval', status='pending')
    wr.set_status(a, 'awaiting-approval')
    b = wr.start(temporal_workflow_id='release:unity:def456', temporal_run_id='run-b')
    wr.record_stage(b, stage='assemble', status='ok')
    wr.set_status(b, 'done')
    """)
  end

  defp seed_policy!(home) do
    py!(home, """
    db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
    db.execute("INSERT INTO policy(name, kind, rule) VALUES ('rel-approvers', 'approval', ?)",
               (json.dumps({'approvers': ['esteban'], 'scope': 'release:*'}),))
    db.commit()
    """)
  end

  defp install_stub!(home, exit_code) do
    stub = Path.join(home, "stub_client.sh")

    # Records $0 too so tests can prove DT_RELEASE_CLIENT_CMD was honored
    # verbatim (the stub path in argv, never bare python3). Failure output
    # goes to stderr — the FAILED banner must carry it.
    File.write!(stub, """
    #!/bin/sh
    echo "$0 $@" >> #{home}/argv.log
    if [ #{exit_code} -ne 0 ]; then
      echo "stub says: delivery failed" >&2
    else
      echo "signal accepted by stub"
    fi
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("DT_RELEASE_CLIENT_CMD", stub)
    stub
  end

  setup do
    home = "/tmp/dt-runs-live-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    System.delete_env("DT_OPERATOR")
    seed_runs!(home)

    on_exit(fn ->
      System.delete_env("DT_OPERATOR")
      System.delete_env("DT_RELEASE_CLIENT_CMD")
      File.rm_rf(home)
    end)

    {:ok, home: home}
  end

  test "index lists runs with status chips, newest first", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs")

    assert html =~ @wf
    assert html =~ "release:unity:def456"
    assert html =~ "awaiting-approval"
    assert html =~ "done"
    assert html =~ "3 stages"
    # Newest (def456) renders before the older run.
    {pos_new, _} = :binary.match(html, "release:unity:def456")
    {pos_old, _} = :binary.match(html, "release:unity:abc123")
    assert pos_new < pos_old
  end

  test "index links use the colon-safe query form of the detail URL", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs")

    assert html =~ ~s(href="/runs/show?id=release%3Aunity%3Aabc123")
    assert html =~ ~s(href="/runs/show?id=release%3Aunity%3Adef456")
  end

  test "direct GET of a colon-bearing run URL works via /runs/show?id=", %{conn: conn} do
    # Regression (wave-9 L5): Temporal workflow ids contain colons
    # (release:x:y); the query-param route must render the run on a
    # direct GET, no path-segment mangling.
    {:ok, _view, html} = live(conn, "/runs/show?id=#{@wf}")

    assert html =~ @wf
    assert html =~ ~s(id="run-timeline")
    assert html =~ "awaiting-approval"
  end

  test "legacy path-segment detail route keeps working", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs/#{@wf}")
    assert html =~ ~s(id="run-timeline")
  end

  test "detail shows the compact stage timeline", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs/show?id=#{@wf}")

    assert html =~ ~s(id="run-timeline")
    assert html =~ "assemble"
    assert html =~ "canary"
    assert html =~ "approval"
    assert html =~ "pending"
  end

  test "approval panel renders only for awaiting-approval runs", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs/show?id=#{@wf}")
    assert html =~ ~s(id="approval-panel")

    {:ok, _view, done_html} = live(conn, "/runs/show?id=release:unity:def456")
    refute done_html =~ ~s(id="approval-panel")
  end

  test "buttons are disabled with a hint when DT_OPERATOR is unset", %{conn: conn} do
    {:ok, view, html} = live(conn, "/runs/show?id=#{@wf}")

    assert html =~ "set" and html =~ "DT_OPERATOR"
    assert view |> element("#approve-button") |> render() =~ "disabled"
    assert view |> element("#reject-button") |> render() =~ "disabled"
  end

  test "approve click delivers via stub client, writes ONE audit row, flashes", %{
    conn: conn,
    home: home
  } do
    seed_policy!(home)
    stub = install_stub!(home, 0)
    System.put_env("DT_OPERATOR", "esteban")

    {:ok, view, html} = live(conn, "/runs/show?id=#{@wf}")
    assert html =~ "esteban"
    refute view |> element("#approve-button") |> render() =~ "disabled"

    html =
      view
      |> form("#approval-form", %{"reason" => "ship it"})
      |> render_submit(%{"decision" => "approve"})

    # Stub client received the exact argv — and argv[0] is the stub path
    # from DT_RELEASE_CLIENT_CMD, honored verbatim, never bare python3.
    argv = File.read!(Path.join(home, "argv.log"))
    assert argv =~ "approve #{@wf} --approver esteban --reason ship it"
    assert argv =~ stub
    refute argv =~ "python3"

    # Exactly ONE audit row exists and renders in the approval history.
    assert [%{decision: "approved", approver: "esteban", reason: "ship it"}] =
             Approvals.list_events(@wf)

    # Flash confirms the recorded decision.
    assert Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info) ==
             "Decision recorded: approved"

    assert html =~ ~s(id="approval-history")
    assert html =~ "ship it"

    # Delivery-status line confirms the accepted Signal from client output.
    assert html =~ ~s(id="delivery-status")
    assert html =~ "approval delivered (signal accepted)"
    assert html =~ "signal accepted by stub"
    refute html =~ ~s(id="approval-error")
  end

  test "denied decision shows the error banner and writes no audit row", %{
    conn: conn,
    home: home
  } do
    # Active policy exists but does not list this operator.
    py!(home, """
    db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
    db.execute("INSERT INTO policy(name, kind, rule) VALUES ('other', 'approval', ?)",
               (json.dumps({'approvers': ['someone-else'], 'scope': '*'}),))
    db.commit()
    """)

    install_stub!(home, 0)
    System.put_env("DT_OPERATOR", "esteban")

    {:ok, view, _html} = live(conn, "/runs/show?id=#{@wf}")

    html =
      view
      |> form("#approval-form", %{"reason" => ""})
      |> render_submit(%{"decision" => "approve"})

    assert html =~ ~s(id="approval-error")
    assert html =~ "Denied:"
    assert html =~ "not an allowlisted approver"
    assert Approvals.list_events(@wf) == []
    refute File.exists?(Path.join(home, "argv.log"))
  end

  test "failed delivery keeps the audit row, shows FAILED banner + Retry button", %{
    conn: conn,
    home: home
  } do
    seed_policy!(home)
    install_stub!(home, 1)
    System.put_env("DT_OPERATOR", "esteban")

    {:ok, view, _html} = live(conn, "/runs/show?id=#{@wf}")

    html =
      view
      |> form("#approval-form", %{"reason" => ""})
      |> render_submit(%{"decision" => "reject"})

    assert html =~ ~s(id="approval-error")
    assert html =~ "Audit recorded, delivery FAILED"
    # The client's stderr rides along in the banner.
    assert html =~ "stub says: delivery failed"
    # Retry re-dispatches ONLY the Signal; no delivered line yet.
    assert html =~ ~s(id="retry-delivery-button")
    refute html =~ ~s(id="delivery-status")
    # Recorded intent survives the failed Signal — by design.
    assert [%{decision: "rejected"}] = Approvals.list_events(@wf)
  end

  test "Retry delivery re-sends ONLY the signal — no second audit row", %{
    conn: conn,
    home: home
  } do
    seed_policy!(home)
    install_stub!(home, 1)
    System.put_env("DT_OPERATOR", "esteban")

    {:ok, view, _html} = live(conn, "/runs/show?id=#{@wf}")

    html =
      view
      |> form("#approval-form", %{"reason" => "ship it"})
      |> render_submit(%{"decision" => "approve"})

    assert html =~ ~s(id="retry-delivery-button")
    assert [%{decision: "approved"}] = Approvals.list_events(@wf)

    # The transient failure clears; the retry click must deliver.
    install_stub!(home, 0)

    html = view |> element("#retry-delivery-button") |> render_click()

    # Still exactly ONE audit row — the retry never records a second event.
    assert [%{decision: "approved", reason: "ship it"}] = Approvals.list_events(@wf)

    # The signal went out again with the same argv shape.
    argv = File.read!(Path.join(home, "argv.log"))
    assert length(String.split(argv, "\n", trim: true)) == 2
    assert argv =~ "approve #{@wf} --approver esteban --reason ship it"

    # Banner gone, delivered line present.
    refute html =~ ~s(id="approval-error")
    assert html =~ ~s(id="delivery-status")
    assert html =~ "approval delivered (signal accepted)"

    assert Phoenix.Flash.get(:sys.get_state(view.pid).socket.assigns.flash, :info) ==
             "Delivery retried: signal accepted"
  end

  test "unknown workflow id renders the empty state", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/runs/show?id=release:none:zzz")
    assert html =~ "No run recorded for"
  end

  # ── wave-13 timeline: full detail, raw JSON, ship linkage, honest gaps ──

  # Overwrite the run's stage_history with raw JSON so tests control the
  # exact shape (string vs map details, missing 'at' timestamps) without
  # depending on what bin/workflow_runs.py chooses to stamp.
  defp set_stage_history!(home, workflow_id, history) do
    py!(home, """
    db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
    db.execute("UPDATE workflow_run SET stage_history=? WHERE temporal_workflow_id=?",
               (json.dumps(#{history}), '#{workflow_id}'))
    db.commit()
    """)
  end

  test "stage detail expands to the FULL text verbatim (incl. DRY-RUN labels)", %{
    conn: conn,
    home: home
  } do
    long_tail = String.duplicate("x", 500)

    set_stage_history!(home, @wf, """
    [
      {'stage': 'assemble', 'status': 'ok', 'at': '2026-08-18T01:00:00Z',
       'detail': 'DRY-RUN: would build unity-bundle @ abc123 <script>alert("d")</script> #{long_tail}'},
      {'stage': 'canary', 'status': 'ok', 'at': '2026-08-18T01:05:00Z',
       'detail': {'replicas': 3, 'note': 'DRY-RUN promote skipped'}},
      {'stage': 'approval', 'status': 'pending', 'at': '2026-08-18T01:06:00Z'}
    ]
    """)

    {:ok, view, html} = live(conn, "/runs/show?id=#{@wf}")

    # Detail toggles exist only for stages that carry detail.
    assert html =~ ~s(id="stage-detail-toggle-0")
    assert html =~ ~s(id="stage-detail-toggle-1")
    refute html =~ ~s(id="stage-detail-toggle-2")
    # Collapsed until asked.
    refute html =~ ~s(id="stage-detail-0")

    # String detail renders verbatim and IN FULL — DRY-RUN label, the whole
    # 500-char tail, and candidate-ish markup escaped, never raw.
    html = view |> element("#stage-detail-toggle-0") |> render_click()
    assert html =~ ~s(id="stage-detail-0")
    assert html =~ "DRY-RUN: would build unity-bundle @ abc123"
    assert html =~ long_tail
    assert html =~ "&lt;script&gt;alert(&quot;d&quot;)&lt;/script&gt;"
    refute html =~ ~s|<script>alert("d")</script>|

    # Map detail pretty-prints as JSON (quotes HEEx-escaped in the output).
    html = view |> element("#stage-detail-toggle-1") |> render_click()
    assert html =~ ~s(id="stage-detail-1")
    assert html =~ "&quot;replicas&quot;: 3"
    assert html =~ "DRY-RUN promote skipped"

    # Toggle off hides the detail again.
    html = view |> element("#stage-detail-toggle-0") |> render_click()
    refute html =~ ~s(id="stage-detail-0")
  end

  test "stage without a timestamp says 'timestamp not recorded' — never fabricated", %{
    conn: conn,
    home: home
  } do
    set_stage_history!(home, @wf, """
    [
      {'stage': 'assemble', 'status': 'ok'},
      {'stage': 'canary', 'status': 'ok', 'at': '2026-08-18T01:05:00Z'}
    ]
    """)

    {:ok, view, _html} = live(conn, "/runs/show?id=#{@wf}")

    assert view |> element("#stage-at-0") |> render() =~ "timestamp not recorded"
    assert view |> element("#stage-at-1") |> render() =~ "2026-08-18T01:05:00Z"
    refute view |> element("#stage-at-1") |> render() =~ "not recorded"
  end

  test "raw status JSON toggle shows the projection payload, nothing synthesized", %{
    conn: conn
  } do
    {:ok, view, html} = live(conn, "/runs/show?id=#{@wf}")

    assert html =~ ~s(id="raw-json-toggle")
    refute html =~ ~s(id="raw-json")

    html = view |> element("#raw-json-toggle") |> render_click()
    assert html =~ ~s(id="raw-json")
    assert html =~ "temporal_workflow_id"
    assert html =~ "release:unity:abc123"
    assert html =~ ~s(&quot;status&quot;: &quot;awaiting-approval&quot;)
    assert html =~ "stage_history"
    assert html =~ "assemble"

    html = view |> element("#raw-json-toggle") |> render_click()
    refute html =~ ~s(id="raw-json")
  end

  test "run shipped from a branch links back via the fix_branch_ship row", %{
    conn: conn,
    home: home
  } do
    # Raw-SQL seed of the sibling ship table — the linkage contract.
    py!(home, """
    db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS fix_branch_ship (
      id INTEGER PRIMARY KEY,
      fix_branch_id INTEGER NOT NULL,
      workflow_id TEXT NOT NULL,
      tested_sha TEXT NOT NULL,
      requested_by TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT INTO fix_branch_ship (fix_branch_id, workflow_id, tested_sha, requested_by)
    VALUES (7, '#{@wf}', 'headaaaa1111', 'esteban');
    ''')
    db.commit()
    """)

    {:ok, view, html} = live(conn, "/runs/show?id=#{@wf}")

    assert html =~ ~s(id="ship-linkage")
    assert html =~ "Shipped from branch"
    link = view |> element("#ship-linkage-link") |> render()
    assert link =~ ~s(href="/branch-fixes/7")
    assert link =~ "#7"
    assert html =~ "tested headaaaa1111"
    assert html =~ "requested by esteban"
  end

  test "run with no ship row has NO origin section — absent, not empty", %{conn: conn} do
    # The fix_branch_ship table doesn't even exist in this data home.
    {:ok, _view, html} = live(conn, "/runs/show?id=#{@wf}")

    refute html =~ ~s(id="ship-linkage")
    refute html =~ "Shipped from branch"
  end
end
