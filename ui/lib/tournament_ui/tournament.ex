defmodule TournamentUi.Tournament do
  @moduledoc """
  Read-only adapter over the sqlite tournament DBs produced by
  `/tmp/harness/run-tournament.py`. One DB = one tournament.
  """

  alias Exqlite.Sqlite3

  @default_root "/tmp"

  def list_tournaments(root \\ @default_root) do
    root
    |> File.ls!()
    |> Enum.filter(&String.ends_with?(&1, ".db"))
    |> Enum.map(&Path.join(root, &1))
    |> Enum.flat_map(fn path ->
      case summary(path) do
        {:ok, s} -> [s]
        _ -> []
      end
    end)
    |> Enum.sort_by(& &1.name)
  end

  def summary(db_path) do
    with {:ok, rounds} <- query(db_path, "SELECT MAX(round), COUNT(*) FROM matches"),
         [[max_round, n_matches]] <- rounds do
      {:ok,
       %{
         path: db_path,
         name: Path.basename(db_path, ".db"),
         rounds: max_round || 0,
         matches: n_matches || 0,
         final: final_conclusion(db_path)
       }}
    else
      _ -> :error
    end
  end

  def bracket(db_path) do
    {:ok, rows} =
      query(
        db_path,
        "SELECT id, round, slot, input_a, input_b, is_bye, conclusion FROM matches ORDER BY round, slot"
      )

    matches =
      Enum.map(rows, fn [id, round, slot, a, b, is_bye, concl] ->
        %{
          id: id,
          round: round,
          slot: slot,
          input_a: a,
          input_b: b,
          is_bye: is_bye == 1,
          conclusion: concl,
          title: extract_title(concl),
          ready: concl != nil
        }
      end)

    rounds =
      matches
      |> Enum.group_by(& &1.round)
      |> Enum.sort_by(fn {r, _} -> r end)

    %{rounds: rounds, matches_by_id: Map.new(matches, &{&1.id, &1})}
  end

  def match(db_path, id) do
    {:ok, rows} =
      query(
        db_path,
        "SELECT id, round, slot, input_a, input_b, is_bye, conclusion FROM matches WHERE id = ?",
        [id]
      )

    case rows do
      [[id, round, slot, a, b, is_bye, concl]] ->
        %{
          id: id,
          round: round,
          slot: slot,
          input_a: a,
          input_b: b,
          is_bye: is_bye == 1,
          conclusion: concl,
          title: extract_title(concl)
        }

      _ ->
        nil
    end
  end

  @doc """
  Returns `%{round: N, total: M, done: K}` for the max round, or nil on error.
  Used by the runner to detect true "tournament complete" state.
  """
  def last_round_progress(db_path) do
    try do
      {:ok, [[round, total, done]]} =
        query(db_path, """
        SELECT MAX(round),
               COUNT(*),
               SUM(CASE WHEN conclusion IS NOT NULL THEN 1 ELSE 0 END)
        FROM matches
        WHERE round = (SELECT MAX(round) FROM matches)
        """)

      %{round: round, total: total || 0, done: done || 0}
    rescue
      _ -> nil
    end
  end

  def sample_conclusions(db_path, n \\ 3) do
    {:ok, rows} =
      query(
        db_path,
        "SELECT id, round, slot, conclusion FROM matches WHERE conclusion IS NOT NULL AND is_bye = 0 ORDER BY round, slot"
      )

    rows
    |> Enum.shuffle()
    |> Enum.take(n)
    |> Enum.map(fn [id, round, slot, concl] ->
      %{id: id, label: "R#{round}-#{slot + 1}", conclusion: concl}
    end)
  end

  defp final_conclusion(db_path) do
    {:ok, rows} =
      query(
        db_path,
        "SELECT conclusion FROM matches WHERE round = (SELECT MAX(round) FROM matches) ORDER BY slot LIMIT 1"
      )

    case rows do
      [[c]] when is_binary(c) -> c
      _ -> nil
    end
  end

  defp extract_title(nil), do: "(pending)"

  defp extract_title(concl) do
    concl
    |> String.split("\n", trim: true)
    |> Enum.find(fn line -> String.starts_with?(line, "#") end)
    |> case do
      nil ->
        "(untitled)"

      line ->
        line
        |> String.trim_leading("#")
        |> String.trim()
        |> String.replace_prefix("Match ", "")
        |> String.slice(0, 80)
    end
  end

  defp query(db_path, sql, params \\ []) do
    {:ok, conn} = Sqlite3.open(db_path, mode: :readonly)

    try do
      # Not every *.db under the root is a tournament DB (test data homes,
      # judgement fabrics, ...). A failed prepare (e.g. "no such table:
      # matches") means "not one of ours" — return the error instead of
      # crashing so summary/1's `with` can skip the file.
      case Sqlite3.prepare(conn, sql) do
        {:ok, stmt} ->
          :ok = Sqlite3.bind(stmt, params)
          rows = collect(conn, stmt, [])
          Sqlite3.release(conn, stmt)
          {:ok, Enum.reverse(rows)}

        {:error, reason} ->
          {:error, reason}
      end
    after
      Sqlite3.close(conn)
    end
  end

  defp collect(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, row} -> collect(conn, stmt, [row | acc])
      :done -> acc
    end
  end
end
