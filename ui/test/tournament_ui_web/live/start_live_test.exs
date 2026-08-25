defmodule TournamentUiWeb.StartLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  test "explains the source to generation to review workflow", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/start")

    assert html =~ "Start with one question"
    assert html =~ "Generate"
    assert html =~ "Review"
    assert html =~ "Compare"
    assert html =~ "Improve"
    assert html =~ "one evaluation lens"
  end

  test "offers separate code-review categories and a direct bracket path", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/start")

    for starter <- ~w(correctness security maintainability testing conventions custom) do
      assert has_element?(live, "#starter-#{starter}[href='/domains/new?starter=#{starter}']")
    end

    assert has_element?(live, "#start-direct-bracket[href='/new']")
  end

  test "category starter prefills a single-lens fan-out domain", %{conn: conn} do
    {:ok, live, html} = live(conn, "/domains/new?starter=security")

    assert has_element?(live, "#selected-evaluation-lens")
    assert html =~ "Security &amp; privacy"
    assert html =~ "Find concrete security and privacy risks"
    assert html =~ "one category per domain"
    assert html =~ "**/*.cs"
  end
end
