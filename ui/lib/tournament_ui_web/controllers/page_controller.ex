defmodule TournamentUiWeb.PageController do
  use TournamentUiWeb, :controller

  def home(conn, _params) do
    render(conn, :home)
  end
end
