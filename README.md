# aeread-board-sync

Keeps the [AERead board](https://github.com/orgs/aeread-org/projects/1) current. A scheduled GitHub Action runs `sync_board.py` every hour; it can also be started by hand from the Actions tab.

## What it changes

Deterministic rules on GitHub facts; no model calls.

- Adds any open issue or pull request of `aeread-org/AERead` that is not on the board, with Status `Triage`.
- Pull requests: draft goes to `In progress`, ready goes to `In review`, merged or closed goes to `Done`. "Waiting on" follows the requested reviewers, or the author after changes are requested. A failing check, other than the `kernel-review` approval gate, is written to "Blocked by" as `auto: CI red: <checks>`.
- Issues: closed goes to `Done`. An open issue with an open pull request that closes it goes to `In review`, with `auto: fix in PR #n`.

It moves Status only between `Triage`, `Backlog`, `Ready`, `In progress` and `In review`, so a human-set `Needs decision`, `Design review` or `Blocked` stays until the item closes (a closed item always goes to `Done`). It rewrites "Blocked by" only when it wrote the text itself (prefix `auto:`).

### Sync state

The machine-owned text field `Sync state` holds compact JSON of the Status and "Waiting on" values the script itself last wrote for the item, for example `{"Status":"Done","Waiting on":"Chenyu"}`. The script creates the field on its first real run (not in `--dry-run`, where state is treated as empty). It changes Status or "Waiting on" only when the current value is empty, equals what Sync state says the script wrote, or (Status only) is one of the machine-managed states above. A value a person set is never overwritten. Because of that:

- "Waiting on" is cleared when the review request it came from is gone, or the pull request closed, if the value is the script's own.
- An item the script set to `Done` that is open again goes back to `Backlog` (issue) or `In progress` / `In review` (pull request, by draft state).
- An issue the script set to `In review` for a closing pull request goes back to `Backlog` when no open closing pull request remains.

The values are written first; Sync state is then written in a second call and records only the Status / "Waiting on" writes that were confirmed, so it never claims a value that failed to land. If the state write itself fails, the run reports a failure. Do not edit it by hand; clear it to hand an item fully back to people.

### CI and paging

Per check name only the latest run counts (by completion time, else start time). If the latest run cannot be determined (competing runs without usable times) or a queued or in-progress run exists for that name, CI is unknown. It is red when that run is `FAILURE`, `TIMED_OUT`, `STARTUP_FAILURE`, `ACTION_REQUIRED`, `CANCELLED` or `STALE`, or a status context is `FAILURE` or `ERROR`. If a check is still pending and nothing is red, or a pull request has more than 100 check contexts, CI is unknown and an existing `auto:` blocker is left as it is. `kernel-review` is ignored. All open issues and pull requests are read page by page. New items are added, the board is reloaded, and the rules then run on them in the same run, so a second run with unchanged inputs makes no edits.

### Failures

Any failed read or write (non-zero `gh` exit, unparsable output, GraphQL errors, a mutation alias that comes back null, or a GraphQL error on a mutation alias or sub-field even when the alias came back non-null; an error without a path fails the whole batch) is reported at the end of the run, which then exits with status 1 so the Action run turns red. Other batches still run after a failure. A pull request with no author never crashes the run; review requests still drive "Waiting on" (including clearing the script's own stale value), only the changes-requested rule needs the author.

Theme, Kind and Priority are set at triage by a person; the script never guesses them. Each run writes a digest (items to triage, decisions and reviews waiting per person, red pull requests under the 24-hour rule) to the job summary.

## Setup

The workflow reads one secret, `BOARD_TOKEN`: a classic personal access token with the `project` and `read:org` scopes (`repo` is not needed; AERead is public). Edits made by the action appear under that token's account.

Run locally with `python3 sync_board.py --dry-run` to print the planned changes without writing. Tests: `python -m pytest tests -q` (offline, `gql` is mocked).

Note: GitHub disables scheduled workflows in a public repository after 60 days without repository activity; re-enable it from the Actions tab if that happens.
