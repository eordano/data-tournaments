defmodule TournamentUiWeb.JudgeVerdictComponentsTest do
  @moduledoc """
  Eject dressing is VERDICT-driven, never geometry-driven: the swiss
  engine ejects on the discard vocabulary only (DISCARD_VERDICTS in
  bin/swiss.py), and the SingleJudgement axis reuses position "se" for
  verdicts that merely score. A position-keyed rule dressed that rung as
  "Ejects B permanently" on a payload with no B at all.
  """
  use ExUnit.Case, async: true

  alias TournamentUiWeb.JudgeVerdictComponents, as: Verdicts

  test "only the discard verdicts eject" do
    assert Verdicts.ejects?("discard-a")
    assert Verdicts.ejects?("discard-b")
  end

  test "verdicts that merely score are not destructive, eject-position residents included" do
    refute Verdicts.ejects?("revise")
    refute Verdicts.ejects?("not-worth-pursuing")
    refute Verdicts.ejects?("ui-a-out")
    refute Verdicts.ejects?("ui-b-out")
  end

  test "the axis's terminal rung scores, it does not eject" do
    refute Verdicts.ejects?("invalid")
    refute Verdicts.ejects?("reject-invalid")
    assert Verdicts.eject_consequence("invalid") == nil
    assert Verdicts.eject_consequence("reject-invalid") == nil
  end

  test "eject_consequence names what happens to the OTHER side, verdict-keyed" do
    assert Verdicts.eject_consequence("discard-a") ==
             "Ejects A permanently; B is not credited and is paired again."

    assert Verdicts.eject_consequence("discard-b") ==
             "Ejects B permanently; A is not credited and is paired again."

    assert Verdicts.eject_consequence("revise") == nil
    assert Verdicts.eject_consequence("not-worth-pursuing") == nil
  end
end
