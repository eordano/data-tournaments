defmodule TournamentUiWeb.JudgementExportController do
  @moduledoc """
  /api/judgements/export — JSONL export of judgements, optionally filtered
  by rubric, rater type and domain.

  One JSON object per line. Designed to be fed straight into a fine-tuning
  harness (TRL, Axolotl, etc.) — see docs/judgement-fabric.md "Training
  pipeline" section.

  ## Scope matches the page

  With no `rubric` param the export covers `TournamentUi.Judgement`'s
  `pair_rubrics/0` — the same set /results renders. This controller used to
  default to one hardcoded rubric name while the page read a list, so after
  the rubric moved the export returned none of the judgements new work
  produced. There is no rubric name in this file for that reason.

  Query params:
    * rubric      (optional; default: every pair rubric on disk)
    * rater_type  (optional: "human" | "llm" | "agent" | "programmatic")
    * domain      (optional domain name)

  Response:
    Content-Type: application/x-ndjson
    Content-Disposition: attachment; filename="judgements-<scope>.jsonl"
  """
  use TournamentUiWeb, :controller
  alias TournamentUi.Judgement

  def export(conn, params) do
    rubrics = requested_rubrics(Map.get(params, "rubric"))
    rater_type = sanitize_rater_type(Map.get(params, "rater_type"))
    domain = normalize_domain(Map.get(params, "domain"))

    records = Judgement.export_records(rubrics: rubrics, rater_type: rater_type, domain: domain)

    body =
      records
      |> Enum.map(&Jason.encode!/1)
      |> Enum.join("\n")
      |> case do
        "" -> ""
        s -> s <> "\n"
      end

    conn
    |> put_resp_content_type("application/x-ndjson")
    |> put_resp_header(
      "content-disposition",
      ~s|attachment; filename="#{filename(rubrics, rater_type)}"|
    )
    |> send_resp(200, body)
  end

  defp requested_rubrics(nil), do: Judgement.pair_rubrics()

  defp requested_rubrics(name) when is_binary(name) do
    if String.trim(name) == "", do: Judgement.pair_rubrics(), else: [name]
  end

  defp filename(rubrics, rater_type) do
    scope =
      case rubrics do
        [one] -> one
        _ -> "pair-rubrics"
      end

    rater = if rater_type, do: "-#{rater_type}", else: ""
    "judgements-#{scope}#{rater}.jsonl"
  end

  defp sanitize_rater_type(nil), do: nil

  defp sanitize_rater_type(s) when is_binary(s) do
    if s in ~w(human llm agent programmatic), do: s, else: nil
  end

  defp normalize_domain(nil), do: nil
  defp normalize_domain(value), do: if(String.trim(value) == "", do: nil, else: value)
end
