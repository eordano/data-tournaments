defmodule TournamentUi.FixBranches do
  @moduledoc """
  Read-only Elixir adapter over the fix-branch loop tables in the fabric
  SQLite DB (`fix_branch`, `fix_branch_validation`, `fix_branch_review` —
  see bin/fix_branches.py and bin/judgement_schema.sql).

  Python owns the schema and all writes; Elixir reads only and never
  executes DDL. Older fabric DBs may predate these tables entirely — every
  reader treats "no such table" as empty results, never a crash
  (Catalog/WorkflowRuns/Campaigns precedent).

  `list_branches/0` carries, per branch, the LATEST validation summary
  ("RED r/i GREEN g/t GUARD p/t") and the LATEST review decision.
  `get_branch/1` returns the full evidence trail (ALL validation rows, ALL
  review rows) plus `current?` — whether the latest validation actually
  tested the branch's current head (`tested_sha == head_sha`). A branch is
  decidable in the UI only when its status is `validated`, the latest
  validation passed, AND it is current; Python enforces the same rule for
  real on write.
  """

  alias Exqlite.Sqlite3

  # Rendered diffs are capped (bytes) so a pathological patch can never blow
  # up the page; the UI shows an honest "truncated" chip when this bites.
  @diff_cap 200_000

  # Validation logs are capped harder — they are evidence transcripts, not
  # documents; the scorecard shows an honest "truncated" note when this bites.
  @log_cap 100_000

  @branch_columns "id, finding_id, workorder_ref, repo_path, branch_name, " <>
                    "base_sha, head_sha, patch_digest, status, created_at, updated_at"

  @validation_columns "id, fix_branch_id, tested_sha, red_cmd, red_intended, " <>
                        "red_observed, green_cmd, green_total, green_passed, " <>
                        "guard_total, guard_passed, passed, log_digest, created_at"

  @review_columns "id, fix_branch_id, tested_sha, reviewer, decision, rationale, " <>
                    "approval_event_id, created_at"

  @doc """
  All fix branches, newest first (id DESC). Each row carries
  `validation_summary` ("RED r/i GREEN g/t GUARD p/t" from the LATEST
  validation, "—" when none), `validation_passed` (boolean, latest row),
  and `review_decision` (latest review's decision or nil).
  """
  def list_branches do
    sql = "SELECT #{@branch_columns} FROM fix_branch ORDER BY id DESC"

    case query(sql, []) do
      {:ok, rows} ->
        validations = validations_by_branch()
        reviews = reviews_by_branch()

        Enum.map(rows, fn row ->
          branch = branch_to_map(row)
          latest_v = validations |> Map.get(branch.id, []) |> List.last()
          latest_r = reviews |> Map.get(branch.id, []) |> List.last()

          branch
          |> Map.put(:validation_summary, validation_summary(latest_v))
          |> Map.put(:validation_passed, latest_v != nil and latest_v.passed == 1)
          |> Map.put(:review_decision, latest_r && latest_r.decision)
        end)

      _ ->
        []
    end
  end

  @doc """
  One branch by id with its full evidence trail: `validations` (ALL rows,
  oldest first), `reviews` (ALL rows, oldest first), and `current?` — true
  only when the LATEST validation's `tested_sha` equals the branch's
  `head_sha`. Nil when the branch doesn't exist or the tables are missing.

  Also carries the branch's patch as evidence: `diff` is the unified diff
  text read from `$DATA_TOURNAMENTS_HOME/branch-diffs/<patch_digest>.patch`
  (nil when the env, digest, or file is missing — never a crash),
  `changed_files` is a per-file `%{path, additions, deletions}` summary
  parsed from that text, and `diff_truncated?` is true when the rendered
  text was capped at #{@diff_cap} bytes (counts still come from the full
  file, so they stay honest).
  """
  def get_branch(id) when is_integer(id) do
    sql = "SELECT #{@branch_columns} FROM fix_branch WHERE id = ?"

    case query(sql, [id]) do
      {:ok, [row]} ->
        branch = branch_to_map(row)
        validations = branch_validations(branch.id)
        latest = List.last(validations)
        raw_diff = read_raw_diff(branch.patch_digest)
        {diff, truncated?} = cap_diff(raw_diff)

        branch
        |> Map.put(:validations, validations)
        |> Map.put(:reviews, branch_reviews(branch.id))
        |> Map.put(:current?, latest != nil and latest.tested_sha == branch.head_sha)
        |> Map.put(:diff, diff)
        |> Map.put(:changed_files, parse_changed_files(raw_diff))
        |> Map.put(:diff_truncated?, truncated?)
        |> Map.put(:harness_tampered?, harness_tampered?(latest))
        |> Map.put(:authoring, branch_authoring(branch.id))
        |> Map.put(:ship, branch_ship(branch.id))

      _ ->
        nil
    end
  end

  def get_branch(id) when is_binary(id) do
    case Integer.parse(id) do
      {n, ""} -> get_branch(n)
      _ -> nil
    end
  end

  def get_branch(_), do: nil

  # Mirrors bin/fix_branches.py summary: latest validation row only.
  @doc false
  def validation_summary(nil), do: "—"

  def validation_summary(v) do
    "RED #{v.red_observed}/#{v.red_intended} " <>
      "GREEN #{v.green_passed}/#{v.green_total} " <>
      "GUARD #{v.guard_passed}/#{v.guard_total}"
  end

  # ── diff evidence ─────────────────────────────────────────────────────────

  # The unified diff lives beside the DB, keyed by patch_digest (contract
  # with bin/: $DATA_TOURNAMENTS_HOME/branch-diffs/<digest>.patch). Every
  # miss — unset env, nil digest, absent file — is nil, never a crash.
  defp read_raw_diff(nil), do: nil
  defp read_raw_diff(""), do: nil

  defp read_raw_diff(patch_digest) do
    case System.get_env("DATA_TOURNAMENTS_HOME") do
      nil ->
        nil

      home ->
        path = Path.join([home, "branch-diffs", "#{patch_digest}.patch"])

        case File.read(path) do
          {:ok, text} -> text
          _ -> nil
        end
    end
  end

  defp cap_diff(nil), do: {nil, false}

  defp cap_diff(text) do
    if byte_size(text) > @diff_cap do
      {text |> binary_slice(0, @diff_cap) |> trim_partial_utf8(), true}
    else
      {text, false}
    end
  end

  # A byte cap can land mid-codepoint; drop trailing bytes until the string
  # is valid again so the renderer never sees broken UTF-8.
  defp trim_partial_utf8(bin) do
    if String.valid?(bin) or byte_size(bin) == 0 do
      bin
    else
      trim_partial_utf8(binary_slice(bin, 0, byte_size(bin) - 1))
    end
  end

  # Per-file %{path, additions, deletions} from unified diff text. File
  # sections open with "diff --git a/<path> b/<path>"; inside a section,
  # +/- body lines are counted (headers "+++"/"---" excluded). Anything
  # that doesn't parse contributes nothing — this is a summary, not a parser.
  @doc false
  def parse_changed_files(nil), do: []

  def parse_changed_files(text) do
    text
    |> String.split("\n")
    |> Enum.reduce([], fn line, acc ->
      cond do
        String.starts_with?(line, "diff --git ") ->
          [%{path: diff_header_path(line), additions: 0, deletions: 0} | acc]

        acc == [] ->
          acc

        String.starts_with?(line, "+++") or String.starts_with?(line, "---") ->
          acc

        String.starts_with?(line, "+") ->
          [current | rest] = acc
          [%{current | additions: current.additions + 1} | rest]

        String.starts_with?(line, "-") ->
          [current | rest] = acc
          [%{current | deletions: current.deletions + 1} | rest]

        true ->
          acc
      end
    end)
    |> Enum.reverse()
  end

  # "diff --git a/lib/foo.ex b/lib/foo.ex" → "lib/foo.ex" (the b/ side —
  # where the file ends up). Falls back to the raw header when it doesn't
  # match the expected shape.
  defp diff_header_path(line) do
    case Regex.run(~r{^diff --git a/(.+) b/(.+)$}, line) do
      [_, _a, b] -> b
      _ -> String.trim_leading(line, "diff --git ")
    end
  end

  # ── authoring provenance (branch_authoring, sibling contract) ────────────

  # LATEST branch_authoring row for a branch (append-only: corrections are
  # new rows, so the newest one is the truth). Older DBs predate the table —
  # any query failure is nil, never a crash.
  defp branch_authoring(branch_id) do
    sql =
      "SELECT id, fix_branch_id, backend, workorder_ref, base_sha, head_sha, " <>
        "patch_digest, provenance, created_at FROM branch_authoring " <>
        "WHERE fix_branch_id = ? ORDER BY id DESC LIMIT 1"

    case query(sql, [branch_id]) do
      {:ok, [[id, fb_id, backend, workorder_ref, base_sha, head_sha, digest, prov, created_at]]} ->
        %{
          id: id,
          fix_branch_id: fb_id,
          backend: backend,
          workorder_ref: workorder_ref,
          base_sha: base_sha,
          head_sha: head_sha,
          patch_digest: digest,
          provenance: prov,
          created_at: created_at
        }

      _ ->
        nil
    end
  end

  # ── ship record (fix_branch_ship, sibling contract) ──────────────────────

  # LATEST fix_branch_ship row — the workflow a 'shipping' branch rides on.
  # The table is append-only and may not exist yet; nil on any miss.
  defp branch_ship(branch_id) do
    sql =
      "SELECT id, fix_branch_id, workflow_id, tested_sha, requested_by, created_at " <>
        "FROM fix_branch_ship WHERE fix_branch_id = ? ORDER BY id DESC LIMIT 1"

    case query(sql, [branch_id]) do
      {:ok, [[id, fb_id, workflow_id, tested_sha, requested_by, created_at]]} ->
        %{
          id: id,
          fix_branch_id: fb_id,
          workflow_id: workflow_id,
          tested_sha: tested_sha,
          requested_by: requested_by,
          created_at: created_at
        }

      _ ->
        nil
    end
  end

  @doc """
  The LATEST fix_branch_ship row referencing a Temporal workflow id — how
  /runs/show links a release run back to the branch it shipped from. Nil
  when no ship row references the workflow, the table doesn't exist, or
  the id is not a string — never a crash.
  """
  def ship_for_workflow(workflow_id) when is_binary(workflow_id) and workflow_id != "" do
    sql =
      "SELECT id, fix_branch_id, workflow_id, tested_sha, requested_by, created_at " <>
        "FROM fix_branch_ship WHERE workflow_id = ? ORDER BY id DESC LIMIT 1"

    case query(sql, [workflow_id]) do
      {:ok, [[id, fb_id, wf_id, tested_sha, requested_by, created_at]]} ->
        %{
          id: id,
          fix_branch_id: fb_id,
          workflow_id: wf_id,
          tested_sha: tested_sha,
          requested_by: requested_by,
          created_at: created_at
        }

      _ ->
        nil
    end
  end

  def ship_for_workflow(_), do: nil

  # ── harness-tamper refusal evidence ───────────────────────────────────────

  # bin/branch_validator.py refuses BEFORE running any candidate code when
  # base..head touches a protected harness file: it writes a passed=0
  # validation whose stored log STARTS with 'HARNESS-TAMPERED:'. The UI
  # derives the refusal banner from that first line of the LATEST
  # validation's log — no new schema, no fabrication. Any miss (no
  # validation, no digest, no log file) is false, never a crash.
  defp harness_tampered?(nil), do: false

  defp harness_tampered?(validation) do
    case read_log(validation.log_digest) do
      {:ok, text, _truncated?} -> String.starts_with?(text, "HARNESS-TAMPERED")
      _ -> false
    end
  end

  # ── validation log evidence ───────────────────────────────────────────────

  @doc """
  Read a validation log by digest. The catalyrst-run contract stores logs at
  `$DATA_TOURNAMENTS_HOME/branch-logs/<digest>.log`; the branch_validator
  stores them content-addressed in the CAS
  (`$DATA_TOURNAMENTS_HOME/cas/sha256/<first-2-hex>/<hex>`) — both paths are
  tried in that order. Returns `{:ok, text, truncated?}` (capped at
  #{@log_cap} bytes) or `:not_found` — never a crash.
  """
  def read_log(digest) when is_binary(digest) and digest != "" do
    case System.get_env("DATA_TOURNAMENTS_HOME") do
      nil ->
        :not_found

      home ->
        [
          Path.join([home, "branch-logs", "#{digest}.log"]),
          Path.join([home, "cas", "sha256", String.slice(digest, 0, 2), digest])
        ]
        |> Enum.find_value(:not_found, fn path ->
          case File.read(path) do
            {:ok, text} ->
              {capped, truncated?} = cap_log(text)
              {:ok, capped, truncated?}

            _ ->
              nil
          end
        end)
    end
  end

  def read_log(_), do: :not_found

  defp cap_log(text) do
    if byte_size(text) > @log_cap do
      {text |> binary_slice(0, @log_cap) |> trim_partial_utf8(), true}
    else
      {text, false}
    end
  end

  # ── row plumbing ─────────────────────────────────────────────────────────

  defp validations_by_branch do
    sql = "SELECT #{@validation_columns} FROM fix_branch_validation ORDER BY id"

    case query(sql, []) do
      {:ok, rows} ->
        rows
        |> Enum.map(&validation_to_map/1)
        |> Enum.group_by(& &1.fix_branch_id)

      _ ->
        %{}
    end
  end

  defp branch_validations(branch_id) do
    sql =
      "SELECT #{@validation_columns} FROM fix_branch_validation " <>
        "WHERE fix_branch_id = ? ORDER BY id"

    case query(sql, [branch_id]) do
      {:ok, rows} -> Enum.map(rows, &validation_to_map/1)
      _ -> []
    end
  end

  defp reviews_by_branch do
    sql = "SELECT #{@review_columns} FROM fix_branch_review ORDER BY id"

    case query(sql, []) do
      {:ok, rows} ->
        rows
        |> Enum.map(&review_to_map/1)
        |> Enum.group_by(& &1.fix_branch_id)

      _ ->
        %{}
    end
  end

  defp branch_reviews(branch_id) do
    sql =
      "SELECT #{@review_columns} FROM fix_branch_review " <>
        "WHERE fix_branch_id = ? ORDER BY id"

    case query(sql, [branch_id]) do
      {:ok, rows} -> Enum.map(rows, &review_to_map/1)
      _ -> []
    end
  end

  defp branch_to_map([
         id,
         finding_id,
         workorder_ref,
         repo_path,
         branch_name,
         base_sha,
         head_sha,
         patch_digest,
         status,
         created_at,
         updated_at
       ]) do
    %{
      id: id,
      finding_id: finding_id,
      workorder_ref: workorder_ref,
      repo_path: repo_path,
      branch_name: branch_name,
      base_sha: base_sha,
      head_sha: head_sha,
      patch_digest: patch_digest,
      status: status,
      created_at: created_at,
      updated_at: updated_at
    }
  end

  defp validation_to_map([
         id,
         fix_branch_id,
         tested_sha,
         red_cmd,
         red_intended,
         red_observed,
         green_cmd,
         green_total,
         green_passed,
         guard_total,
         guard_passed,
         passed,
         log_digest,
         created_at
       ]) do
    %{
      id: id,
      fix_branch_id: fix_branch_id,
      tested_sha: tested_sha,
      red_cmd: red_cmd,
      red_intended: red_intended,
      red_observed: red_observed,
      green_cmd: green_cmd,
      green_total: green_total,
      green_passed: green_passed,
      guard_total: guard_total,
      guard_passed: guard_passed,
      passed: passed,
      log_digest: log_digest,
      created_at: created_at
    }
  end

  defp review_to_map([
         id,
         fix_branch_id,
         tested_sha,
         reviewer,
         decision,
         rationale,
         approval_event_id,
         created_at
       ]) do
    %{
      id: id,
      fix_branch_id: fix_branch_id,
      tested_sha: tested_sha,
      reviewer: reviewer,
      decision: decision,
      rationale: rationale,
      approval_event_id: approval_event_id,
      created_at: created_at
    }
  end

  # ── private ────────────────────────────────────────────────────────────

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
