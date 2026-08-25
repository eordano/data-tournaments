defmodule TournamentUiWeb.BracketLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # /brackets was demoted from primary nav (wave-13 §3): direct bracket
  # construction is plumbing, not an operator job. The route stays alive
  # for deep links and debugging, and the header carries the contract's
  # advanced/legacy note. No feature work.

  test "route stays alive and carries the advanced/legacy header note", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/brackets")

    assert html =~ ~s(id="brackets-legacy-note")
    assert html =~ "advanced/legacy — normal entry is Start → generate → judge"
  end

  test "primary nav on the page carries no brackets entry", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/brackets")

    refute html =~ ~s(href="/brackets")
    assert html =~ ~s(href="/environment")
  end
end
