defmodule TournamentUi.Diff do
  @moduledoc """
  Pure-Elixir unified-diff parser for the /branch-fixes/:id GitHub-style
  patch view (operator-environment-v13 §5). No shell-outs, no regex over
  attacker-controlled input beyond anchored header matching, and NEVER a
  crash: the diff text is candidate-authored evidence, so any shape we
  don't understand degrades — a malformed file section contributes what it
  can, and a text with no recognizable file headers parses to `[]` (the
  caller falls back to the raw escaped block).

  `parse_unified/1` returns one map per file:

      %{
        path_old: "lib/foo.ex" | nil,
        path_new: "lib/foo.ex" | nil,
        status: :added | :deleted | :modified | :renamed,
        additions: n,            # honest counts over the WHOLE section,
        deletions: n,            # even past the per-file render cap
        binary?: boolean,
        truncated?: boolean,     # per-file line cap hit (#{2000} lines)
        hunks: [
          %{header: "@@ -1,4 +1,5 @@", old_start: 1, new_start: 1,
            lines: [%{type: :add | :del | :ctx | :meta,
                      old_no: n | nil, new_no: n | nil, text: "..."}]}
        ]
      }

  Line numbering: `:ctx` carries both gutters, `:add` only the new
  number, `:del` only the old one, `:meta` (`\\ No newline at end of
  file`) neither. The per-file cap stops STORING lines but keeps
  COUNTING additions/deletions, so badges stay honest on capped files.
  """

  # Past this many stored lines a file renders collapsed with an honest
  # "large file truncated" chip; counts keep accumulating past the cap.
  @file_line_cap 2000

  def file_line_cap, do: @file_line_cap

  @doc "Parse unified diff text into per-file maps. Nil/garbage → []."
  def parse_unified(text) when is_binary(text) do
    text
    |> String.split("\n")
    |> drop_trailing_empty()
    |> Enum.reduce({[], nil, nil}, &step/2)
    |> finalize()
  rescue
    # The diff is untrusted input; a parser bug must degrade to the raw
    # block, never take the page down.
    _ -> []
  end

  def parse_unified(_), do: []

  # A newline-terminated diff splits into a trailing "" that is NOT a
  # context line — drop exactly that artifact.
  defp drop_trailing_empty(lines) do
    case List.last(lines) do
      "" -> Enum.drop(lines, -1)
      _ -> lines
    end
  end

  # ── reducer: {finished_files_rev, current_file, current_hunk} ──────────

  defp step("diff --git " <> rest, {files, file, hunk}) do
    {flush(files, file, hunk), new_file(rest), nil}
  end

  defp step(line, {files, nil, nil}) do
    # Prologue junk before the first file header contributes nothing.
    _ = line
    {files, nil, nil}
  end

  defp step("new file mode" <> _, {files, file, hunk}),
    do: {files, %{file | status: :added}, hunk}

  defp step("deleted file mode" <> _, {files, file, hunk}),
    do: {files, %{file | status: :deleted}, hunk}

  defp step("rename from " <> path, {files, file, hunk}),
    do: {files, %{file | status: :renamed, path_old: path}, hunk}

  defp step("rename to " <> path, {files, file, hunk}),
    do: {files, %{file | status: :renamed, path_new: path}, hunk}

  defp step("copy from " <> path, {files, file, hunk}),
    do: {files, %{file | path_old: path}, hunk}

  defp step("copy to " <> path, {files, file, hunk}),
    do: {files, %{file | path_new: path}, hunk}

  defp step("Binary files" <> _, {files, file, hunk}),
    do: {files, %{file | binary?: true}, hunk}

  defp step("GIT binary patch" <> _, {files, file, hunk}),
    do: {files, %{file | binary?: true}, hunk}

  defp step("--- " <> rest, {files, file, nil}) do
    case rest do
      "/dev/null" -> {files, %{file | status: :added, path_old: nil}, nil}
      "a/" <> path -> {files, %{file | path_old: path}, nil}
      _ -> {files, file, nil}
    end
  end

  defp step("+++ " <> rest, {files, file, nil}) do
    case rest do
      "/dev/null" -> {files, %{file | status: :deleted, path_new: nil}, nil}
      "b/" <> path -> {files, %{file | path_new: path}, nil}
      _ -> {files, file, nil}
    end
  end

  defp step("@@" <> _ = line, {files, file, hunk}) do
    case parse_hunk_header(line) do
      {old_start, new_start} ->
        file = close_hunk(file, hunk)

        {files, file,
         %{
           header: line,
           old_start: old_start,
           new_start: new_start,
           old_no: old_start,
           new_no: new_start,
           lines: []
         }}

      :error ->
        # Unparseable @@ line inside a file: treat as opaque meta text.
        {files, file, push_line(hunk, :meta, nil, nil, line)}
    end
  end

  # Body lines only mean something inside a hunk.
  defp step(_line, {files, file, nil}), do: {files, file, nil}

  defp step("\\" <> _ = line, {files, file, hunk}),
    do: {files, file, push_line(hunk, :meta, nil, nil, line)}

  defp step("+" <> text, {files, file, hunk}) do
    file = %{file | additions: file.additions + 1}
    hunk = push_line(hunk, :add, nil, hunk.new_no, text)
    {files, file, %{hunk | new_no: hunk.new_no + 1}}
  end

  defp step("-" <> text, {files, file, hunk}) do
    file = %{file | deletions: file.deletions + 1}
    hunk = push_line(hunk, :del, hunk.old_no, nil, text)
    {files, file, %{hunk | old_no: hunk.old_no + 1}}
  end

  defp step(" " <> text, {files, file, hunk}) do
    hunk = push_line(hunk, :ctx, hunk.old_no, hunk.new_no, text)
    {files, file, %{hunk | old_no: hunk.old_no + 1, new_no: hunk.new_no + 1}}
  end

  # A completely empty line inside a hunk is a context line whose trailing
  # space got stripped somewhere along the way.
  defp step("", {files, file, hunk}) do
    hunk = push_line(hunk, :ctx, hunk.old_no, hunk.new_no, "")
    {files, file, %{hunk | old_no: hunk.old_no + 1, new_no: hunk.new_no + 1}}
  end

  # Anything else mid-hunk (corrupted diff): opaque meta line, never a crash.
  defp step(line, {files, file, hunk}),
    do: {files, file, push_line(hunk, :meta, nil, nil, line)}

  # ── helpers ────────────────────────────────────────────────────────────

  defp new_file(header_rest) do
    {path_old, path_new} =
      case Regex.run(~r{\Aa/(.+) b/(.+)\z}, header_rest) do
        [_, a, b] -> {a, b}
        _ -> {header_rest, header_rest}
      end

    %{
      path_old: path_old,
      path_new: path_new,
      status: :modified,
      additions: 0,
      deletions: 0,
      binary?: false,
      truncated?: false,
      line_count: 0,
      hunks: []
    }
  end

  # "@@ -1,4 +1,5 @@ optional section" → {1, 1}. Zero-length sides
  # ("-0,0") are legal for new/deleted files.
  defp parse_hunk_header(line) do
    case Regex.run(~r/\A@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/, line) do
      [_, old_s, new_s] -> {String.to_integer(old_s), String.to_integer(new_s)}
      _ -> :error
    end
  end

  defp push_line(nil, _type, _old_no, _new_no, _text), do: nil

  defp push_line(hunk, type, old_no, new_no, text) do
    %{hunk | lines: [%{type: type, old_no: old_no, new_no: new_no, text: text} | hunk.lines]}
  end

  # Fold the open hunk into the file. Applies the per-file line cap:
  # stored lines stop at @file_line_cap, but the file keeps its honest
  # additions/deletions totals (counted in step/2 regardless).
  defp close_hunk(file, nil), do: file

  defp close_hunk(file, hunk) do
    lines = Enum.reverse(hunk.lines)
    remaining = @file_line_cap - file.line_count

    {kept, truncated?} =
      cond do
        remaining <= 0 -> {[], true}
        length(lines) > remaining -> {Enum.take(lines, remaining), true}
        true -> {lines, file.truncated?}
      end

    hunk =
      hunk
      |> Map.take([:header, :old_start, :new_start])
      |> Map.put(:lines, kept)

    hunks =
      if kept == [] and file.line_count >= @file_line_cap,
        do: file.hunks,
        else: [hunk | file.hunks]

    %{file | hunks: hunks, line_count: file.line_count + length(kept), truncated?: truncated?}
  end

  defp flush(files, nil, _hunk), do: files

  defp flush(files, file, hunk) do
    file = close_hunk(file, hunk)

    file =
      file
      |> Map.update!(:hunks, &Enum.reverse/1)
      |> Map.delete(:line_count)

    [file | files]
  end

  defp finalize({files, file, hunk}), do: files |> flush(file, hunk) |> Enum.reverse()
end
