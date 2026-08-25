defmodule TournamentUiWeb.WorkspaceNavTest do
  use ExUnit.Case, async: true
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

  test "demoted surfaces are gone from primary nav (wave-13 IA)" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :judge)

    # Catalog + Prompts merged into Environment; direct brackets demoted
    # (route stays alive, nav entry removed).
    refute html =~ ~s(href="/prompts")
    refute html =~ ~s(href="/catalog")
    refute html =~ ~s(href="/brackets")
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

  test "legacy :brackets current renders the nav with no active link" do
    html =
      render_component(&CoreComponents.workspace_nav/1, current: :brackets)

    refute html =~ "is-active"
  end
end
