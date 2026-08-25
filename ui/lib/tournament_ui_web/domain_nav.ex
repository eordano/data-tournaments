defmodule TournamentUiWeb.DomainNav do
  @moduledoc """
  Domain-scoped navigation helpers shared by the Review (`/judge`),
  Results (`/results`), and Domains LiveViews.

  A `nil` (or blank) domain means the global, unfiltered view; any other
  value keeps links scoped to that domain so cross-page navigation never
  silently drops the active filter.
  """

  @doc """
  Normalize a `?domain=` query param: `nil` or `""` mean "no filter"
  (global view); anything else is the domain name.
  """
  def normalize(nil), do: nil
  def normalize(""), do: nil
  def normalize(value), do: value

  @doc "Path to the Review queue, scoped to `domain` when one is active."
  def judge_path(domain), do: scoped_path("/judge", normalize(domain))

  @doc "Path to the Results view, scoped to `domain` when one is active."
  def results_path(domain), do: scoped_path("/results", normalize(domain))

  defp scoped_path(base, nil), do: base
  defp scoped_path(base, domain), do: base <> "?domain=" <> URI.encode_www_form(domain)
end
