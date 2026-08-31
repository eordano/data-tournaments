defmodule TournamentUiWeb.StartLiveTest do
  @moduledoc """
  The front door is the only place that teaches the shape of the product, so
  the ladder it renders has to be the flow that actually runs.

  The retired ladder read Source → Generate → Review → Compare → Improve and
  stopped two stages short: it never mentioned the settled priority order the
  comparisons are spent to produce, nor the dispatch and reviewed diff that
  consume it, and it presented rubric tuning as the destination. It was also
  inert — five captions no one could follow. These tests pin both halves: the
  six real stages, and the fact that every stage is a link to the page where
  that stage is performed.
  """
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  @pipeline [
    {"pipeline-step-domain", "/domains", "Domain"},
    {"pipeline-step-generate", "/domains", "Work orders"},
    {"pipeline-step-compare", "/judge", "Compare"},
    {"pipeline-step-standings", "/standings", "Standings"},
    {"pipeline-step-dispatch", "/branch-fixes", "Dispatch"},
    {"pipeline-step-diff", "/branch-fixes", "Reviewed diff"}
  ]

  test "explains the source to generation to review workflow", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/start")

    assert html =~ "Start with one question"
    assert html =~ "one evaluation lens"
  end

  test "the pipeline names the real flow through to a reviewed diff", %{conn: conn} do
    {:ok, _live, html} = live(conn, "/start")

    for {_id, _href, title} <- @pipeline do
      assert html =~ title, "the pipeline dropped the #{title} stage"
    end

    refute html =~ "Tune the rubric from reviewed examples",
           "rubric tuning is the return edge, not the last stage of the flow"

    refute html =~ "See human and model outcomes together",
           "the flow does not end at the verdict log; it ends at a reviewed diff"
  end

  test "every pipeline stage links to the page where that stage happens", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/start")

    for {id, href, title} <- @pipeline do
      assert has_element?(live, "##{id}[href='#{href}']"),
             "#{title} must navigate to #{href}, not merely name itself"
    end
  end

  test "the front door offers the settled order as a destination", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/start")

    assert has_element?(live, "a[href='/standings']"),
           "/standings is what ninety-six comparisons buy; the front door must route to it"
  end

  test "offers separate code-review categories and no retired bracket path", %{conn: conn} do
    {:ok, live, _html} = live(conn, "/start")

    for starter <- ~w(correctness security maintainability testing conventions custom) do
      assert has_element?(live, "#starter-#{starter}[href='/domains/new?starter=#{starter}']")
    end

    refute has_element?(live, "#start-direct-bracket"),
           "single elimination is a retired product; the front door must not sell it"
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
