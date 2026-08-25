defmodule TournamentUiWeb.JudgementExportController do
  @moduledoc """
  /api/judgements/export — JSONL export of all judgements for a rubric,
  optionally filtered by rater type and domain.

  One JSON object per line. Designed to be fed straight into a fine-tuning
  harness (TRL, Axolotl, etc.) — see docs/judgement-fabric.md "Training
  pipeline" section.

  Query params:
    * rubric      (default: "card-prioritizer-v0")
    * rater_type  (optional: "human" | "llm" | "agent" | "programmatic")
    * domain      (optional domain name)

  Response:
    Content-Type: application/x-ndjson
    Content-Disposition: attachment; filename="judgements-<rubric>.jsonl"
  """
  use TournamentUiWeb, :controller
  alias TournamentUi.Judgement

  def export(conn, params) do
    rubric = Map.get(params, "rubric") || Judgement.default_rubric()
    rater_type = sanitize_rater_type(Map.get(params, "rater_type"))
    domain = normalize_domain(Map.get(params, "domain"))

    records = Judgement.export_records(rubric: rubric, rater_type: rater_type, domain: domain)

    body =
      records
      |> Enum.map(&Jason.encode!/1)
      |> Enum.join("\n")
      |> case do
        "" -> ""
        s -> s <> "\n"
      end

    fn_part = if rater_type, do: "-#{rater_type}", else: ""
    filename = "judgements-#{rubric}#{fn_part}.jsonl"

    conn
    |> put_resp_content_type("application/x-ndjson")
    |> put_resp_header("content-disposition", ~s|attachment; filename="#{filename}"|)
    |> send_resp(200, body)
  end

  defp sanitize_rater_type(nil), do: nil

  defp sanitize_rater_type(s) when is_binary(s) do
    if s in ~w(human llm agent programmatic), do: s, else: nil
  end

  defp normalize_domain(nil), do: nil
  defp normalize_domain(value), do: if(String.trim(value) == "", do: nil, else: value)
end
