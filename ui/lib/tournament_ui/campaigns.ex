defmodule TournamentUi.Campaigns do
  @moduledoc """
  Read-only Elixir adapter over the campaign/finding spine in the fabric
  SQLite DB (`campaign`, `finding`, `review_lens_verdict`,
  `validation_ledger` — see bin/campaigns.py and bin/judgement_schema.sql).

  ADR 0001 §2: Python (`bin/campaigns.py`) owns the schema and all writes;
  Elixir reads only and never executes DDL. Older fabric DBs may predate
  the campaign tables entirely — every reader treats "no such table" as
  empty results, never a crash (Catalog/WorkflowRuns precedent).

  `get_campaign/1` returns the INDEX.md-shaped ledger: one row per finding
  with the lens summary ("CONFIRM ×2", "REFUTE→repaired", "REFUTE ×1 open")
  derived from `review_lens_verdict` rows exactly like
  `bin/campaigns.py::_lens_summary`, and the validation summary
  ("RED 2/2 GREEN 5/5 + 1 guards") from the LATEST `validation_ledger` row.
  """

  alias Exqlite.Sqlite3

  @campaign_columns "id, name, kind, status, objective, time_window, base_commit, created_at"

  @doc """
  All campaigns (any status), ordered by name, each with per-state finding
  counts (`counts` map, string state keys) and a `finding_count` total.
  """
  def list_campaigns do
    sql = "SELECT #{@campaign_columns} FROM campaign ORDER BY name"

    case query(sql, []) do
      {:ok, rows} ->
        counts = finding_counts()

        Enum.map(rows, fn row ->
          camp = campaign_to_map(row)
          by_state = Map.get(counts, camp.id, %{})

          camp
          |> Map.put(:counts, by_state)
          |> Map.put(:finding_count, by_state |> Map.values() |> Enum.sum())
        end)

      _ ->
        []
    end
  end

  @doc """
  One campaign by name with its full ledger (`findings`): one row per
  finding carrying slug, source_kind, state, no_go_reason, root_cause,
  `lens_summary` and `validation_summary`. Nil when the campaign doesn't
  exist or the tables are missing.
  """
  def get_campaign(name) when is_binary(name) do
    sql = "SELECT #{@campaign_columns} FROM campaign WHERE name = ?"

    case query(sql, [name]) do
      {:ok, [row]} ->
        camp = campaign_to_map(row)
        Map.put(camp, :findings, ledger_rows(camp.id))

      _ ->
        nil
    end
  end

  def get_campaign(_), do: nil

  # ── ledger assembly ──────────────────────────────────────────────────

  defp ledger_rows(campaign_id) do
    sql = """
    SELECT id, slug, source_kind, state, no_go_reason, root_cause
    FROM finding WHERE campaign_id = ? ORDER BY slug
    """

    case query(sql, [campaign_id]) do
      {:ok, rows} ->
        verdicts = verdicts_by_finding(campaign_id)
        validations = validations_by_finding(campaign_id)

        Enum.map(rows, fn [id, slug, source_kind, state, no_go_reason, root_cause] ->
          %{
            slug: slug,
            source_kind: source_kind,
            state: state,
            no_go_reason: no_go_reason,
            root_cause: root_cause,
            lens_summary: lens_summary(Map.get(verdicts, id, [])),
            validation_summary: validation_summary(Map.get(validations, id, []))
          }
        end)

      _ ->
        []
    end
  end

  defp verdicts_by_finding(campaign_id) do
    sql = """
    SELECT v.finding_id, v.id, v.verdict, v.repair_of
    FROM review_lens_verdict v
    JOIN finding f ON f.id = v.finding_id
    WHERE f.campaign_id = ? ORDER BY v.id
    """

    case query(sql, [campaign_id]) do
      {:ok, rows} ->
        rows
        |> Enum.map(fn [fid, id, verdict, repair_of] ->
          {fid, %{id: id, verdict: verdict, repair_of: repair_of}}
        end)
        |> Enum.group_by(fn {fid, _} -> fid end, fn {_, v} -> v end)

      _ ->
        %{}
    end
  end

  defp validations_by_finding(campaign_id) do
    sql = """
    SELECT v.finding_id, v.red_intended, v.red_observed, v.green_total,
           v.green_passed, v.guards
    FROM validation_ledger v
    JOIN finding f ON f.id = v.finding_id
    WHERE f.campaign_id = ? ORDER BY v.id
    """

    case query(sql, [campaign_id]) do
      {:ok, rows} ->
        rows
        |> Enum.map(fn [fid, ri, ro, gt, gp, guards] ->
          {fid,
           %{
             red_intended: ri,
             red_observed: ro,
             green_total: gt,
             green_passed: gp,
             guards: guards
           }}
        end)
        |> Enum.group_by(fn {fid, _} -> fid end, fn {_, v} -> v end)

      _ ->
        %{}
    end
  end

  # Mirrors bin/campaigns.py::_lens_summary: 'CONFIRM ×2',
  # 'CONFIRM ×2 + REFUTE→repaired', 'REFUTE ×2 open', '—'.
  @doc false
  def lens_summary(verdicts) do
    top = Enum.filter(verdicts, &is_nil(&1.repair_of))

    repaired_ids =
      verdicts
      |> Enum.reject(&is_nil(&1.repair_of))
      |> MapSet.new(& &1.repair_of)

    confirms = Enum.count(top, &(&1.verdict == "CONFIRM"))
    refutes = Enum.filter(top, &(&1.verdict == "REFUTE"))
    repaired = Enum.count(refutes, &MapSet.member?(repaired_ids, &1.id))
    open = length(refutes) - repaired

    parts =
      Enum.reject(
        [
          if(confirms > 0, do: "CONFIRM ×#{confirms}"),
          case repaired do
            0 -> nil
            1 -> "REFUTE→repaired"
            n -> "REFUTE ×#{n}→repaired"
          end,
          if(open > 0, do: "REFUTE ×#{open} open")
        ],
        &is_nil/1
      )

    if parts == [], do: "—", else: Enum.join(parts, " + ")
  end

  # Mirrors bin/campaigns.py::_validation_summary — the LATEST run only.
  @doc false
  def validation_summary([]), do: "—"

  def validation_summary(rows) when is_list(rows) do
    v = List.last(rows)

    base =
      "RED #{v.red_observed}/#{v.red_intended} GREEN #{v.green_passed}/#{v.green_total}"

    if v.guards > 0, do: base <> " + #{v.guards} guards", else: base
  end

  # ── private ────────────────────────────────────────────────────────────

  defp finding_counts do
    sql = "SELECT campaign_id, state, COUNT(*) FROM finding GROUP BY campaign_id, state"

    case query(sql, []) do
      {:ok, rows} ->
        Enum.reduce(rows, %{}, fn [cid, state, n], acc ->
          Map.update(acc, cid, %{state => n}, &Map.put(&1, state, n))
        end)

      _ ->
        %{}
    end
  end

  defp campaign_to_map([id, name, kind, status, objective, time_window, base_commit, created_at]) do
    %{
      id: id,
      name: name,
      kind: kind,
      status: status,
      objective: objective,
      time_window: time_window,
      base_commit: base_commit,
      created_at: created_at
    }
  end

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
