defmodule TournamentUi.SweepGraph do
  @moduledoc """
  Node-graph model for the visual sweep designer (/designer).

  A SweepSpec is a fixed pipeline with variable multiplicity in two places
  (corpus sources, lenses), so the graph is typed, not free-form: corpus*
  -> intake -> lens*/human -> rounds -> validation -> publish. Nodes carry
  editable `data` and canvas positions; edges are DERIVED from node types,
  never stored — there is no way to wire a graph that compiles to an
  invalid pipeline shape.

  `to_spec/2` compiles the graph to the SweepSpec map that
  `bin/sweep_spec.py` validates and `bin/campaigns.py create-campaign`
  freezes; `from_spec/1` loads any existing spec (templates, stored
  campaigns) back onto the canvas. Round-tripping an example config
  through from_spec |> to_spec is covered by tests.
  """

  @lens_x 432
  @col_x %{corpus: 20, intake: 226, rounds: 638, validation: 844, publish: 1050}
  @row_h 118
  @top 40

  @adapters ~w(sentry_csv slack_csv github_autoclosed foundry_stories)
  def adapters, do: @adapters

  def from_spec(spec) when is_map(spec) do
    corpus = List.wrap(spec["corpus"] || [])
    lenses = List.wrap(get_in(spec, ["panel", "lenses"]) || [])
    human = get_in(spec, ["panel", "human"])

    corpus_nodes =
      corpus
      |> Enum.with_index()
      |> Enum.map(fn {c, i} ->
        node("corpus-#{i}", :corpus, @col_x.corpus, @top + i * @row_h, %{
          "adapter" => c["adapter"] || "foundry_stories",
          "config_raw" => Jason.encode!(c["config"] || %{}, pretty: true)
        })
      end)

    lens_nodes =
      lenses
      |> Enum.with_index()
      |> Enum.map(fn {l, i} ->
        node("lens-#{i}", :lens, @lens_x, @top + i * @row_h, %{
          "name" => l["name"] || "",
          "prompt_ref" => l["prompt_ref"] || "",
          "burden" => l["burden"] || "refute"
        })
      end)

    human_nodes =
      if human do
        [
          node("human", :human, @lens_x, @top + length(lenses) * @row_h, %{
            "rubric" => human["rubric"] || "",
            "judgement_kind" => human["judgement_kind"] || "single",
            "required" => human["required"] != false,
            "quorum" => get_in(spec, ["panel", "quorum"]) || "all_lenses_and_human"
          })
        ]
      else
        []
      end

    rounds = spec["rounds"] || %{}
    validation = spec["validation"] || %{}
    publish = spec["publish"] || %{}
    intake = spec["intake"] || %{}

    corpus_nodes ++
      [
        node("intake", :intake, @col_x.intake, @top + 40, %{
          "max_candidates" => intake["max_candidates"] || 30,
          "rationale_required" => intake["rationale_required"] != false
        })
      ] ++
      lens_nodes ++
      human_nodes ++
      [
        node("rounds", :rounds, @col_x.rounds, @top + 40, %{
          "max" => rounds["max"] || 3,
          "batching" => rounds["batching"] || "required",
          "convergence" => rounds["convergence"] || "no_new_confirmed_findings",
          "repair" => rounds["repair_max_cycles_per_finding"] || 1
        }),
        node("validation", :validation, @col_x.validation, @top + 40, %{
          "mode" => validation["mode"] || "red_green",
          "perf_budgets_raw" => Jason.encode!(validation["perf_budgets"] || [], pretty: true)
        }),
        node("publish", :publish, @col_x.publish, @top + 40, %{
          "gate" => publish["gate"] || "human",
          "granularity" => publish["granularity"] || "branch-per-finding"
        }),
        runner_node(spec["runner"])
      ]
  end

  # The runner node always exists on the canvas; driver "manual" means no
  # runner section in the compiled spec (humans/ad-hoc agents drive the CLI).
  defp runner_node(nil),
    do:
      node("runner", :runner, @col_x.rounds, @top + 210, %{
        "driver" => "manual",
        "model" => "",
        "parallel" => 4
      })

  defp runner_node(runner),
    do:
      node("runner", :runner, @col_x.rounds, @top + 210, %{
        "driver" => runner["driver"] || "manual",
        "model" => runner["model"] || "",
        "parallel" => runner["parallel"] || 4
      })

  defp node(id, type, x, y, data), do: %{id: id, type: type, x: x, y: y, data: data}

  @doc "Compile the graph to a SweepSpec map. Raw JSON fields that fail to
  parse raise with a node-scoped message — the designer surfaces it inline."
  def to_spec(nodes, kind) do
    by_type = Enum.group_by(nodes, & &1.type)
    intake = single!(by_type, :intake)
    rounds = single!(by_type, :rounds)
    validation = single!(by_type, :validation)
    publish = single!(by_type, :publish)
    human = by_type |> Map.get(:human, []) |> List.first()

    spec = %{
      "kind" => kind,
      "corpus" =>
        for c <- ordered(by_type, :corpus) do
          %{
            "adapter" => c.data["adapter"],
            "config" => parse_json!(c.data["config_raw"], "#{c.id} config")
          }
        end,
      "intake" => %{
        "max_candidates" => int(intake.data["max_candidates"], 30),
        "rationale_required" => truthy(intake.data["rationale_required"])
      },
      "panel" =>
        %{
          "lenses" =>
            for l <- ordered(by_type, :lens) do
              %{
                "name" => l.data["name"],
                "prompt_ref" => l.data["prompt_ref"],
                "burden" => l.data["burden"]
              }
            end
        }
        |> maybe_human(human),
      "rounds" => %{
        "max" => int(rounds.data["max"], 3),
        "batching" => rounds.data["batching"],
        "convergence" => rounds.data["convergence"],
        "repair_max_cycles_per_finding" => int(rounds.data["repair"], 1)
      },
      "validation" =>
        %{"mode" => validation.data["mode"]}
        |> maybe_budgets(validation.data),
      "publish" => %{
        "gate" => publish.data["gate"],
        "granularity" => publish.data["granularity"]
      }
    }

    runner = by_type |> Map.get(:runner, []) |> List.first()

    case runner do
      %{data: %{"driver" => driver} = data} when driver in ["opencode", "claude-workflow"] ->
        Map.put(spec, "runner", %{
          "driver" => driver,
          "model" => data["model"] || "",
          "parallel" => int(data["parallel"], 4)
        })

      _ ->
        spec
    end
  end

  defp maybe_human(panel, nil), do: Map.put(panel, "quorum", "all_lenses")

  defp maybe_human(panel, human) do
    panel
    |> Map.put("human", %{
      "rubric" => human.data["rubric"],
      "judgement_kind" => human.data["judgement_kind"],
      "required" => truthy(human.data["required"])
    })
    |> Map.put("quorum", human.data["quorum"])
  end

  defp maybe_budgets(validation, %{"mode" => "perf_budget"} = data) do
    Map.put(
      validation,
      "perf_budgets",
      parse_json!(data["perf_budgets_raw"], "perf budgets")
    )
  end

  defp maybe_budgets(validation, data) do
    case parse_json(data["perf_budgets_raw"]) do
      {:ok, []} -> validation
      _ -> validation
    end
  end

  @doc "Derived wires: [{from_id, to_id, source_type}]."
  def edges(nodes) do
    by_type = Enum.group_by(nodes, & &1.type)
    lenses = ordered(by_type, :lens)
    human = Map.get(by_type, :human, [])

    Enum.map(ordered(by_type, :corpus), &{&1.id, "intake", :corpus}) ++
      Enum.map(lenses ++ human, &{"intake", &1.id, :intake}) ++
      Enum.map(lenses ++ human, &{&1.id, "rounds", &1.type}) ++
      [{"rounds", "validation", :rounds}, {"validation", "publish", :validation}] ++
      if(Map.has_key?(by_type, :runner), do: [{"runner", "rounds", :runner}], else: [])
  end

  def add_lens(nodes) do
    n = Enum.count(nodes, &(&1.type == :lens))
    y = @top + panel_stack_height(nodes)

    nodes ++
      [
        node("lens-#{System.unique_integer([:positive])}", :lens, @lens_x, y, %{
          "name" => "lens-#{n + 1}",
          "prompt_ref" => "lens:root-cause",
          "burden" => "refute"
        })
      ]
  end

  def add_corpus(nodes) do
    n = Enum.count(nodes, &(&1.type == :corpus))

    nodes ++
      [
        node(
          "corpus-#{System.unique_integer([:positive])}",
          :corpus,
          @col_x.corpus,
          @top + n * @row_h,
          %{
            "adapter" => "foundry_stories",
            "config_raw" => "{\n  \"root\": \"\"\n}"
          }
        )
      ]
  end

  def toggle_human(nodes) do
    if Enum.any?(nodes, &(&1.type == :human)) do
      Enum.reject(nodes, &(&1.type == :human))
    else
      y = @top + panel_stack_height(nodes)

      nodes ++
        [
          node("human", :human, @lens_x, y, %{
            "rubric" => "",
            "judgement_kind" => "single",
            "required" => true,
            "quorum" => "all_lenses_and_human"
          })
        ]
    end
  end

  defp panel_stack_height(nodes) do
    Enum.count(nodes, &(&1.type in [:lens, :human])) * @row_h
  end

  def remove_node(nodes, id) do
    case Enum.find(nodes, &(&1.id == id)) do
      %{type: t} when t in [:lens, :corpus, :human] -> Enum.reject(nodes, &(&1.id == id))
      _ -> nodes
    end
  end

  def update_node(nodes, id, params) do
    Enum.map(nodes, fn n ->
      if n.id == id, do: %{n | data: Map.merge(n.data, params)}, else: n
    end)
  end

  def move_node(nodes, id, x, y) do
    Enum.map(nodes, fn n ->
      if n.id == id, do: %{n | x: clamp(x, 0, 1140), y: clamp(y, 0, 900)}, else: n
    end)
  end

  defp clamp(v, lo, hi), do: v |> max(lo) |> min(hi)
  defp ordered(by_type, type), do: by_type |> Map.get(type, []) |> Enum.sort_by(& &1.y)

  defp single!(by_type, type) do
    case Map.get(by_type, type) do
      [n] -> n
      _ -> raise ArgumentError, "graph must have exactly one #{type} node"
    end
  end

  defp parse_json!(raw, label) do
    case parse_json(raw) do
      {:ok, v} -> v
      _ -> raise ArgumentError, "#{label}: not valid JSON"
    end
  end

  defp parse_json(nil), do: {:ok, %{}}
  defp parse_json(""), do: {:ok, %{}}
  defp parse_json(raw), do: Jason.decode(raw)

  defp int(v, _default) when is_integer(v), do: v

  defp int(v, default) when is_binary(v) do
    case Integer.parse(v) do
      {i, _} -> i
      :error -> default
    end
  end

  defp int(_, default), do: default

  defp truthy(v), do: v in [true, "true", "on", 1, "1"]
end
