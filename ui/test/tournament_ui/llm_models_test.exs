defmodule TournamentUi.LlmModelsTest do
  use ExUnit.Case, async: false

  alias TournamentUi.LlmModels

  setup do
    previous_url = Application.get_env(:tournament_ui, :llm_base_url)
    previous_key = Application.get_env(:tournament_ui, :llm_api_key)

    Application.put_env(:tournament_ui, :llm_base_url, "http://fake-llm.test/v1")
    Application.put_env(:tournament_ui, :llm_api_key, "key-fake")

    on_exit(fn ->
      restore_env(:llm_base_url, previous_url)
      restore_env(:llm_api_key, previous_key)
    end)

    Req.Test.set_req_test_from_context(%{async: true})
    :ok
  end

  defp restore_env(key, nil), do: Application.delete_env(:tournament_ui, key)
  defp restore_env(key, value), do: Application.put_env(:tournament_ui, key, value)

  test "list/0 returns sorted model ids from /v1/models" do
    Req.Test.stub(LlmModels, fn conn ->
      assert conn.method == "GET"
      assert conn.request_path =~ "/models"

      Req.Test.json(conn, %{
        "data" => [
          %{"id" => "gpt-5", "object" => "model"},
          %{"id" => "llm-default", "object" => "model"},
          %{"id" => "claude-opus-4-7", "object" => "model"}
        ]
      })
    end)

    assert LlmModels.list() == ["claude-opus-4-7", "gpt-5", "llm-default"]
  end

  test "list/0 sends bearer auth from the configured api key" do
    test_pid = self()

    Req.Test.stub(LlmModels, fn conn ->
      auth = List.first(Plug.Conn.get_req_header(conn, "authorization"))
      send(test_pid, {:auth, auth})
      Req.Test.json(conn, %{"data" => []})
    end)

    LlmModels.list()
    assert_receive {:auth, "Bearer key-fake"}
  end

  test "OpenRouter exposes the configured frontier panel in role order" do
    Application.put_env(:tournament_ui, :llm_base_url, "https://openrouter.ai/api/v1")

    Req.Test.stub(LlmModels, fn conn ->
      assert conn.query_string =~ "supported_parameters=structured_outputs"
      refute conn.query_string =~ "sort=most-popular"

      Req.Test.json(conn, %{
        "data" => [
          %{"id" => "anthropic/claude-opus-5"},
          %{"id" => "unrelated/model"},
          %{"id" => "moonshotai/kimi-k3"},
          %{"id" => "z-ai/glm-5.2"}
        ]
      })
    end)

    assert LlmModels.list() == [
             "moonshotai/kimi-k3",
             "z-ai/glm-5.2",
             "anthropic/claude-opus-5"
           ]
  end

  test "list/0 returns [] when the gateway is unreachable" do
    Req.Test.stub(LlmModels, fn _conn -> raise "boom" end)
    assert LlmModels.list() == []
  end

  test "list/0 returns [] on non-200 responses" do
    Req.Test.stub(LlmModels, fn conn ->
      conn |> Plug.Conn.put_status(500) |> Req.Test.json(%{"error" => "down"})
    end)

    assert LlmModels.list() == []
  end
end
