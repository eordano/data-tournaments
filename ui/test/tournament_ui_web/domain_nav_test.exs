defmodule TournamentUiWeb.DomainNavTest do
  use ExUnit.Case, async: true

  alias TournamentUiWeb.DomainNav

  describe "normalize/1" do
    test "treats nil and blank as the global (unfiltered) view" do
      assert DomainNav.normalize(nil) == nil
      assert DomainNav.normalize("") == nil
    end

    test "passes real domain names through" do
      assert DomainNav.normalize("quality-review") == "quality-review"
    end
  end

  describe "path helpers" do
    test "unfiltered paths stay global with no dangling query string" do
      assert DomainNav.judge_path(nil) == "/judge"
      assert DomainNav.results_path(nil) == "/results"
      assert DomainNav.judge_path("") == "/judge"
      assert DomainNav.results_path("") == "/results"
    end

    test "filtered paths carry the domain" do
      assert DomainNav.judge_path("alpha-review") == "/judge?domain=alpha-review"
      assert DomainNav.results_path("alpha-review") == "/results?domain=alpha-review"
    end

    test "domain names are URL-encoded" do
      assert DomainNav.results_path("my domain") == "/results?domain=my+domain"
      assert DomainNav.judge_path("a&b") == "/judge?domain=a%26b"
    end
  end
end
