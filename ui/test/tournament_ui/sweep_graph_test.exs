defmodule TournamentUi.SweepGraphTest do
  use ExUnit.Case, async: true

  alias TournamentUi.SweepGraph

  @configs Path.expand("../../../configs/sweeps", __DIR__)

  test "every example config round-trips through the graph unchanged" do
    for file <- Path.wildcard(Path.join(@configs, "*.json")) do
      spec = file |> File.read!() |> Jason.decode!()
      nodes = SweepGraph.from_spec(spec)
      assert SweepGraph.to_spec(nodes, spec["kind"]) == spec, "round-trip drift in #{file}"
    end
  end

  test "add_lens and remove_node change the compiled panel" do
    spec =
      @configs |> Path.join("featuresweep-foundry.json") |> File.read!() |> Jason.decode!()

    nodes = spec |> SweepGraph.from_spec() |> SweepGraph.add_lens()
    compiled = SweepGraph.to_spec(nodes, "featuresweep")
    assert length(get_in(compiled, ["panel", "lenses"])) == 3

    [extra | _] =
      nodes |> Enum.filter(&(&1.type == :lens)) |> Enum.sort_by(& &1.y) |> Enum.take(-1)

    compiled2 =
      nodes |> SweepGraph.remove_node(extra.id) |> SweepGraph.to_spec("featuresweep")

    assert length(get_in(compiled2, ["panel", "lenses"])) == 2
  end

  test "toggle_human off compiles to an all_lenses quorum with no human leg" do
    spec =
      @configs |> Path.join("featuresweep-foundry.json") |> File.read!() |> Jason.decode!()

    compiled =
      spec
      |> SweepGraph.from_spec()
      |> SweepGraph.toggle_human()
      |> SweepGraph.to_spec("featuresweep")

    refute get_in(compiled, ["panel", "human"])
    assert get_in(compiled, ["panel", "quorum"]) == "all_lenses"
  end

  test "invalid corpus config JSON raises with a node-scoped message" do
    spec =
      @configs |> Path.join("featuresweep-foundry.json") |> File.read!() |> Jason.decode!()

    nodes = SweepGraph.from_spec(spec)
    [corpus | _] = Enum.filter(nodes, &(&1.type == :corpus))
    broken = SweepGraph.update_node(nodes, corpus.id, %{"config_raw" => "{nope"})

    assert_raise ArgumentError, ~r/config: not valid JSON/, fn ->
      SweepGraph.to_spec(broken, "featuresweep")
    end
  end

  test "runner node round-trips: manual omits, opencode serializes" do
    spec =
      @configs |> Path.join("featuresweep-foundry.json") |> File.read!() |> Jason.decode!()

    nodes = SweepGraph.from_spec(spec)
    runner = Enum.find(nodes, &(&1.type == :runner))
    assert runner.data["driver"] == "manual"
    refute Map.has_key?(SweepGraph.to_spec(nodes, "featuresweep"), "runner")

    compiled =
      nodes
      |> SweepGraph.update_node("runner", %{"driver" => "opencode", "parallel" => "6"})
      |> SweepGraph.to_spec("featuresweep")

    assert compiled["runner"] == %{"driver" => "opencode", "model" => "", "parallel" => 6}

    reloaded = SweepGraph.from_spec(compiled)
    assert Enum.find(reloaded, &(&1.type == :runner)).data["driver"] == "opencode"
    assert {"runner", "rounds", :runner} in SweepGraph.edges(nodes)
  end

  test "edges derive the full pipeline shape" do
    spec =
      @configs |> Path.join("featuresweep-foundry.json") |> File.read!() |> Jason.decode!()

    nodes = SweepGraph.from_spec(spec)
    edges = SweepGraph.edges(nodes)
    assert {"rounds", "validation", :rounds} in edges
    assert {"validation", "publish", :validation} in edges
    assert Enum.count(edges, fn {_, to, _} -> to == "intake" end) == 1
    assert Enum.count(edges, fn {from, _, _} -> from == "intake" end) == 3
  end
end
