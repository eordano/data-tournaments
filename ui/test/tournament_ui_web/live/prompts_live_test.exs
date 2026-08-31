defmodule TournamentUiWeb.PromptsLiveTest do
  # Mutates process-global env (DATA_TOURNAMENTS_HOME) — must not run
  # alongside other tests.
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  alias TournamentUi.LangfusePrompts
  alias TournamentUi.LlmModels

  # /prompts is a legacy route (wave-13 §2): it push_navigates to
  # /environment?tab=prompts on mount, so every mount below follows the
  # redirect and asserts against the Environment prompts tab — the same
  # prompt-studio surface, relocated, never weakened.

  test "/prompts redirects to the Environment prompts tab", %{conn: conn} do
    assert {:error, {:live_redirect, %{to: "/environment?tab=prompts"}}} =
             live(conn, "/prompts")
  end

  @domain_ddl """
  CREATE TABLE IF NOT EXISTS domain (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    generator_prompt TEXT NOT NULL,
    judge_prompt TEXT NOT NULL,
    rubric TEXT NOT NULL DEFAULT 'pair-wheel-v2',
    corpus_source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
  );
  CREATE TABLE IF NOT EXISTS pending_judgement (id INTEGER PRIMARY KEY);
  """

  # The optimizer form's rubric is read off the fabric's own schema default,
  # so this suite seeds a data home rather than inheriting whichever one ran
  # last. A hidden input pinned to a hardcoded rubric is exactly what left
  # this form posting a template nobody seeds after the last rename.
  defp seed_fabric!(home) do
    script =
      "import sqlite3\n" <>
        "db = sqlite3.connect(#{inspect(Path.join(home, "judgements.db"))})\n" <>
        "db.executescript(#{inspect(@domain_ddl)})\n" <>
        "db.commit()\n"

    {out, status} = System.cmd("python3", ["-c", script], stderr_to_stdout: true)
    assert status == 0, "seed failed: #{out}"
  end

  setup do
    previous_backend = Application.get_env(:tournament_ui, :prompt_backend)
    Application.put_env(:tournament_ui, :prompt_backend, "langfuse")

    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")

    home =
      "/tmp/dt-prompts-live-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    seed_fabric!(home)

    on_exit(fn ->
      if previous_backend,
        do: Application.put_env(:tournament_ui, :prompt_backend, previous_backend),
        else: Application.delete_env(:tournament_ui, :prompt_backend)

      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    :ok
  end

  defp stub_models(ids) do
    Req.Test.stub(LlmModels, fn conn ->
      Req.Test.json(conn, %{"data" => Enum.map(ids, &%{"id" => &1})})
    end)
  end

  defp stub_list(rows) do
    Req.Test.stub(LangfusePrompts, fn conn ->
      cond do
        conn.method == "GET" and conn.request_path =~ "/prompts" ->
          data =
            Enum.flat_map(rows, fn r ->
              versions =
                Enum.map(r.all_versions, fn v ->
                  labels =
                    Enum.filter(
                      [
                        if(v == r.production_version, do: "production"),
                        if(v == r.candidate_version, do: "candidate")
                      ],
                      & &1
                    )

                  %{"name" => r.name, "version" => v, "labels" => labels}
                end)

              versions
            end)

          Req.Test.json(conn, %{
            "data" => data,
            "meta" => %{"page" => 1, "totalPages" => 1, "totalItems" => length(data)}
          })

        true ->
          Req.Test.json(conn, %{})
      end
    end)
  end

  test "renders one row per prompt with current production version", %{conn: conn} do
    stub_list([
      %{
        name: "judge-instructions",
        production_version: 1,
        candidate_version: 2,
        all_versions: [1, 2]
      }
    ])

    {:ok, _live, html} = live(conn, "/prompts") |> follow_redirect(conn)
    assert html =~ "judge-instructions"
    assert html =~ "v1"
    assert html =~ "v2"
  end

  test "shows promote button next to candidate version", %{conn: conn} do
    stub_list([%{name: "j", production_version: 1, candidate_version: 2, all_versions: [1, 2]}])
    {:ok, _live, html} = live(conn, "/prompts") |> follow_redirect(conn)
    assert html =~ "Promote"
  end

  test "shows kick-off optimization button per prompt", %{conn: conn} do
    stub_list([
      %{
        name: "judge-instructions",
        production_version: 1,
        candidate_version: nil,
        all_versions: [1]
      }
    ])

    {:ok, _live, html} = live(conn, "/prompts") |> follow_redirect(conn)
    assert html =~ "Evolve context"
  end

  test "renders generator, reflector, and curator controls populated from /v1/models", %{
    conn: conn
  } do
    stub_list([])
    stub_models(["claude-opus-4-7", "gpt-5", "llm-default"])

    {:ok, _live, html} = live(conn, "/prompts") |> follow_redirect(conn)
    assert html =~ ~s(name="judge_model")
    assert html =~ ~s(name="reflection_model")
    assert html =~ ~s(name="curator_model")
    assert html =~ ~s(name="metric_calls")
    assert html =~ "llm-default"
    assert html =~ "gpt-5"
    assert html =~ "claude-opus-4-7"
    assert html =~ "(default)"
  end

  test "form posts all three model roles and the explicit budget", %{conn: conn} do
    stub_list([])
    stub_models(["llm-default", "gpt-5"])

    {:ok, _live, html} = live(conn, "/prompts") |> follow_redirect(conn)
    # The form has the right action + carries the rubric hidden input and both selects.
    assert html =~ ~s(phx-submit="optimize")
    assert html =~ ~s(name="rubric")
    assert html =~ ~s(value="pair-wheel-v2")
    assert TournamentUi.Judgement.default_rubric() == "pair-wheel-v2"
    assert html =~ ~s(name="prompt_name")
    assert html =~ ~s(name="judge_model")
    assert html =~ ~s(name="reflection_model")
    assert html =~ ~s(name="curator_model")
    assert html =~ ~s(name="metric_calls")
  end

  describe "model dropdown filtering (F-6)" do
    @non_chat_models ["whisper-large", "tts-1", "text-embedding-3-small"]
    @chat_models ["gpt-5", "claude-opus-4-7"]

    test "hides non-chat models (STT/TTS/embeddings) by default", %{conn: conn} do
      stub_list([])
      stub_models(@chat_models ++ @non_chat_models)

      {:ok, live, html} = live(conn, "/prompts") |> follow_redirect(conn)

      for m <- @chat_models, do: assert(html =~ m)
      for m <- @non_chat_models, do: refute(html =~ m)
      assert has_element?(live, "#show-all-models-toggle", "show all models")
    end

    test "show-all toggle restores the unfiltered list, and toggles back", %{conn: conn} do
      stub_list([])
      stub_models(@chat_models ++ @non_chat_models)

      {:ok, live, _html} = live(conn, "/prompts") |> follow_redirect(conn)

      html = live |> element("#show-all-models-toggle") |> render_click()
      for m <- @chat_models ++ @non_chat_models, do: assert(html =~ m)
      assert has_element?(live, "#show-all-models-toggle", "chat models only")

      html = live |> element("#show-all-models-toggle") |> render_click()
      for m <- @non_chat_models, do: refute(html =~ m)
      for m <- @chat_models, do: assert(html =~ m)
    end

    test "chat_models/1 excludes moderation/dall-e/audio/realtime names too" do
      models = [
        "gpt-5",
        "omni-moderation-latest",
        "dall-e-3",
        "gpt-4o-audio-preview",
        "gpt-4o-realtime-preview",
        "WHISPER-LARGE-V3"
      ]

      assert TournamentUiWeb.EnvironmentLive.chat_models(models) == ["gpt-5"]
    end
  end

  describe "clickable prompt cards" do
    # The prompt name is a real button: clicking expands the card inline
    # with the production prompt text (user report: nothing on /prompts
    # was clickable except Promote).
    defp stub_list_and_body(rows, bodies) do
      Req.Test.stub(LangfusePrompts, fn conn ->
        single =
          Enum.find(Map.keys(bodies), fn name ->
            conn.request_path == "/api/public/v2/prompts/#{name}"
          end)

        cond do
          conn.method == "GET" and single != nil ->
            Req.Test.json(conn, %{"prompt" => Map.fetch!(bodies, single)})

          conn.method == "GET" and conn.request_path =~ "/prompts" ->
            data =
              Enum.flat_map(rows, fn r ->
                Enum.map(r.all_versions, fn v ->
                  labels =
                    Enum.filter(
                      [
                        if(v == r.production_version, do: "production"),
                        if(v == r.candidate_version, do: "candidate")
                      ],
                      & &1
                    )

                  %{"name" => r.name, "version" => v, "labels" => labels}
                end)
              end)

            Req.Test.json(conn, %{
              "data" => data,
              "meta" => %{"page" => 1, "totalPages" => 1, "totalItems" => length(data)}
            })

          true ->
            Req.Test.json(conn, %{})
        end
      end)
    end

    test "prompt name is a button; clicking reveals the prompt body inline", %{conn: conn} do
      stub_list_and_body(
        [
          %{
            name: "judge-instructions",
            production_version: 1,
            candidate_version: nil,
            all_versions: [1]
          }
        ],
        %{"judge-instructions" => "JUDGE PROMPT BODY TEXT"}
      )

      {:ok, live, html} = live(conn, "/prompts") |> follow_redirect(conn)

      # The card header is a real interactive element, not dead text.
      assert has_element?(
               live,
               ~s(button[phx-click="toggle_prompt"][phx-value-name="judge-instructions"])
             )

      refute html =~ "JUDGE PROMPT BODY TEXT"

      html =
        live
        |> element(~s(button[phx-click="toggle_prompt"][phx-value-name="judge-instructions"]))
        |> render_click()

      assert html =~ "JUDGE PROMPT BODY TEXT"
      assert has_element?(live, "#prompt-body-judge-instructions")

      # Second click collapses.
      html =
        live
        |> element(~s(button[phx-click="toggle_prompt"][phx-value-name="judge-instructions"]))
        |> render_click()

      refute html =~ "JUDGE PROMPT BODY TEXT"
    end

    test "body fetch failure shows an honest inline message, never crashes", %{conn: conn} do
      stub_list_and_body(
        [
          %{
            name: "card-generator",
            production_version: 1,
            candidate_version: nil,
            all_versions: [1]
          }
        ],
        %{}
      )

      {:ok, live, _html} = live(conn, "/prompts") |> follow_redirect(conn)

      html =
        live
        |> element(~s(button[phx-click="toggle_prompt"][phx-value-name="card-generator"]))
        |> render_click()

      assert html =~ "Couldn&#39;t load the prompt text"
    end
  end
end
