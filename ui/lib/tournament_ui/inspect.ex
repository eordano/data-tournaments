defmodule TournamentUi.Inspect do
  @moduledoc """
  Read-only adapter that exposes the SQLite fabric tables to /inspect.

  Each entity returns a list of maps with stringly-named columns so the
  LiveView can iterate generically. Filters supported per-entity:
    * domains   — none
    * pending   — domain_name (optional), status (optional), run (tournament_db_path)
    * scores    — domain_name (optional), rater_type (optional)
    * prompts   — handled by TournamentUi.LangfusePrompts (not here)
  """

  alias Exqlite.Sqlite3

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  @doc "Counts of rows in each entity, used for the header chips."
  def counts do
    %{
      domains: count("domain"),
      pending: count("pending_judgement"),
      scores: count("score")
    }
  end

  defp count(table) do
    case query("SELECT COUNT(*) FROM #{table}", []) do
      {:ok, [[n]]} -> n
      _ -> 0
    end
  end

  @doc "All active domains."
  def domains do
    sql =
      "SELECT id, name, description, generator_prompt, judge_prompt, " <>
        "rubric, corpus_source, status, created_at " <>
        "FROM domain ORDER BY id"

    case query(sql, []) do
      {:ok, rows} ->
        Enum.map(
          rows,
          &row_to_map(
            [
              :id,
              :name,
              :description,
              :generator_prompt,
              :judge_prompt,
              :rubric,
              :corpus_source,
              :status,
              :created_at
            ],
            &1
          )
        )

      _ ->
        []
    end
  end

  @doc """
  Pending judgements with optional filters.

  Filters: %{domain: name | nil, status: "pending" | "done" | ..., run: tournament_db_path | nil}
  """
  def pending(filters \\ %{}) do
    {where, params} = build_pending_where(filters)

    sql = """
    SELECT pj.id, pj.config_id, pj.tournament_db_path, pj.match_id,
           pj.trace_payload, pj.status, pj.created_at, pj.completed_at,
           pj.domain_id, d.name AS domain_name
    FROM pending_judgement pj
    LEFT JOIN domain d ON d.id = pj.domain_id
    #{where}
    ORDER BY pj.id DESC
    """

    case query(sql, params) do
      {:ok, rows} ->
        cols = [
          :id,
          :config_id,
          :tournament_db_path,
          :match_id,
          :trace_payload,
          :status,
          :created_at,
          :completed_at,
          :domain_id,
          :domain_name
        ]

        Enum.map(rows, &row_to_map(cols, &1))

      _ ->
        []
    end
  end

  defp build_pending_where(filters) do
    clauses = []
    params = []

    {clauses, params} =
      case Map.get(filters, :domain) do
        nil -> {clauses, params}
        "" -> {clauses, params}
        name -> {clauses ++ ["d.name = ?"], params ++ [name]}
      end

    {clauses, params} =
      case Map.get(filters, :status) do
        nil -> {clauses, params}
        "" -> {clauses, params}
        s -> {clauses ++ ["pj.status = ?"], params ++ [s]}
      end

    {clauses, params} =
      case Map.get(filters, :run) do
        nil -> {clauses, params}
        "" -> {clauses, params}
        r -> {clauses ++ ["pj.tournament_db_path = ?"], params ++ [r]}
      end

    where = if clauses == [], do: "", else: "WHERE " <> Enum.join(clauses, " AND ")
    {where, params}
  end

  @doc "Score rows with optional filters."
  def scores(filters \\ %{}) do
    {where, params} = build_score_where(filters)

    sql = """
    SELECT s.id, s.rating_id, s.pending_id, s.template_id, s.rubric_version,
           s.name, s.value, s.rater_type, s.config_id, s.payload,
           s.tournament_db_path, s.match_id, s.created_at,
           d.name AS domain_name
    FROM score s
    LEFT JOIN pending_judgement pj ON pj.id = s.pending_id
    LEFT JOIN domain d ON d.id = pj.domain_id
    #{where}
    ORDER BY s.id DESC
    """

    case query(sql, params) do
      {:ok, rows} ->
        cols = [
          :id,
          :rating_id,
          :pending_id,
          :template_id,
          :rubric_version,
          :name,
          :value,
          :rater_type,
          :config_id,
          :payload,
          :tournament_db_path,
          :match_id,
          :created_at,
          :domain_name
        ]

        Enum.map(rows, &row_to_map(cols, &1))

      _ ->
        []
    end
  end

  defp build_score_where(filters) do
    clauses = []
    params = []

    {clauses, params} =
      case Map.get(filters, :domain) do
        nil -> {clauses, params}
        "" -> {clauses, params}
        name -> {clauses ++ ["d.name = ?"], params ++ [name]}
      end

    {clauses, params} =
      case Map.get(filters, :rater_type) do
        nil -> {clauses, params}
        "" -> {clauses, params}
        r -> {clauses ++ ["s.rater_type = ?"], params ++ [r]}
      end

    where = if clauses == [], do: "", else: "WHERE " <> Enum.join(clauses, " AND ")
    {where, params}
  end

  @doc "Distinct tournament_db_path values for the run-filter dropdown."
  def runs do
    case query(
           "SELECT DISTINCT tournament_db_path FROM pending_judgement ORDER BY tournament_db_path",
           []
         ) do
      {:ok, rows} -> Enum.map(rows, fn [r] -> r end)
      _ -> []
    end
  end

  @doc "Distinct domain names (for filter dropdown)."
  def domain_names do
    case query("SELECT name FROM domain ORDER BY name", []) do
      {:ok, rows} -> Enum.map(rows, fn [r] -> r end)
      _ -> []
    end
  end

  defp row_to_map(cols, values) do
    cols
    |> Enum.zip(values)
    |> Enum.into(%{})
    |> maybe_decode_json()
  end

  defp maybe_decode_json(map) do
    map
    |> decode_field(:trace_payload)
    |> decode_field(:payload)
    |> decode_field(:corpus_source)
  end

  defp decode_field(map, key) do
    case Map.get(map, key) do
      s when is_binary(s) ->
        case Jason.decode(s) do
          {:ok, parsed} -> Map.put(map, key, parsed)
          _ -> map
        end

      _ ->
        map
    end
  end

  defp query(sql, params) do
    if not File.exists?(db_path()) do
      {:error, :no_db}
    else
      {:ok, conn} = Sqlite3.open(db_path(), mode: :readonly)

      try do
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
