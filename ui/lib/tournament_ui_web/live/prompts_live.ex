defmodule TournamentUiWeb.PromptsLive do
  @moduledoc """
  /prompts — legacy route. The prompt studio moved into the Environment
  surface (wave-13 §2: catalog + prompts are one thing — the ENVIRONMENT a
  campaign runs in). This LiveView only push_navigates to
  /environment?tab=prompts on mount so old deep links keep working.
  """
  use TournamentUiWeb, :live_view

  @impl true
  def mount(_params, _session, socket) do
    {:ok, push_navigate(socket, to: "/environment?tab=prompts")}
  end

  @impl true
  def render(assigns) do
    ~H"""
    <div class="p-8 text-sm opacity-60">
      Prompts moved — <.link navigate="/environment?tab=prompts" class="link">Environment → Prompts</.link>.
    </div>
    """
  end
end
