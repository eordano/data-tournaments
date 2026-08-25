defmodule TournamentUi.OptimizerRunnerTest do
  use ExUnit.Case, async: false
  alias TournamentUi.OptimizerRunner

  @moduletag :tmp_dir

  test "start/2 spawns and forwards stdout lines, then exit code", %{tmp_dir: tmp_dir} do
    fake = Path.join(tmp_dir, "fake.sh")

    File.write!(fake, """
    #!/bin/sh
    echo "[optimize] loaded 5 examples"
    echo "[optimize] starting GEPA"
    echo "[optimize] candidate v2 pushed"
    """)

    File.chmod!(fake, 0o755)

    {:ok, _pid} = OptimizerRunner.start(fake, [], parent: self(), rubric_lock: "ut1")

    assert_receive {:optimizer_line, "[optimize] loaded 5 examples"}, 5_000
    assert_receive {:optimizer_line, "[optimize] starting GEPA"}, 5_000
    assert_receive {:optimizer_line, "[optimize] candidate v2 pushed"}, 5_000
    assert_receive {:optimizer_exit, 0}, 5_000
  end

  test "start/2 sends optimizer_exit with non-zero status on failure", %{tmp_dir: tmp_dir} do
    fake = Path.join(tmp_dir, "fail.sh")
    File.write!(fake, "#!/bin/sh\nexit 7\n")
    File.chmod!(fake, 0o755)
    {:ok, _} = OptimizerRunner.start(fake, [], parent: self(), rubric_lock: "ut2")
    assert_receive {:optimizer_exit, 7}, 5_000
  end

  test "only one optimizer runs at a time per rubric_lock", %{tmp_dir: tmp_dir} do
    fake = Path.join(tmp_dir, "slow.sh")
    File.write!(fake, "#!/bin/sh\nsleep 3\n")
    File.chmod!(fake, 0o755)
    {:ok, _} = OptimizerRunner.start(fake, [], parent: self(), rubric_lock: "shared")

    assert {:error, :already_running} =
             OptimizerRunner.start(fake, [], parent: self(), rubric_lock: "shared")
  end

  test "registry keeps status + log tail and jobs survive their LiveView", %{tmp_dir: tmp_dir} do
    # Jobs must be discoverable after the starting process navigated away:
    # no parent pid is passed at all; observation goes via PubSub + registry.
    Phoenix.PubSub.subscribe(TournamentUi.PubSub, OptimizerRunner.topic())

    fake = Path.join(tmp_dir, "reg.sh")
    File.write!(fake, "#!/bin/sh\necho line-one\necho line-two\n")
    File.chmod!(fake, 0o755)

    meta = %{source: :regtest, kind: :fan_out, domain: "d1"}
    {:ok, _} = OptimizerRunner.start(fake, [], rubric_lock: "reg1", meta: meta)

    assert_receive {:optimizer_line, "reg1", ^meta, "line-one"}, 5_000
    assert_receive {:optimizer_exit, "reg1", ^meta, 0}, 5_000

    job = OptimizerRunner.latest_job(:regtest)
    assert job.status == :finished
    assert job.exit_status == 0
    assert "line-one" in job.lines and "line-two" in job.lines
    # Lock released: the same key can run again.
    {:ok, _} = OptimizerRunner.start(fake, [], rubric_lock: "reg1", meta: meta)
    assert_receive {:optimizer_exit, "reg1", ^meta, 0}, 5_000
  end

  test "start failure rolls back lock and registry entry", %{tmp_dir: tmp_dir} do
    # A nonexistent relative executable fails resolution before any insert…
    assert {:error, {:executable_not_found, _}} =
             OptimizerRunner.start("definitely-not-a-real-exe-xyz", [],
               rubric_lock: "rb1",
               meta: %{source: :rbtest}
             )

    # …and the lock must be free: a real job with the same key starts fine.
    fake = Path.join(tmp_dir, "ok.sh")
    File.write!(fake, "#!/bin/sh\nexit 0\n")
    File.chmod!(fake, 0o755)
    Phoenix.PubSub.subscribe(TournamentUi.PubSub, OptimizerRunner.topic())
    meta = %{source: :rbtest}
    {:ok, _} = OptimizerRunner.start(fake, [], rubric_lock: "rb1", meta: meta)
    assert_receive {:optimizer_exit, "rb1", ^meta, 0}, 5_000
  end

  test "job completes and stays in registry after the starting process dies", %{tmp_dir: tmp_dir} do
    # The exact user scenario: kick off a job, navigate away (caller dies),
    # job must still finish, be recorded, and release its lock.
    fake = Path.join(tmp_dir, "slowok.sh")
    File.write!(fake, "#!/bin/sh\nsleep 1\necho done-after-caller-died\n")
    File.chmod!(fake, 0o755)

    meta = %{source: :deathtest, kind: :fan_out, domain: "dx"}

    caller =
      spawn(fn ->
        {:ok, _} = OptimizerRunner.start(fake, [], rubric_lock: "death1", meta: meta)
      end)

    # Caller exits almost immediately, long before the job finishes.
    ref = Process.monitor(caller)
    assert_receive {:DOWN, ^ref, :process, ^caller, _}, 5_000

    Phoenix.PubSub.subscribe(TournamentUi.PubSub, OptimizerRunner.topic())
    assert_receive {:optimizer_exit, "death1", ^meta, 0}, 10_000

    job = OptimizerRunner.latest_job(:deathtest)
    assert job.status == :finished
    assert job.exit_status == 0
    assert "done-after-caller-died" in job.lines
  end
end
