defmodule TournamentUi.OptimizerRunsTest do
  use ExUnit.Case, async: false

  alias TournamentUi.OptimizerRuns

  setup do
    home = "/tmp/dt-opt-runs-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)
    repo_root = File.cwd!() |> Path.join("..") |> Path.expand()

    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root}')
          import bin.prompts as p
          class _S:
              class api:
                  class prompts:
                      @staticmethod
                      def list(**kw):
                          M=type('M',(),{'total_pages':1})
                          return type('R',(),{'data':[],'meta':M})()
                  class prompt_version:
                      @staticmethod
                      def update(**kw): return None
              def get_prompt(self, name, label='production', version=None):
                  raise LookupError(name)
              def create_prompt(self, **kw):
                  return type('P',(),{'version':1,'prompt':kw['prompt'],'name':kw['name'],'labels':kw.get('labels') or []})()
          p._client_factory = lambda: _S()
          from bin import judgement, optimizer_runs
          judgement.init_db()
          optimizer_runs.init()
          rid = optimizer_runs.start(domain='commit-msg', target='judge', rubric='card-prioritizer-v0', prompt_name='judge-instructions:commit-msg')
          optimizer_runs.append_log(rid, 'starting')
          optimizer_runs.append_log(rid, 'metric: 0.8')
          optimizer_runs.finish(rid, status='done', exit_code=0, result={'candidate_version': 3, 'metric': 0.8})
          rid2 = optimizer_runs.start(domain='commit-msg', target='generator')
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "seed failed: #{out}"
    on_exit(fn -> File.rm_rf!(home) end)
    :ok
  end

  test "list_for_domain returns newest-first" do
    rows = OptimizerRuns.list_for_domain("commit-msg")
    assert length(rows) == 2
    assert hd(rows).target == "generator"
    assert hd(rows).status == "running"
  end

  test "latest filters by target" do
    row = OptimizerRuns.latest(domain: "commit-msg", target: "judge")
    assert row.status == "done"
    assert row.result["candidate_version"] == 3
  end

  test "latest returns nil when no runs match" do
    assert OptimizerRuns.latest(domain: "nope", target: "judge") == nil
  end

  test "log accumulates lines" do
    row = OptimizerRuns.latest(domain: "commit-msg", target: "judge")
    assert row.log =~ "starting"
    assert row.log =~ "metric: 0.8"
  end
end
