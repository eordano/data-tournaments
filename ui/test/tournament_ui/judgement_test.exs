defmodule TournamentUi.JudgementTest do
  @moduledoc """
  The fabric adapter, under the pair-wheel-v2 vocabulary.

  Two things this file now pins that used to drift: the rubric scope is READ
  from `eval_template` rather than named in Elixir, and a discard ejects the
  ONE side its verdict names.
  """
  use ExUnit.Case, async: false

  alias TournamentUi.Judgement

  @pair_rubric "pair-wheel-v2"

  defp data_home_no_crashed_earlier_run_can_hand_back(prefix) do
    home =
      Path.join("/tmp", "#{prefix}-#{Base.encode16(:crypto.strong_rand_bytes(8), case: :lower)}")

    File.rm_rf!(home)
    home
  end

  setup do
    home = data_home_no_crashed_earlier_run_can_hand_back("dt-judgement")
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
    {:ok, repo_root: repo_root}
  end

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

  defp raw_exec(sql, params) do
    {:ok, conn} = Sqlite3.open(Judgement.db_path())
    {:ok, stmt} = Sqlite3.prepare(conn, sql)
    :ok = Sqlite3.bind(stmt, params)
    :done = Sqlite3.step(conn, stmt)
    Sqlite3.release(conn, stmt)
    Sqlite3.close(conn)
    :ok
  end

  defp seed_pending(pool, match_id, payload, rubric \\ @pair_rubric) do
    [[cfg_id]] =
      raw_query(
        """
        SELECT c.id FROM job_configuration c
        JOIN eval_template t ON t.id = c.template_id
        WHERE t.name = ? AND c.rater_type='human' AND c.status='active'
        """,
        [rubric]
      )

    raw_exec(
      "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, trace_payload) VALUES (?,?,?,?)",
      [cfg_id, pool, match_id, Jason.encode!(payload)]
    )

    [[pending_id]] = raw_query("SELECT MAX(id) FROM pending_judgement")
    pending_id
  end

  defp round_pair(round, slot) do
    %{
      "label" => "R#{round}-#{slot}",
      "round" => round,
      "card_a" => %{"title" => "R#{round}S#{slot} A", "body" => "a"},
      "card_b" => %{"title" => "R#{round}S#{slot} B", "body" => "b"}
    }
  end

  defp seed_done_pending do
    pending_id =
      seed_pending("/tmp/revise.db", 1, %{
        "label" => "R1-1",
        "card_a" => %{"title" => "A", "body" => "a"},
        "card_b" => %{"title" => "B", "body" => "b"}
      })

    {:ok, rating_id} =
      Judgement.submit_human(pending_id, "a-wins", "mid",
        rationale: "first take",
        rater_id: "reviewer"
      )

    {pending_id, rating_id}
  end

  # ── Rubric scope ───────────────────────────────────────────────────────

  test "pair rubrics are read from the fabric, not named in Elixir" do
    assert Enum.sort(Judgement.pair_rubrics()) == ~w(pair-idea-wheel-v2 pair-wheel-v2)
  end

  test "single-artifact rubrics are not pair rubrics" do
    refute "single-idea-v1" in Judgement.pair_rubrics()
    refute "single-execution-v1" in Judgement.pair_rubrics()
  end

  test "a template predating judgement_kind still counts as a pair rubric" do
    raw_exec(
      "INSERT INTO eval_template(name, version, output_definition) VALUES (?,?,?)",
      ["legacy-pair", 1, Jason.encode!(%{"verdict_enum" => ["a-wins", "b-wins"]})]
    )

    assert "legacy-pair" in Judgement.pair_rubrics()
  end

  test "list_judgements defaults to every pair rubric, so a rubric move loses nothing" do
    a = seed_pending("pool:one", 1, round_pair(1, 1), "pair-wheel-v2")
    b = seed_pending("pool:one", 2, round_pair(1, 2), "pair-idea-wheel-v2")
    {:ok, _} = Judgement.submit_human(a, "a-wins", "mid")
    {:ok, _} = Judgement.submit_human(b, "b-wins", "mid")

    rubrics = Judgement.list_judgements() |> Enum.map(& &1.rubric) |> Enum.sort()

    assert rubrics == ~w(pair-idea-wheel-v2 pair-wheel-v2)
  end

  test "a named rubric narrows the scope" do
    a = seed_pending("pool:one", 1, round_pair(1, 1), "pair-wheel-v2")
    b = seed_pending("pool:one", 2, round_pair(1, 2), "pair-idea-wheel-v2")
    {:ok, _} = Judgement.submit_human(a, "a-wins", "mid")
    {:ok, _} = Judgement.submit_human(b, "b-wins", "mid")

    assert Judgement.list_judgements(rubric: "pair-idea-wheel-v2")
           |> Enum.map(& &1.rubric) == ["pair-idea-wheel-v2"]
  end

  test "export_records covers exactly the rubric set list_judgements reads" do
    a = seed_pending("pool:one", 1, round_pair(1, 1), "pair-wheel-v2")
    b = seed_pending("pool:one", 2, round_pair(1, 2), "pair-idea-wheel-v2")
    {:ok, ra} = Judgement.submit_human(a, "a-wins", "mid")
    {:ok, rb} = Judgement.submit_human(b, "b-wins", "mid")

    exported = Judgement.export_records() |> Enum.map(& &1.ratingId) |> Enum.sort()
    listed = Judgement.list_judgements() |> Enum.map(& &1.rating_id) |> Enum.sort()

    assert exported == listed
    assert exported == Enum.sort([ra, rb])
  end

  # ── Discard vocabulary ─────────────────────────────────────────────────

  test "the discard vocabulary is exactly bin/swiss.py's, per side", ctx do
    {out, 0} =
      System.cmd(
        "python3",
        [
          "-c",
          "import sys, json; sys.path.insert(0, '#{ctx.repo_root}'); " <>
            "from bin import swiss; " <>
            "print(json.dumps({k: v for k, v in sorted(swiss.EJECTED_SIDE_BY_VERDICT.items())}))"
        ],
        stderr_to_stdout: true
      )

    engine = out |> String.trim() |> Jason.decode!()

    assert Judgement.discard_verdicts() == Enum.sort(Map.keys(engine))

    for {verdict, side} <- engine do
      assert to_string(Judgement.discarded_side(verdict)) == side
    end
  end

  test "a discard names one side; nothing else discards" do
    assert Judgement.discarded_side("discard-a") == :a
    assert Judgement.discarded_side("discard-b") == :b
    assert Judgement.discarded_side("a-wins-big") == nil
    assert Judgement.discarded_side("tie") == nil
    refute Judgement.discard_verdict?("skip")
    refute Judgement.discard_verdict?("a-wins")
  end

  # ── Item identity ──────────────────────────────────────────────────────

  test "item_key is the digest bin/standings_view.py hashes", ctx do
    card = %{title: "T", body: "the judged body", source_ref: "x.md"}

    {out, 0} =
      System.cmd(
        "python3",
        [
          "-c",
          "import sys; sys.path.insert(0, '#{ctx.repo_root}'); " <>
            "from bin import standings_view; " <>
            "print(standings_view.item_key('the judged body'))"
        ],
        stderr_to_stdout: true
      )

    assert Judgement.item_key(card) == String.trim(out)
  end

  test "item_key ignores source_ref, so two findings from one corpus item differ" do
    one = %{title: "A", body: "first finding", source_ref: "same.md"}
    two = %{title: "B", body: "second finding", source_ref: "same.md"}

    assert Judgement.item_key(one) != Judgement.item_key(two)
  end

  test "item_key falls back to the title when there is no body" do
    assert Judgement.item_key(%{title: "only a title", body: "", source_ref: nil}) ==
             Judgement.item_key(%{title: "only a title", body: nil, source_ref: "elsewhere.md"})
  end

  # ── Pending status reaches the reader ──────────────────────────────────

  test "list_judgements carries the queue row's status" do
    {pending_id, _rating_id} = seed_done_pending()

    [row] = Judgement.list_judgements() |> Enum.filter(&(&1.pending_id == pending_id))
    assert row.pending_status == "done"

    raw_exec("UPDATE pending_judgement SET status='cancelled' WHERE id=?", [pending_id])

    [row] = Judgement.list_judgements() |> Enum.filter(&(&1.pending_id == pending_id))
    assert row.pending_status == "cancelled"
  end

  # ── wave-13 slice A: append-only revision ──────────────────────────────

  test "revise_human appends a new rating + revision row; old rows intact" do
    {pending_id, rating_id} = seed_done_pending()

    original_rows =
      raw_query("SELECT * FROM score WHERE rating_id=? ORDER BY id", [rating_id])

    assert {:ok, new_rating_id} =
             Judgement.revise_human(pending_id, rating_id, "b-wins-big", "high",
               reason: "misread B",
               revised_by: "reviewer-2"
             )

    assert new_rating_id != rating_id

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

    assert [["done", ^rating_id]] =
             raw_query("SELECT status, rating_id FROM pending_judgement WHERE id=?", [pending_id])

    chain = Judgement.revision_chain(pending_id)
    assert Enum.map(chain, & &1.rating_id) == [rating_id, new_rating_id]

    rows = Judgement.list_judgements() |> Enum.filter(&(&1.pending_id == pending_id))

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
             Judgement.revise_human(pending_id, "not-the-tip", "b-wins-big", "mid",
               reason: "real",
               revised_by: "u1"
             )

    assert msg =~ "stale revision"

    assert {:error, msg} =
             Judgement.revise_human(pending_id, rating_id, "b-wins-big", "mid",
               reason: "   ",
               revised_by: "u1"
             )

    assert msg =~ "reason"

    assert {:ok, _} =
             Judgement.revise_human(pending_id, rating_id, "b-wins-big", "mid",
               reason: "fix",
               revised_by: "u1"
             )

    assert {:error, msg} =
             Judgement.revise_human(pending_id, rating_id, "a-wins", "mid",
               reason: "too late",
               revised_by: "u2"
             )

    assert msg =~ "stale revision"

    assert [[1]] =
             raw_query("SELECT COUNT(*) FROM judgement_revision WHERE pending_id=?", [pending_id])
  end

  test "a cancelled queue row cannot be revised, and says so" do
    {pending_id, rating_id} = seed_done_pending()
    raw_exec("UPDATE pending_judgement SET status='cancelled' WHERE id=?", [pending_id])

    assert {:error, msg} =
             Judgement.revise_human(pending_id, rating_id, "b-wins", "mid",
               reason: "after the sweep",
               revised_by: "u1"
             )

    assert msg =~ "cancelled"
  end

  # ── Round-scoped queue ─────────────────────────────────────────────────

  test "payload_round reads the explicit round, falls back to the label, else nil" do
    assert Judgement.payload_round(%{"round" => 3, "label" => "R3-2"}) == 3
    assert Judgement.payload_round(%{"label" => "R7-1"}) == 7
    assert Judgement.payload_round(%{"label" => "Repository"}) == nil
    assert Judgement.payload_round(%{}) == nil
    assert Judgement.payload_round(nil) == nil
  end

  test "open_round_queue offers only the lowest open round of each pool" do
    seed_pending("pool:swiss", 1, round_pair(1, 1))
    seed_pending("pool:swiss", 2, round_pair(1, 2))
    seed_pending("pool:swiss", 3, round_pair(2, 1))

    queue = Judgement.open_round_queue(rater_type: "human")
    labels = Enum.map(queue.rows, &Map.get(&1.trace_payload, "label"))

    assert labels == ["R1-1", "R1-2"]
    assert queue.rounds["pool:swiss"] == %{round: 1, remaining: 2, total: 2}
  end

  test "open_round_queue advances to the next round only once the current one closes" do
    a = seed_pending("pool:swiss", 1, round_pair(1, 1))
    b = seed_pending("pool:swiss", 2, round_pair(1, 2))
    seed_pending("pool:swiss", 3, round_pair(2, 1))

    {:ok, _} = Judgement.submit_human(a, "a-wins", "mid")

    queue = Judgement.open_round_queue(rater_type: "human")
    assert Enum.map(queue.rows, &Map.get(&1.trace_payload, "label")) == ["R1-2"]
    assert queue.rounds["pool:swiss"] == %{round: 1, remaining: 1, total: 2}

    {:ok, _} = Judgement.submit_human(b, "b-wins", "mid")

    queue = Judgement.open_round_queue(rater_type: "human")
    assert Enum.map(queue.rows, &Map.get(&1.trace_payload, "label")) == ["R2-1"]
    assert queue.rounds["pool:swiss"] == %{round: 2, remaining: 1, total: 1}
  end

  test "open_round_queue scopes the open round per pool, not globally" do
    seed_pending("pool:one", 1, round_pair(1, 1))
    seed_pending("pool:two", 1, round_pair(4, 1))
    seed_pending("pool:two", 2, round_pair(5, 1))

    queue = Judgement.open_round_queue(rater_type: "human")

    assert Enum.map(queue.rows, & &1.tournament_db_path) |> Enum.sort() ==
             ["pool:one", "pool:two"]

    assert queue.rounds["pool:one"].round == 1
    assert queue.rounds["pool:two"].round == 4
  end

  test "open_round_queue always offers roundless payloads" do
    seed_pending("pool:legacy", 1, %{"label" => "hand seeded", "card_a" => %{"title" => "X"}})
    seed_pending("pool:swiss", 1, round_pair(1, 1))
    seed_pending("pool:swiss", 2, round_pair(2, 1))

    queue = Judgement.open_round_queue(rater_type: "human")
    labels = Enum.map(queue.rows, &Map.get(&1.trace_payload, "label"))

    assert "hand seeded" in labels
    assert "R1-1" in labels
    refute "R2-1" in labels
    refute Map.has_key?(queue.rounds, "pool:legacy")
  end

  test "open_round_queue drops a pool once every pending row of it is resolved" do
    id = seed_pending("pool:closed", 1, round_pair(1, 1))
    {:ok, _} = Judgement.submit_human(id, "a-wins", "mid")

    queue = Judgement.open_round_queue(rater_type: "human")

    assert queue.rows == []
    assert queue.rounds == %{}
  end
end
