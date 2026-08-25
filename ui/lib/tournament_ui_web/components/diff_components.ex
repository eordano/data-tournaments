defmodule TournamentUiWeb.DiffComponents do
  @moduledoc """
  GitHub-style rendering for parsed unified diffs (operator-environment-v13
  §5). Consumes `TournamentUi.Diff.parse_unified/1` output: a file-tree
  sidebar of anchor links, one card per file (sticky path header, +/−
  badges, A/M/D/R status chip, client-side collapse + 'viewed' checkbox),
  and hunk tables with old/new line-number gutters and +/− row tints.

  The diff text is CANDIDATE-AUTHORED — untrusted. Every path and line
  renders through HEEx interpolation (escaped); nothing is ever marked
  raw. Collapse/viewed state is pure client-side JS (JS.toggle) — the
  server holds no per-file UI state.
  """
  use Phoenix.Component

  alias Phoenix.LiveView.JS

  attr :files, :list, required: true, doc: "Diff.parse_unified/1 output"

  def diff_view(assigns) do
    assigns = assign(assigns, :indexed, Enum.with_index(assigns.files))

    ~H"""
    <div class="flex gap-4 items-start">
      <nav
        class="hidden lg:block w-60 shrink-0 sticky top-4 max-h-[70vh] overflow-y-auto"
        id="changed-files"
      >
        <ul class="space-y-0.5">
          <li :for={{f, idx} <- @indexed}>
            <a
              href={"#diff-file-#{idx}"}
              class="flex items-center gap-2 text-xs font-mono px-2 py-1 rounded hover:bg-base-200/60 transition"
              id={"file-tree-link-#{idx}"}
            >
              <span class={["text-[9px] font-bold w-3 shrink-0", status_text_class(f.status)]}>
                {status_letter(f.status)}
              </span>
              <span class="truncate flex-1" title={display_path(f)}>{display_path(f)}</span>
              <span class="text-success shrink-0">+{f.additions}</span>
              <span class="text-error shrink-0">−{f.deletions}</span>
            </a>
          </li>
        </ul>
      </nav>

      <div class="flex-1 min-w-0 space-y-3">
        <.file_card :for={{f, idx} <- @indexed} file={f} idx={idx} />
      </div>
    </div>
    """
  end

  attr :file, :map, required: true
  attr :idx, :integer, required: true

  defp file_card(assigns) do
    ~H"""
    <article
      class="border border-base-200 rounded-lg overflow-hidden"
      id={"diff-file-#{@idx}"}
    >
      <header class="sticky top-0 z-[1] flex items-center gap-2 flex-wrap px-3 py-2 bg-base-200/80 backdrop-blur border-b app-hairline">
        <button
          type="button"
          class="btn btn-ghost btn-xs px-1"
          id={"diff-collapse-#{@idx}"}
          phx-click={JS.toggle(to: "#diff-file-body-#{@idx}")}
          title="collapse/expand this file"
        >
          <span class="font-mono text-xs">▾</span>
        </button>
        <span class={[
          "text-[10px] font-bold px-1.5 py-0.5 rounded",
          status_chip_class(@file.status)
        ]}>
          {status_letter(@file.status)}
        </span>
        <span class="font-mono text-xs font-semibold truncate" title={display_path(@file)}>
          <%= if @file.status == :renamed do %>
            <span class="opacity-60">{@file.path_old}</span> → {@file.path_new}
          <% else %>
            {display_path(@file)}
          <% end %>
        </span>
        <span class="text-xs font-mono text-success">+{@file.additions}</span>
        <span class="text-xs font-mono text-error">−{@file.deletions}</span>
        <span
          :if={@file.truncated?}
          class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-warning/20 text-warning"
          id={"diff-file-truncated-#{@idx}"}
          title="this file exceeds the per-file render cap; the patch on disk is complete"
        >
          large file truncated
        </span>
        <label class="ml-auto flex items-center gap-1.5 text-xs opacity-70 cursor-pointer select-none">
          <input
            type="checkbox"
            class="checkbox checkbox-xs"
            id={"diff-viewed-#{@idx}"}
            phx-click={JS.toggle(to: "#diff-file-body-#{@idx}")}
          /> viewed
        </label>
      </header>

      <div id={"diff-file-body-#{@idx}"} class={@file.truncated? && "hidden"}>
        <p :if={@file.binary?} class="text-xs opacity-60 px-3 py-2 font-mono">
          Binary file — content not rendered.
        </p>
        <p
          :if={@file.truncated?}
          class="text-xs opacity-60 px-3 py-2"
          id={"diff-file-truncated-note-#{@idx}"}
        >
          Rendering stopped at the per-file line cap — counts above cover the
          whole file; the patch on disk is complete.
        </p>
        <table :if={@file.hunks != []} class="w-full text-xs font-mono border-collapse">
          <tbody>
            <%= for hunk <- @file.hunks do %>
              <tr class="diff-hunk-header bg-info/10 text-info">
                <td class="w-10 px-2 py-0.5 text-right select-none opacity-40" colspan="2">…</td>
                <td class="px-2 py-0.5 whitespace-pre-wrap break-all">{hunk.header}</td>
              </tr>
              <tr :for={ln <- hunk.lines} class={row_class(ln.type)}>
                <td class="diff-gutter-old w-10 px-2 py-0 text-right select-none opacity-40 align-top border-r app-hairline">
                  {ln.old_no}
                </td>
                <td class="diff-gutter-new w-10 px-2 py-0 text-right select-none opacity-40 align-top border-r app-hairline">
                  {ln.new_no}
                </td>
                <td class="px-2 py-0 whitespace-pre-wrap break-all align-top">
                  <span class="select-none opacity-60 mr-1">{sign(ln.type)}</span>{ln.text}
                </td>
              </tr>
            <% end %>
          </tbody>
        </table>
      </div>
    </article>
    """
  end

  # The b-side path is where the file ends up; deleted files only have a/.
  def display_path(%{path_new: nil, path_old: old}), do: old || "(unknown)"
  def display_path(%{path_new: new}), do: new

  defp status_letter(:added), do: "A"
  defp status_letter(:deleted), do: "D"
  defp status_letter(:renamed), do: "R"
  defp status_letter(_), do: "M"

  defp status_chip_class(:added), do: "bg-success/15 text-success"
  defp status_chip_class(:deleted), do: "bg-error/20 text-error"
  defp status_chip_class(:renamed), do: "bg-info/15 text-info"
  defp status_chip_class(_), do: "bg-warning/20 text-warning"

  defp status_text_class(:added), do: "text-success"
  defp status_text_class(:deleted), do: "text-error"
  defp status_text_class(:renamed), do: "text-info"
  defp status_text_class(_), do: "text-warning"

  defp row_class(:add), do: "diff-add bg-success/10"
  defp row_class(:del), do: "diff-del bg-error/10"
  defp row_class(:meta), do: "opacity-50"
  defp row_class(_), do: nil

  defp sign(:add), do: "+"
  defp sign(:del), do: "-"
  # Meta lines ("\\ No newline at end of file", corrupted-diff residue)
  # carry their own leading marker in the text itself.
  defp sign(:meta), do: ""
  defp sign(_), do: " "
end
