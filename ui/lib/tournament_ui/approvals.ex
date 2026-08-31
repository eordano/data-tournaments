defmodule TournamentUi.Approvals do
  @moduledoc """
  Elixir mirror of the approval gateway (bin/approvals.py) — the ONLY
  sanctioned Phoenix path for approval/rejection Signals.

  Enforces, in order (same contract as the Python module):

  1. IDENTITY — the caller supplies a principal. In this single-operator
     local deployment the principal is the `DT_OPERATOR` env var read at
     runtime by the LiveView; blank principals are rejected here.
  2. AUTHORIZATION — `authorize/2` replicates the fail-closed policy check:
     the principal must appear in the approver allowlist of an ACTIVE
     policy row (kind='approval') whose scope glob matches the workflow id.
     No active approval policy means NO approvals (fail closed).
  3. AUDIT — an `approval_event` row is INSERTed via exqlite BEFORE
     delivery (append-only; UPDATE/DELETE blocked by triggers). A failed
     delivery keeps the audit row: it records intent, by design.
  4. DELIVERY — shell-out to the release-workflow client CLI
     (`python3 -m bin.release_workflow.client approve|reject ...`), run
     from the repo root. Overridable via `DT_RELEASE_CLIENT_CMD` so tests
     can point at a stub instead of a live Temporal server.

  Glob matching supports `*` and `?` only — Python's fnmatch additionally
  supports `[seq]` character classes (documented divergence; scopes here
  use plain `release:*` shapes).
  """

  alias Exqlite.Sqlite3

  @approve "approved"
  @reject "rejected"

  @doc """
  Return `{:ok, policy_id}` or `{:error, reason}` (fail closed).

  Mirrors bin/approvals.py authorize(): no principal, no active approval
  policy, malformed/non-object rules, scope mismatch, or an unlisted
  principal all deny.
  """
  def authorize(principal, workflow_id) do
    principal = String.trim(principal || "")

    if principal == "" do
      {:error, "no authenticated principal supplied"}
    else
      case active_approval_policies() do
        [] ->
          {:error,
           "no active approval policy exists — approvals fail closed; " <>
             "create one: bin/catalog.py create-policy --kind approval"}

        policies ->
          find_grant(policies, principal, workflow_id)
      end
    end
  end

  defp find_grant(policies, principal, workflow_id) do
    granted =
      Enum.find_value(policies, fn [id, rule_json] ->
        case decode_rule(rule_json) do
          %{} = rule ->
            scope = validated_scope(rule)
            approvers = validated_approvers(rule["approvers"] || [])

            if is_binary(scope) and glob_match?(scope, workflow_id) and
                 principal in approvers,
               do: id

          _ ->
            # malformed or non-object rule: never treat as a grant
            nil
        end
      end)

    case granted do
      nil ->
        {:error,
         "principal #{inspect(principal)} is not an allowlisted approver " <>
           "for #{inspect(workflow_id)}"}

      id ->
        {:ok, id}
    end
  end

  # Missing scope defaults to "*"; a PRESENT but malformed scope must DENY,
  # never widen (mirrors bin/approvals.py _scope_matches). Scopes using
  # unsupported glob syntax ([seq] character classes) are rejected outright.
  defp validated_scope(rule) do
    case Map.fetch(rule, "scope") do
      :error ->
        "*"

      {:ok, scope} when is_binary(scope) and scope != "" ->
        if String.contains?(scope, "[") or String.contains?(scope, "]"),
          do: nil,
          else: scope

      {:ok, _} ->
        nil
    end
  end

  # Approvers must be a list of non-empty strings — anything else denies.
  # A bare string is rejected on BOTH sides now: in Python `principal in
  # "changeme"` is substring matching and would grant 'chan'
  # (mirrors bin/approvals.py _valid_approvers).
  defp validated_approvers(approvers) when is_list(approvers) do
    if Enum.all?(approvers, &(is_binary(&1) and String.trim(&1) != "")),
      do: approvers,
      else: []
  end

  defp validated_approvers(_), do: []

  defp decode_rule(rule_json) when is_binary(rule_json) do
    case Jason.decode(rule_json) do
      {:ok, %{} = rule} -> rule
      _ -> nil
    end
  end

  defp decode_rule(_), do: nil

  @doc """
  Glob match supporting `*` (any run) and `?` (any single char), anchored
  at both ends — the subset of Python fnmatch semantics our approval
  scopes use.
  """
  def glob_match?(pattern, value) do
    regex =
      pattern
      |> String.graphemes()
      |> Enum.map_join(fn
        "*" -> ".*"
        "?" -> "."
        ch -> Regex.escape(ch)
      end)

    Regex.match?(~r/\A#{regex}\z/s, value)
  end

  @doc """
  Append the audit row (immutable, delete-blocked). Returns the row id.
  Raises on any DB failure — the caller must not proceed to delivery
  without a recorded audit row.
  """
  def record_event!(workflow_id, decision, approver, reason, policy_id)
      when decision in [@approve, @reject] do
    sql =
      "INSERT INTO approval_event(temporal_workflow_id, decision, approver, " <>
        "reason, policy_id) VALUES (?, ?, ?, ?, ?)"

    {:ok, conn} = Sqlite3.open(db_path())

    try do
      :ok = Sqlite3.execute(conn, "PRAGMA busy_timeout = 5000")
      :ok = Sqlite3.execute(conn, "PRAGMA foreign_keys = ON")
      {:ok, stmt} = Sqlite3.prepare(conn, sql)
      :ok = Sqlite3.bind(stmt, [workflow_id, decision, approver, reason, policy_id])
      :done = Sqlite3.step(conn, stmt)
      Sqlite3.release(conn, stmt)
      {:ok, event_id} = Sqlite3.last_insert_rowid(conn)
      event_id
    after
      Sqlite3.close(conn)
    end
  end

  @doc "Approval events for a workflow id, oldest first (audit order)."
  def list_events(workflow_id) when is_binary(workflow_id) do
    sql =
      "SELECT id, temporal_workflow_id, decision, approver, reason, " <>
        "policy_id, created_at FROM approval_event " <>
        "WHERE temporal_workflow_id = ? ORDER BY id"

    case read_query(sql, [workflow_id]) do
      {:ok, rows} ->
        Enum.map(rows, fn [id, wf_id, decision, approver, reason, policy_id, created_at] ->
          %{
            id: id,
            temporal_workflow_id: wf_id,
            decision: decision,
            approver: approver,
            reason: reason,
            policy_id: policy_id,
            created_at: created_at
          }
        end)

      _ ->
        []
    end
  end

  @doc """
  Authorize → audit → deliver. The one sanctioned Phoenix entry point.

  Returns `{:ok, %{event_id: id, decision: d, delivery: :ok | {:failed,
  output}}}` or `{:error, reason}` when authorization denies. Audit is
  written BEFORE delivery — a failed send keeps the recorded intent for
  operator reconciliation and is reported, not rolled back.
  """
  def submit_decision(workflow_id, approved?, principal, reason \\ "") do
    with {:ok, policy_id} <- authorize(principal, workflow_id) do
      decision = if approved?, do: @approve, else: @reject
      principal = String.trim(principal)
      event_id = record_event!(workflow_id, decision, principal, reason, policy_id)

      case deliver_signal(workflow_id, approved?, principal, reason) do
        {:ok, output} ->
          {:ok, %{event_id: event_id, decision: decision, delivery: :ok, delivery_output: output}}

        {:failed, output} ->
          {:ok, %{event_id: event_id, decision: decision, delivery: {:failed, output}}}
      end
    end
  end

  @doc """
  Deliver ONLY the Signal — no audit row. The retry path for a decision
  whose `approval_event` row is already recorded (audit-before-delivery
  means a failed send leaves recorded intent; re-sending must not record
  it twice). Returns `{:ok, output}` or `{:failed, output}`.

  Shells out to the release-workflow client from the repo root.
  `DT_RELEASE_CLIENT_CMD` is used VERBATIM when set (point it at the
  temporalio venv python: `/path/.venv/bin/python -m
  bin.release_workflow.client`); otherwise the default comes from
  Application config `:release_client_cmd` — bare `python3` typically
  lacks the temporalio package, so deployments should set one of the two.
  """
  def deliver_signal(workflow_id, approved?, approver, reason) do
    [cmd | base_args] = String.split(release_client_cmd())

    action = if approved?, do: "approve", else: "reject"

    args =
      base_args ++ [action, workflow_id, "--approver", approver, "--reason", reason]

    case System.cmd(cmd, args, stderr_to_stdout: true, cd: repo_root()) do
      {out, 0} -> {:ok, String.trim(out)}
      {out, _nonzero} -> {:failed, String.trim(out)}
    end
  end

  # Env override wins (verbatim); the Application config carries the
  # deployment default so nothing hardcodes bare `python3` at call sites.
  defp release_client_cmd do
    System.get_env("DT_RELEASE_CLIENT_CMD") ||
      Application.get_env(
        :tournament_ui,
        :release_client_cmd,
        "python3 -m bin.release_workflow.client"
      )
  end

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../..", __DIR__)

  # ── DB plumbing ─────────────────────────────────────────────────────────

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  # Older DB without the policy table: empty list, so authorize fails
  # closed with the no-active-policy error (Python parity).
  defp active_approval_policies do
    sql = "SELECT id, rule FROM policy WHERE kind='approval' AND status='active'"

    case read_query(sql, []) do
      {:ok, rows} -> rows
      _ -> []
    end
  end

  defp read_query(sql, params) do
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
