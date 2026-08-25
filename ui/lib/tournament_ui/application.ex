defmodule TournamentUi.Application do
  # See https://hexdocs.pm/elixir/Application.html
  # for more information on OTP Applications
  @moduledoc false

  use Application

  @impl true
  def start(_type, _args) do
    children = [
      TournamentUiWeb.Telemetry,
      {DNSCluster, query: Application.get_env(:tournament_ui, :dns_cluster_query) || :ignore},
      {Phoenix.PubSub, name: TournamentUi.PubSub},
      # Stable owner of the background-job registry tables; must outlive
      # every LiveView that starts or observes a job.
      TournamentUi.OptimizerRunner.Tables,
      # Start to serve requests, typically the last entry
      TournamentUiWeb.Endpoint
    ]

    # See https://hexdocs.pm/elixir/Supervisor.html
    # for other strategies and supported options
    opts = [strategy: :one_for_one, name: TournamentUi.Supervisor]
    Supervisor.start_link(children, opts)
  end

  # Tell Phoenix to update the endpoint configuration
  # whenever the application is updated.
  @impl true
  def config_change(changed, _new, removed) do
    TournamentUiWeb.Endpoint.config_change(changed, removed)
    :ok
  end
end
