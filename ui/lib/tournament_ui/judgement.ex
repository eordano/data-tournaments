defmodule TournamentUi.Judgement do
  @moduledoc """
  Read/write adapter over the central judgement-fabric SQLite DB.

  Path: `${DATA_TOURNAMENTS_HOME}/judgements.db` (default `/tmp/data-tournaments/judgements.db`).

  This module mirrors the Python side at `bin/judgement.py` for fields
  the UI needs:
    * `list_pending/1` → rows the wheel UI offers raters
    * `submit_human/4` → writes 2 Score rows, marks pending row done
    * `list_judgements/1` → rendered for the comparison/listing view
    * `export_jsonl/2` → for the JSONL export controller

  The Python side is the source of truth for schema bootstrap. When the
  fabric DB (or its tables) are missing, `ensure_initialized/0` shells
  out once to the same init the CLI runs (`python3 bin/judgement.py
  init`); on failure the UI degrades to the empty state plus a warning
  banner instead of crashing.

  ## Rubrics are read, never listed

  `pair_rubrics/0` asks the fabric which rubrics are pair-shaped instead
  of naming them. A hand-kept list is what let one page follow a rubric
  rename while its siblings kept querying a name nothing writes any more,
  so exports returned none of the judgements new work produced.
  """

  alias Exqlite.Sqlite3

  # ui/lib/tournament_ui → repo root is three levels up (dev fallback only:
  # release builds bake DATA_TOURNAMENTS_REPO at compile time).
  @repo_root System.get_env("DATA_TOURNAMENTS_REPO") ||
               Path.expand("../../..", __DIR__)

  def repo_root, do: @repo_root

  def db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  def db_exists?, do: File.exists?(db_path())

  @pair_rubric_sql """
  SELECT DISTINCT name FROM eval_template
  WHERE COALESCE(json_extract(output_definition, '$.judgement_kind'), 'pair') = 'pair'
  ORDER BY name
  """

  @doc """
  Every pair-shaped rubric the fabric holds, alphabetically.

  Derived from `eval_template`, using the same default
  `bin/judgement.py`'s `normalize_output_definition/1` applies: a template
  with no `judgement_kind` predates the field and is a pair rubric. This
  is the ONLY rubric list in the UI — the standings scope, the results
  page and the export all read it, so a rubric that moves cannot leave one
  surface behind.
  """
  def pair_rubrics do
    case query(@pair_rubric_sql, []) do
      {:ok, rows} -> Enum.map(rows, fn [name] -> name end)
      _ -> []
    end
  end

  @default_rubric_sql """
  SELECT dflt_value FROM pragma_table_info('domain') WHERE name = 'rubric'
  """

  @doc """
  The pair rubric a domain created without one is bound to.

  Read off `domain.rubric`'s schema DEFAULT rather than named here, for the
  same reason `pair_rubrics/0` queries instead of listing: a hand-kept copy
  is what let one surface keep naming a rubric after a rename while its
  siblings moved on. Returns nil when the fabric is not initialized, so the
  caller degrades instead of inventing a name.
  """
  def default_rubric do
    case query(@default_rubric_sql, []) do
      {:ok, [[value] | _]} when is_binary(value) -> String.trim(value, "'")
      _ -> nil
    end
  end

  @discarded_side %{"discard-a" => :a, "discard-b" => :b}

  @doc """
  Verdicts that eject ONE named side of a pairing, permanently.

  `bin/swiss.py` `EJECTED_SIDE_BY_VERDICT` is the source; this map mirrors
  it so a chip can be coloured without a shell-out. `discard-a` removes A
  and leaves B in the pool with nothing recorded about it — an item is
  ejected on its own merits and NEVER as collateral from the card it was
  drawn against. A discard is not a score of zero (zero is a real position,
  held by items that lost honestly) and it is not `skip` (which says the
  rater could not judge, leaving the pairing open).
  """
  def discard_verdicts, do: Map.keys(@discarded_side) |> Enum.sort()

  def discard_verdict?(verdict), do: Map.has_key?(@discarded_side, verdict)

  @doc "`:a`, `:b`, or nil — which side of the pairing this verdict ejects."
  def discarded_side(verdict), do: Map.get(@discarded_side, verdict)

  @doc """
  True when the fabric DB file exists and carries the core schema. Checks
  every table the LiveViews mount against (a partially created DB — e.g.
  `pending_judgement` present but `domain` missing — must NOT count as
  initialized, or /domains/new could bypass bootstrap and hit raw SQL
  errors at save).
  """
  def initialized? do
    db_exists?() and table_exists?("pending_judgement") and table_exists?("domain")
  end

  @doc """
  Run the same schema bootstrap the CLI runs (`python3 bin/judgement.py
  init`) when the fabric DB or its tables are missing. No-op (`:ok`)
  when already initialized. Returns `{:error, reason}` on failure so the
  caller can degrade to a warning banner — never raises, never retries.

  Tests can override the command via the `JUDGEMENT_CLI_CMD` env var
  (whitespace-split; `init` is appended as the subcommand).
  """
  def ensure_initialized do
    if initialized?() do
      :ok
    else
      run_init_cli()
    end
  end

  defp run_init_cli do
    {cmd, args} = init_cli()

    case System.cmd(cmd, args, cd: @repo_root, stderr_to_stdout: true) do
      {_out, 0} ->
        :ok

      {out, status} ->
        {:error, "judgement init exited #{status}: #{String.slice(String.trim(out), 0, 300)}"}
    end
  rescue
    e -> {:error, Exception.message(e)}
  end

  defp init_cli do
    case System.get_env("JUDGEMENT_CLI_CMD") do
      nil ->
        {"python3", [Path.join(@repo_root, "bin/judgement.py"), "init"]}

      override ->
        [cmd | args] = String.split(override)
        {cmd, args ++ ["init"]}
    end
  end

  defp table_exists?(name) do
    case query("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", [name]) do
      {:ok, [_ | _]} -> true
      _ -> false
    end
  end

  # ─────────────────────────────────────────────────────────────────────
  # Pending queue
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Pending rows for `rater_type` (default `"human"`), newest-first.
  Each row is a map ready to render in the LiveView:

      %{
        id: 42,
        rater_type: "human",
        template_name: "<the rubric bound to this row's config>",
        template_version: 2,
        output_definition: %{...},  # parsed JSON
        trace_payload: %{...},      # parsed JSON
        tournament_db_path: "...",
        match_id: 7,
        trace_id: nil | "...",
        created_at: "2026-..."
      }
  """
  def list_pending(opts \\ []) do
    rater_type = Keyword.get(opts, :rater_type, "human")
    domain = Keyword.get(opts, :domain)
    limit = Keyword.get(opts, :limit, 50)

    {domain_where, domain_params} =
      if is_binary(domain) and domain != "",
        do: {" AND d.name = ?", [domain]},
        else: {"", []}

    sql = """
    SELECT p.id, p.tournament_db_path, p.match_id, p.trace_id,
           p.trace_payload, p.created_at,
           c.rater_type,
           t.name AS template_name, t.version AS template_version,
           t.output_definition,
           d.name AS domain_name,
           d.description AS domain_description,
           d.judge_prompt AS judge_prompt_name
    FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    JOIN eval_template t ON t.id = c.template_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE p.status = 'pending' AND c.rater_type = ? #{domain_where}
    ORDER BY p.created_at ASC
    LIMIT ?
    """

    case query(sql, [rater_type] ++ domain_params ++ [limit]) do
      {:ok, rows} -> Enum.map(rows, &decode_pending/1)
      {:error, _} -> []
    end
  end

  defp decode_pending([
         id,
         tdb_path,
         match_id,
         trace_id,
         trace_payload,
         created_at,
         rater_type,
         template_name,
         template_version,
         output_definition,
         domain_name,
         domain_description,
         judge_prompt_name
       ]) do
    %{
      id: id,
      tournament_db_path: tdb_path,
      match_id: match_id,
      trace_id: trace_id,
      trace_payload: parse_json(trace_payload),
      created_at: created_at,
      rater_type: rater_type,
      template_name: template_name,
      template_version: template_version,
      output_definition: parse_json(output_definition),
      domain_name: domain_name,
      domain_description: domain_description,
      judge_prompt_name: judge_prompt_name
    }
  end

  @doc """
  Get one pending row by id, returning the decoded map or nil.
  """
  def get_pending(id) when is_integer(id) do
    list_pending(limit: 1000)
    |> Enum.find(&(&1.id == id))
  end

  # ─────────────────────────────────────────────────────────────────────
  # Round-scoped queue (Swiss)
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Swiss round a trace payload belongs to, or `nil` when the payload
  carries no round at all.

  `bin/judgement.py`'s `_trace_payload` writes an explicit integer
  `round`; older payloads only carry the `"R<round>-<slot>"` label, which
  is parsed as a fallback. A payload with neither (hand-seeded pairs,
  single-artifact judgements) is roundless and is never withheld by
  `open_round_queue/1`.
  """
  def payload_round(payload) when is_map(payload) do
    case Map.get(payload, "round") do
      n when is_integer(n) -> n
      n when is_binary(n) -> parse_round_int(n)
      _ -> label_round(Map.get(payload, "label"))
    end
  end

  def payload_round(_), do: nil

  defp label_round("R" <> rest) when is_binary(rest) do
    rest |> String.split("-") |> List.first() |> parse_round_int()
  end

  defp label_round(_), do: nil

  defp parse_round_int(value) when is_binary(value) do
    case Integer.parse(value) do
      {n, ""} -> n
      _ -> nil
    end
  end

  defp parse_round_int(_), do: nil

  @doc """
  The pending rows a human may judge right now, plus per-pool round
  progress.

  Swiss rounds are sequential: a pool's round N+1 pairings are computed
  from the standings round N produced, so serving them out of order
  corrupts the input to the next pairing. Only the *lowest* open round of
  each pool (`tournament_db_path`) is offered; a pool whose open round has
  been fully resolved contributes nothing until its next round is
  enqueued. Roundless payloads are always offered.

  Returns

      %{
        rows: [pending row, ...],
        rounds: %{pool_path => %{round: n, remaining: k, total: m}}
      }

  where `remaining` counts still-pending pairings of that pool's open
  round and `total` counts every pairing enqueued for it, resolved or not.

  Accepts the same options as `list_pending/1`.
  """
  def open_round_queue(opts \\ []) do
    rows = list_pending(opts)

    open =
      Enum.reduce(rows, %{}, fn row, acc ->
        case payload_round(row.trace_payload) do
          nil -> acc
          n -> Map.update(acc, row.tournament_db_path, n, &min(&1, n))
        end
      end)

    offered =
      Enum.filter(rows, fn row ->
        case payload_round(row.trace_payload) do
          nil -> true
          n -> n == Map.get(open, row.tournament_db_path)
        end
      end)

    %{
      rows: offered,
      rounds:
        round_progress(open, Keyword.get(opts, :rater_type, "human"), Keyword.get(opts, :domain))
    }
  end

  defp round_progress(open, _rater_type, _domain) when map_size(open) == 0, do: %{}

  defp round_progress(open, rater_type, domain) do
    {domain_where, domain_params} = domain_filter_sql(domain)

    sql = """
    SELECT p.tournament_db_path,
           json_extract(p.trace_payload, '$.round'),
           json_extract(p.trace_payload, '$.label'),
           p.status
    FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE c.rater_type = ? #{domain_where}
    """

    case query(sql, [rater_type] ++ domain_params) do
      {:ok, rows} -> tally_rounds(rows, open)
      {:error, _} -> %{}
    end
  end

  defp tally_rounds(rows, open) do
    Enum.reduce(rows, %{}, fn [path, round, label, status], acc ->
      round = payload_round(%{"round" => round, "label" => label})
      target = Map.get(open, path)

      if round != nil and round == target do
        Map.update(
          acc,
          path,
          %{round: round, remaining: pending_unit(status), total: 1},
          fn tally ->
            %{
              tally
              | remaining: tally.remaining + pending_unit(status),
                total: tally.total + 1
            }
          end
        )
      else
        acc
      end
    end)
  end

  defp pending_unit("pending"), do: 1
  defp pending_unit(_), do: 0

  @doc """
  Fetch a judgement row by id REGARDLESS of status — candidate permalinks
  must keep working after the pair is judged. Returns the decoded map
  (plus `:status`) or nil.
  """
  def get_judgement(id) when is_integer(id) do
    sql = """
    SELECT p.id, p.tournament_db_path, p.match_id, p.trace_id,
           p.trace_payload, p.created_at,
           c.rater_type,
           t.name AS template_name, t.version AS template_version,
           t.output_definition,
           d.name AS domain_name,
           d.description AS domain_description,
           d.judge_prompt AS judge_prompt_name,
           p.status
    FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    JOIN eval_template t ON t.id = c.template_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE p.id = ?
    """

    case query(sql, [id]) do
      {:ok, [row | _]} ->
        {status, core} = List.pop_at(row, -1)
        Map.put(decode_pending(core), :status, status)

      _ ->
        nil
    end
  end

  def get_judgement(_), do: nil

  # ─────────────────────────────────────────────────────────────────────
  # Submit a human judgement
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Write a 2-row judgement for a pending row. `rater_id` is whatever
  identity the UI tracks (we don't have auth in v0; defaults to "anon").

  Returns `{:ok, rating_id}` on success or `{:error, reason}`. Validates
  verdict + confidence against the active template's enums.
  """
  def submit_human(pending_id, verdict, confidence, opts \\ [])
      when is_integer(pending_id) and is_binary(verdict) and is_binary(confidence) do
    rationale = Keyword.get(opts, :rationale)
    rater_id = Keyword.get(opts, :rater_id, "anon")

    with {:ok, pending} <- fetch_pending_for_write(pending_id),
         outdef <- pending.output_definition,
         :ok <- validate_enum(verdict, outdef["verdict_enum"], "verdict"),
         :ok <-
           validate_enum(
             confidence,
             outdef["confidence_enum"] || ["low", "mid", "high"],
             "confidence"
           ),
         :ok <- validate_rationale(rationale, outdef["rationale_required"]) do
      rating_id = uuid()
      rater = %{"type" => "human", "userId" => rater_id}

      verdict_meta =
        Map.merge(%{"rater" => rater}, if(rationale, do: %{"rationale" => rationale}, else: %{}))

      confidence_meta = %{"rater" => rater}

      now = now_str()

      ops = [
        {"INSERT INTO score(rating_id, pending_id, template_id, rubric_version, name, data_type, value, metadata, tournament_db_path, match_id, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         [
           rating_id,
           pending.id,
           pending.template_id,
           pending.template_version,
           "judgement.verdict",
           "CATEGORICAL",
           verdict,
           Jason.encode!(verdict_meta),
           pending.tournament_db_path,
           pending.match_id,
           pending.trace_id,
           now
         ]},
        {"INSERT INTO score(rating_id, pending_id, template_id, rubric_version, name, data_type, value, metadata, tournament_db_path, match_id, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         [
           rating_id,
           pending.id,
           pending.template_id,
           pending.template_version,
           "judgement.confidence",
           "CATEGORICAL",
           confidence,
           Jason.encode!(confidence_meta),
           pending.tournament_db_path,
           pending.match_id,
           pending.trace_id,
           now
         ]},
        {"UPDATE pending_judgement SET status='done', rating_id=?, completed_at=? WHERE id=?",
         [rating_id, now, pending.id]}
      ]

      case write_transaction(ops) do
        :ok -> {:ok, rating_id}
        {:error, reason} -> {:error, reason}
      end
    end
  end

  @doc """
  Multi-subject submit (wave-12, docs/design/judgement-wheel-v2.md §3).

  `subjects` maps subject name → %{"verdict" => v, "confidence" => c,
  "rationale" => r-or-nil}. ONE pending row resolves exactly once; each
  subject writes its own Score rows (`judgement.<subject>.verdict` /
  `judgement.<subject>.confidence`) under the SAME rating_id — the exact
  mechanism `submit_human/4` uses (direct SQLite writes), extended
  symmetrically. The single-subject path above is untouched.
  """
  def submit_human_subjects(pending_id, subjects, opts \\ [])
      when is_integer(pending_id) and is_map(subjects) do
    rater_id = Keyword.get(opts, :rater_id, "anon")
    subject_order = Keyword.get(opts, :subject_order, Enum.sort(Map.keys(subjects)))

    with {:ok, pending} <- fetch_pending_for_write(pending_id),
         outdef <- pending.output_definition,
         :ok <- validate_subject_entries(subjects, subject_order, outdef) do
      rating_id = uuid()
      rater = %{"type" => "human", "userId" => rater_id}
      now = now_str()

      score_ops =
        Enum.flat_map(subject_order, fn subject ->
          entry = Map.fetch!(subjects, subject)
          rationale = entry["rationale"]

          verdict_meta =
            Map.merge(
              %{"rater" => rater, "subject" => subject},
              if(is_binary(rationale) and rationale != "",
                do: %{"rationale" => rationale},
                else: %{}
              )
            )

          [
            score_insert(
              pending,
              rating_id,
              "judgement.#{subject}.verdict",
              entry["verdict"],
              verdict_meta,
              now
            ),
            score_insert(
              pending,
              rating_id,
              "judgement.#{subject}.confidence",
              entry["confidence"],
              %{"rater" => rater, "subject" => subject},
              now
            )
          ]
        end)

      ops =
        score_ops ++
          [
            {"UPDATE pending_judgement SET status='done', rating_id=?, completed_at=? WHERE id=?",
             [rating_id, now, pending.id]}
          ]

      case write_transaction(ops) do
        :ok -> {:ok, rating_id}
        {:error, reason} -> {:error, reason}
      end
    end
  end

  defp score_insert(pending, rating_id, name, value, metadata, now) do
    {"INSERT INTO score(rating_id, pending_id, template_id, rubric_version, name, data_type, value, metadata, tournament_db_path, match_id, trace_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
     [
       rating_id,
       pending.id,
       pending.template_id,
       pending.template_version,
       name,
       "CATEGORICAL",
       value,
       Jason.encode!(metadata),
       pending.tournament_db_path,
       pending.match_id,
       pending.trace_id,
       now
     ]}
  end

  defp validate_subject_entries(subjects, subject_order, outdef) do
    verdict_enum = outdef["verdict_enum"]
    confidence_enum = outdef["confidence_enum"] || ["low", "mid", "high"]

    Enum.reduce_while(subject_order, :ok, fn subject, :ok ->
      case Map.get(subjects, subject) do
        %{"verdict" => v, "confidence" => c} = entry ->
          with :ok <- validate_enum(v, verdict_enum, "#{subject} verdict"),
               :ok <- validate_enum(c, confidence_enum, "#{subject} confidence"),
               :ok <- validate_rationale(entry["rationale"], outdef["rationale_required"]) do
            {:cont, :ok}
          else
            error -> {:halt, error}
          end

        _ ->
          {:halt, {:error, "missing verdict/confidence for subject #{inspect(subject)}"}}
      end
    end)
  end

  defp fetch_pending_for_write(pending_id) do
    sql = """
    SELECT p.id, p.tournament_db_path, p.match_id, p.trace_id, p.status,
           c.template_id,
           t.version AS template_version,
           t.output_definition
    FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    JOIN eval_template t ON t.id = c.template_id
    WHERE p.id = ?
    """

    case query(sql, [pending_id]) do
      {:ok,
       [[id, tdb_path, match_id, trace_id, "pending", template_id, template_version, outdef]]} ->
        {:ok,
         %{
           id: id,
           tournament_db_path: tdb_path,
           match_id: match_id,
           trace_id: trace_id,
           template_id: template_id,
           template_version: template_version,
           output_definition: parse_json(outdef)
         }}

      {:ok, [[_id, _, _, _, status, _, _, _]]} ->
        {:error, "pending row already #{status}"}

      {:ok, []} ->
        {:error, "pending row not found"}

      {:error, e} ->
        {:error, "query failed: #{inspect(e)}"}
    end
  end

  defp validate_enum(value, enum, field) when is_list(enum) do
    if value in enum,
      do: :ok,
      else: {:error, "#{field} #{inspect(value)} not in #{inspect(enum)}"}
  end

  defp validate_enum(_, _, field), do: {:error, "no enum for #{field}"}

  defp validate_rationale(_rationale, false), do: :ok
  defp validate_rationale(_rationale, nil), do: :ok

  defp validate_rationale(rationale, true) do
    if is_binary(rationale) and String.trim(rationale) != "",
      do: :ok,
      else: {:error, "rubric requires rationale"}
  end

  # ─────────────────────────────────────────────────────────────────────
  # Append-only revision (wave-13 slice A; operator-environment-v13 §1)
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Revise an already-'done' human judgement without touching its rows.

  Mirrors `submit_human/4`'s persistence mechanism (direct SQLite score
  writes) plus one `judgement_revision` row linking `previous_rating_id`
  to the freshly written rating. The pending row STAYS 'done' — no
  status churn; the old score rows are never modified.

  Refuses when: the pending row is not 'done'; `previous_rating_id` is
  not the current chain tip (stale — someone revised first); `reason`
  is empty. The whole write runs under BEGIN IMMEDIATE so the staleness
  check and the inserts are atomic against concurrent revisers.

  Returns `{:ok, new_rating_id}` or `{:error, reason}`.
  """
  def revise_human(pending_id, previous_rating_id, verdict, confidence, opts \\ [])
      when is_integer(pending_id) and is_binary(previous_rating_id) and
             is_binary(verdict) and is_binary(confidence) do
    reason = Keyword.get(opts, :reason)
    rationale = Keyword.get(opts, :rationale)
    revised_by = Keyword.get(opts, :revised_by, "anon")

    with :ok <- validate_required_text(reason, "revision reason"),
         :ok <- validate_required_text(revised_by, "revised_by"),
         {:ok, pending} <- fetch_done_pending(pending_id),
         outdef <- pending.output_definition,
         :ok <- validate_enum(verdict, outdef["verdict_enum"], "verdict"),
         :ok <-
           validate_enum(
             confidence,
             outdef["confidence_enum"] || ["low", "mid", "high"],
             "confidence"
           ),
         :ok <- validate_rationale(rationale, outdef["rationale_required"]) do
      rating_id = uuid()
      rater = %{"type" => "human", "userId" => revised_by}

      verdict_meta =
        Map.merge(
          %{"rater" => rater},
          if(is_binary(rationale) and rationale != "",
            do: %{"rationale" => rationale},
            else: %{}
          )
        )

      now = now_str()

      score_ops = [
        score_insert(pending, rating_id, "judgement.verdict", verdict, verdict_meta, now),
        score_insert(
          pending,
          rating_id,
          "judgement.confidence",
          confidence,
          %{"rater" => rater},
          now
        )
      ]

      revise_transaction(pending_id, previous_rating_id, rating_id, score_ops, %{
        revised_by: String.trim(revised_by),
        reason: String.trim(reason)
      })
    end
  end

  # BEGIN IMMEDIATE takes the write lock before the staleness check, so
  # check + inserts are atomic against concurrent revisers.
  defp revise_transaction(pending_id, previous_rating_id, rating_id, score_ops, rev) do
    {:ok, conn} = Sqlite3.open(db_path(), mode: :readwrite)

    try do
      :ok = Sqlite3.execute(conn, "BEGIN IMMEDIATE")

      case conn_effective_rating(conn, pending_id) do
        ^previous_rating_id ->
          Enum.each(score_ops, fn {sql, params} ->
            {:ok, stmt} = Sqlite3.prepare(conn, sql)
            :ok = Sqlite3.bind(stmt, params)
            :done = Sqlite3.step(conn, stmt)
            Sqlite3.release(conn, stmt)
          end)

          {:ok, stmt} =
            Sqlite3.prepare(
              conn,
              "INSERT INTO judgement_revision(pending_id, previous_rating_id, new_rating_id, revised_by, reason) VALUES (?,?,?,?,?)"
            )

          :ok =
            Sqlite3.bind(stmt, [
              pending_id,
              previous_rating_id,
              rating_id,
              rev.revised_by,
              rev.reason
            ])

          :done = Sqlite3.step(conn, stmt)
          Sqlite3.release(conn, stmt)
          :ok = Sqlite3.execute(conn, "COMMIT")
          {:ok, rating_id}

        other ->
          Sqlite3.execute(conn, "ROLLBACK")

          {:error,
           "stale revision: previous rating #{previous_rating_id} is not the effective rating #{inspect(other)} — someone revised first; reload and retry"}
      end
    catch
      kind, value ->
        Sqlite3.execute(conn, "ROLLBACK")
        {:error, {kind, value}}
    after
      Sqlite3.close(conn)
    end
  end

  defp conn_effective_rating(conn, pending_id) do
    sql = """
    SELECT COALESCE(
      (SELECT new_rating_id FROM judgement_revision
         WHERE pending_id = ?1 ORDER BY id DESC LIMIT 1),
      (SELECT rating_id FROM pending_judgement WHERE id = ?1))
    """

    {:ok, stmt} = Sqlite3.prepare(conn, sql)
    :ok = Sqlite3.bind(stmt, [pending_id])

    result =
      case Sqlite3.step(conn, stmt) do
        {:row, [value]} -> value
        _ -> nil
      end

    Sqlite3.release(conn, stmt)
    result
  end

  # Like fetch_pending_for_write/1 but requires status 'done' — revision
  # only applies to completed judgements.
  defp fetch_done_pending(pending_id) do
    sql = """
    SELECT p.id, p.tournament_db_path, p.match_id, p.trace_id, p.status,
           c.template_id,
           t.version AS template_version,
           t.output_definition
    FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    JOIN eval_template t ON t.id = c.template_id
    WHERE p.id = ?
    """

    case query(sql, [pending_id]) do
      {:ok, [[id, tdb_path, match_id, trace_id, "done", template_id, template_version, outdef]]} ->
        {:ok,
         %{
           id: id,
           tournament_db_path: tdb_path,
           match_id: match_id,
           trace_id: trace_id,
           template_id: template_id,
           template_version: template_version,
           output_definition: parse_json(outdef)
         }}

      {:ok, [[_id, _, _, _, status, _, _, _]]} ->
        {:error,
         "pending row is '#{status}', not 'done' — only completed judgements can be revised"}

      {:ok, []} ->
        {:error, "pending row not found"}

      {:error, e} ->
        {:error, "query failed: #{inspect(e)}"}
    end
  end

  defp validate_required_text(value, field) do
    if is_binary(value) and String.trim(value) != "",
      do: :ok,
      else: {:error, "#{field} must be non-empty"}
  end

  @doc """
  Revision chain for a pending row, original rating first, tip last:
  `[%{rating_id, revised_by, reason, created_at}, ...]`. The original
  entry carries `revised_by: nil, reason: nil`.
  """
  def revision_chain(pending_id) when is_integer(pending_id) do
    Map.get(revisions_by_pending([pending_id]), pending_id, [])
  end

  defp revisions_by_pending([]), do: %{}

  defp revisions_by_pending(pending_ids) do
    ids = Enum.uniq(pending_ids)
    placeholders = ids |> Enum.map(fn _ -> "?" end) |> Enum.join(",")

    base_sql = """
    SELECT id, rating_id, completed_at FROM pending_judgement
    WHERE id IN (#{placeholders}) AND rating_id IS NOT NULL
    """

    rev_sql = """
    SELECT pending_id, new_rating_id, revised_by, reason, created_at
    FROM judgement_revision
    WHERE pending_id IN (#{placeholders})
    ORDER BY id ASC
    """

    with {:ok, base_rows} <- query(base_sql, ids),
         {:ok, rev_rows} <- query(rev_sql, ids) do
      revs =
        Enum.group_by(rev_rows, &List.first/1, fn [_pid, rid, by, reason, at] ->
          %{rating_id: rid, revised_by: by, reason: reason, created_at: at}
        end)

      Map.new(base_rows, fn [pid, rid, completed_at] ->
        original = %{rating_id: rid, revised_by: nil, reason: nil, created_at: completed_at}
        {pid, [original | Map.get(revs, pid, [])]}
      end)
    else
      _ -> %{}
    end
  end

  # ─────────────────────────────────────────────────────────────────────
  # List judgements (for the /judgements view)
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Joined verdict + confidence rows, optionally filtered by rater_type and
  domain. Returns one map per judgement (not one per Score row).

  Rubric scope defaults to `pair_rubrics/0` — every pair rubric on disk, not
  one name. `:rubric` (a single name) or `:rubrics` (a list) narrows it.
  """
  def list_judgements(opts \\ []) do
    rubrics = rubric_scope(opts)
    rater_type = Keyword.get(opts, :rater_type)
    domain = Keyword.get(opts, :domain)
    limit = Keyword.get(opts, :limit, 200)

    {rater_where, rater_params} =
      if is_binary(rater_type) and rater_type != "",
        do: {" AND json_extract(s_v.metadata, '$.rater.type') = ?", [rater_type]},
        else: {"", []}

    {domain_where, domain_params} =
      if is_binary(domain) and domain != "",
        do: {" AND d.name = ?", [domain]},
        else: {"", []}

    rubric_placeholders = Enum.map_join(rubrics, ",", fn _ -> "?" end)

    sql = """
    SELECT s_v.rating_id, s_v.value AS verdict, s_v.metadata AS verdict_meta,
           s_c.value AS confidence, s_c.metadata AS confidence_meta,
           s_v.tournament_db_path, s_v.match_id, s_v.trace_id,
           s_v.rubric_version, t.name AS rubric, s_v.created_at,
           p.id AS pending_id, p.trace_payload, d.id AS domain_id, d.name AS domain_name,
           p.status AS pending_status
    FROM score s_v
    JOIN score s_c ON s_c.rating_id = s_v.rating_id AND s_c.name = 'judgement.confidence'
    JOIN eval_template t ON t.id = s_v.template_id
    LEFT JOIN pending_judgement p ON p.id = s_v.pending_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE s_v.name = 'judgement.verdict' AND t.name IN (#{rubric_placeholders})
      #{rater_where} #{domain_where}
    ORDER BY s_v.created_at DESC
    LIMIT ?
    """

    if rubrics == [] do
      []
    else
      case query(sql, rubrics ++ rater_params ++ domain_params ++ [limit]) do
        {:ok, rows} -> rows |> Enum.map(&decode_judgement/1) |> annotate_revisions()
        {:error, _} -> []
      end
    end
  end

  defp rubric_scope(opts) do
    cond do
      is_list(opts[:rubrics]) -> opts[:rubrics]
      is_binary(opts[:rubric]) -> [opts[:rubric]]
      true -> pair_rubrics()
    end
  end

  # Adds revision keys to each judgement row (existing keys unchanged):
  #   superseded:     true when a later rating in the chain replaced this one
  #   revised:        true when this rating is the tip of a chain length > 1
  #   revision_chain: [%{rating_id, revised_by, reason, created_at}, ...]
  #                   (original first, tip last; [] when never revised)
  #   revision_reason / revised_by: the reason/author that produced THIS
  #                   rating (nil for original ratings)
  defp annotate_revisions(rows) do
    chains =
      rows
      |> Enum.map(& &1.pending_id)
      |> Enum.filter(&is_integer/1)
      |> revisions_by_pending()

    Enum.map(rows, fn row ->
      chain = Map.get(chains, row.pending_id, [])

      if length(chain) < 2 do
        Map.merge(row, %{
          superseded: false,
          revised: false,
          revision_chain: [],
          revision_reason: nil,
          revised_by: nil
        })
      else
        tip = List.last(chain)
        own = Enum.find(chain, &(&1.rating_id == row.rating_id))

        Map.merge(row, %{
          superseded: row.rating_id != tip.rating_id,
          revised: row.rating_id == tip.rating_id,
          revision_chain: chain,
          revision_reason: own && own.reason,
          revised_by: own && own.revised_by
        })
      end
    end)
  end

  defp decode_judgement([
         rating_id,
         verdict,
         verdict_meta,
         confidence,
         _confidence_meta,
         tdb_path,
         match_id,
         trace_id,
         rubric_version,
         rubric,
         created_at,
         pending_id,
         trace_payload,
         domain_id,
         domain_name,
         pending_status
       ]) do
    vmeta = parse_json(verdict_meta)
    payload = parse_json(trace_payload)
    fallback_name = Path.basename(tdb_path, ".db")

    %{
      rating_id: rating_id,
      rubric: rubric,
      rubric_version: rubric_version,
      verdict: verdict,
      confidence: confidence,
      rationale: Map.get(vmeta, "rationale"),
      rater: Map.get(vmeta, "rater") || %{},
      tournament_db_path: tdb_path,
      tournament_name: domain_name || fallback_name,
      domain_id: domain_id,
      domain_name: domain_name,
      pending_id: pending_id,
      pending_status: pending_status,
      match_id: match_id,
      trace_id: trace_id,
      trace_payload: payload,
      match_label: Map.get(payload, "label") || "Match #{match_id}",
      card_a: decode_card(payload, "card_a", "input_a", "Candidate A"),
      card_b: decode_card(payload, "card_b", "input_b", "Candidate B"),
      created_at: created_at
    }
  end

  @doc """
  Stable identity for one judged card: the digest of the text that was
  judged.

  The SAME rule `bin/judgement.py`'s `_side_snapshot/2` applies before it
  hashes a pair key — body, else text, else title — so a card carries one
  identity in the queue, in the points table and here. Keyed by content and
  never by `source_ref`: `bin/generate_cards.py` stamps every card drafted
  from one corpus item with that item's ref, so a ref-keyed identity
  silently collapses distinct findings into one.

  A payload that carries only a file ref is snapshotted by READING that file
  at enqueue time, which the UI does not do; those rows key on the ref
  string here and are therefore UI-local. The points table never uses this
  function — `bin/standings_view.py` computes its own keys from the stored
  snapshot.
  """
  def item_key(card) do
    :crypto.hash(:sha256, snapshot_text(card))
    |> Base.encode16(case: :lower)
    |> binary_part(0, 16)
  end

  defp snapshot_text(card) do
    [Map.get(card, :body), Map.get(card, :text), Map.get(card, :title)]
    |> Enum.find("", fn value -> is_binary(value) and value != "" end)
  end

  defp export_side(payload, card_key, input_key) do
    case Map.get(payload, card_key) do
      card when is_map(card) ->
        %{
          title: card["title"],
          body: card["body"] || card["text"],
          source_ref: card["source_ref"] || card["ref"]
        }

      _ ->
        Map.get(payload, input_key)
    end
  end

  defp decode_card(payload, card_key, input_key, fallback_title) do
    case Map.get(payload, card_key) do
      card when is_map(card) ->
        %{
          title: card["title"] || fallback_title,
          body: card["body"] || card["text"] || "",
          source_ref: card["source_ref"] || card["ref"]
        }

      _ ->
        value = Map.get(payload, input_key)

        %{
          title: source_title(value, fallback_title),
          body: if(is_binary(value), do: value, else: ""),
          source_ref: if(is_binary(value), do: value, else: nil)
        }
    end
  end

  defp source_title(value, fallback) when is_binary(value) do
    case Path.basename(value) do
      "" -> fallback
      title -> title
    end
  end

  defp source_title(_, fallback), do: fallback

  # ─────────────────────────────────────────────────────────────────────
  # Counts (for sidebar / banner)
  # ─────────────────────────────────────────────────────────────────────

  def counts(opts \\ []) do
    domain = Keyword.get(opts, :domain)

    if not db_exists?() do
      %{pending_human: 0, pending_llm: 0, judgements_total: 0, db_present: false}
    else
      %{
        pending_human: count_pending("human", domain),
        pending_llm: count_pending("llm", domain),
        judgements_total: count_scores(domain),
        db_present: true
      }
    end
  end

  defp count_pending(rater_type, domain) do
    {domain_where, domain_params} = domain_filter_sql(domain)

    sql = """
    SELECT COUNT(*) FROM pending_judgement p
    JOIN job_configuration c ON c.id = p.config_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE p.status='pending' AND c.rater_type = ? #{domain_where}
    """

    case query(sql, [rater_type] ++ domain_params) do
      {:ok, [[n]]} -> n || 0
      _ -> 0
    end
  end

  defp count_scores(domain) do
    {domain_where, domain_params} = domain_filter_sql(domain)

    sql = """
    SELECT COUNT(*) FROM score s
    LEFT JOIN pending_judgement p ON p.id = s.pending_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE s.name='judgement.verdict' #{domain_where}
    """

    case query(sql, domain_params) do
      {:ok, [[n]]} -> n || 0
      _ -> 0
    end
  end

  defp domain_filter_sql(domain) when is_binary(domain) and domain != "",
    do: {" AND d.name = ?", [domain]}

  defp domain_filter_sql(_domain), do: {"", []}

  # ─────────────────────────────────────────────────────────────────────
  # Export (JSONL)
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Returns a list of maps, one per judgement, ready to be encoded as
  individual JSON lines by the export controller.

  Scope defaults to the SAME rubric set `list_judgements/1` reads, so an
  export covers exactly what the results page displays. An export that
  reports a narrower set than the page is a silent data loss, not a filter.
  """
  def export_records(opts \\ []) do
    rater_type = Keyword.get(opts, :rater_type)
    domain = Keyword.get(opts, :domain)

    list_judgements(
      rubrics: rubric_scope(opts),
      rater_type: rater_type,
      domain: domain,
      limit: 10_000
    )
    |> Enum.map(fn j ->
      payload =
        if map_size(j.trace_payload) > 0,
          do: j.trace_payload,
          else: read_trace_payload(j.tournament_db_path, j.match_id) || %{}

      %{
        ratingId: j.rating_id,
        rubric: j.rubric,
        rubricVersion: j.rubric_version,
        trace: %{
          tournamentDbPath: j.tournament_db_path,
          tournamentName: j.tournament_name,
          domainName: j.domain_name,
          matchId: j.match_id,
          label: Map.get(payload, "label"),
          input_a: export_side(payload, "card_a", "input_a"),
          input_b: export_side(payload, "card_b", "input_b"),
          synthesis: Map.get(payload, "synthesis") || Map.get(payload, "conclusion"),
          winner_id: Map.get(payload, "winner_id"),
          winner_reasoning: Map.get(payload, "winner_reasoning"),
          langfuseTraceId: j.trace_id
        },
        judgement: %{
          verdict: j.verdict,
          confidence: j.confidence,
          rationale: j.rationale
        },
        rater: j.rater,
        createdAt: j.created_at
      }
    end)
  end

  # ─────────────────────────────────────────────────────────────────────
  # Tournament DB read (for trace payload on demand)
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Reads the row for a given match from a tournament DB and returns it as
  a map. Falls back gracefully on older schemas that lack synthesis
  / winner_id columns.
  """
  def read_trace_payload(tdb_path, match_id) do
    if not File.exists?(tdb_path) do
      nil
    else
      sql_new =
        "SELECT id, round, slot, input_a, input_b, is_bye, conclusion, " <>
          "synthesis, winner_id, winner_reasoning, trace_id " <>
          "FROM matches WHERE id = ?"

      sql_old =
        "SELECT id, round, slot, input_a, input_b, is_bye, conclusion FROM matches WHERE id = ?"

      case ro_query(tdb_path, sql_new, [match_id]) do
        {:ok, [row]} ->
          trace_payload_new(row)

        _ ->
          case ro_query(tdb_path, sql_old, [match_id]) do
            {:ok, [row]} -> trace_payload_old(row)
            _ -> nil
          end
      end
    end
  end

  defp trace_payload_new([
         id,
         round,
         slot,
         a,
         b,
         is_bye,
         conclusion,
         synthesis,
         winner_id,
         winner_reasoning,
         trace_id
       ]) do
    %{
      "match_id" => id,
      "label" => "R#{round}-#{slot + 1}",
      "round" => round,
      "slot" => slot,
      "input_a" => a,
      "input_b" => b,
      "is_bye" => is_bye == 1,
      "conclusion" => conclusion,
      "synthesis" => synthesis,
      "winner_id" => winner_id,
      "winner_reasoning" => winner_reasoning,
      "trace_id" => trace_id
    }
  end

  defp trace_payload_old([id, round, slot, a, b, is_bye, conclusion]) do
    %{
      "match_id" => id,
      "label" => "R#{round}-#{slot + 1}",
      "round" => round,
      "slot" => slot,
      "input_a" => a,
      "input_b" => b,
      "is_bye" => is_bye == 1,
      "conclusion" => conclusion
    }
  end

  # ─────────────────────────────────────────────────────────────────────
  # Internals
  # ─────────────────────────────────────────────────────────────────────

  @doc """
  Run one read-only SELECT against the fabric DB.

  The single connection path other modules in this app read through, so
  there is one place that knows where the DB is and one place that degrades
  to `{:error, :no_db}` instead of raising when it is not there yet.
  """
  def fabric_query(sql, params), do: query(sql, params)

  defp query(sql, params) do
    if not db_exists?() do
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

  defp ro_query(path, sql, params) do
    {:ok, conn} = Sqlite3.open(path, mode: :readonly)

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

  defp write_transaction(ops) do
    {:ok, conn} = Sqlite3.open(db_path(), mode: :readwrite)

    try do
      :ok = Sqlite3.execute(conn, "BEGIN")

      Enum.each(ops, fn {sql, params} ->
        {:ok, stmt} = Sqlite3.prepare(conn, sql)
        :ok = Sqlite3.bind(stmt, params)
        :done = Sqlite3.step(conn, stmt)
        Sqlite3.release(conn, stmt)
      end)

      :ok = Sqlite3.execute(conn, "COMMIT")
      :ok
    catch
      kind, value ->
        Sqlite3.execute(conn, "ROLLBACK")
        {:error, {kind, value}}
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

  defp parse_json(nil), do: %{}

  defp parse_json(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, m} -> m
      _ -> %{}
    end
  end

  defp uuid do
    # 16 random bytes formatted as RFC4122-ish, no need for the v4 dashes
    # to be perfect — Python side uses `uuid.uuid4()` and we just need
    # uniqueness across rating_ids.
    :crypto.strong_rand_bytes(16)
    |> Base.encode16(case: :lower)
    |> String.replace_prefix("", "")
  end

  defp now_str do
    DateTime.utc_now() |> DateTime.to_iso8601()
  end
end
