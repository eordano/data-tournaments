defmodule TournamentUiWeb.Layouts do
  @moduledoc """
  Layout-level components.

  `flash_group/1` lives here because Phoenix v1.8 (see `ui/AGENTS.md`)
  reserves the flash group for the layout module. The workspace shells in
  `TournamentUiWeb.CoreComponents` render it from here rather than
  assembling a second copy of the same markup out of `<.flash>`.

  The generated `app/1` shell that used to sit in this module has been
  deleted rather than adopted. Nothing called it: no LiveView declared
  `layout: {Layouts, :app}`, no controller rendered it, and its body was
  framework marketing chrome -- the Phoenix logo, the linked Phoenix
  version, and "Website"/"GitHub"/"Get Started" links pointing at
  phoenixframework.org. Adopting it as the real shell would have meant
  editing `use Phoenix.LiveView` in `lib/tournament_ui_web.ex`, and the
  workspace already has a real shell in `workspace_page/1` and
  `workspace_split/1`. `theme_toggle/1` outlived it: it is the only control
  that reaches dark mode, and the one call site left over from the deleted
  shell -- `page_html/home.html.heex` -- has no route, so the workspace
  shells now render the toggle in their header bar.
  """
  use TournamentUiWeb, :html

  embed_templates "layouts/*"

  @doc """
  Shows the flash group with standard titles and content.

  `corner` picks the fixed viewport corner the toasts occupy. The workspace
  shells pass `"toast-bottom toast-start"`: the judging screen packs the
  queue strip, the domain filter and the round counter along the top edge,
  and the Skip and Submit buttons along the right edge, so the bottom-left
  is the only corner a toast can take without landing on a control a judge
  presses every few seconds.

  ## Examples

      <Layouts.flash_group flash={@flash} />
      <Layouts.flash_group flash={@flash} corner="toast-bottom toast-start" />
  """
  attr :flash, :map, required: true, doc: "the map of flash messages"
  attr :id, :string, default: "flash-group", doc: "the optional id of flash container"

  attr :corner, :string,
    default: "toast-top toast-end",
    doc: "daisyUI placement classes for the fixed toast container"

  def flash_group(assigns) do
    ~H"""
    <div id={@id} aria-live="polite">
      <.flash kind={:info} flash={@flash} corner={@corner} />
      <.flash kind={:error} flash={@flash} corner={@corner} />

      <.flash
        id="client-error"
        kind={:error}
        corner={@corner}
        title="We can't find the internet"
        phx-disconnected={show(".phx-client-error #client-error") |> JS.remove_attribute("hidden")}
        phx-connected={hide("#client-error") |> JS.set_attribute({"hidden", ""})}
        hidden
      >
        Attempting to reconnect
        <.icon name="hero-arrow-path" class="ml-1 size-3 motion-safe:animate-spin" />
      </.flash>

      <.flash
        id="server-error"
        kind={:error}
        corner={@corner}
        title="Something went wrong!"
        phx-disconnected={show(".phx-server-error #server-error") |> JS.remove_attribute("hidden")}
        phx-connected={hide("#server-error") |> JS.set_attribute({"hidden", ""})}
        hidden
      >
        Attempting to reconnect
        <.icon name="hero-arrow-path" class="ml-1 size-3 motion-safe:animate-spin" />
      </.flash>
    </div>
    """
  end

  @doc """
  Provides dark vs light theme toggle based on themes defined in app.css.

  See <head> in root.html.heex which applies the theme before page load.
  """
  def theme_toggle(assigns) do
    ~H"""
    <div class="card relative flex flex-row items-center border-2 border-base-300 bg-base-300 rounded-full">
      <div class="absolute w-1/3 h-full rounded-full border-1 border-base-200 bg-base-100 brightness-200 left-0 [[data-theme=light]_&]:left-1/3 [[data-theme=dark]_&]:left-2/3 transition-[left]" />

      <button
        class="flex p-2 cursor-pointer w-1/3"
        phx-click={JS.dispatch("phx:set-theme")}
        data-phx-theme="system"
        aria-label="Match system theme"
      >
        <.icon name="hero-computer-desktop-micro" class="size-4 opacity-75 hover:opacity-100" />
      </button>

      <button
        class="flex p-2 cursor-pointer w-1/3"
        phx-click={JS.dispatch("phx:set-theme")}
        data-phx-theme="light"
        aria-label="Light theme"
      >
        <.icon name="hero-sun-micro" class="size-4 opacity-75 hover:opacity-100" />
      </button>

      <button
        class="flex p-2 cursor-pointer w-1/3"
        phx-click={JS.dispatch("phx:set-theme")}
        data-phx-theme="dark"
        aria-label="Dark theme"
      >
        <.icon name="hero-moon-micro" class="size-4 opacity-75 hover:opacity-100" />
      </button>
    </div>
    """
  end
end
