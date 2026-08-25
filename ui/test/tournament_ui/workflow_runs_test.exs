defmodule TournamentUi.WorkflowRunsTest do
  use ExUnit.Case, async: false

  # Read-only adapter over the workflow_run Temporal projection: Python
  # (bin/workflow_runs.py) writes; Elixir reads only. Covers list/detail,
  # status filtering, JSON parsing, and both grace paths (no DB at all,
  # DB without the table).

  alias TournamentUi.WorkflowRuns

  defp repo_root, do: File.cwd!() |> Path.join("..") |> Path.expand()

  defp seed!(home) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root()}')
          from bin import workflow_runs as wr
          a = wr.start(temporal_workflow_id='release:unity:abc123', temporal_run_id='run-a', detail={'repo': 'unity'})
          wr.record_stage(a, stage='assemble', status='ok')
          wr.record_stage(a, stage='canary', status='ok')
          wr.record_stage(a, stage='approval', status='pending')
          wr.set_status(a, 'awaiting-approval')
          b = wr.start(temporal_workflow_id='release:unity:def456', temporal_run_id='run-b')
          wr.record_stage(b, stage='assemble', status='failed')
          wr.set_status(b, 'failed')
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
  end

  describe "with python-seeded runs" do
    setup do
      home = "/tmp/dt-wfruns-#{System.unique_integer([:positive])}"
      File.mkdir_p!(home)
      System.put_env("DATA_TOURNAMENTS_HOME", home)
      seed!(home)
      on_exit(fn -> File.rm_rf(home) end)
      :ok
    end

    test "list_runs returns all runs newest first with stage counts" do
      runs = WorkflowRuns.list_runs()
      assert length(runs) == 2

      [newest, oldest] = runs
      assert newest.temporal_workflow_id == "release:unity:def456"
      assert newest.status == "failed"
      assert newest.stage_count == 1
      assert oldest.temporal_workflow_id == "release:unity:abc123"
      assert oldest.status == "awaiting-approval"
      assert oldest.stage_count == 3
    end

    test "list_runs filters by status" do
      runs = WorkflowRuns.list_runs(status: "awaiting-approval")
      assert [%{temporal_workflow_id: "release:unity:abc123"}] = runs
      assert WorkflowRuns.list_runs(status: "done") == []
    end

    test "get_run parses stage_history and detail JSON" do
      run = WorkflowRuns.get_run("release:unity:abc123")
      assert run.status == "awaiting-approval"
      assert run.detail == %{"repo" => "unity"}
      assert run.started_at

      assert [
               %{"stage" => "assemble", "status" => "ok", "at" => _},
               %{"stage" => "canary", "status" => "ok"},
               %{"stage" => "approval", "status" => "pending"}
             ] = run.stage_history
    end

    test "get_run returns nil for unknown workflow id" do
      assert WorkflowRuns.get_run("release:nope:000") == nil
    end
  end

  test "graceful empty when the DB does not exist" do
    home = "/tmp/dt-wfruns-nodb-#{System.unique_integer([:positive])}"
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    on_exit(fn -> File.rm_rf(home) end)

    assert WorkflowRuns.list_runs() == []
    assert WorkflowRuns.get_run("release:unity:abc123") == nil
  end

  test "graceful empty when the workflow_run table is missing" do
    home = "/tmp/dt-wfruns-notable-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    # A zero-byte file is a valid empty SQLite DB: opens fine, prepare
    # fails with "no such table", the adapter returns empty.
    File.write!(Path.join(home, "judgements.db"), "")
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    on_exit(fn -> File.rm_rf(home) end)

    assert WorkflowRuns.list_runs() == []
    assert WorkflowRuns.get_run("release:unity:abc123") == nil
  end
end
