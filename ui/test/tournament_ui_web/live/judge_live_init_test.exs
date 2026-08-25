defmodule TournamentUiWeb.JudgeLiveInitTest do
  @moduledoc """
  F-4: /judge auto-initializes the judgement-fabric DB on mount by
  shelling out to the same init the CLI runs. The CLI command is stubbed
  via the JUDGEMENT_CLI_CMD env override so we can count invocations and
  force failures without a real python bootstrap.
  """
  use TournamentUiWeb.ConnCase, async: false
  import Phoenix.LiveViewTest

  setup do
    previous_home = System.get_env("DATA_TOURNAMENTS_HOME")
    previous_cli = System.get_env("JUDGEMENT_CLI_CMD")

    home = "/tmp/dt-judge-init-#{System.unique_integer([:positive])}"
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

  # Minimal fabric schema covering every table the LiveView queries.
  defp schema_python(home) do
    """
    import sqlite3
    db = sqlite3.connect('#{home}/judgements.db')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS eval_template(
      id INTEGER PRIMARY KEY, name TEXT, version INTEGER, output_definition TEXT);
    CREATE TABLE IF NOT EXISTS job_configuration(
      id INTEGER PRIMARY KEY, template_id INTEGER, rater_type TEXT, rater_config TEXT);
    CREATE TABLE IF NOT EXISTS domain(
      id INTEGER PRIMARY KEY, name TEXT, description TEXT, judge_prompt TEXT);
    CREATE TABLE IF NOT EXISTS pending_judgement(
      id INTEGER PRIMARY KEY, config_id INTEGER, tournament_db_path TEXT,
      match_id INTEGER, trace_id TEXT, trace_payload TEXT,
      status TEXT DEFAULT 'pending', created_at TEXT, domain_id INTEGER,
      rating_id TEXT, completed_at TEXT);
    CREATE TABLE IF NOT EXISTS score(
      rating_id TEXT, pending_id INTEGER, template_id INTEGER,
      rubric_version INTEGER, name TEXT, data_type TEXT, value TEXT,
      metadata TEXT, tournament_db_path TEXT, match_id INTEGER,
      trace_id TEXT, created_at TEXT);
    ''')
    db.commit()
    """
  end

  # Stub init CLI: records every invocation; on exit 0 it also creates the
  # schema (mirroring what `python3 bin/judgement.py init` would do).
  defp write_stub(home, calls, exit_code) do
    stub = Path.join(home, "stub-judgement-init.sh")

    create_db =
      if exit_code == 0 do
        """
        python3 - <<'PY'
        #{schema_python(home)}
        PY
        """
      else
        "echo 'stub init failure' >&2"
      end

    File.write!(stub, """
    #!/bin/sh
    echo "called $@" >> #{calls}
    #{create_db}
    exit #{exit_code}
    """)

    File.chmod!(stub, 0o755)
    System.put_env("JUDGEMENT_CLI_CMD", stub)
    stub
  end

  defp seed_schema!(home) do
    {out, status} = System.cmd("python3", ["-c", schema_python(home)], stderr_to_stdout: true)
    assert status == 0, "schema seed failed: #{out}"
  end

  defp call_count(calls) do
    case File.read(calls) do
      {:ok, content} -> content |> String.split("\n", trim: true) |> length()
      {:error, :enoent} -> 0
    end
  end

  test "fresh home: mount runs the init CLI exactly once and renders the empty queue",
       %{conn: conn, home: home, calls: calls} do
    write_stub(home, calls, 0)
    refute File.exists?(Path.join(home, "judgements.db"))

    {:ok, live, html} = live(conn, "/judge")

    # Stub invoked exactly once (2nd mount sees the tables and skips it).
    assert call_count(calls) == 1

    # Normal empty queue, not the CLI dead-end and no warning banner.
    assert html =~ "Review queue"
    assert html =~ "Inbox zero"
    refute html =~ "fabric DB missing"
    refute has_element?(live, "#judge-init-warning")
  end

  test "init CLI failure: page still renders with a visible warning banner",
       %{conn: conn, home: home, calls: calls} do
    write_stub(home, calls, 1)

    {:ok, live, html} = live(conn, "/judge")

    assert call_count(calls) >= 1
    assert html =~ "Review queue"
    assert has_element?(live, "#judge-init-warning")
    assert render(live) =~ "Couldn&#39;t auto-initialize the judgement DB"
  end

  test "init is not re-attempted when the tables already exist",
       %{conn: conn, home: home, calls: calls} do
    seed_schema!(home)
    write_stub(home, calls, 0)

    {:ok, live, html} = live(conn, "/judge")

    assert call_count(calls) == 0
    assert html =~ "Inbox zero"
    refute has_element?(live, "#judge-init-warning")
  end
end
