defmodule TournamentUi.LangfusePromptsTest do
  use ExUnit.Case, async: false

  alias TournamentUi.LangfusePrompts

  setup do
    previous_backend = Application.get_env(:tournament_ui, :prompt_backend)
    Application.put_env(:tournament_ui, :prompt_backend, "langfuse")

    on_exit(fn ->
      if previous_backend,
        do: Application.put_env(:tournament_ui, :prompt_backend, previous_backend),
        else: Application.delete_env(:tournament_ui, :prompt_backend)
    end)

    # Tell Req to route to the test stub instead of langfuse.example.com.
    Application.put_env(:tournament_ui, :langfuse_host, "http://fake-langfuse.test")
    Application.put_env(:tournament_ui, :langfuse_public_key, "pk-fake")
    Application.put_env(:tournament_ui, :langfuse_secret_key, "sk-fake")
    Req.Test.set_req_test_from_context(%{async: true})
    :ok
  end

  test "list/0 returns prompts grouped by name with version + label info" do
    Req.Test.stub(LangfusePrompts, fn conn ->
      Req.Test.json(conn, %{
        "data" => [
          %{"name" => "judge-instructions", "version" => 1, "labels" => ["production"]},
          %{"name" => "judge-instructions", "version" => 2, "labels" => ["candidate"]},
          %{"name" => "card-generator", "version" => 1, "labels" => ["production"]}
        ],
        "meta" => %{"page" => 1, "totalPages" => 1, "totalItems" => 3}
      })
    end)

    result = LangfusePrompts.list()
    assert length(result) == 2
    by_name = Map.new(result, &{&1.name, &1})
    assert by_name["judge-instructions"].production_version == 1
    assert by_name["judge-instructions"].candidate_version == 2
  end

  test "promote/2 PATCHes the prompt-version with new_labels=production" do
    test_pid = self()

    Req.Test.stub(LangfusePrompts, fn conn ->
      send(test_pid, {:request, conn.method, conn.request_path})
      Req.Test.json(conn, %{})
    end)

    :ok = LangfusePrompts.promote("judge-instructions", 2)
    assert_receive {:request, method, path}
    assert method in ["PATCH", "POST"]
    assert path =~ "judge-instructions"
  end

  test "list/0 returns [] when langfuse is unreachable" do
    Req.Test.stub(LangfusePrompts, fn _conn -> raise "boom" end)
    assert LangfusePrompts.list() == []
  end

  test "get/2 fetches a prompt body by name+label" do
    Req.Test.stub(LangfusePrompts, fn conn ->
      Req.Test.json(conn, %{"prompt" => "hello world", "version" => 1, "labels" => ["production"]})
    end)

    assert LangfusePrompts.get("judge-instructions", "production") == "hello world"
  end

  test "local backend reads, lists, and promotes the shared prompts.json store" do
    home =
      "/tmp/dt-local-prompts-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"

    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    File.mkdir_p!(home)

    File.write!(
      Path.join(home, "prompts.json"),
      Jason.encode!(%{
        "prompts" => %{
          "judge-instructions" => [
            %{"version" => 1, "prompt" => "production one", "labels" => ["production"]},
            %{"version" => 2, "prompt" => "candidate two", "labels" => ["candidate"]}
          ]
        }
      })
    )

    System.put_env("DATA_TOURNAMENTS_HOME", home)
    Application.put_env(:tournament_ui, :prompt_backend, "local")

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      File.rm_rf!(home)
    end)

    assert LangfusePrompts.backend() == :local
    assert LangfusePrompts.backend_info().location == Path.join(home, "prompts.json")
    assert LangfusePrompts.get("judge-instructions") == "production one"

    [info] = LangfusePrompts.list()
    assert info.all_versions == [1, 2]
    assert info.production_version == 1
    assert info.candidate_version == 2

    assert :ok = LangfusePrompts.promote("judge-instructions", 2)
    assert LangfusePrompts.get("judge-instructions") == "candidate two"
  end
end
