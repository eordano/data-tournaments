defmodule TournamentUiWeb.StartLive do
  @moduledoc """
  Entry point for starting a fan-out domain. Keeps unlike evaluation
  categories in separate tournaments so a pairwise verdict always answers one
  stable question.

  The ladder at the top is the flow `docs/design/priority-tournament.md`
  actually describes, and it does not stop at the judging surface: a domain
  generates work orders, a person compares them in pairs, Swiss standings
  settle a priority order, and the top of that table is dispatched and comes
  back as a reviewed diff. Each rung navigates to the page where that step is
  performed, so the front door teaches the route rather than only naming it.
  A ladder that ended at "Improve" described a rubric-tuning loop as if it
  were the product; the product is the ordered queue and the branches it
  dispatches.
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
      flash={@flash}
      max_width="max-w-6xl"
      title="Start with one question"
      subtitle="Choose one evaluation lens, generate work orders from source material, compare them in pairs, and dispatch the top of the settled order."
      id="start-workflow"
    >
      <section id="tournament-pipeline" class="app-card p-5 mb-7" aria-label="Tournament workflow">
        <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
          <.pipeline_step
            id="pipeline-step-domain"
            number="1"
            title="Domain"
            detail="One source corpus paired with one evaluation lens"
            href="/domains"
          />
          <.pipeline_step
            id="pipeline-step-generate"
            number="2"
            title="Work orders"
            detail="Generate the findings that will compete"
            href="/domains"
          />
          <.pipeline_step
            id="pipeline-step-compare"
            number="3"
            title="Compare"
            detail="Judge pairs on the wheel, one round at a time"
            href="/judge"
          />
          <.pipeline_step
            id="pipeline-step-standings"
            number="4"
            title="Standings"
            detail="Swiss points settle the priority order"
            href="/standings"
          />
          <.pipeline_step
            id="pipeline-step-dispatch"
            number="5"
            title="Dispatch"
            detail="The top of the table is authored into a branch"
            href="/branch-fixes"
          />
          <.pipeline_step
            id="pipeline-step-diff"
            number="6"
            title="Reviewed diff"
            detail="Red, green and guard, then approve and ship"
            href="/branch-fixes"
            last
          />
        </div>

        <p class="text-xs opacity-55 mt-4 pt-3 border-t app-hairline">
          The loop closes on the way back. Every verdict is a labelled example of what
          this team considers important, and
          <.link navigate="/domains" class="font-medium underline underline-offset-2">
            Improve rubric
          </.link>
          replays them into the judge that should eventually make these calls unaided.
        </p>
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
    </.workspace_page>
    """
  end

  attr :id, :string, required: true
  attr :number, :string, required: true
  attr :title, :string, required: true
  attr :detail, :string, required: true
  attr :href, :string, required: true
  attr :last, :boolean, default: false

  defp pipeline_step(assigns) do
    ~H"""
    <.link
      id={@id}
      navigate={@href}
      class="group relative flex gap-3 pr-3 -m-1 p-1 rounded-lg transition hover:bg-base-200/60"
    >
      <div class="size-7 shrink-0 rounded-full bg-primary text-primary-content text-xs font-bold grid place-items-center">
        {@number}
      </div>
      <div>
        <div class="text-sm font-semibold group-hover:text-primary transition">{@title}</div>
        <div class="text-xs opacity-60 mt-0.5 leading-4">{@detail}</div>
      </div>
      <.icon
        :if={!@last}
        name="hero-chevron-right"
        class="hidden xl:block absolute right-0 top-1 size-4 opacity-25"
      />
    </.link>
    """
  end
end
