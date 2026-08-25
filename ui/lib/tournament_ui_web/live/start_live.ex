defmodule TournamentUiWeb.StartLive do
  @moduledoc """
  Entry point for choosing between a fan-out domain and a direct artifact
  bracket. Keeps unlike evaluation categories in separate tournaments so a
  pairwise verdict always answers one stable question.
  """
  use TournamentUiWeb, :live_view

  @code_lenses [
    %{
      id: "correctness",
      icon: "hero-bug-ant",
      title: "Correctness & reliability",
      description: "Surface concrete bugs, failure modes, and unsafe edge cases.",
      judge: "Impact · evidence · reproducibility"
    },
    %{
      id: "security",
      icon: "hero-shield-check",
      title: "Security & privacy",
      description: "Surface exploitable trust-boundary, access, and data-handling risks.",
      judge: "Severity · exploitability · confidence"
    },
    %{
      id: "maintainability",
      icon: "hero-squares-2x2",
      title: "Architecture & maintainability",
      description: "Surface coupling, duplication, and design choices that slow change.",
      judge: "Blast radius · leverage · actionability"
    },
    %{
      id: "testing",
      icon: "hero-beaker",
      title: "Tests & observability",
      description: "Surface missing coverage and signals that hide regressions.",
      judge: "Risk covered · diagnostic value · specificity"
    },
    %{
      id: "conventions",
      icon: "hero-code-bracket-square",
      title: "Conventions & consistency",
      description: "Extract patterns worth standardizing across the codebase.",
      judge: "Frequency · clarity · enforceability"
    }
  ]

  @impl true
  def mount(_params, _session, socket) do
    {:ok, assign(socket, code_lenses: @code_lenses)}
  end

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:start}
      max_width="max-w-6xl"
      title="Start with one question"
      subtitle="Choose one evaluation lens, generate comparable candidates from source material, then review every pair against that lens."
      id="start-workflow"
    >
      <section id="tournament-pipeline" class="app-card p-5 mb-7" aria-label="Tournament workflow">
        <div class="grid gap-3 md:grid-cols-5">
          <.pipeline_step
            number="1"
            title="Source"
            detail="Code, records, notes, or prepared artifacts"
          />
          <.pipeline_step
            number="2"
            title="Generate"
            detail="Produce focused candidates for one lens"
          />
          <.pipeline_step
            number="3"
            title="Review"
            detail="Compare each pair with one stable rubric"
          />
          <.pipeline_step
            number="4"
            title="Compare"
            detail="See human and model outcomes together"
          />
          <.pipeline_step
            number="5"
            title="Improve"
            detail="Tune the rubric from reviewed examples"
            last
          />
        </div>
      </section>

      <div class="flex items-end justify-between gap-4 mb-4">
        <div>
          <h2 class="text-lg font-semibold">Generate code findings</h2>
          <p class="text-sm opacity-65 mt-1">
            Pick a category for this domain. Create another domain when you want a different lens.
          </p>
        </div>
        <span class="hidden sm:inline-flex text-xs font-medium px-2.5 py-1 rounded-full bg-primary/10 text-primary">
          recommended for code review
        </span>
      </div>

      <div id="code-lenses" class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        <%= for lens <- @code_lenses do %>
          <.link
            navigate={"/domains/new?starter=#{lens.id}"}
            id={"starter-#{lens.id}"}
            class="app-card group p-5 transition hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-lg"
          >
            <div class="flex items-start gap-3">
              <div class="rounded-lg bg-primary/10 text-primary p-2.5">
                <.icon name={lens.icon} class="size-5" />
              </div>
              <div class="min-w-0">
                <h3 class="font-semibold group-hover:text-primary transition">{lens.title}</h3>
                <p class="text-sm opacity-70 mt-1 leading-5">{lens.description}</p>
              </div>
            </div>
            <div class="mt-4 pt-3 border-t app-hairline text-xs opacity-60">
              Judge on <span class="font-medium opacity-90">{lens.judge}</span>
            </div>
          </.link>
        <% end %>

        <.link
          navigate="/domains/new?starter=custom"
          id="starter-custom"
          class="app-card group p-5 border-dashed transition hover:-translate-y-0.5 hover:border-primary/40"
        >
          <div class="flex items-start gap-3">
            <div class="rounded-lg bg-base-200 p-2.5">
              <.icon name="hero-plus" class="size-5" />
            </div>
            <div>
              <h3 class="font-semibold group-hover:text-primary transition">Custom category</h3>
              <p class="text-sm opacity-70 mt-1 leading-5">
                Define another extraction goal and its pairwise judging criteria.
              </p>
            </div>
          </div>
        </.link>
      </div>

      <section class="app-card p-5 mt-7 flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <div class="text-xs uppercase tracking-wider opacity-55 mb-1">
            Already have comparable candidates?
          </div>
          <h2 class="font-semibold">Run a direct artifact bracket</h2>
          <p class="text-sm opacity-65 mt-1">
            Compare existing files or documents without generating findings first.
            A bracket is a single-elimination tournament: artifacts are paired,
            judged head-to-head, and winners advance until one remains.
          </p>
        </div>
        <.link navigate="/new" id="start-direct-bracket" class="btn btn-outline shrink-0">
          Configure direct bracket <span aria-hidden="true">→</span>
        </.link>
      </section>
    </.workspace_page>
    """
  end

  attr :number, :string, required: true
  attr :title, :string, required: true
  attr :detail, :string, required: true
  attr :last, :boolean, default: false

  defp pipeline_step(assigns) do
    ~H"""
    <div class="relative flex gap-3 pr-3">
      <div class="size-7 shrink-0 rounded-full bg-primary text-primary-content text-xs font-bold grid place-items-center">
        {@number}
      </div>
      <div>
        <div class="text-sm font-semibold">{@title}</div>
        <div class="text-xs opacity-60 mt-0.5 leading-4">{@detail}</div>
      </div>
      <.icon
        :if={!@last}
        name="hero-chevron-right"
        class="hidden md:block absolute right-0 top-1 size-4 opacity-25"
      />
    </div>
    """
  end
end
