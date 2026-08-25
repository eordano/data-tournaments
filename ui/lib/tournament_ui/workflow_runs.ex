defmodule TournamentUi.WorkflowRuns do
  @moduledoc """
  Read-only Elixir adapter over the Python-side `workflow_run` table — the
  queryable projection of Temporal release-workflow state (ADR 0001 §4 step
  6, see bin/workflow_runs.py).

  Temporal is the source of truth; rows are written ONLY by Python Temporal
  Activities. Elixir reads and never executes DDL. Older DBs may predate the
  table entirely — every reader treats "no such table" as empty, never a
  crash (Catalog precedent).
  """

  alias Exqlite.Sqlite3

  @columns "id, spec_digest, temporal_workflow_id, temporal_run_id, status, " <>
             "environment_id, detail, stage_history, started_at, finished_at"

  @doc """
  Workflow runs, newest first (id DESC), optionally filtered by status.

  Options: `status:` (string), `limit:` (default 50). Each row carries a
  `stage_count` derived from the parsed `stage_history`.
  """
  def list_runs(opts \\ []) do
    status = Keyword.get(opts, :status)
    limit = Keyword.get(opts, :limit, 50)

    {where, params} =
      case status do
        nil -> {"", []}
        s -> {" WHERE status = ?", [s]}
      end

    sql =
      "SELECT #{@columns} FROM workflow_run#{where} ORDER BY id DESC LIMIT ?"

    case query(sql, params ++ [limit]) do
      {:ok, rows} -> Enum.map(rows, &row_to_map/1)
      _ -> []
    end
  end

  @doc """
  Newest run for a temporal workflow id (retries/continue-as-new mint new
  run ids), with parsed `detail` and `stage_history`. Nil when unknown or
  when the table is missing.
  """
  def get_run(workflow_id) when is_binary(workflow_id) do
    sql =
      "SELECT #{@columns} FROM workflow_run WHERE temporal_workflow_id = ? " <>
        "ORDER BY id DESC LIMIT 1"

    case query(sql, [workflow_id]) do
      {:ok, [row | _]} -> row_to_map(row)
      _ -> nil
    end
  end

  def get_run(_), do: nil

  # ── private ────────────────────────────────────────────────────────────

  defp row_to_map([
         id,
         spec_digest,
         workflow_id,
         run_id,
         status,
         environment_id,
         detail,
         stage_history,
         started_at,
         finished_at
       ]) do
    stages = decode_list(stage_history)

    %{
      id: id,
      spec_digest: spec_digest,
      temporal_workflow_id: workflow_id,
      temporal_run_id: run_id,
      status: status,
      environment_id: environment_id,
      detail: decode_map(detail),
      stage_history: stages,
      stage_count: length(stages),
      started_at: started_at,
      finished_at: finished_at
    }
  end

  defp decode_map(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, m} when is_map(m) -> m
      _ -> %{}
    end
  end

  defp decode_map(_), do: %{}

  defp decode_list(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, l} when is_list(l) -> l
      _ -> []
    end
  end

  defp decode_list(_), do: []

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  # Catalog.query/2 shape: readonly open, busy_timeout, missing tables raise
  # inside prepare, get caught, and callers translate to empty results.
  defp query(sql, params) do
    if not File.exists?(db_path()) do
      {:error, :no_db}
    else
      {:ok, conn} = Sqlite3.open(db_path(), mode: :readonly)

      try do
        :ok = Sqlite3.execute(conn, "PRAGMA busy_timeout = 5000")
        {:ok, stmt} = Sqlite3.prepare(conn, sql)
        :ok = Sqlite3.bind(stmt, params)
        rows = collect(conn, stmt, [])
        Sqlite3.release(conn, stmt)
        {:ok, Enum.reverse(rows)}
      catch
        kind, value -> {:error, {kind, value}}
      after
        Sqlite3.close(conn)
      end
    end
  end

  defp collect(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, row} -> collect(conn, stmt, [row | acc])
      :done -> acc
    end
  end
end
