defmodule TournamentUi.LlmModels do
  @moduledoc """
  Fetch model ids from the configured OpenAI-compatible gateway.

  OpenRouter uses a deliberate frontier panel rather than a volatile
  popularity ranking. Other compatible gateways expose their complete,
  alphabetically sorted model list.

  Used by the /prompts page to populate judge + reflection model dropdowns
  on "Run optimizer." Read-only; swallows errors and returns [] so a flaky
  gateway can't take down the page.
  """

  @frontier_models [
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "anthropic/claude-opus-5"
  ]

  @doc "The configured OpenRouter frontier panel, in preferred role order."
  def frontier_models, do: @frontier_models

  @doc "List selectable model ids exposed by the configured gateway."
  def list do
    url = base_url()

    base_opts = [
      method: :get,
      url: url <> "/models",
      auth: {:bearer, api_key()},
      receive_timeout: 5_000
    ]

    base_opts =
      if openrouter?(url) do
        Keyword.put(base_opts, :params, supported_parameters: "structured_outputs")
      else
        base_opts
      end

    case Req.request(maybe_test_plug(base_opts)) do
      {:ok, %{status: 200, body: %{"data" => data}}} when is_list(data) ->
        ids = data |> Enum.map(&Map.get(&1, "id")) |> Enum.reject(&is_nil/1)
        if openrouter?(url), do: available_frontier(ids), else: Enum.sort(ids)

      _ ->
        fallback_models(url)
    end
  rescue
    _ -> fallback_models(base_url())
  end

  defp available_frontier(ids) do
    available = MapSet.new(ids)
    Enum.filter(@frontier_models, &MapSet.member?(available, &1))
  end

  defp fallback_models(url), do: if(openrouter?(url), do: @frontier_models, else: [])

  defp openrouter?(url), do: String.contains?(url, "openrouter.ai")

  defp base_url do
    (Application.get_env(:tournament_ui, :llm_base_url) ||
       System.get_env("LLM_BASE_URL") ||
       if(System.get_env("OPENROUTER_API_KEY"),
         do: "https://openrouter.ai/api/v1",
         else: "https://llm.example/v1"
       ))
    |> String.trim_trailing("/")
  end

  defp api_key do
    Application.get_env(:tournament_ui, :llm_api_key) ||
      System.get_env("LLM_GATEWAY_API_KEY") ||
      System.get_env("OPENROUTER_API_KEY") ||
      "none"
  end

  if Mix.env() == :test do
    defp maybe_test_plug(opts), do: Keyword.put(opts, :plug, {Req.Test, __MODULE__})
  else
    defp maybe_test_plug(opts), do: opts
  end
end
