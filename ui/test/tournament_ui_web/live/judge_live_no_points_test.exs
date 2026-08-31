defmodule TournamentUiWeb.JudgeLiveNoPointsTest do
  @moduledoc """
  Regression lock: the judging surface never shows standing.

  `docs/design/priority-tournament.md` — "The judge never sees the score".
  The person comparing two items is shown two items; standing is a derived
  view for the queue, not context for the judgement, because showing it
  anchors the next comparison to the last one.

  This is written before a Swiss engine exists so the invariant is held by
  a test rather than by the absence of the data. The pool below carries
  non-zero, distinct standings on both sides and a full standings table on
  the payload; none of it may reach the rendered page, and none of it may
  reach the assigns the template is handed — unrendered-but-present is a
  leak waiting for the next template edit.

  ## The invariant is every absolute score, not only the tournament's

  The cards also carry the model's self-assessed `WorkOrder.priority`.
  It is not a standing — nothing in the tournament produced it — but it is
  an absolute score, produced by a model that saw one item and could not
  see the other thirty-two, and a red P0 beside a grey P2 anchors the next
  comparison exactly as hard as a points column would. Widening the scrub
  to it is the decision this file locks: `points` and `priority` leave the
  judging payload by the same pass, for the same reason.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  @points_a "731"
  @points_b "417"
  @rank_a "3"
  @score_a "0.9431"
  @priority "P0"

  @standing_keys ~w(points standing standings rank ranking score scores
                    matches_played played wins losses draws record
                    top_group leaderboard)

  @absolute_score_keys @standing_keys ++ ~w(priority)

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-judge-nopoints-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          os.environ['PROMPT_BACKEND'] = 'local'
          sys.path.insert(0, '#{repo_root}')
          sys.path.insert(0, '#{repo_root}/bin')

          import judgement
          judgement.init_db()
          import bin.domains as domains
          domains.create_domain(
              name='graded-pool',
              description='A pool that already has a settled order',
              corpus_source={'kind': 'inline', 'items': []},
          )

          db = sqlite3.connect(os.path.join('#{home}', 'judgements.db'))
          db.row_factory = sqlite3.Row
          cfg_id = db.execute(
              "SELECT c.id FROM job_configuration c JOIN eval_template t ON t.id = c.template_id "
              "WHERE c.rater_type='human' AND c.status='active' AND t.name='pair-wheel-v2'"
          ).fetchone()['id']
          dom = db.execute("SELECT id FROM domain WHERE name='graded-pool'").fetchone()['id']

          def scored_card(title, points, rank, wins, losses):
              return {
                  'kind': 'work-order',
                  'title': title,
                  'body': 'body of ' + title,
                  'source_ref': title.lower() + '.md',
                  'work_order': {
                      'title': title,
                      'work_type': 'bug-fix',
                      'priority': '#{@priority}',
                  },
                  'priority': '#{@priority}',
                  'points': points,
                  'rank': rank,
                  'standing': {'points': points, 'rank': rank},
                  'score': #{@score_a},
                  'wins': wins,
                  'losses': losses,
                  'draws': 1,
                  'matches_played': wins + losses + 1,
              }

          payload = json.dumps({
              'label': 'R2-1',
              'round': 2,
              'standings': [
                  {'title': 'CARDALPHA', 'points': #{@points_a}, 'rank': #{@rank_a}},
                  {'title': 'CARDBETA', 'points': #{@points_b}, 'rank': 9},
              ],
              'leaderboard': {'top_group': ['CARDALPHA']},
              'card_a': scored_card('CARDALPHA', #{@points_a}, #{@rank_a}, 5, 1),
              'card_b': scored_card('CARDBETA', #{@points_b}, 9, 2, 4),
          })

          db.execute(
              "INSERT INTO pending_judgement(config_id,tournament_db_path,match_id,trace_payload,domain_id)"
              " VALUES (?,?,?,?,?)",
              (cfg_id, 'domain:graded-pool', 1, payload, dom))
          db.commit()
          db.close()
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}, {"PROMPT_BACKEND", "local"}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    :ok
  end

  test "the pair still renders — the scrub removes standing, not content", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=graded-pool")

    assert html =~ "CARDALPHA"
    assert html =~ "CARDBETA"
    assert html =~ "body of CARDALPHA"
    assert html =~ "cardalpha.md"
  end

  test "no points value, rank, standings table or per-item score is rendered", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/judge?domain=graded-pool")

    refute html =~ @points_a
    refute html =~ @points_b
    refute html =~ @score_a
    refute String.contains?(String.downcase(html), "standing")
    refute String.contains?(String.downcase(html), "leaderboard")
    refute String.contains?(String.downcase(html), "points")
  end

  test "the model's self-assessed priority is scrubbed like a standing is", %{conn: conn} do
    {:ok, view, html} = live(conn, "/judge?domain=graded-pool")

    for card <- ["#judge-card-left", "#judge-card-right"] do
      rendered = view |> element(card) |> render()

      refute rendered =~ @priority,
             "priority is an absolute score: a model that saw one item rating it " <>
               "out of four, shown beside the item it is being compared against"

      assert rendered =~ "bug-fix",
             "work_type stays — it routes the item to branch_author or to a person, " <>
               "and this refutation must not pass by the badge row disappearing"
    end

    refute html =~ "bg-error/20 text-error",
           "priority_class/1 paints P0 in the loudest colour on the page"

    for side <- ["card_a", "card_b"] do
      card = :sys.get_state(view.pid).socket.assigns.active.trace_payload |> Map.fetch!(side)

      refute Map.has_key?(card, "priority"),
             "#{side} still carries priority at the card level in the judge assigns"

      refute Map.has_key?(card["work_order"], "priority"),
             "#{side} still carries priority inside work_order — the scrub must be " <>
               "as deep as the payload is"

      assert card["work_order"]["work_type"] == "bug-fix"
    end
  end

  test "the assigns handed to the template carry no standing key for either side", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/judge?domain=graded-pool")

    assigns = :sys.get_state(view.pid).socket.assigns
    payload = assigns.active.trace_payload

    assert deep_keys(payload) != []
    assert Enum.all?(@absolute_score_keys, &(&1 not in deep_keys(payload)))

    for side <- ["card_a", "card_b"] do
      card = Map.fetch!(payload, side)
      assert card["title"] in ["CARDALPHA", "CARDBETA"]

      for key <- @absolute_score_keys do
        refute Map.has_key?(card, key),
               "#{side} still carries the standing key #{key} in the judge assigns"
      end
    end

    for row <- assigns.pending do
      assert Enum.all?(@absolute_score_keys, &(&1 not in deep_keys(row.trace_payload)))
    end
  end

  defp deep_keys(value) when is_map(value) do
    Enum.flat_map(value, fn {key, inner} -> [key | deep_keys(inner)] end)
  end

  defp deep_keys(value) when is_list(value), do: Enum.flat_map(value, &deep_keys/1)
  defp deep_keys(_), do: []
end
