defmodule TournamentUi.Runner do
  @moduledoc """
  Spawns `run-tournament.py <config>` as a detached task. Status is derived
  from the log file's mtime and the DB contents (no GenServer bookkeeping).
  """

  defp script, do: TournamentUi.Paths.run_script()
  defp log_dir, do: TournamentUi.Paths.runs_dir()

  def log_path(name), do: Path.join(log_dir(), "#{name}.log")

  def start(config_path, opts \\ []) do
    File.mkdir_p!(log_dir())
    name = Path.basename(config_path, ".json")
    log = log_path(name)

    args =
      if opts[:fresh] do
        [config_path, "--fresh"]
      else
        [config_path]
      end

    Task.start(fn ->
      File.write!(
        log,
        "== starting #{name} at #{DateTime.utc_now()} (fresh=#{!!opts[:fresh]}) ==\n"
      )

      stream = File.stream!(log, [:append])
      System.cmd(script(), args, stderr_to_stdout: true, into: stream)
    end)
  end

  @doc """
  Returns `:done`, `:running`, `:failed`, `:pending`, or `:unknown`.
  - `:done`    — DB exists, max round has single match with a conclusion
  - `:running` — log file was touched in the last 60s
  - `:failed`  — log contains "aborting" or a failed run marker, no final
  - `:pending` — config exists, no DB, no recent log, no log
  """
  def status(%{json: %{"db_path" => db_path, "name" => name}}) do
    cond do
      done?(db_path) -> :done
      running?(name) -> :running
      failed?(name) -> :failed
      true -> :pending
    end
  end

  def status(_), do: :unknown

  defp done?(db_path) do
    case TournamentUi.Tournament.last_round_progress(db_path) do
      %{total: 1, done: 1} -> true
      _ -> false
    end
  end

  defp running?(name) do
    case File.stat(log_path(name)) do
      {:ok, %{mtime: mtime}} ->
        now = :calendar.universal_time() |> :calendar.datetime_to_gregorian_seconds()
        t = :calendar.datetime_to_gregorian_seconds(mtime)
        now - t < 60

      _ ->
        false
    end
  end

  defp failed?(name) do
    case File.read(log_path(name)) do
      {:ok, text} ->
        String.contains?(text, "aborting") or
          (String.contains?(text, "had ") and
             String.contains?(text, "failure(s)"))

      _ ->
        false
    end
  end
end
