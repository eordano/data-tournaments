defmodule TournamentUiWeb.PageControllerTest do
  use TournamentUiWeb.ConnCase

  test "GET /", %{conn: conn} do
    conn = get(conn, ~p"/")
    html = html_response(conn, 200)
    assert html =~ "Start with one question"
    assert html =~ "environment"
    assert html =~ "domains"
    assert html =~ "judge"
    assert html =~ "inspect"
  end
end
