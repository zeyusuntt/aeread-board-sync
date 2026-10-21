# aeread-board-sync

Keeps the [AERead board](https://github.com/orgs/aeread-org/projects/1) current. A scheduled GitHub Action runs `sync_board.py` every hour; it can also be started by hand from the Actions tab.

## What it changes

Deterministic rules on GitHub facts; no model calls.

- Adds any open issue or pull request of `aeread-org/AERead` that is not on the board, with Status `Triage`.
- Pull requests: draft goes to `In progress`, ready goes to `In review`, merged or closed goes to `Done`. "Waiting on" follows the requested reviewers, or the author after changes are requested. A failing check, other than the `kernel-review` approval gate, is written to "Blocked by" as `auto: CI red: <checks>`.
- Issues: closed goes to `Done`. An open issue with an open pull request that closes it goes to `In review`, with `auto: fix in PR #n`.

It moves Status only between `Triage`, `Backlog`, `Ready`, `In progress` and `In review`, so a human-set `Needs decision`, `Design review` or `Blocked` stays until the item closes. It rewrites "Blocked by" only when it wrote the text itself (prefix `auto:`), and "Waiting on" only when it is empty or names a person.

Theme, Kind and Priority are set at triage by a person; the script never guesses them. Each run writes a digest (items to triage, decisions and reviews waiting per person, red pull requests under the 24-hour rule) to the job summary.

## Setup

The workflow reads one secret, `BOARD_TOKEN`: a classic personal access token with the `project` and `read:org` scopes (`repo` is not needed; AERead is public). Edits made by the action appear under that token's account.

Run locally with `python3 sync_board.py --dry-run` to print the planned changes without writing.

Note: GitHub disables scheduled workflows in a public repository after 60 days without repository activity; re-enable it from the Actions tab if that happens.
