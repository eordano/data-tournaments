defmodule TournamentUi.Builder do
  @moduledoc """
  Helpers for assembling a new tournament config + running a preview match.
  """

  defp config_dir, do: TournamentUi.Paths.configs_dir()
  defp upload_root, do: TournamentUi.Paths.uploads_dir()

  def upload_dir(tournament_name) do
    slug = slugify(tournament_name)
    Path.join(upload_root(), slug)
  end

  def slugify(name) do
    name
    |> String.downcase()
    |> String.replace(~r/[^a-z0-9]+/, "-")
    |> String.trim("-")
  end

  def write_config(params) do
    name = params[:name]
    slug = slugify(name)

    json = %{
      "name" => slug,
      "seed" => params[:seed],
      "parallelism" => params[:parallelism],
      "advance" => params[:advance] || "synthesis",
      "db_path" => "/tmp/#{slug}.db",
      "workdir" => "/tmp/#{slug}",
      "inputs" => params[:inputs],
      "required_sections" => params[:required_sections],
      "match_prompt" => params[:match_prompt]
    }

    File.mkdir_p!(config_dir())
    path = Path.join(config_dir(), "#{slug}.json")
    File.write!(path, Jason.encode!(json, pretty: true))
    {:ok, path, slug}
  end

  @doc """
  Synchronously runs ONE match through the Hermes harness against two files.
  Returns `{:ok, markdown, stderr_tail}` or `{:error, reason}`.
  """
  def preview_match(match_prompt, required_sections, file_a, file_b, opts \\ []) do
    harness = TournamentUi.Paths.harness()

    prompt =
      match_prompt
      |> String.replace("{LABEL}", "preview")
      |> String.replace("{INPUTS}", "  1. #{file_a}\n  2. #{file_b}")
      |> String.replace("{N_INPUTS}", "2")

    env = [
      {"TOURNAMENT_PROMPT_OVERRIDE", prompt},
      {"TOURNAMENT_REQUIRED_SECTIONS", Enum.join(required_sections, "\x1f")}
    ]

    args = ["-p", to_string(opts[:parallelism] || 1), "preview", file_a, file_b]

    case System.cmd(harness, args, env: env, stderr_to_stdout: false, into: "") do
      {stdout, 0} -> {:ok, stdout, ""}
      {stdout, code} -> {:error, "harness exit #{code}: #{String.slice(stdout, 0, 2000)}"}
    end
  end
end
