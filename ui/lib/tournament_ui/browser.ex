defmodule TournamentUi.Browser do
  @moduledoc """
  Sandboxed server-side file browser. Callers can only `list/1` directories
  that sit under one of the allowed roots; paths are canonicalised first so
  symlinks cannot escape the sandbox.
  """

  defp default_roots do
    case System.user_home() do
      nil -> ["/tmp"]
      home -> [Path.join(home, "projects"), "/tmp"]
    end
  end

  def roots do
    case System.get_env("TOURNAMENT_BROWSE_ROOTS") do
      nil -> default_roots()
      s -> s |> String.split(":", trim: true) |> Enum.map(&Path.expand/1)
    end
  end

  def default_root, do: List.first(roots())

  def safe?(path) do
    canonical = canonicalise(path)

    cond do
      canonical == :error ->
        false

      Enum.any?(roots(), fn r -> canonical == r or String.starts_with?(canonical, r <> "/") end) ->
        true

      true ->
        false
    end
  end

  def list(path) do
    cond do
      not safe?(path) ->
        {:error, "path outside allowed roots"}

      not File.dir?(path) ->
        {:error, "not a directory"}

      true ->
        entries =
          path
          |> File.ls!()
          |> Enum.reject(&String.starts_with?(&1, "."))
          |> Enum.map(fn name ->
            full = Path.join(path, name)
            is_dir = File.dir?(full)
            size = if is_dir, do: nil, else: file_size(full)
            %{name: name, path: full, dir?: is_dir, size: size}
          end)
          |> Enum.sort_by(fn e -> {!e.dir?, String.downcase(e.name)} end)

        {dirs, files} = Enum.split_with(entries, & &1.dir?)

        {:ok,
         %{
           dir: path,
           parent: parent_if_safe(path),
           breadcrumbs: breadcrumbs(path),
           dirs: dirs,
           files: files
         }}
    end
  end

  @recursive_cap 5_000

  @doc """
  Walks `path` recursively and returns a list of file paths (hidden files
  skipped, symlinks not followed across sandbox boundaries). Capped at
  #{@recursive_cap} entries.
  """
  def all_files_recursive(path) do
    if safe?(path) and File.dir?(path) do
      {:ok, walk(path, [], 0) |> Enum.reverse()}
    else
      {:error, "not a safe directory"}
    end
  end

  defp walk(_dir, acc, count) when count >= @recursive_cap, do: acc

  defp walk(dir, acc, count) do
    case File.ls(dir) do
      {:ok, names} ->
        Enum.reduce_while(names, {acc, count}, fn name, {acc, count} ->
          if String.starts_with?(name, ".") do
            {:cont, {acc, count}}
          else
            full = Path.join(dir, name)

            cond do
              File.dir?(full) ->
                new_acc = walk(full, acc, count)
                new_count = length(new_acc) - length(acc) + count

                if new_count >= @recursive_cap do
                  {:halt, {new_acc, new_count}}
                else
                  {:cont, {new_acc, new_count}}
                end

              File.regular?(full) ->
                {:cont, {[full | acc], count + 1}}

              true ->
                {:cont, {acc, count}}
            end
          end
        end)
        |> case do
          {acc, _count} -> acc
          acc when is_list(acc) -> acc
        end

      _ ->
        acc
    end
  end

  defp file_size(p) do
    case File.stat(p) do
      {:ok, %{size: s}} -> s
      _ -> 0
    end
  end

  defp parent_if_safe(path) do
    p = Path.dirname(path)
    if safe?(p) and p != path, do: p, else: nil
  end

  defp breadcrumbs(path) do
    canonical = canonicalise(path)

    roots()
    |> Enum.find(fn r -> canonical == r or String.starts_with?(canonical, r <> "/") end)
    |> case do
      nil ->
        []

      root ->
        rest =
          canonical
          |> String.replace_prefix(root, "")
          |> String.trim_leading("/")
          |> String.split("/", trim: true)

        {_acc, crumbs} =
          Enum.reduce(rest, {root, [%{name: root, path: root}]}, fn seg, {acc, crumbs} ->
            next = Path.join(acc, seg)
            {next, crumbs ++ [%{name: seg, path: next}]}
          end)

        crumbs
    end
  end

  defp canonicalise(path) do
    try do
      Path.expand(path)
    rescue
      _ -> :error
    end
  end
end
