defmodule TournamentUi.CampaignHub do
  @moduledoc """
  Read-only assembly of everything a campaign touched, for the
  /campaigns/:name exploration hub (contract:
  docs/design/operator-environment-v13.md §4).

  Linkage rules, most-honest first:

    * fix branches — direct FK chain (finding.campaign_id →
      fix_branch.finding_id). No guessing.
    * release runs — fix_branch_ship rows of the campaign's branches FIRST
      (real recorded linkage: the gateway wrote the workflow id). Only when
      no ship row exists do we fall back to workflow ids that carry the
      campaign name, and every fallback row is tagged `source: :prefix` so
      the UI can caption it honestly.
    * generation domains + bound pipeline — domain names prefixed with the
      campaign name. That is naming convention only; the UI captions these
      sections "linked by domain naming".

  ADR 0001 §2: Python owns the schema and all writes; this module reads
  only. Missing tables/DB are empty sections, never a crash
  (Catalog/Campaigns/FixBranches precedent).
  """

  alias Exqlite.Sqlite3
  alias TournamentUi.Domains
  alias TournamentUi.Environment
  alias TournamentUi.FixBranches
  alias TournamentUi.WorkflowRuns

  @doc """
  Everything the hub renders beyond the ledger, for one campaign map
  (needs `:id` and `:name`): `domains` (name-prefix matched, for the judge
  queue links), `branches` (finding-FK matched fix branches), `releases`
  (`%{workflow_id, branch_id, run, source}` where source is `:ship` or
  `:prefix`), and `bindings` (pipeline bindings of the matched domains).
  """
  def load(%{id: campaign_id, name: campaign_name}) do
    finding_ids = campaign_id |> finding_ids() |> MapSet.new()

    branches =
      Enum.filter(
        FixBranches.list_branches(),
        &(&1.finding_id != nil and MapSet.member?(finding_ids, &1.finding_id))
      )

    domain_names =
      Domains.list()
      |> Enum.map(& &1.name)
      |> Enum.filter(&String.starts_with?(&1, campaign_name))

    %{
      domains: domain_names,
      branches: branches,
      releases: releases(branches, campaign_name),
      bindings: Environment.bindings_for_domains(domain_names)
    }
  end

  # Ship-derived linkage first; prefix fallback only when no ship row exists.
  defp releases(branches, campaign_name) do
    case ship_releases(branches) do
      [] -> prefix_releases(campaign_name)
      ships -> ships
    end
  end

  defp ship_releases(branches) do
    branches
    |> Enum.map(& &1.id)
    |> ship_rows()
    |> Enum.uniq_by(& &1.workflow_id)
    |> Enum.map(fn ship ->
      %{
        workflow_id: ship.workflow_id,
        branch_id: ship.fix_branch_id,
        run: WorkflowRuns.get_run(ship.workflow_id),
        source: :ship
      }
    end)
  end

  defp prefix_releases(campaign_name) do
    WorkflowRuns.list_runs()
    |> Enum.filter(&String.contains?(&1.temporal_workflow_id, campaign_name))
    |> Enum.map(fn run ->
      %{workflow_id: run.temporal_workflow_id, branch_id: nil, run: run, source: :prefix}
    end)
  end

  # ── row plumbing ─────────────────────────────────────────────────────

  defp finding_ids(campaign_id) do
    case query("SELECT id FROM finding WHERE campaign_id = ?", [campaign_id]) do
      {:ok, rows} -> Enum.map(rows, fn [id] -> id end)
      _ -> []
    end
  end

  defp ship_rows([]), do: []

  defp ship_rows(branch_ids) do
    placeholders = branch_ids |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    SELECT id, fix_branch_id, workflow_id, tested_sha, requested_by, created_at
    FROM fix_branch_ship WHERE fix_branch_id IN (#{placeholders})
    ORDER BY id DESC
    """

    case query(sql, branch_ids) do
      {:ok, rows} ->
        Enum.map(rows, fn [id, fb_id, workflow_id, tested_sha, requested_by, created_at] ->
          %{
            id: id,
            fix_branch_id: fb_id,
            workflow_id: workflow_id,
            tested_sha: tested_sha,
            requested_by: requested_by,
            created_at: created_at
          }
        end)

      _ ->
        []
    end
  end

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  # Catalog.query/2 shape: readonly open, busy_timeout; missing tables raise
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
