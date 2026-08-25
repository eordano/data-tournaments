defmodule TournamentUi.Domains do
  @moduledoc """
  Read-only Elixir adapter over the `domain` table in the fabric SQLite.

  Writes (create/archive) go through Python (`bin/domains.py`) so the
  Langfuse Prompts side and the SQLite side stay atomic.
  """

  alias Exqlite.Sqlite3

  defmodule Spec do
    defstruct [
      :id,
      :name,
      :description,
      :generator_prompt,
      :judge_prompt,
      :rubric,
      :corpus_source,
      :status,
      :created_at,
      :match_count,
      :pending_human,
      :pending_llm,
      :completed_human,
      :completed_llm,
      :error_count,
      :last_activity
    ]
  end

  @doc "All active domains, newest first."
  def list do
    case query(domain_stats_sql("WHERE d.status='active'", "ORDER BY d.created_at DESC"), []) do
      {:ok, rows} -> Enum.map(rows, &decode/1)
      _ -> []
    end
  end

  @doc "Single domain by name, or nil."
  def get(name) when is_binary(name) do
    case query(domain_stats_sql("WHERE d.name = ?", ""), [name]) do
      {:ok, [row]} -> decode(row)
      _ -> nil
    end
  end

  defp domain_stats_sql(where, order) do
    """
    SELECT d.id, d.name, d.description, d.generator_prompt, d.judge_prompt,
           d.rubric, d.corpus_source, d.status, d.created_at,
           COUNT(DISTINCT p.match_id) AS match_count,
           COALESCE(SUM(CASE WHEN p.status='pending' AND c.rater_type='human' THEN 1 ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN p.status='pending' AND c.rater_type='llm' THEN 1 ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN p.status='done' AND c.rater_type='human' THEN 1 ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN p.status='done' AND c.rater_type='llm' THEN 1 ELSE 0 END), 0),
           COALESCE(SUM(CASE WHEN p.status='error' THEN 1 ELSE 0 END), 0),
           MAX(COALESCE(p.completed_at, p.created_at)) AS last_activity
    FROM domain d
    LEFT JOIN pending_judgement p ON p.domain_id = d.id
    LEFT JOIN job_configuration c ON c.id = p.config_id
    #{where}
    GROUP BY d.id
    #{order}
    """
  end

  defp decode([
         id,
         name,
         description,
         gen,
         jud,
         rubric,
         source_json,
         status,
         created_at,
         match_count,
         pending_human,
         pending_llm,
         completed_human,
         completed_llm,
         error_count,
         last_activity
       ]) do
    %Spec{
      id: id,
      name: name,
      description: description,
      generator_prompt: gen,
      judge_prompt: jud,
      rubric: rubric,
      corpus_source: parse_json(source_json),
      status: status,
      created_at: created_at,
      match_count: match_count || 0,
      pending_human: pending_human || 0,
      pending_llm: pending_llm || 0,
      completed_human: completed_human || 0,
      completed_llm: completed_llm || 0,
      error_count: error_count || 0,
      last_activity: last_activity
    }
  end

  defp parse_json(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, m} -> m
      _ -> %{}
    end
  end

  defp parse_json(_), do: %{}

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
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
