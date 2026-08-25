defmodule TournamentUi.OptimizerRunner do
  @moduledoc """
  Spawn `bin/optimize.py` / `bin/generate_cards.py` (or any script) as an OS
  process via `Port`, keep its status and a bounded log tail in a public job
  registry, and broadcast progress over Phoenix.PubSub.

  Jobs are durable relative to LiveViews: navigating away neither cancels a
  running OS process nor loses its log — any LiveView can re-subscribe on
  mount and restore the latest job for its `source` from the registry.

  Per-`rubric_lock` locking ensures only one job runs at a time per lock.
  """

  use GenServer

  @locks :optimizer_runner_locks
  @jobs :optimizer_runner_jobs
  @topic "optimizer:jobs"
  @log_tail 200

  defmodule Tables do
    @moduledoc """
    Supervised owner of the runner's named ETS tables, and lifecycle watchdog
    for runner processes.

    ETS tables die with their creating process. Without a stable owner, the
    first LiveView to start a job would own the registry and take it down on
    navigation — exactly the data loss the registry exists to prevent.

    It also monitors every runner pid: if a runner dies without reporting an
    exit status, the watchdog marks the job failed and releases the lock so
    the UI can never show "running…" forever for a dead job.
    """
    use GenServer

    def start_link(opts), do: GenServer.start_link(__MODULE__, opts, name: __MODULE__)

    @doc "Monitor a runner pid so its job cannot stay running after death."
    def watch(pid, lock, meta) do
      case GenServer.whereis(__MODULE__) do
        # Isolated unit tests may bypass the app supervision tree.
        nil -> :ok
        _ -> GenServer.cast(__MODULE__, {:watch, pid, lock, meta})
      end
    end

    @impl true
    def init(_opts) do
      for table <- [:optimizer_runner_locks, :optimizer_runner_jobs],
          :ets.whereis(table) == :undefined,
          do: :ets.new(table, [:named_table, :public, :set])

      {:ok, %{}}
    end

    @impl true
    def handle_cast({:watch, pid, lock, meta}, refs) do
      ref = Process.monitor(pid)
      {:noreply, Map.put(refs, ref, {lock, meta})}
    end

    @impl true
    def handle_info({:DOWN, ref, :process, _pid, reason}, refs) do
      {entry, refs} = Map.pop(refs, ref)

      with {lock, meta} <- entry,
           [{^lock, %{status: :running} = job}] <- :ets.lookup(:optimizer_runner_jobs, lock) do
        # Runner died without an exit report (crash/kill). Normal completion
        # already flipped status to :finished, so this branch never fires
        # for healthy jobs.
        :ets.insert(
          :optimizer_runner_jobs,
          {lock, %{job | status: :finished, exit_status: {:died, reason}}}
        )

        :ets.delete(:optimizer_runner_locks, lock)

        Phoenix.PubSub.broadcast(
          TournamentUi.PubSub,
          "optimizer:jobs",
          {:optimizer_exit, lock, meta, {:died, reason}}
        )
      end

      {:noreply, refs}
    end

    def handle_info(_other, refs), do: {:noreply, refs}
  end

  # ── public API ─────────────────────────────────────────────────────────

  def topic, do: @topic

  @doc """
  Start a job. Options:

    * `:rubric_lock` — lock key; only one job per key runs at a time
    * `:meta` — map describing the job (`:source`, `:kind`, `:domain`, …);
      broadcast with every event and stored in the registry
    * `:cd` — working directory
    * `:parent` — legacy: also send `{:optimizer_line, line}` /
      `{:optimizer_exit, status}` to this pid
  """
  def start(executable, args, opts)
      when is_binary(executable) and is_list(args) and is_list(opts) do
    rubric_lock = Keyword.get(opts, :rubric_lock, :default)
    meta = Keyword.get(opts, :meta, %{})
    ensure_tables()

    resolved =
      cond do
        String.starts_with?(executable, "/") -> {:ok, executable}
        path = System.find_executable(executable) -> {:ok, path}
        true -> {:error, {:executable_not_found, executable}}
      end

    with {:ok, exe_abs} <- resolved,
         true <- :ets.insert_new(@locks, {rubric_lock, meta}) do
      job = %{
        lock: rubric_lock,
        meta: meta,
        status: :running,
        exit_status: nil,
        lines: [],
        started_at: DateTime.utc_now()
      }

      :ets.insert(@jobs, {rubric_lock, job})

      case GenServer.start(__MODULE__, %{
             executable: exe_abs,
             args: args,
             parent: Keyword.get(opts, :parent),
             rubric_lock: rubric_lock,
             meta: meta,
             cd: Keyword.get(opts, :cd)
           }) do
        {:ok, pid} ->
          # The supervised table owner monitors the runner: if it dies
          # without reporting an exit, the job must not stay "running"
          # forever (see Tables.handle_info/2).
          Tables.watch(pid, rubric_lock, meta)
          {:ok, pid}

        {:error, _} = error ->
          # Startup failed after the optimistic registry insert — roll both
          # back or the lock is stuck and the registry shows a phantom
          # running job.
          :ets.delete(@locks, rubric_lock)
          :ets.delete(@jobs, rubric_lock)
          error
      end
    else
      false -> {:error, :already_running}
      {:error, _} = e -> e
    end
  end

  @doc "All registry entries (running and finished), newest first."
  def jobs do
    ensure_tables()

    @jobs
    |> :ets.tab2list()
    |> Enum.map(&elem(&1, 1))
    |> Enum.sort_by(& &1.started_at, {:desc, DateTime})
  end

  @doc "Latest job whose meta.source matches, or nil."
  def latest_job(source) do
    Enum.find(jobs(), &(&1.meta[:source] == source))
  end

  # ── GenServer ──────────────────────────────────────────────────────────

  @impl true
  def init(%{executable: exe, args: args} = s) do
    port_opts =
      [
        :binary,
        :exit_status,
        :stderr_to_stdout,
        :line,
        {:line, 4096},
        {:args, args}
      ] ++ if s[:cd], do: [{:cd, to_charlist(s.cd)}], else: []

    port = Port.open({:spawn_executable, exe}, port_opts)

    {:ok,
     %{
       port: port,
       parent: s.parent,
       rubric_lock: s.rubric_lock,
       meta: s.meta,
       buffer: ""
     }}
  end

  @impl true
  def handle_info({port, {:data, {:eol, line}}}, %{port: port} = state) do
    text = IO.iodata_to_binary(line)
    record_line(state.rubric_lock, text)
    broadcast({:optimizer_line, state.rubric_lock, state.meta, text})
    if state.parent, do: send(state.parent, {:optimizer_line, text})
    {:noreply, state}
  end

  def handle_info({port, {:data, {:noeol, partial}}}, %{port: port} = state) do
    {:noreply, %{state | buffer: state.buffer <> IO.iodata_to_binary(partial)}}
  end

  def handle_info({port, {:exit_status, status}}, %{port: port} = state) do
    if state.buffer != "" do
      record_line(state.rubric_lock, state.buffer)
      broadcast({:optimizer_line, state.rubric_lock, state.meta, state.buffer})
      if state.parent, do: send(state.parent, {:optimizer_line, state.buffer})
    end

    update_job(state.rubric_lock, fn job ->
      %{job | status: :finished, exit_status: status}
    end)

    broadcast({:optimizer_exit, state.rubric_lock, state.meta, status})
    if state.parent, do: send(state.parent, {:optimizer_exit, status})

    # The registry keeps the finished job (status + log tail) so a LiveView
    # mounted later can still show the outcome; only the lock is released.
    if :ets.whereis(@locks) != :undefined, do: :ets.delete(@locks, state.rubric_lock)
    {:stop, :normal, state}
  end

  # ── internals ──────────────────────────────────────────────────────────

  defp record_line(lock, line) do
    update_job(lock, fn job ->
      %{job | lines: Enum.take([line | job.lines], @log_tail)}
    end)
  end

  defp update_job(lock, fun) do
    case :ets.whereis(@jobs) do
      :undefined ->
        :ok

      _ ->
        case :ets.lookup(@jobs, lock) do
          [{^lock, job}] -> :ets.insert(@jobs, {lock, fun.(job)})
          [] -> :ok
        end
    end
  end

  defp broadcast(message) do
    Phoenix.PubSub.broadcast(TournamentUi.PubSub, @topic, message)
  end

  defp ensure_tables do
    # Normal path: tables already exist, owned by the supervised
    # OptimizerRunner.Tables process. The fallback creation only serves
    # isolated unit tests that bypass the application supervision tree —
    # and inherits their transient ownership, which is fine there.
    case GenServer.whereis(Tables) do
      nil ->
        for table <- [@locks, @jobs],
            :ets.whereis(table) == :undefined,
            do: :ets.new(table, [:named_table, :public, :set])

      _pid ->
        :ok
    end

    :ok
  end
end
