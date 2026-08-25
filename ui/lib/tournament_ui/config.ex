defmodule TournamentUi.Config do
  @moduledoc """
  Read/write the JSON config files that drive `run-tournament.py`.
  Directory is resolved via `TournamentUi.Paths.configs_dir/0`.
  """

  defp config_dir, do: TournamentUi.Paths.configs_dir()

  def list do
    case File.ls(config_dir()) do
      {:ok, files} ->
        files
        |> Enum.filter(&String.ends_with?(&1, ".json"))
        |> Enum.map(fn f ->
          path = Path.join(config_dir(), f)

          case File.read(path) do
            {:ok, body} ->
              case Jason.decode(body) do
                {:ok, json} ->
                  %{name: json["name"] || Path.basename(f, ".json"), path: path, json: json}

                _ ->
                  nil
              end

            _ ->
              nil
          end
        end)
        |> Enum.reject(&is_nil/1)
        |> Enum.sort_by(& &1.name)

      _ ->
        []
    end
  end

  def get(path) do
    with {:ok, body} <- File.read(path),
         {:ok, json} <- Jason.decode(body) do
      {:ok, json}
    end
  end

  def update_prompt(path, new_prompt) do
    with {:ok, body} <- File.read(path),
         {:ok, json} <- Jason.decode(body) do
      updated = Map.put(json, "match_prompt", new_prompt)
      File.write(path, Jason.encode!(updated, pretty: true))
    end
  end
end
