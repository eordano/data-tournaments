defmodule TournamentUi.Optimizer do
  @moduledoc """
  Calls `claude -p` locally to critique + rewrite a match prompt given
  sample outputs and optional free-form critique.
  """

  @meta_prompt ~S"""
  You are improving the judging prompt used in a single-elimination code-style
  tournament. Each match feeds two inputs to an agent; the agent reads them and
  submits a structured markdown analysis. The prompt below produced the SAMPLE
  OUTPUTS below. Read them critically, identify specific weaknesses (vague
  bullets, missed patterns, off-topic sections, inconsistent structure, poor
  signal density), and produce an improved prompt that fixes those weaknesses
  while preserving the existing output schema.

  OUTPUT FORMAT — exactly three fenced blocks, in this order:
  ```diagnosis
  (3-7 bullets naming concrete weaknesses observed in the samples)
  ```
  ```improved_prompt
  (the full replacement prompt — keep {LABEL}, {INPUTS}, {N_INPUTS} placeholders
  and the section-header schema; trim fluff, tighten requirements, add concrete
  anti-patterns where useful)
  ```
  ```rationale
  (2-4 bullets: what the improved prompt changes and why)
  ```

  Output NOTHING outside those three blocks.
  """

  @doc """
  Returns `{:ok, %{diagnosis: s, improved: s, rationale: s}}` or `{:error, reason}`.

  `samples` = list of %{label: "R1-3", conclusion: "..."}; `critique` is a
  free-form string from the user (may be empty).
  """
  def optimize(current_prompt, samples, critique \\ "") do
    user_text = build_user_text(current_prompt, samples, critique)

    args =
      [
        "-p",
        "--allowedTools",
        "",
        "--disallowedTools",
        "Bash Edit Write Glob Grep Agent WebFetch WebSearch NotebookEdit"
      ]

    case System.cmd("claude", args, input: user_text, stderr_to_stdout: true) do
      {output, 0} -> {:ok, parse(output)}
      {output, code} -> {:error, "claude -p exit #{code}: #{String.slice(output, 0, 2000)}"}
    end
  end

  defp build_user_text(current_prompt, samples, critique) do
    samples_text =
      samples
      |> Enum.map(fn %{label: l, conclusion: c} ->
        "--- SAMPLE #{l} ---\n#{String.slice(c || "", 0, 2500)}"
      end)
      |> Enum.join("\n\n")

    crit =
      if critique == "" do
        "(no explicit user critique)"
      else
        critique
      end

    """
    #{@meta_prompt}

    === CURRENT PROMPT ===
    #{current_prompt}

    === USER CRITIQUE ===
    #{crit}

    === SAMPLE OUTPUTS ===
    #{samples_text}
    """
  end

  defp parse(output) do
    %{
      diagnosis: extract_block(output, "diagnosis"),
      improved: extract_block(output, "improved_prompt"),
      rationale: extract_block(output, "rationale")
    }
  end

  defp extract_block(text, tag) do
    pattern = ~r/```#{tag}\s*\n(.*?)\n```/s

    case Regex.run(pattern, text) do
      [_, body] -> String.trim(body)
      _ -> ""
    end
  end
end
