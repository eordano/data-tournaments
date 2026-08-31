defmodule TournamentUi.DomainsTest do
  use ExUnit.Case, async: false

  alias TournamentUi.Domains

  setup do
    home = "/tmp/dt-test-#{System.os_time(:nanosecond)}-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    seed_script = """
    import os
    os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
    import sys
    sys.path.insert(0, '#{repo_root}')
    sys.path.insert(0, '#{repo_root}/bin')
    import bin.prompts as p
    class _Stub:
        class api:
            class prompts:
                @staticmethod
                def list(**kw):
                    M = type('M', (), {'total_pages': 1})
                    return type('R', (), {'data': [], 'meta': M})()
            class prompt_version:
                @staticmethod
                def update(**kw): return None
        def get_prompt(self, name, label='production', version=None):
            raise LookupError(name)
        def create_prompt(self, **kw):
            return type('P', (), {'version': 1, 'prompt': kw['prompt'], 'name': kw['name'], 'labels': kw.get('labels') or []})()
    p._client_factory = lambda: _Stub()
    import judgement; judgement.init_db()
    import bin.domains as d
    d.create_domain(name='alpha', description='Alpha', corpus_source={'kind': 'inline', 'items': []})
    d.create_domain(name='beta',  description='Beta',  corpus_source={'kind': 'inline', 'items': []})
    print('OK')
    """

    {out, status} =
      System.cmd("python3", ["-c", seed_script],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    assert File.exists?(Path.join(home, "judgements.db"))

    on_exit(fn -> File.rm_rf!(home) end)
    %{home: home}
  end

  test "list/0 returns active domains, newest first" do
    rows = Domains.list()
    assert length(rows) == 2
    names = Enum.map(rows, & &1.name)
    assert "alpha" in names
    assert "beta" in names
  end

  test "get/1 returns a single domain by name" do
    spec = Domains.get("alpha")
    assert spec.name == "alpha"
    assert spec.description == "Alpha"
    assert spec.corpus_source["kind"] == "inline"
  end

  test "get/1 returns nil for unknown domains" do
    assert Domains.get("does-not-exist") == nil
  end
end
