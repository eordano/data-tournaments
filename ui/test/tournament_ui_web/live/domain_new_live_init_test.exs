defmodule TournamentUiWeb.DomainNewLiveInitTest do
  @moduledoc """
  Blank-state bootstrap for /domains/new (journey finding: fresh data dir +
  wizard save -> raw 'no such table: domain'). Mount now runs the same
  idempotent init /judge uses (JUDGEMENT_CLI_CMD-stubbed here); an init
  failure degrades to a visible warning banner, never a raw SQL error.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    previous_cli = System.get_env("JUDGEMENT_CLI_CMD")

    home = "/tmp/dt-domainnew-init-#{System.unique_integer([:positive])}"
    File.mkdir_p!(home)
    System.put_env("DATA_TOURNAMENTS_HOME", home)

    on_exit(fn ->
      if previous_home,
        do: System.put_env("DATA_TOURNAMENTS_HOME", previous_home),
        else: System.delete_env("DATA_TOURNAMENTS_HOME")

      if previous_cli,
        do: System.put_env("JUDGEMENT_CLI_CMD", previous_cli),
        else: System.delete_env("JUDGEMENT_CLI_CMD")

      File.rm_rf!(home)
    end)

    {:ok, home: home, calls: Path.join(home, "init-calls.log")}
  end

  defp install_counting_stub(home, calls, exit_code \\ 0) do
    stub = Path.join(home, "init-stub.sh")

    # On success the stub must actually create the schema marker table:
    # live/2 mounts twice (dead + connected render), and ensure_initialized
    # re-runs whenever initialized?/0 is still false — a no-op success stub
    # would be invoked once per mount and fail the exactly-once assertion
    # for the wrong reason.
    create_schema =
      if exit_code == 0 do
        """
        python3 -c "import sqlite3; \
        db = sqlite3.connect('#{home}/judgements.db'); \
        db.execute('CREATE TABLE IF NOT EXISTS pending_judgement(id INTEGER PRIMARY KEY)'); \
        db.execute('CREATE TABLE IF NOT EXISTS domain(id INTEGER PRIMARY KEY)'); \
        db.commit()"
        """
      else
        ""
      end

    File.write!(stub, """
    #!/bin/sh
    echo "$@" >> #{calls}
    #{create_schema}
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("JUDGEMENT_CLI_CMD", stub)
    stub
  end

  defp call_count(calls) do
    case File.read(calls) do
      {:ok, body} -> body |> String.split("\n", trim: true) |> length()
      _ -> 0
    end
  end

  test "fresh home: mount invokes init once, no warning banner", %{
    conn: conn,
    home: home,
    calls: calls
  } do
    install_counting_stub(home, calls, 0)

    {:ok, _view, html} = live(conn, "/domains/new?starter=custom")

    assert call_count(calls) == 1
    refute html =~ "init-warning"
  end

  test "init failure: wizard still renders with a visible warning", %{
    conn: conn,
    home: home,
    calls: calls
  } do
    install_counting_stub(home, calls, 1)

    {:ok, _view, html} = live(conn, "/domains/new?starter=custom")

    assert html =~ "init-warning"
    assert html =~ "not initialized"
    # The wizard itself is still usable (stage-1 form present).
    assert html =~ "requested_name"
  end

  test "existing schema: no init shell-out", %{conn: conn, home: home, calls: calls} do
    # Real bootstrap once so the tables exist...
    repo_root = Path.expand("../../../..", __DIR__)

    {_, 0} =
      System.cmd("python3", [Path.join(repo_root, "bin/judgement.py"), "init"],
        env: [{"DATA_TOURNAMENTS_HOME", home}],
        stderr_to_stdout: true,
        cd: repo_root
      )

    # ...then mount with a counting stub: it must never be called.
    install_counting_stub(home, calls, 0)

    {:ok, _view, html} = live(conn, "/domains/new?starter=custom")

    assert call_count(calls) == 0
    refute html =~ "init-warning"
  end
end
