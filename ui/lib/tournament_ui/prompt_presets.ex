defmodule TournamentUi.PromptPresets do
  @moduledoc "Built-in match-prompt presets the 'new tournament' UI offers."

  @schema_footer """
  Output a markdown block with EXACTLY these section headers, in order:

  ```
  # Match {LABEL} — <short title>

  ## Shared patterns
  - ...
  ## Divergent patterns
  - ...
  ## Naming & exports
  - ...
  ## Validation
  - ...
  ## Error handling
  - ...
  ## Auth & session
  - ...
  ## Return shapes
  - ...
  ## Database access
  - ...
  ## Other conventions
  - ...
  ## Guideline candidates
  1. ...
  2. ...
  ```

  (Claude harness: wrap in <submit>...</submit>. Hermes harness: call submit(markdown=...).)

  Under 500 words inside the block. The answer must stand alone — downstream
  rounds will NOT see the original files, only your submitted markdown.
  """

  @default_sections [
    "## Shared patterns",
    "## Divergent patterns",
    "## Naming & exports",
    "## Validation",
    "## Error handling",
    "## Auth & session",
    "## Return shapes",
    "## Database access",
    "## Other conventions",
    "## Guideline candidates"
  ]

  def all do
    [
      %{
        id: "code-style-convergence",
        name: "Code-style convergence",
        description:
          "Compare two source files; extract shared + divergent conventions; distill prescriptive guidelines.",
        required_sections: @default_sections,
        match_prompt: """
        You are judging a single match in a code-style tournament. Read each
        input fully, identify shared patterns (things both files do the same),
        divergent patterns (where they differ — name which approach is preferable
        and why), and distill 5-12 PRESCRIPTIVE, testable guideline candidates.

        Match label: {LABEL}
        Inputs:
        {INPUTS}

        #{@schema_footer}
        """
      },
      %{
        id: "algorithm-comparison",
        name: "Algorithm comparison",
        description:
          "Two implementations of the same task; compare correctness, complexity, edge-case handling.",
        required_sections: [
          "## Shared approach",
          "## Divergent approach",
          "## Complexity",
          "## Correctness concerns",
          "## Edge cases",
          "## Readability",
          "## Verdict",
          "## Test suggestions",
          "## Refactor notes",
          "## Guideline candidates"
        ],
        match_prompt: """
        Two implementations of the same task. Read both fully. Compare correctness,
        time/space complexity, edge-case handling, and readability.

        Match label: {LABEL}
        Inputs:
        {INPUTS}

        Submit markdown with these headers: ## Shared approach, ## Divergent
        approach, ## Complexity, ## Correctness concerns, ## Edge cases,
        ## Readability, ## Verdict, ## Test suggestions, ## Refactor notes,
        ## Guideline candidates.

        (Claude harness: wrap in <submit>...</submit>. Hermes harness: call
        submit(markdown=...).) Under 500 words.
        """
      },
      %{
        id: "bug-hunt",
        name: "Bug hunt",
        description: "Two similar code blocks; find bugs in each and rank severity.",
        required_sections: [
          "## Bugs in A",
          "## Bugs in B",
          "## Common bug patterns",
          "## Severity ranking",
          "## Root causes",
          "## Minimal fix for A",
          "## Minimal fix for B",
          "## Hardening suggestions",
          "## Test gaps",
          "## Guideline candidates"
        ],
        match_prompt: """
        Two code files. Find bugs, logic errors, race conditions, missing error
        handling, and security issues in each. Rank by severity.

        Match label: {LABEL}
        Inputs:
        {INPUTS}

        Submit markdown with these headers: ## Bugs in A, ## Bugs in B,
        ## Common bug patterns, ## Severity ranking, ## Root causes,
        ## Minimal fix for A, ## Minimal fix for B, ## Hardening suggestions,
        ## Test gaps, ## Guideline candidates.

        (Claude: wrap in <submit>. Hermes: call submit(markdown=...).) Under 500 words.
        """
      },
      %{
        id: "custom",
        name: "Custom (free-form)",
        description:
          "Write your own prompt from scratch. Placeholders: {LABEL}, {INPUTS}, {N_INPUTS}.",
        required_sections: @default_sections,
        match_prompt: """
        (Write your prompt here. Available placeholders: {LABEL}, {INPUTS}, {N_INPUTS}.)

        Match label: {LABEL}
        Inputs:
        {INPUTS}
        """
      }
    ]
  end

  def get(id), do: Enum.find(all(), &(&1.id == id)) || List.first(all())
end
