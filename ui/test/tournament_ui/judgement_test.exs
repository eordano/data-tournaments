defmodule TournamentUi.JudgementTest do
  use ExUnit.Case, async: false

  alias TournamentUi.Judgement

  setup do
    home = "/tmp/dt-judgement-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root}')
          import bin.prompts as p
          class _S:
              class api:
                  class prompts:
                      @staticmethod
                      def list(**kw):
                          M = type('M', (), {'total_pages': 1})
                          return type('R', (), {'data': [], 'meta': M})()
                  class prompt_version:
                      @staticmethod
                      def update(**kw): return None
              def get_prompt(self, name, label='production', version=None):
                  raise LookupError(name)
              def create_prompt(self, **kw):
                  return type('P', (), {'version': 1, 'prompt': kw['prompt'], 'name': kw['name'], 'labels': kw.get('labels') or []})()
          p._client_factory = lambda: _S()
          from bin import judgement
          judgement.init_db()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "default rubric matches the seeded card prioritizer rubric" do
    assert Judgement.default_rubric() == "card-prioritizer-v0"
  end

  # ── wave-13 slice A: append-only revision ──────────────────────────────

  alias Exqlite.Sqlite3

  defp raw_query(sql, params \\ []) do
    {:ok, conn} = Sqlite3.open(Judgement.db_path())
    {:ok, stmt} = Sqlite3.prepare(conn, sql)
    :ok = Sqlite3.bind(stmt, params)
    rows = collect(conn, stmt, [])
    Sqlite3.release(conn, stmt)
    Sqlite3.close(conn)
    Enum.reverse(rows)
  end

  defp collect(conn, stmt, acc) do
    case Sqlite3.step(conn, stmt) do
      {:row, row} -> collect(conn, stmt, [row | acc])
      :done -> acc
    end
  end

  defp seed_done_pending do
    [[cfg_id]] =
      raw_query("""
      SELECT c.id FROM job_configuration c
      JOIN eval_template t ON t.id = c.template_id
      WHERE t.name='card-prioritizer-v0' AND c.rater_type='human' AND c.status='active'
      """)

    payload =
      Jason.encode!(%{
        "label" => "R1-1",
        "card_a" => %{"title" => "A", "body" => "a"},
        "card_b" => %{"title" => "B", "body" => "b"}
      })

    {:ok, conn} = Sqlite3.open(Judgement.db_path())

    {:ok, stmt} =
      Sqlite3.prepare(
        conn,
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?,?,?,?)"
      )

    :ok = Sqlite3.bind(stmt, [cfg_id, "/tmp/revise.db", 1, payload])
    :done = Sqlite3.step(conn, stmt)
    Sqlite3.release(conn, stmt)
    [[pending_id]] = raw_query("SELECT MAX(id) FROM pending_judgement")
    Sqlite3.close(conn)

    {:ok, rating_id} =
      Judgement.submit_human(pending_id, "a-clearly-better", "mid",
        rationale: "first take",
        rater_id: "reviewer"
      )

    {pending_id, rating_id}
  end

  test "revise_human appends a new rating + revision row; old rows intact" do
    {pending_id, rating_id} = seed_done_pending()

    original_rows =
      raw_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id])

    assert {:ok, new_rating_id} =
             Judgement.revise_human(pending_id, rating_id, "b-clearly-better", "high",
               reason: "misread B",
               revised_by: "reviewer-2"
             )

    assert new_rating_id != rating_id

    # Raw SQL: 2 ratings' worth of score rows, 1 revision row, old rows intact.
    [[n_scores]] =
      raw_query("SELECT COUNT(*) FROM score WHERE pending_id=?", [pending_id])

    assert n_scores == 4

    [[n_ratings]] =
      raw_query("SELECT COUNT(DISTINCT rating_id) FROM score WHERE pending_id=?", [pending_id])

    assert n_ratings == 2

    assert [[^pending_id, ^rating_id, ^new_rating_id, "reviewer-2", "misread B"]] =
             raw_query(
               "SELECT pending_id, previous_rating_id, new_rating_id, revised_by, reason FROM judgement_revision WHERE pending_id=?",
               [pending_id]
             )

    assert raw_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id]) ==
             original_rows

    # Pending stays 'done'; rating_id column untouched.
    assert [["done", ^rating_id]] =
             raw_query("SELECT status, rating_id FROM pending_judgement WHERE id=?", [pending_id])

    # Chain + list_judgements annotations.
    chain = Judgement.revision_chain(pending_id)
    assert Enum.map(chain, & &1.rating_id) == [rating_id, new_rating_id]

    rows =
      Judgement.list_judgements(rubric: "card-prioritizer-v0")
      |> Enum.filter(&(&1.pending_id == pending_id))

    tip = Enum.find(rows, &(&1.rating_id == new_rating_id))
    old = Enum.find(rows, &(&1.rating_id == rating_id))
    assert tip.revised and not tip.superseded
    assert tip.revision_reason == "misread B"
    assert old.superseded and not old.revised
    assert length(tip.revision_chain) == 2
  end

  test "revise_human refuses stale previous ratings, empty reason, undone pendings" do
    {pending_id, rating_id} = seed_done_pending()

    assert {:error, msg} =
             Judgement.revise_human(pending_id, "not-the-tip", "b-clearly-better", "mid",
               reason: "real",
               revised_by: "u1"
             )

    assert msg =~ "stale revision"

    assert {:error, msg} =
             Judgement.revise_human(pending_id, rating_id, "b-clearly-better", "mid",
               reason: "   ",
               revised_by: "u1"
             )

    assert msg =~ "reason"

    # Double-revise with the original id refused after the first succeeds.
    assert {:ok, _} =
             Judgement.revise_human(pending_id, rating_id, "b-clearly-better", "mid",
               reason: "fix",
               revised_by: "u1"
             )

    assert {:error, msg} =
             Judgement.revise_human(pending_id, rating_id, "a-clearly-better", "mid",
               reason: "too late",
               revised_by: "u2"
             )

    assert msg =~ "stale revision"

    assert [[1]] =
             raw_query("SELECT COUNT(*) FROM judgement_revision WHERE pending_id=?", [pending_id])
  end
end
