defmodule TournamentUiWeb.DomainNewLiveTest do
  use TournamentUiWeb.ConnCase
  import Phoenix.LiveViewTest

  # Regression suite for the 2026-08-16 user-testing session: clicking
  # "Draft prompts" produced no visible feedback for 20+ seconds and the
  # user concluded the button was broken. Root causes covered here:
  #   1. The synchronous shell-out blocked the LiveView, so nothing
  #      repainted (now async with a drafting spinner).
  #   2. A pasted GitHub URL / nonexistent path only failed after the
  #      slow LM call (now validated instantly, before any shell-out).

  test "draft with a URL pasted as filesystem root fails fast with a clone hint", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/domains/new?starter=correctness")

    view
    |> form("#stage1", %{
      "description" => "Find concrete correctness bugs",
      "corpus_kind" => "filesystem",
      "corpus_path" => "https://github.com/someone/some-repo"
    })
    |> render_change()

    html = view |> form("#stage1") |> render_submit()

    assert html =~ "looks like a URL"
    assert html =~ "clone the repository first"
    # Still on stage 1, button back to idle — never left the page silently.
    assert html =~ "Draft prompts"
    refute html =~ "drafting-status"
  end

  test "draft with a nonexistent filesystem root fails fast", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/domains/new?starter=correctness")

    view
    |> form("#stage1", %{
      "corpus_kind" => "filesystem",
      "corpus_path" => "/nonexistent/path/for/this/test"
    })
    |> render_change()

    html = view |> form("#stage1") |> render_submit()
    assert html =~ "is not a directory on this machine"
  end

  test "draft with empty inline corpus fails fast", %{conn: conn} do
    {:ok, view, _html} = live(conn, "/domains/new?starter=custom")

    view
    |> form("#stage1", %{"corpus_kind" => "inline", "corpus_inline" => ""})
    |> render_change()

    html = view |> form("#stage1") |> render_submit()
    assert html =~ "Paste at least one item"
  end

  test "valid draft submit shows immediate drafting feedback", %{conn: conn} do
    # Stub builder: sleeps briefly then emits a draft, letting us observe
    # the intermediate drafting state that was invisible before the fix.
    stub = Path.join(System.tmp_dir!(), "stub_builder_#{System.unique_integer([:positive])}.py")

    File.write!(stub, """
    import json, sys, time
    time.sleep(0.3)
    print("DRAFT_JSON: " + json.dumps({
        "domain_name": "stub-domain",
        "generator_prompt": "gen",
        "judge_prompt": "judge",
    }))
    """)

    corpus_dir = Path.join(System.tmp_dir!(), "stub_corpus_#{System.unique_integer([:positive])}")
    File.mkdir_p!(corpus_dir)
    File.write!(Path.join(corpus_dir, "sample.py"), "print('hi')\n")

    System.put_env("DOMAIN_BUILDER_SCRIPT", stub)

    on_exit_cleanup = fn ->
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
      File.rm(stub)
      File.rm_rf(corpus_dir)
    end

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=correctness")

      view
      |> form("#stage1", %{"corpus_kind" => "filesystem", "corpus_path" => corpus_dir})
      |> render_change()

      html = view |> form("#stage1") |> render_submit()

      # Immediately after submit: spinner + status line, button disabled.
      assert html =~ "Drafting…"
      assert html =~ "10–30 seconds"

      # After the async completes we land on stage 2 with the draft.
      html = render_async(view, 2_000)
      assert html =~ "stub-domain"
      assert html =~ "Save domain"
    after
      on_exit_cleanup.()
    end
  end

  # ── Stage-1 domain name field (2026-08-17 user request) ────────────────

  test "stage 1 renders the optional domain name field", %{conn: conn} do
    {:ok, _view, html} = live(conn, "/domains/new?starter=correctness")
    assert html =~ "domain-name"
    assert html =~ "Domain name"
    assert html =~ "leave blank to let the AI suggest one"
  end

  test "invalid domain name fails fast without invoking the builder", %{conn: conn} do
    # Point the builder at a script that would blow up if ever invoked.
    System.put_env("DOMAIN_BUILDER_SCRIPT", "/nonexistent/never-called.py")

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=correctness")

      # Name validation runs before corpus validation, so the default
      # (filesystem, empty path) corpus is fine — the name error must win.
      view
      |> form("#stage1", %{"requested_name" => "Invalid Name With Spaces!"})
      |> render_change()

      html = view |> form("#stage1") |> render_submit()

      assert html =~ "must be a slug"
      # Never went async — no drafting state, still on stage 1.
      refute html =~ "Drafting…"
    after
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
    end
  end

  test "user-chosen name overrides the AI suggestion after drafting", %{conn: conn} do
    stub = Path.join(System.tmp_dir!(), "stub_named_#{System.unique_integer([:positive])}.py")

    File.write!(stub, """
    import json
    print("DRAFT_JSON: " + json.dumps({
        "domain_name": "ai-suggested-name",
        "generator_prompt": "gen",
        "judge_prompt": "judge",
    }))
    """)

    System.put_env("DOMAIN_BUILDER_SCRIPT", stub)

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=custom")

      view
      |> form("#stage1", %{
        "requested_name" => "my-chosen-name",
        "corpus_kind" => "inline",
        "corpus_inline" => "an item"
      })
      |> render_change()

      view |> form("#stage1") |> render_submit()
      html = render_async(view, 2_000)

      assert html =~ "my-chosen-name"
      refute html =~ "ai-suggested-name"
    after
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
      File.rm(stub)
    end
  end

  test "blank name keeps the AI suggestion", %{conn: conn} do
    stub = Path.join(System.tmp_dir!(), "stub_blank_#{System.unique_integer([:positive])}.py")

    File.write!(stub, """
    import json
    print("DRAFT_JSON: " + json.dumps({
        "domain_name": "ai-suggested-name",
        "generator_prompt": "gen",
        "judge_prompt": "judge",
    }))
    """)

    System.put_env("DOMAIN_BUILDER_SCRIPT", stub)

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=custom")

      view
      |> form("#stage1", %{"corpus_kind" => "inline", "corpus_inline" => "an item"})
      |> render_change()

      view |> form("#stage1") |> render_submit()
      html = render_async(view, 2_000)

      assert html =~ "ai-suggested-name"
    after
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
      File.rm(stub)
    end
  end

  test "back to stage 1 preserves the chosen name", %{conn: conn} do
    stub = Path.join(System.tmp_dir!(), "stub_back_#{System.unique_integer([:positive])}.py")

    File.write!(stub, """
    import json
    print("DRAFT_JSON: " + json.dumps({
        "domain_name": "ai-name",
        "generator_prompt": "gen",
        "judge_prompt": "judge",
    }))
    """)

    System.put_env("DOMAIN_BUILDER_SCRIPT", stub)

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=custom")

      view
      |> form("#stage1", %{
        "requested_name" => "sticky-name",
        "corpus_kind" => "inline",
        "corpus_inline" => "an item"
      })
      |> render_change()

      view |> form("#stage1") |> render_submit()
      render_async(view, 2_000)

      html = view |> element("button[phx-click=back_to_stage1]") |> render_click()
      assert html =~ "sticky-name"
    after
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
      File.rm(stub)
    end
  end

  # ── Work-order artifact default (2026-08-17 user: cards too small) ─────

  test "saving a new domain stamps artifact=work-order in the corpus spec", %{conn: conn} do
    argv_file = Path.join(System.tmp_dir!(), "argv_#{System.unique_integer([:positive])}.json")
    stub = Path.join(System.tmp_dir!(), "stub_artifact_#{System.unique_integer([:positive])}.py")

    File.write!(stub, """
    import json, sys
    with open(#{inspect(argv_file)}, "w") as f:
        json.dump(sys.argv, f)
    if "--save" in sys.argv:
        print(json.dumps({"domain_id": 1, "name": "wo-default"}))
    else:
        print("DRAFT_JSON: " + json.dumps({
            "domain_name": "wo-default",
            "generator_prompt": "gen",
            "judge_prompt": "judge",
        }))
    """)

    System.put_env("DOMAIN_BUILDER_SCRIPT", stub)

    try do
      {:ok, view, _html} = live(conn, "/domains/new?starter=custom")

      view
      |> form("#stage1", %{"corpus_kind" => "inline", "corpus_inline" => "one item"})
      |> render_change()

      view |> form("#stage1") |> render_submit()
      render_async(view, 2_000)

      view |> form("#stage2") |> render_submit()

      argv = argv_file |> File.read!() |> Jason.decode!()
      spec_idx = Enum.find_index(argv, &(&1 == "--corpus-spec"))
      assert spec_idx, "save argv missing --corpus-spec: #{inspect(argv)}"
      spec = argv |> Enum.at(spec_idx + 1) |> Jason.decode!()

      # The root cause of 'cards too small': new domains must generate
      # rich WorkOrder documents, not legacy compact cards.
      assert spec["artifact"] == "work-order"
      assert spec["kind"] == "inline"
    after
      System.delete_env("DOMAIN_BUILDER_SCRIPT")
      File.rm(stub)
      File.rm(argv_file)
    end
  end
end
