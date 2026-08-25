defmodule TournamentUiWeb.InspectDownloadController do
  @moduledoc """
  GET /inspect/download?entity=...&fmt=json|csv&domain=...&status=...

  Sends the current filtered view as a JSON or CSV file. Same data the
  /inspect LiveView shows, just downloadable.
  """
  use TournamentUiWeb, :controller

  alias TournamentUi.Inspect, as: Data

  def show(conn, params) do
    entity = Map.get(params, "entity", "domains")
    fmt = Map.get(params, "fmt", "json")
    filters = build_filters(params)

    rows = fetch_rows(entity, filters)
    filename = "#{entity}-#{Date.utc_today() |> Date.to_iso8601()}.#{fmt}"

    case fmt do
      "json" ->
        conn
        |> put_resp_content_type("application/json")
        |> put_resp_header("content-disposition", "attachment; filename=\"#{filename}\"")
        |> send_resp(200, Jason.encode!(rows, pretty: true))

      "csv" ->
        body = to_csv(rows)

        conn
        |> put_resp_content_type("text/csv")
        |> put_resp_header("content-disposition", "attachment; filename=\"#{filename}\"")
        |> send_resp(200, body)

      _ ->
        send_resp(conn, 400, "fmt must be json or csv")
    end
  end

  defp build_filters(p) do
    %{
      domain: Map.get(p, "domain"),
      status: Map.get(p, "status"),
      run: Map.get(p, "run"),
      rater_type: Map.get(p, "rater_type")
    }
  end

  defp fetch_rows("domains", _), do: Data.domains()
  defp fetch_rows("pending", f), do: Data.pending(f)
  defp fetch_rows("scores", f), do: Data.scores(f)
  defp fetch_rows(_, _), do: []

  defp to_csv([]), do: ""

  defp to_csv(rows) do
    cols = rows |> List.first() |> Map.keys() |> Enum.map(&Atom.to_string/1)
    header = Enum.join(cols, ",")

    body =
      rows
      |> Enum.map(fn row ->
        cols
        |> Enum.map(fn c -> row |> Map.get(String.to_atom(c)) |> format_csv() end)
        |> Enum.join(",")
      end)
      |> Enum.join("\n")

    header <> "\n" <> body
  end

  defp format_csv(nil), do: ""
  defp format_csv(v) when is_binary(v), do: csv_escape(v)
  defp format_csv(v) when is_map(v) or is_list(v), do: csv_escape(Jason.encode!(v))
  defp format_csv(v), do: csv_escape(to_string(v))

  defp csv_escape(s) do
    if String.contains?(s, [",", "\"", "\n"]) do
      "\"" <> String.replace(s, "\"", "\"\"") <> "\""
    else
      s
    end
  end
end
