---
id: foundry-tour-activation
status: running
owner: owner@example.com
hypothesis:
  statement: >-
    Auto-starting the guided tour on a visitor's first front-door view raises
    the share of exposed visitors who submit a pledge versus keeping the tour
    opt-in behind the header button.
  because: >-
    Opt-in tours are discovered by almost nobody; auto-opening moves every
    first-time visitor onto the guided path at the moment intent is highest.
metric:
  primary: fd_pledge_submitted_rate
  numerator: fd_pledge_submitted
  denominator: experiment_exposed
  guardrails:
    - fd_game_link_opened
    - fd_tour_dismissed
decision:
  rule: >-
    Ship auto-tour if the primary beats control by the MDE and both
    guardrails hold; otherwise keep the opt-in tour.
experiment:
  key: foundry-tour-activation
  unit: session
  variants:
    - id: control
    - id: auto
---

# Tour activation

The front door is dense; the tour's second step walks the visitor to the
Exchange and asks them to pledge.

## Data reality

The pledge submit event fires from the real Exchange form; the tour steps
are simulated in Storybook only.
