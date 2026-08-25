defmodule TournamentUiWeb.Router do
  use TournamentUiWeb, :router

  pipeline :browser do
    plug :accepts, ["html"]
    plug :fetch_session
    plug :fetch_live_flash
    plug :put_root_layout, html: {TournamentUiWeb.Layouts, :root}
    plug :protect_from_forgery
    plug :put_secure_browser_headers
  end

  pipeline :api do
    plug :accepts, ["json"]
  end

  scope "/", TournamentUiWeb do
    pipe_through :browser

    live "/", StartLive, :index
    live "/start", StartLive, :index
    live "/brackets", BracketLive, :index
    live "/new", NewTournamentLive, :new
    live "/judge", JudgeLive, :index

    live "/candidates/:id/:side", CandidateLive, :show
    live "/results", JudgementsLive, :index
    live "/judgements", JudgementsLive, :index
    # /environment unifies the old /catalog + /prompts surfaces (wave-13 §2);
    # both legacy routes stay alive and push_navigate to their tab on mount.
    # /catalog/:project remains a live detail page (source forms live there).
    live "/environment", EnvironmentLive, :index
    live "/prompts", PromptsLive, :index
    live "/domains", DomainsLive, :index
    live "/domains/new", DomainNewLive, :new
    live "/domains/:name/edit", DomainEditLive, :edit
    live "/catalog", CatalogLive, :index
    live "/catalog/:project", CatalogLive, :show
    live "/campaigns", CampaignsLive, :index
    live "/campaigns/:name", CampaignsLive, :show
    live "/runs", RunsLive, :index
    # Query-param detail route: colon-bearing Temporal workflow ids (e.g.
    # release:unity:abc123) break path-segment routing on direct GET, so
    # links use /runs/show?id=... — the path route below stays for
    # backward compatibility with old bookmarks.
    live "/runs/show", RunsLive, :show_q
    live "/runs/:workflow_id", RunsLive, :show
    live "/branch-fixes", BranchFixesLive, :index
    live "/branch-fixes/:id", BranchFixesLive, :show
    live "/inspect", InspectLive, :index
    get "/inspect/download", InspectDownloadController, :show
  end

  scope "/api", TournamentUiWeb do
    pipe_through :api

    get "/judgements/export", JudgementExportController, :export
  end
end
