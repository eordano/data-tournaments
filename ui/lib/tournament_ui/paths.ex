defmodule TournamentUi.Paths do
  @moduledoc """
  Central path resolver. Overridable via env vars; sensible defaults.

  DATA_TOURNAMENTS_HOME — runtime state root (logs, uploads, slot files, configs).
  DATA_TOURNAMENTS_BIN  — bin directory with harness scripts.
  DATA_TOURNAMENTS_CONFIGS — config JSON directory (defaults to $REPO/configs).
  TOURNAMENT_BROWSE_ROOTS — ':'-separated list of allowed browsable roots.
  """

  def home, do: System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"

  def bin,
    do:
      System.get_env("DATA_TOURNAMENTS_BIN") ||
        Path.expand("~/projects/data-tournaments/bin")

  def configs_dir do
    System.get_env("DATA_TOURNAMENTS_CONFIGS") ||
      Path.expand("~/projects/data-tournaments/configs")
  end

  def runs_dir, do: Path.join(home(), "runs")
  def sessions_dir, do: Path.join(home(), "sessions")
  def uploads_dir, do: Path.join(home(), "uploads")

  def harness, do: Path.join(bin(), "hermes-harness.sh")

  def run_script, do: Path.join(bin(), "run-tournament.py")

  def ensure_dirs! do
    for d <- [home(), configs_dir(), runs_dir(), sessions_dir(), uploads_dir()] do
      File.mkdir_p!(d)
    end
  end
end
