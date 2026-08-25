# This file is responsible for configuring your application
# and its dependencies with the aid of the Config module.
#
# This configuration file is loaded before any dependency and
# is restricted to this project.

# General application configuration
import Config

config :tournament_ui,
  generators: [timestamp_type: :utc_datetime]

# Release-workflow client used to deliver approval/rejection Signals
# (TournamentUi.Approvals). Deployments whose system python3 lacks the
# temporalio package MUST override this (or set DT_RELEASE_CLIENT_CMD,
# which wins verbatim at runtime) to point at the venv python, e.g.
# "spikes/temporal-unity-release/.venv/bin/python -m bin.release_workflow.client".
config :tournament_ui,
  release_client_cmd: "python3 -m bin.release_workflow.client"

# Configure the endpoint
config :tournament_ui, TournamentUiWeb.Endpoint,
  url: [host: "localhost"],
  adapter: Bandit.PhoenixAdapter,
  render_errors: [
    formats: [html: TournamentUiWeb.ErrorHTML, json: TournamentUiWeb.ErrorJSON],
    layout: false
  ],
  pubsub_server: TournamentUi.PubSub,
  live_view: [signing_salt: "LJ+R29Mw"]

# Configure esbuild (the version is required)
config :esbuild,
  version: "0.25.4",
  tournament_ui: [
    args:
      ~w(js/app.js --bundle --target=es2022 --outdir=../priv/static/assets/js --external:/fonts/* --external:/images/* --alias:@=.),
    cd: Path.expand("../assets", __DIR__),
    env: %{"NODE_PATH" => [Path.expand("../deps", __DIR__), Mix.Project.build_path()]}
  ]

# Configure tailwind (the version is required)
config :tailwind,
  version: "4.1.12",
  tournament_ui: [
    args: ~w(
      --input=assets/css/app.css
      --output=priv/static/assets/css/app.css
    ),
    cd: Path.expand("..", __DIR__)
  ]

# Configure Elixir's Logger
config :logger, :default_formatter,
  format: "$time $metadata[$level] $message\n",
  metadata: [:request_id]

# Use Jason for JSON parsing in Phoenix
config :phoenix, :json_library, Jason

# Import environment specific config. This must remain at the bottom
# of this file so it overrides the configuration defined above.
import_config "#{config_env()}.exs"
