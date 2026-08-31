defmodule TournamentUi.Standings do
  @moduledoc """
  Read side of the points table.

  Every number this module returns was computed by `bin/standings_view.py`
  through `bin/swiss.py` and stored in the `standings_view` table. There is
  no points constant here, no discard set, and no rank derivation — a
  second scoring implementation in Elixir is exactly how the two halves
  came to disagree about who was in the pool (Elixir classified verdicts by
  prefix, Python by an exact map; Elixir folded pairings by
  `{db_path, match_id}`, Python by the pair key the design makes
  load-bearing). The fix is that only one of them scores anything.

  `table/1` is one SELECT plus a JSON decode, so a page paints without ever
  waiting on a Python process. `materialise/0` is the write side and is
  deliberately a separate call: run it off the render path.

  A scope that has never been materialised reports `materialised?: false`
  rather than an empty table — "nothing judged yet" and "nobody has
  recomputed the view" are different facts, and rendering them the same way
  is how a person mistakes an unrefreshed page for an empty corpus.
  """

  alias TournamentUi.Judgement

  @every_rater ""
  @every_domain ""
  @human "human"

  @only_a_persons_verdict_orders_the_queue_by_default """
  The default scope is human verdicts. The model panel plays the same pool
  in the background; letting it score by default would quietly make the
  ordering a model's opinion, which is the thing the tournament exists to
  prevent. Pass `rater_type: nil` to read the every-rater scope on purpose.
  """
  def only_a_persons_verdict_orders_the_queue_by_default,
    do: @only_a_persons_verdict_orders_the_queue_by_default

  @empty_totals %{rubrics: 0, items: 0, matches: 0, discarded: 0}

  @doc """
  The materialised table for one scope.

  Options: `:domain` (nil = every domain), `:rater_type` (default
  `"human"`; nil = every rater).

      %{
        materialised?: bool,
        computed_at: nil | binary,
        behind_by: n,             # verdict rows written since it was computed
        scope: %{domain: ..., rater_type: ...},
        tables: [table],          # one per pair rubric with judgements
        totals: %{rubrics:, items:, matches:, discarded:},
        unscored_verdicts: [%{verdict:, count:}],
        unpairable_rows: n
      }

  Each `table` carries `:rubric`, `:rubric_version`, `:round`, `:matches`,
  `:stale_matches`, `:top_group_points`, `:standings` and `:discards`,
  every field already final.
  """
  def table(opts \\ []) do
    domain = Keyword.get(opts, :domain) || @every_domain
    rater_type = Keyword.get(opts, :rater_type, @human) || @every_rater

    case stored(rater_type, domain) do
      nil -> empty(rater_type, domain)
      {document, computed_at, source_rows} -> decode(document, computed_at, source_rows)
    end
  end

  @doc """
  Recompute every scope by running `bin/standings_view.py refresh`.

  Blocking and explicit: never call it while painting a page. Returns `:ok`
  or `{:error, reason}` — the page keeps rendering the last good table
  either way, which is the point of materialising it.

  Tests can override the command with `STANDINGS_VIEW_CMD` (whitespace
  split; `refresh` is appended).
  """
  def materialise do
    {cmd, args} = refresh_cli()

    case System.cmd(cmd, args, cd: Judgement.repo_root(), stderr_to_stdout: true) do
      {_out, 0} -> :ok
      {out, status} -> {:error, "standings refresh exited #{status}: #{trim(out)}"}
    end
  rescue
    e -> {:error, Exception.message(e)}
  end

  @doc "True once any scope has been materialised at least once."
  def view_present? do
    case Judgement.fabric_query("SELECT 1 FROM standings_view LIMIT 1", []) do
      {:ok, [_ | _]} -> true
      _ -> false
    end
  end

  defp refresh_cli do
    case System.get_env("STANDINGS_VIEW_CMD") do
      nil ->
        {"python3", [Path.join(Judgement.repo_root(), "bin/standings_view.py"), "refresh"]}

      override ->
        [cmd | args] = String.split(override)
        {cmd, args ++ ["refresh"]}
    end
  end

  defp trim(out), do: out |> String.trim() |> String.slice(0, 300)

  defp stored(rater_type, domain) do
    sql = """
    SELECT document, computed_at, source_verdict_rows
    FROM standings_view WHERE rater_type = ? AND domain = ?
    """

    case Judgement.fabric_query(sql, [rater_type, domain]) do
      {:ok, [[document, computed_at, source_rows]]} -> {document, computed_at, source_rows}
      _ -> nil
    end
  end

  defp source_verdict_rows do
    case Judgement.fabric_query(
           "SELECT COUNT(*) FROM score WHERE name='judgement.verdict'",
           []
         ) do
      {:ok, [[n]]} -> n || 0
      _ -> 0
    end
  end

  defp empty(rater_type, domain) do
    %{
      materialised?: false,
      computed_at: nil,
      behind_by: 0,
      scope: scope(rater_type, domain),
      tables: [],
      totals: @empty_totals,
      unscored_verdicts: [],
      unpairable_rows: 0
    }
  end

  defp decode(document, computed_at, source_rows) do
    case Jason.decode(document) do
      {:ok, %{} = doc} ->
        %{
          materialised?: true,
          computed_at: computed_at,
          behind_by: max(source_verdict_rows() - (source_rows || 0), 0),
          scope:
            scope(
              get_in(doc, ["scope", "rater_type"]) || @every_rater,
              get_in(doc, ["scope", "domain"]) || @every_domain
            ),
          tables: Enum.map(doc["tables"] || [], &decode_table/1),
          totals: decode_totals(doc["totals"] || %{}),
          unscored_verdicts:
            Enum.map(doc["unscored_verdicts"] || [], fn row ->
              %{verdict: row["verdict"], count: row["count"]}
            end),
          unpairable_rows: doc["unpairable_rows"] || 0
        }

      _ ->
        empty(@human, @every_domain)
    end
  end

  defp scope(rater_type, domain) do
    %{
      rater_type: if(rater_type == @every_rater, do: nil, else: rater_type),
      domain: if(domain == @every_domain, do: nil, else: domain)
    }
  end

  defp decode_totals(totals) do
    %{
      rubrics: totals["rubrics"] || 0,
      items: totals["items"] || 0,
      matches: totals["matches"] || 0,
      discarded: totals["discarded"] || 0
    }
  end

  defp decode_table(table) do
    %{
      rubric: table["rubric"],
      rubric_version: table["rubric_version"],
      round: table["round"],
      matches: table["matches"] || 0,
      stale_matches: table["stale_matches"] || 0,
      top_group_points: table["top_group_points"],
      standings: Enum.map(table["standings"] || [], &decode_entry/1),
      discards: Enum.map(table["discards"] || [], &decode_discard/1)
    }
  end

  defp decode_entry(entry) do
    %{
      item_key: entry["item_key"],
      title: entry["title"],
      source_ref: entry["source_ref"],
      pool: entry["pool"],
      rank: entry["rank"],
      points: entry["points"],
      played: entry["played"],
      wins: entry["wins"],
      draws: entry["draws"],
      losses: entry["losses"],
      byes: entry["byes"],
      lost_honestly: entry["lost_honestly"] == true,
      awaiting_first_result: entry["awaiting_first_result"] == true,
      top_group: entry["top_group"] == true
    }
  end

  defp decode_discard(discard) do
    %{
      item_key: discard["item_key"],
      title: discard["title"],
      source_ref: discard["source_ref"],
      pool: discard["pool"],
      verdict: discard["verdict"],
      side: discard["side"],
      round: discard["round"],
      survivor_key: discard["survivor_key"],
      survivor_title: discard["survivor_title"]
    }
  end
end
