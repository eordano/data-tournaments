defmodule TournamentUi.ApprovalsTest do
  use ExUnit.Case, async: false

  # Elixir mirror of bin/approvals.py: fail-closed authorize, append-only
  # audit BEFORE delivery, delivery failure keeps the audit row. Policies
  # are seeded via Python (bin.catalog owns the schema and normal writes);
  # malformed rules are injected with raw sqlite3 because create_policy
  # only writes well-formed JSON.

  alias TournamentUi.Approvals

  @wf "release:unity:abc123"

  defp repo_root, do: File.cwd!() |> Path.join("..") |> Path.expand()

  defp py!(home, code) do
    {out, status} =
      System.cmd(
        "python3",
        [
          "-c",
          """
          import os, sys, json, sqlite3
          os.environ['DATA_TOURNAMENTS_HOME'] = '#{home}'
          sys.path.insert(0, '#{repo_root()}')
          #{code}
          """
        ],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true
      )

    assert status == 0, "python failed: #{out}"
    out
  end

  defp init!(home), do: py!(home, "import bin.catalog as cat; cat.init()")

  defp create_policy!(home, rule_json, name \\ nil) do
    name = name || "pol-#{System.unique_integer([:positive])}"

    py!(home, """
    db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
    db.execute("INSERT INTO policy(name, kind, rule) VALUES (?, 'approval', ?)", ('#{name}', '''#{rule_json}'''))
    db.commit()
    """)
  end

  # A stub client that appends its argv to argv.log and snapshots the
  # approval_event row count at delivery time (ordering proof).
  defp install_stub!(home, exit_code) do
    stub = Path.join(home, "stub_client.sh")

    File.write!(stub, """
    #!/bin/sh
    echo "$@" >> #{home}/argv.log
    python3 -c "import sqlite3; print(sqlite3.connect('#{home}/judgements.db').execute('SELECT COUNT(*) FROM approval_event').fetchone()[0])" >> #{home}/rows_at_delivery.log
    echo "stub says: signal delivery exploded"
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("DT_RELEASE_CLIENT_CMD", stub)
  end

  setup do
    home = "/tmp/dt-approvals-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    on_exit(fn ->
      System.delete_env("DT_RELEASE_CLIENT_CMD")
      File.rm_rf(home)
    end)

    {:ok, home: home}
  end

  describe "authorize/2 fail-closed deny paths" do
    test "denies blank / nil principal", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "*"}))

      assert {:error, reason} = Approvals.authorize(nil, @wf)
      assert reason =~ "no authenticated principal"
      assert {:error, _} = Approvals.authorize("", @wf)
      assert {:error, _} = Approvals.authorize("   ", @wf)
    end

    test "denies when no active approval policy exists", %{home: home} do
      init!(home)

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "no active approval policy"
      assert reason =~ "fail closed"
    end

    test "denies when the policy table is missing entirely (older DB)", %{home: home} do
      File.write!(Path.join(home, "judgements.db"), "")

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "no active approval policy"
    end

    test "archived policies do not grant", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "*"}), "archived-pol")

      py!(home, """
      db = sqlite3.connect(os.environ['DATA_TOURNAMENTS_HOME'] + '/judgements.db')
      db.execute("UPDATE policy SET status='archived' WHERE name='archived-pol'")
      db.commit()
      """)

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "no active approval policy"
    end

    test "malformed rule JSON is never a grant", %{home: home} do
      init!(home)
      create_policy!(home, "this is not json")

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "not an allowlisted approver"
    end

    test "non-object rule (bare JSON string) is never a grant", %{home: home} do
      init!(home)
      create_policy!(home, ~s("just a string"))

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "not an allowlisted approver"
    end

    test "scope glob mismatch denies", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "deploy:*"}))

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "not an allowlisted approver"
    end

    test "unlisted principal denies even when scope matches", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["someone-else"], "scope": "release:*"}))

      assert {:error, reason} = Approvals.authorize("esteban", @wf)
      assert reason =~ "esteban"
      assert reason =~ "not an allowlisted approver"
    end
  end

  describe "authorize/2 grants" do
    test "returns the matching policy id", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["nope"], "scope": "*"}))
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:*"}))

      assert {:ok, policy_id} = Approvals.authorize("esteban", @wf)
      assert is_integer(policy_id)
    end

    test "missing scope defaults to * and ? matches one char", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"]}))

      assert {:ok, _} = Approvals.authorize("esteban", @wf)
      assert Approvals.glob_match?("release:?:x", "release:a:x")
      refute Approvals.glob_match?("release:?:x", "release:ab:x")
      refute Approvals.glob_match?("release:*", "deploy:release:x")
    end
  end

  describe "submit_decision/4" do
    test "authorize -> audit -> deliver; audit row exists BEFORE delivery", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:*"}))
      install_stub!(home, 0)

      assert {:ok, %{event_id: event_id, decision: "approved", delivery: :ok}} =
               Approvals.submit_decision(@wf, true, "esteban", "looks good")

      # Delivery argv reached the client CLI unchanged.
      assert File.read!(Path.join(home, "argv.log")) =~
               "approve #{@wf} --approver esteban --reason looks good"

      # Ordering proof: at the moment the stub ran, the audit row was
      # already committed.
      assert String.trim(File.read!(Path.join(home, "rows_at_delivery.log"))) == "1"

      assert [%{id: ^event_id, decision: "approved", approver: "esteban", reason: "looks good"}] =
               Approvals.list_events(@wf)
    end

    test "reject path records a rejected event", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:*"}))
      install_stub!(home, 0)

      assert {:ok, %{decision: "rejected", delivery: :ok}} =
               Approvals.submit_decision(@wf, false, "esteban", "canary regressed")

      assert File.read!(Path.join(home, "argv.log")) =~ "reject #{@wf}"
      assert [%{decision: "rejected"}] = Approvals.list_events(@wf)
    end

    test "failed delivery keeps the audit row and reports the output", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:*"}))
      install_stub!(home, 1)

      assert {:ok, %{event_id: event_id, delivery: {:failed, output}}} =
               Approvals.submit_decision(@wf, true, "esteban", "")

      assert output =~ "signal delivery exploded"
      # The recorded intent stands — by design, for operator reconciliation.
      assert [%{id: ^event_id, decision: "approved"}] = Approvals.list_events(@wf)
    end

    test "denied decision writes no audit row and runs no delivery", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["someone-else"], "scope": "*"}))
      install_stub!(home, 0)

      assert {:error, _} = Approvals.submit_decision(@wf, true, "esteban", "")
      assert Approvals.list_events(@wf) == []
      refute File.exists?(Path.join(home, "argv.log"))
    end
  end

  describe "malformed-policy hardening (mirrors bin/approvals.py, 2026-08-17)" do
    # Malformed policy rows must NEVER widen access and never crash the
    # approval path — same regression suite as tests/test_approvals.py.

    test "string approvers never substring-grant", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": "esteban", "scope": "release:*"}))
      # Substring membership would grant "est" (and "esteban"); the shape
      # is malformed, so BOTH must be denied.
      assert {:error, _} = Approvals.authorize("est", @wf)
      assert {:error, _} = Approvals.authorize("esteban", @wf)
    end

    test "non-string approver entries deny", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban", 42], "scope": "*"}))
      assert {:error, _} = Approvals.authorize("esteban", @wf)
    end

    test "empty-string approver entries deny", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban", " "], "scope": "*"}))
      assert {:error, _} = Approvals.authorize("esteban", @wf)
    end

    test "non-string scope denies instead of widening to *", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": 42}))
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": null}))
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": {"glob": "*"}}))
      assert {:error, _} = Approvals.authorize("esteban", @wf)
    end

    test "character-class scope syntax is rejected", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:[au]*"}))
      assert {:error, _} = Approvals.authorize("esteban", "release:unity:abc")
    end

    test "missing scope still defaults to *", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"]}))
      assert {:ok, _} = Approvals.authorize("esteban", @wf)
    end

    test "? glob matches exactly one character", %{home: home} do
      init!(home)
      create_policy!(home, ~s({"approvers": ["esteban"], "scope": "release:unit?:*"}))
      assert {:ok, _} = Approvals.authorize("esteban", "release:unity:abc")
      assert {:error, _} = Approvals.authorize("esteban", "release:unitty:abc")
    end
  end
end
