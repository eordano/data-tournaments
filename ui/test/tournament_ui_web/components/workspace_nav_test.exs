defmodule TournamentUiWeb.WorkspaceNavTest do
  use ExUnit.Case, async: true
  import Phoenix.Component
  import Phoenix.LiveViewTest

  alias TournamentUiWeb.CoreComponents

  test "renders all canonical destinations" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :judge)

    for href <- [
          "/start",
          "/domains",
          "/judge",
          "/results",
          "/environment",
          "/campaigns",
          "/branch-fixes",
          "/runs",
          "/inspect"
        ] do
      assert html =~ ~s(href="#{href}")
    end
  end

  test "the judging surface trades Standings for the neutral Table exit" do
    judge = render_component(&CoreComponents.workspace_nav/1, current: :judge)
    assert judge =~ ~s(href="/table")
    refute String.contains?(String.downcase(judge), "standing")

    elsewhere = render_component(&CoreComponents.workspace_nav/1, current: :domains)
    assert elsewhere =~ ~s(href="/standings")
    refute elsewhere =~ ~s(href="/table")
  end

  test "demoted surfaces are gone from primary nav (wave-13 IA)" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :judge)

    refute html =~ ~s(href="/prompts"), "Prompts merged into Environment"
    refute html =~ ~s(href="/catalog"), "Catalog merged into Environment"

    refute html =~ ~s(href="/brackets"),
           "single-elimination brackets are a retired product: the route, the LiveView " <>
             "and its tests are deleted, so a nav entry would be a dead link"
  end

  test "highlights the current page and not the others" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :domains)

    # Active link gets is-active class
    assert html =~ ~r/href="\/domains"[^>]*is-active/
    # Other links don't get it
    refute html =~ ~r/href="\/judge"[^>]*is-active/
    refute html =~ ~r/href="\/environment"[^>]*is-active/
  end

  test "labels are short and consistent" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :environment)

    # Visible labels (case-sensitive, so we know the wording is locked in)
    for label <- [
          "Start",
          "Domains",
          "Review",
          "Results",
          "Environment",
          "Campaigns",
          "Branches",
          "Runs",
          "Data"
        ] do
      assert html =~ label
    end
  end

  test "unknown :current value renders no active link" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :nonsense)

    refute html =~ "is-active"
  end

  test "the workspace header bar carries the theme toggle on the single-column shell" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_page current={:domains}>BODY</CoreComponents.workspace_page>
      """)

    for theme <- ~w(system light dark) do
      assert html =~ ~s(data-phx-theme="#{theme}"),
             "Layouts.app/1 was the only caller of theme_toggle/1 and it was dead code on " <>
               "an unrouted page; deleting it without rehoming the toggle would have made " <>
               "dark mode unreachable through the UI"
    end
  end

  test "the workspace header bar carries the theme toggle on the judging shell too" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_split current={:judge}>PANE</CoreComponents.workspace_split>
      """)

    for theme <- ~w(system light dark) do
      assert html =~ ~s(data-phx-theme="#{theme}")
    end
  end

  test "the header bar keeps nav actions and the theme toggle side by side" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_page current={:runs}>
        <:nav_actions>NAV_ACT</:nav_actions>
        BODY
      </CoreComponents.workspace_page>
      """)

    assert html =~ "NAV_ACT"
    assert html =~ ~s(data-phx-theme="dark")
  end
end
