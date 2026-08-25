defmodule TournamentUi.Catalog do
  @moduledoc """
  Read-only Elixir adapter over the project-landscape catalog tables in the
  fabric SQLite DB (`project`, `component`, `source`, `evidence_ref`,
  `landscape_snapshot`, `context_pack`).

  ADR 0001 §2: Python (`bin/catalog.py`) owns the schema and all writes;
  Elixir reads only and never executes DDL. Older fabric DBs may predate the
  catalog tables entirely — every reader here treats "no such table" as
  empty results, never a crash.

  `evidence_ref.body` holds the canonical JSON of the EvidenceRef (or NULL
  when the payload lives in the filesystem CAS); `get_evidence/1` parses the
  inline body to surface `excerpt` and `browsable_url` for the UI, falling
  back to the `summary` column when the body is not inline.
  """

  alias Exqlite.Sqlite3

  @doc "Active projects with component/source/snapshot counts, newest first."
  def list_projects do
    sql = """
    SELECT p.id, p.name, p.description, p.status, p.updated_at,
           (SELECT COUNT(*) FROM component c WHERE c.project_id = p.id AND c.status = 'active'),
           (SELECT COUNT(*) FROM source s WHERE s.project_id = p.id AND s.status = 'active'),
           (SELECT COUNT(*) FROM landscape_snapshot ls WHERE ls.project_id = p.id)
    FROM project p
    WHERE p.status = 'active'
    ORDER BY p.updated_at DESC, p.name ASC
    """

    case query(sql, []) do
      {:ok, rows} -> Enum.map(rows, &decode_project_summary/1)
      _ -> []
    end
  end

  defp decode_project_summary([id, name, description, status, updated_at, comps, srcs, snaps]) do
    %{
      id: id,
      name: name,
      description: description,
      status: status,
      updated_at: updated_at,
      component_count: comps || 0,
      source_count: srcs || 0,
      snapshot_count: snaps || 0
    }
  end

  @doc """
  One project by name, with components, sources (kind + trust tier) and its
  recent snapshots (digest, evidence count, per-role pack digests).
  Returns nil when the project doesn't exist or the tables are missing.
  """
  def get_project(name) when is_binary(name) do
    sql = """
    SELECT id, name, description, status, created_at, updated_at
    FROM project WHERE name = ?
    """

    case query(sql, [name]) do
      {:ok, [[id, pname, description, status, created_at, updated_at]]} ->
        %{
          id: id,
          name: pname,
          description: description,
          status: status,
          created_at: created_at,
          updated_at: updated_at,
          components: components(id),
          sources: sources(id),
          snapshots: snapshots(id)
        }

      _ ->
        nil
    end
  end

  defp components(project_id) do
    sql = """
    SELECT name, kind, status, updated_at FROM component
    WHERE project_id = ? AND status = 'active' ORDER BY name
    """

    case query(sql, [project_id]) do
      {:ok, rows} ->
        Enum.map(rows, fn [name, kind, status, updated_at] ->
          %{name: name, kind: kind, status: status, updated_at: updated_at}
        end)

      _ ->
        []
    end
  end

  defp sources(project_id) do
    sql = """
    SELECT s.name, s.kind, s.locator, s.trust_tier, s.status,
           (SELECT COUNT(*) FROM evidence_ref e WHERE e.source_id = s.id)
    FROM source s
    WHERE s.project_id = ? AND s.status = 'active' ORDER BY s.name
    """

    case query(sql, [project_id]) do
      {:ok, rows} ->
        Enum.map(rows, fn [name, kind, locator, trust_tier, status, evidence_count] ->
          %{
            name: name,
            kind: kind,
            locator: locator,
            trust_tier: trust_tier,
            status: status,
            evidence_count: evidence_count || 0
          }
        end)

      _ ->
        []
    end
  end

  @recent_snapshots 5

  defp snapshots(project_id) do
    sql = """
    SELECT ls.digest, ls.created_at,
           (SELECT COUNT(*) FROM snapshot_evidence se WHERE se.snapshot_digest = ls.digest)
    FROM landscape_snapshot ls
    WHERE ls.project_id = ?
    ORDER BY ls.created_at DESC, ls.digest ASC
    LIMIT #{@recent_snapshots}
    """

    case query(sql, [project_id]) do
      {:ok, rows} ->
        Enum.map(rows, fn [digest, created_at, evidence_count] ->
          %{
            digest: digest,
            created_at: created_at,
            evidence_count: evidence_count || 0,
            packs: list_packs_for_snapshot(digest)
          }
        end)

      _ ->
        []
    end
  end

  @doc "Context packs for a snapshot digest, one per role."
  def list_packs_for_snapshot(snapshot_digest) when is_binary(snapshot_digest) do
    sql = """
    SELECT digest, role, schema_version, created_at FROM context_pack
    WHERE snapshot_digest = ? ORDER BY role
    """

    case query(sql, [snapshot_digest]) do
      {:ok, rows} ->
        Enum.map(rows, fn [digest, role, schema_version, created_at] ->
          %{digest: digest, role: role, schema_version: schema_version, created_at: created_at}
        end)

      _ ->
        []
    end
  end

  @doc """
  One evidence_ref by digest, or nil. All columns plus `excerpt` and
  `browsable_url` parsed from the inline canonical body when present.
  """
  def get_evidence(digest) when is_binary(digest) do
    sql = """
    SELECT digest, source_id, kind, locator, trust_tier, summary, body, captured_at
    FROM evidence_ref WHERE digest = ?
    """

    case query(sql, [digest]) do
      {:ok, [[dg, source_id, kind, locator, trust_tier, summary, body, captured_at]]} ->
        payload = parse_json(body)

        %{
          digest: dg,
          source_id: source_id,
          kind: kind,
          locator: locator,
          trust_tier: trust_tier,
          summary: summary,
          body: body,
          captured_at: captured_at,
          excerpt: non_empty(payload["excerpt"]) || summary,
          browsable_url: get_in(payload, ["browsable_link", "url"])
        }

      _ ->
        nil
    end
  end

  def get_evidence(_), do: nil

  defp non_empty(value) when is_binary(value) and value != "", do: value
  defp non_empty(_), do: nil

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

  # Same shape as TournamentUi.Domains.query/2 (the mandated read pattern),
  # plus busy_timeout so a concurrent Python write transaction surfaces as a
  # short wait instead of SQLITE_BUSY. Missing tables raise inside `prepare`,
  # get caught here, and callers translate the error tuple to empty results.
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
