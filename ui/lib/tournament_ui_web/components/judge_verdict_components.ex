defmodule TournamentUiWeb.JudgeVerdictComponents do
  @moduledoc """
  Wave-12 semantic verdict pickers (docs/design/judgement-wheel-v2.md).

  * `verdict_wheel/1` — 3×3 compass for PairJudgements. Grid placement IS
    the semantics: nw|n|ne / w|center|e / sw|s|se. Center cell shows the
    A-vs-B legend, the active subject badge, and the selected verdict.
  * `verdict_axis/1` — vertical 4-position axis (n/ne/se/s, top = best)
    for SingleJudgements.
  * `operational_verdicts/1` — verdicts in `verdict_enum` but off the
    wheel (skip, needs-evidence, …) as small buttons.
  * `subject_stepper/1` — idea → execution progress header.

  Every button keeps the existing `phx-click="set_verdict"
  phx-value-v=<verdict>` contract — the LiveView handler is untouched.

  `normalize_rubric/1` is the single normalization helper the contract
  calls for: it maps any legacy `output_definition` to the v2 shape
  (kind "pair", subjects ["execution"], no wheel) and treats every new
  key as optional/defensive — never trusts sibling-owned template data.

  ## The glyph vocabulary is the current eight verdicts

  Glyphs are POSITION-driven (geometry carries meaning per the contract);
  labels are VERDICT-driven so a template can never be misrepresented.
  The horizontal axis names the side the verdict is about, the vertical
  axis says what happens to it:

      w  ◀◀  A clearly      nw ◀  A marginally      n  ≡  tie
      e  ▶▶  B clearly      ne ▶  B marginally      sw ✕A / se ✕B  eject

  plus `skip` (↻), off the wheel. That is eight verdicts and no more.
  `s` (due south) has no glyph of its own because the rubric asserts the
  position stays empty — there is no "both are bad" verdict, and one
  malformed card must not destroy the good card beside it. A template
  that puts something there still renders (the component stays total),
  but it renders as an unnamed `•` rather than borrowing a meaning.

  The retired `*-lean-both-invalid` vocabulary drew the southern
  diagonals as `◀▽` / `▶▽` — "A wins, weakly". Under the current
  vocabulary sw/se EJECT that side, the opposite reading, so the arrow is
  gone: `✕A` says which item leaves.

  ## The discard verdicts are destructive and dressed as such

  `eject_consequence/2` is the one-line consequence a judge is owed
  before ejecting — it names what happens to the OTHER side, which is the
  half people get wrong. `ejects?/2` drives the error palette and
  `data-destructive` on those cells so a permanent verdict never weighs
  the same as a reversible one.

  The dressing is keyed on the VERDICT alone — the discard vocabulary the
  swiss engine actually ejects on (`DISCARD_VERDICTS` in bin/swiss.py) —
  never on wheel geometry: the SingleJudgement axis reuses position "se"
  for verdicts that merely score, and a position-keyed rule dressed that
  rung as "Ejects B permanently" on a payload with no B in it. The axis's
  bottom rung (`invalid` / `reject-invalid`) stays undressed for the same
  reason: the engine records those as score rows and ejects nothing.
  """
  use Phoenix.Component

  @wheel_positions ~w(nw n ne w e sw s se)
  @axis_positions ~w(n ne se s)

  @numpad_to_position %{
    "7" => "nw",
    "8" => "n",
    "9" => "ne",
    "4" => "w",
    "6" => "e",
    "1" => "sw",
    "2" => "s",
    "3" => "se"
  }

  @wheel_cells [
    {"nw", "col-start-1 row-start-1"},
    {"n", "col-start-2 row-start-1"},
    {"ne", "col-start-3 row-start-1"},
    {"w", "col-start-1 row-start-2"},
    {"e", "col-start-3 row-start-2"},
    {"sw", "col-start-1 row-start-3"},
    {"s", "col-start-2 row-start-3"},
    {"se", "col-start-3 row-start-3"}
  ]

  @doc """
  Wheel position for a digit key ('7' → 'nw'), nil for non-wheel keys.

  Numpad geometry, also accepted from the top digit row:

      7 8 9      nw n  ne
      4   6  →   w     e
      1 2 3      sw s  se
  """
  def numpad_position(key), do: Map.get(@numpad_to_position, key)

  @doc "The 4 positions a SingleJudgement axis uses, top to bottom."
  def axis_positions, do: @axis_positions

  @doc """
  The digit keys that actually do something on this rubric, ascending.

  Derived from the wheel the template declares, never hardcoded: the
  shipped pair rubric leaves due south empty, so "1-9 compass" was a hint
  advertising two keys (2 and 5) that do nothing — and 2 sits physically
  between the two permanent verdicts.
  """
  def numpad_keys(%{wheel: nil}), do: ""

  def numpad_keys(%{wheel: wheel, kind: kind}) do
    live = if kind == "single", do: @axis_positions, else: @wheel_positions

    @numpad_to_position
    |> Enum.filter(fn {_key, pos} -> pos in live and Map.has_key?(wheel, pos) end)
    |> Enum.map(fn {key, _pos} -> key end)
    |> Enum.sort()
    |> Enum.join(" ")
  end

  @doc """
  The one-line consequence of a destructive verdict, or nil.

  It names what happens to the OTHER side, because that is the half a
  judge gets wrong: a discard ejects the item it names and leaves the
  other in the pool UNCREDITED — no win, no played match, drawn again
  next round. `docs/design/priority-tournament.md`, "Discard is a verdict,
  not a loss, and it is PER SIDE".

  Keyed on the verdict alone, so a flat rubric with no wheel still gets
  the warning and a rubric that reuses an eject position for a scoring
  verdict never inherits it.
  """
  def eject_consequence(verdict) do
    case ejected_pair(verdict) do
      nil ->
        nil

      {gone, kept} ->
        "Ejects #{gone} permanently; #{kept} is not credited and is paired again."
    end
  end

  @doc "True when this verdict ends an item's run in the pool for good."
  def ejects?(verdict), do: ejected_pair(verdict) != nil

  defp ejected_pair("discard-a"), do: {"A", "B"}
  defp ejected_pair("discard-b"), do: {"B", "A"}
  defp ejected_pair(_verdict), do: nil

  @doc """
  Normalize an `output_definition` map into the v2 rubric shape:

      %{kind: "pair" | "single",
        subjects: ["idea" | "execution", ...],   # never empty
        wheel: %{"n" => verdict, ...} | nil,      # only valid positions/verdicts
        verdict_enum: [...],
        operational: [...]}                       # enum verdicts off the wheel/axis

  Defensive defaults per the contract: absent `judgement_kind` ⇒ "pair",
  absent `subjects` ⇒ ["execution"], absent/invalid `wheel` ⇒ nil (legacy
  flat row). Wheel entries whose verdict is not in `verdict_enum` are
  dropped rather than rendered.
  """
  def normalize_rubric(outdef) when is_map(outdef) do
    verdict_enum = string_list(Map.get(outdef, "verdict_enum"))
    wheel = normalize_wheel(Map.get(outdef, "wheel"), verdict_enum)
    kind = if Map.get(outdef, "judgement_kind") == "single", do: "single", else: "pair"

    on_wheel =
      case {wheel, kind} do
        {nil, _} ->
          []

        {wheel, "single"} ->
          @axis_positions |> Enum.map(&Map.get(wheel, &1)) |> Enum.reject(&is_nil/1)

        {wheel, _} ->
          Map.values(wheel)
      end

    %{
      kind: kind,
      subjects: normalize_subjects(Map.get(outdef, "subjects")),
      wheel: wheel,
      verdict_enum: verdict_enum,
      operational: if(wheel, do: Enum.reject(verdict_enum, &(&1 in on_wheel)), else: [])
    }
  end

  def normalize_rubric(_), do: normalize_rubric(%{})

  defp normalize_wheel(wheel, verdict_enum) when is_map(wheel) do
    cleaned =
      wheel
      |> Enum.filter(fn {pos, v} ->
        pos in @wheel_positions and is_binary(v) and v in verdict_enum
      end)
      |> Map.new()

    if map_size(cleaned) > 0, do: cleaned, else: nil
  end

  defp normalize_wheel(_wheel, _verdict_enum), do: nil

  defp normalize_subjects(subjects) when is_list(subjects) do
    case subjects |> Enum.filter(&(is_binary(&1) and &1 != "")) |> Enum.uniq() do
      [] -> ["execution"]
      list -> list
    end
  end

  defp normalize_subjects(_), do: ["execution"]

  defp string_list(list) when is_list(list), do: Enum.filter(list, &is_binary/1)
  defp string_list(_), do: []

  # ── Components ─────────────────────────────────────────────────────────

  attr :wheel, :map, required: true, doc: "position ⇒ verdict map (already normalized)"
  attr :chosen, :any, required: true
  attr :subject, :any, required: true

  def verdict_wheel(assigns) do
    assigns = assign(assigns, :cells, @wheel_cells)

    ~H"""
    <div
      id="verdict-wheel"
      role="radiogroup"
      aria-label="Verdict compass — position carries meaning"
      class="grid grid-cols-3 gap-2 max-w-2xl"
    >
      <%= for {pos, grid_class} <- @cells do %>
        <%= case Map.get(@wheel, pos) do %>
          <% nil -> %>
            <div class={["min-h-[44px]", grid_class]} aria-hidden="true"></div>
          <% v -> %>
            <button
              type="button"
              id={"wheel-#{pos}"}
              role="radio"
              aria-checked={to_string(@chosen == v)}
              phx-click="set_verdict"
              phx-value-v={v}
              title={eject_consequence(v) || v}
              data-destructive={to_string(ejects?(v))}
              class={[
                "btn h-auto min-h-[44px] min-w-[44px] py-2 px-2 flex-col gap-1 normal-case leading-tight",
                grid_class,
                @chosen == v && ejects?(v) && "btn-error ring-2 ring-error ring-offset-1",
                @chosen == v && !ejects?(v) && "btn-primary ring-2 ring-primary ring-offset-1",
                @chosen != v && ejects?(v) && "btn-ghost border border-error/50 text-error",
                @chosen != v && !ejects?(v) && "btn-ghost border app-hairline"
              ]}
            >
              <span class="text-sm leading-none font-mono" aria-hidden="true">
                {wheel_glyph(pos)}
              </span>
              <span class="text-[11px] font-medium">{verdict_short_label(v)}</span>
            </button>
        <% end %>
      <% end %>
      <div
        id="wheel-center"
        class="col-start-2 row-start-2 min-h-[44px] rounded-lg border app-hairline bg-base-200/40 flex flex-col items-center justify-center gap-1 px-2 py-2 text-center"
      >
        <div class="text-[11px] font-semibold tracking-widest opacity-70">A vs B</div>
        <div class="text-[10px] leading-tight opacity-60" id="wheel-legend">
          ◀ A · ▶ B · ≡ tie<br />
          <span class="text-error">✕ ejects that side for good</span>
        </div>
        <.subject_badge subject={@subject} />
        <div class="text-xs font-medium max-w-full truncate" id="wheel-selected-label">
          {if @chosen, do: verdict_short_label(@chosen), else: "—"}
        </div>
      </div>
    </div>
    """
  end

  attr :wheel, :map, required: true
  attr :chosen, :any, required: true
  attr :subject, :any, required: true

  def verdict_axis(assigns) do
    assigns = assign(assigns, :positions, @axis_positions)

    ~H"""
    <div class="flex items-stretch gap-3 max-w-2xl">
      <div
        id="verdict-axis"
        role="radiogroup"
        aria-label="Verdict axis — top is best, bottom is invalid"
        class="flex flex-col gap-2 flex-1"
      >
        <%= for pos <- @positions, v = Map.get(@wheel, pos), v != nil do %>
          <button
            type="button"
            id={"axis-#{pos}"}
            role="radio"
            aria-checked={to_string(@chosen == v)}
            phx-click="set_verdict"
            phx-value-v={v}
            title={v}
            class={[
              "btn btn-sm h-auto min-h-[44px] justify-start gap-2 normal-case",
              @chosen == v && "btn-primary ring-2 ring-primary ring-offset-1",
              @chosen != v && "btn-ghost border app-hairline"
            ]}
          >
            <span class="text-sm font-mono w-6 text-center" aria-hidden="true">
              {axis_glyph(pos)}
            </span>
            <span class="text-xs font-medium truncate">{verdict_short_label(v)}</span>
          </button>
        <% end %>
      </div>
      <div
        id="axis-center"
        class="w-40 shrink-0 rounded-lg border app-hairline bg-base-200/40 flex flex-col items-center justify-center gap-1.5 px-2 py-3 text-center"
      >
        <.subject_badge subject={@subject} />
        <div class="text-xs font-medium max-w-full truncate" id="axis-selected-label">
          {if @chosen, do: verdict_short_label(@chosen), else: "—"}
        </div>
      </div>
    </div>
    """
  end

  attr :verdicts, :list, required: true
  attr :chosen, :any, required: true

  def operational_verdicts(assigns) do
    ~H"""
    <div
      :if={@verdicts != []}
      id="operational-verdicts"
      class="flex flex-wrap items-center gap-2 mt-3"
    >
      <span class="text-[10px] uppercase tracking-wider opacity-50">Operational</span>
      <button
        :for={v <- @verdicts}
        type="button"
        id={"operational-#{v}"}
        aria-pressed={to_string(@chosen == v)}
        phx-click="set_verdict"
        phx-value-v={v}
        class={[
          "btn btn-xs normal-case gap-1",
          @chosen == v && "btn-primary",
          @chosen != v && "btn-ghost border app-hairline"
        ]}
      >
        <span class="font-mono opacity-60" aria-hidden="true">{operational_glyph(v)}</span>
        {verdict_short_label(v)}
      </button>
    </div>
    """
  end

  attr :subjects, :list, required: true
  attr :index, :integer, required: true

  def subject_stepper(assigns) do
    ~H"""
    <div id="subject-stepper" class="flex items-center gap-1.5">
      <%= for {s, i} <- Enum.with_index(@subjects) do %>
        <span :if={i > 0} class="opacity-40 text-xs" aria-hidden="true">→</span>
        <span
          id={"subject-step-#{s}"}
          data-active={to_string(i == @index)}
          class={[
            "px-2.5 py-1 rounded-full border text-[11px] uppercase tracking-wide transition",
            i == @index && "border-primary bg-primary/10 font-semibold",
            i < @index && "app-hairline opacity-60",
            i > @index && "app-hairline opacity-40"
          ]}
        >
          <span :if={i < @index} aria-hidden="true">✓ </span>{s}
        </span>
      <% end %>
    </div>
    """
  end

  attr :subject, :any, required: true

  def subject_badge(assigns) do
    ~H"""
    <span
      :if={@subject}
      class={[
        "text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase tracking-wide",
        @subject == "idea" && "bg-amber-400/20 text-amber-600",
        @subject != "idea" && "bg-blue-400/20 text-blue-600"
      ]}
      data-subject={@subject}
    >
      {@subject}
    </span>
    """
  end

  defp wheel_glyph("nw"), do: "◀"
  defp wheel_glyph("n"), do: "≡"
  defp wheel_glyph("ne"), do: "▶"
  defp wheel_glyph("w"), do: "◀◀"
  defp wheel_glyph("e"), do: "▶▶"
  defp wheel_glyph("sw"), do: "✕A"
  defp wheel_glyph("se"), do: "✕B"
  defp wheel_glyph(_), do: "•"

  defp axis_glyph("n"), do: "▲▲"
  defp axis_glyph("ne"), do: "▲"
  defp axis_glyph("se"), do: "▽"
  defp axis_glyph("s"), do: "✕"
  defp axis_glyph(_), do: "•"

  defp operational_glyph("skip"), do: "↻"
  defp operational_glyph(_), do: "•"

  @doc "Human-ish short label straight from the verdict name — data-driven."
  def verdict_short_label(v) when is_binary(v), do: String.replace(v, "-", " ")
  def verdict_short_label(v), do: to_string(v)
end
