defmodule TournamentUiWeb.WorkspaceShellTest do
  use ExUnit.Case, async: true
  import Phoenix.Component
  import Phoenix.LiveViewTest

  alias TournamentUiWeb.CoreComponents

  test "workspace_page renders canonical nav, optional actions, and content" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_page current={:inspect}>
        <:nav_actions>NAV_ACT</:nav_actions>
        BODY
      </CoreComponents.workspace_page>
      """)

    assert html =~ "environment"
    assert html =~ "domains"
    assert html =~ "inspect"
    assert html =~ "BODY"
    assert html =~ "NAV_ACT"
    assert html =~ ~r/href="\/inspect"[^>]*is-active/
  end

  test "workspace_page renders a title block with subtitle and title-side actions" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_page
        current={:domains}
        title="Domains"
        subtitle="manage your card domains"
      >
        <:title_actions>TITLE_ACT</:title_actions>
        BODY
      </CoreComponents.workspace_page>
      """)

    assert html =~ ~r/<h1[^>]*>Domains<\/h1>/
    assert html =~ "manage your card domains"
    assert html =~ "TITLE_ACT"
    assert html =~ "BODY"
  end

  test "workspace_page omits title block when no title given" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_page current={:domains}>
        BODY
      </CoreComponents.workspace_page>
      """)

    refute html =~ ~r/<h1[^>]*>/
    assert html =~ "BODY"
  end

  test "workspace_split renders the same nav in a full-height shell" do
    assigns = %{}

    html =
      rendered_to_string(~H"""
      <CoreComponents.workspace_split current={:judge}>
        SPLIT_BODY
      </CoreComponents.workspace_split>
      """)

    assert html =~ "h-screen"
    assert html =~ "SPLIT_BODY"
    assert html =~ ~r/href="\/judge"[^>]*is-active/
  end
end
