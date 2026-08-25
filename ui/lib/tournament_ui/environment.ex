defmodule TournamentUi.Environment do
  @moduledoc """
  Read-only Elixir adapter for the environment surfaces that had no Elixir
  reader yet: rubrics (`eval_template`), the pipeline registry (`pipeline` +
  `domain_pipeline`), and policies (`policy`).

  ADR 0001 §2: Python owns the schema and all writes; Elixir reads only and
  never executes DDL. Older fabric DBs may predate these tables entirely —
  every reader distinguishes "table exists but is empty" (`{:ok, []}`) from
  "not initialized in this data home" (`:unavailable`, covering both a
  missing DB file and a missing table) so the UI can render an honest note
  instead of a silent empty list.

  SECURITY: `list_policies/0` surfaces policy NAMES, kind, scope, and
  approver NAMES only. The raw `rule` JSON is parsed for those two keys and
  then dropped — rule bodies are never returned, so secret values can never
  reach a template.
  """

  alias Exqlite.Sqlite3

  @doc """
  All eval_template versions, ordered by name then version DESC. Each row
  carries the wheel-v2 output_definition fields with the documented legacy
  defaults (absent judgement_kind => "pair"; absent subjects =>
  ["execution"]; see bin/judgement.py output_definition v2).
  """
  def list_rubrics do
    sql = """
    SELECT name, version, output_definition, created_at
    FROM eval_template ORDER BY name ASC, version DESC
    """

    case query(sql, []) do
      {:ok, rows} ->
        {:ok,
         Enum.map(rows, fn [name, version, outdef_json, created_at] ->
           outdef = parse_json(outdef_json)

           %{
             name: name,
             version: version,
             judgement_kind: outdef["judgement_kind"] || "pair",
             subjects: normalize_subjects(outdef["subjects"]),
             wheel?: is_map(outdef["wheel"]) and map_size(outdef["wheel"]) > 0,
             verdict_enum: normalize_enum(outdef["verdict_enum"]),
             created_at: created_at
           }
         end)}

      _ ->
        :unavailable
    end
  end

  defp normalize_subjects(subjects) when is_list(subjects), do: subjects
  defp normalize_subjects(_), do: ["execution"]

  defp normalize_enum(values) when is_list(values), do: Enum.filter(values, &is_binary/1)
  defp normalize_enum(_), do: []

  @doc """
  All pipeline versions ordered by (name, version), each with its parsed
  stage list (maps with "key" plus either judgement fields or "action")
  and the definition digest.
  """
  def list_pipelines do
    sql = """
    SELECT name, version, definition, definition_digest, created_at
    FROM pipeline ORDER BY name ASC, version ASC
    """

    case query(sql, []) do
      {:ok, rows} ->
        {:ok,
         Enum.map(rows, fn [name, version, definition_json, digest, created_at] ->
           definition = parse_json(definition_json)

           %{
             name: name,
             version: version,
             digest: digest,
             stages: stages_of(definition),
             created_at: created_at
           }
         end)}

      _ ->
        :unavailable
    end
  end

  defp stages_of(%{"stages" => stages}) when is_list(stages),
    do: Enum.filter(stages, &is_map/1)

  defp stages_of(_), do: []

  @doc "Domain → pipeline bindings, ordered by domain name."
  def list_bindings do
    sql = """
    SELECT d.name, p.name, p.version, dp.created_at
    FROM domain_pipeline dp
    JOIN domain d ON d.id = dp.domain_id
    JOIN pipeline p ON p.id = dp.pipeline_id
    ORDER BY d.name ASC
    """

    case query(sql, []) do
      {:ok, rows} ->
        {:ok,
         Enum.map(rows, fn [domain, pipeline, version, created_at] ->
           %{domain: domain, pipeline: pipeline, version: version, created_at: created_at}
         end)}

      _ ->
        :unavailable
    end
  end

  @doc """
  Pipeline bindings for the given domain names, each with the bound
  pipeline's stage count. Empty list when none (or when tables are missing
  — a campaign hub section, so absence renders as an honest empty).
  """
  def bindings_for_domains([]), do: []

  def bindings_for_domains(domain_names) when is_list(domain_names) do
    placeholders = domain_names |> Enum.map(fn _ -> "?" end) |> Enum.join(", ")

    sql = """
    SELECT d.name, p.name, p.version, p.definition
    FROM domain_pipeline dp
    JOIN domain d ON d.id = dp.domain_id
    JOIN pipeline p ON p.id = dp.pipeline_id
    WHERE d.name IN (#{placeholders})
    ORDER BY d.name ASC
    """

    case query(sql, domain_names) do
      {:ok, rows} ->
        Enum.map(rows, fn [domain, pipeline, version, definition_json] ->
          %{
            domain: domain,
            pipeline: pipeline,
            version: version,
            stage_count: definition_json |> parse_json() |> stages_of() |> length()
          }
        end)

      _ ->
        []
    end
  end

  @doc """
  Policy rows: name, kind, status, plus scope and approver NAMES parsed
  out of the rule JSON. The rule body itself is never returned (no secret
  values, no rule dumps).
  """
  def list_policies do
    sql = "SELECT name, kind, rule, status, created_at FROM policy ORDER BY name ASC"

    case query(sql, []) do
      {:ok, rows} ->
        {:ok,
         Enum.map(rows, fn [name, kind, rule_json, status, created_at] ->
           rule = parse_json(rule_json)

           %{
             name: name,
             kind: kind,
             status: status,
             scope: if(is_binary(rule["scope"]), do: rule["scope"], else: nil),
             approvers: approver_names(rule["approvers"]),
             created_at: created_at
           }
         end)}

      _ ->
        :unavailable
    end
  end

  # Names only; anything malformed contributes nothing (never a dump).
  defp approver_names(approvers) when is_list(approvers),
    do: Enum.filter(approvers, &is_binary/1)

  defp approver_names(_), do: []

  # ── private ────────────────────────────────────────────────────────────

  defp parse_json(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, m} when is_map(m) -> m
      _ -> %{}
    end
  end

  defp parse_json(_), do: %{}

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  # Catalog.query/2 shape: readonly open, busy_timeout; missing tables raise
  # inside prepare, get caught, and callers translate to :unavailable.
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
