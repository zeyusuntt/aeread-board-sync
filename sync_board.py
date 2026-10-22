"""Keep the AERead board current from GitHub facts. No model calls; deterministic rules only.

Usage: python3 sync_board.py [--dry-run]

What it does on every run:
- adds any open issue or PR of aeread-org/AERead that is not on the board (Status: Triage), then reloads the board;
- PRs: draft -> In progress, ready -> In review, merged or closed -> Done;
  "Waiting on" follows requested reviewers, or the author after changes are requested, and is cleared
  when the request goes away;
  a failing check other than the kernel-review approval gate is written to "Blocked by" as "auto: CI red: <checks>";
- issues: closed -> Done; open with an open PR that closes it -> In review, "auto: fix in PR #n";
- prints a digest: items to triage, decisions waiting per person, red PRs, review requests.

Provenance: the text field "Sync state" holds compact JSON of the Status / Waiting on values this script
last wrote for the item. The script changes one of those only when the field is empty, when it still holds
the value the script wrote, or (Status only) when it holds a machine-managed state. A closed item always
goes to Done. It only rewrites "Blocked by" text that it wrote itself (prefix "auto:").

Any failed read or write makes the run exit with status 1.
"""
import json
import os
import subprocess
import sys

ORG, NUM, REPO = "aeread-org", 1, "aeread-org/AERead"
DRY = "--dry-run" in sys.argv
PEOPLE = {"LEE-CHENYU": "Chenyu", "zeyusuntt": "Zeyu", "ychenfang": "Yuchen"}
MACHINE_STATES = {None, "Triage", "Backlog", "Ready", "In progress", "In review"}
APPROVAL_GATES = {"kernel-review"}
RED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED", "STALE"}
RED_STATES = {"FAILURE", "ERROR"}
STATE_FIELD = "Sync state"
STATE_KEYS = ("Status", "Waiting on")
BATCH = 15


class GqlError(RuntimeError):
    pass


def gql(query, allow_partial=False, **variables):
    """Run one GraphQL call through gh. Raises GqlError on any transport or GraphQL error.

    allow_partial is for mutation batches: GraphQL errors do not raise, the caller checks every alias.
    """
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        raise GqlError(f"gh exit {proc.returncode}, unparsable output: {(proc.stderr or proc.stdout)[:200]!r}")
    errors = out.get("errors")
    if errors:
        messages = "; ".join(str(e.get("message", e))[:200] for e in errors)
        if not allow_partial:
            raise GqlError(f"GraphQL errors: {messages}")
        print("GRAPHQL ERROR:", messages)
    data = out.get("data")
    if proc.returncode != 0 and not (allow_partial and data):
        raise GqlError(f"gh exit {proc.returncode}: {(proc.stderr or out.get('message', ''))[:200]!r}")
    if data is None:
        if allow_partial:
            return {}
        raise GqlError("response has no data")
    return data


ITEMS = """query($org:String!, $num:Int!, $after:String){ organization(login:$org){ projectV2(number:$num){
  id fields(first:30){ nodes{ ... on ProjectV2FieldCommon{ id name } ... on ProjectV2SingleSelectField{ options{ id name } } } }
  items(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id
    fieldValues(first:30){ nodes{
      ... on ProjectV2ItemFieldSingleSelectValue{ name field{ ... on ProjectV2FieldCommon{ name } } }
      ... on ProjectV2ItemFieldTextValue{ text field{ ... on ProjectV2FieldCommon{ name } } } } }
    content{ __typename
      ... on Issue{ id number state repository{ nameWithOwner }
        closedByPullRequestsReferences(first:20, includeClosedPrs:false){ nodes{ number state } } }
      ... on PullRequest{ id number state isDraft merged reviewDecision author{ login } repository{ nameWithOwner }
        reviewRequests(first:10){ nodes{ requestedReviewer{ ... on User{ login } } } }
        commits(last:1){ nodes{ commit{ statusCheckRollup{ contexts(first:100){ pageInfo{ hasNextPage } nodes{
          ... on CheckRun{ name conclusion status startedAt completedAt } ... on StatusContext{ context state } } } } } } } } } } } } } }"""

OPEN_ISSUES = """query($owner:String!, $name:String!, $after:String){ repository(owner:$owner, name:$name){
  issues(states:OPEN, first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id number } } } }"""

OPEN_PRS = """query($owner:String!, $name:String!, $after:String){ repository(owner:$owner, name:$name){
  pullRequests(states:OPEN, first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id number } } } }"""


def load_project():
    items, after, project = [], None, None
    while True:
        data = gql(ITEMS, org=ORG, num=NUM, **({"after": after} if after else {}))
        project = data["organization"]["projectV2"]
        page = project["items"]
        items += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            break
        after = page["pageInfo"]["endCursor"]
    fields = {f["name"]: f for f in project["fields"]["nodes"] if f and f.get("name")}
    return project["id"], fields, items


def open_nodes(query, key):
    owner, name = REPO.split("/")
    nodes, after = [], None
    while True:
        data = gql(query, owner=owner, name=name, **({"after": after} if after else {}))
        page = data["repository"][key]
        nodes += page["nodes"]
        if not page["pageInfo"]["hasNextPage"] or not page["pageInfo"].get("endCursor"):
            return nodes
        after = page["pageInfo"]["endCursor"]


def values(item):
    out = {}
    for node in item["fieldValues"]["nodes"]:
        if node and node.get("field"):
            out[node["field"]["name"]] = node.get("name", node.get("text"))
    return out


def parse_state(raw):
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in STATE_KEYS and isinstance(v, str)}


def ci_state(pr):
    """Return ("red", [names]), ("ok", []) or ("unknown", [])."""
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if not rollup:
        return "ok", []
    contexts = rollup["contexts"]
    if (contexts.get("pageInfo") or {}).get("hasNextPage"):
        return "unknown", []
    latest = {}
    for check in contexts["nodes"]:
        name = check.get("name") or check.get("context")
        if not name or name in APPROVAL_GATES:
            continue
        if "context" in check and "name" not in check:
            red, wait, stamp = check.get("state") in RED_STATES, check.get("state") in ("PENDING", "EXPECTED"), ""
        else:
            red = check.get("conclusion") in RED_CONCLUSIONS
            wait = check.get("conclusion") is None
            stamp = check.get("completedAt") or check.get("startedAt") or ""
        if name not in latest or stamp >= latest[name][0]:
            latest[name] = (stamp, red, wait)
    red_names = sorted(n for n, (_, red, _) in latest.items() if red)
    if red_names:
        return "red", red_names
    if any(wait for _, _, wait in latest.values()):
        return "unknown", []
    return "ok", []


def owned(field, current, state):
    cur = current.get(field)
    if not cur or cur == state.get(field):
        return True
    return field == "Status" and cur in MACHINE_STATES


def desired(item, current, state):
    """Return {field: value}; value None means clear the field."""
    content = item["content"]
    kind = content["__typename"]
    status = current.get("Status")
    waiting_now = current.get("Waiting on")
    own_waiting = bool(waiting_now) and waiting_now == state.get("Waiting on")
    want = {}
    blocked = current.get("Blocked by") or ""
    auto_blocked, blocked_unknown = None, False
    if kind == "PullRequest":
        if content["merged"] or content["state"] != "OPEN":
            want["Status"] = "Done"
            if own_waiting:
                want["Waiting on"] = None
        else:
            if owned("Status", current, state):
                want["Status"] = "In progress" if content["isDraft"] else "In review"
            author = (content.get("author") or {}).get("login")
            if author is not None:
                reviewers = [n["requestedReviewer"]["login"] for n in content["reviewRequests"]["nodes"]
                             if n.get("requestedReviewer")]
                waiting = [PEOPLE[r] for r in reviewers if r in PEOPLE]
                target = None
                if waiting:
                    target = waiting[0]
                elif content["reviewDecision"] == "CHANGES_REQUESTED" and author in PEOPLE:
                    target = PEOPLE[author]
                if target is not None:
                    if owned("Waiting on", current, state):
                        want["Waiting on"] = target
                elif own_waiting:
                    want["Waiting on"] = None
            verdict, red = ci_state(content)
            if verdict == "red":
                auto_blocked = "auto: CI red: " + ", ".join(red)[:100]
            elif verdict == "unknown":
                blocked_unknown = True
    elif kind == "Issue":
        if content["state"] != "OPEN":
            want["Status"] = "Done"
        else:
            fixes = [p["number"] for p in content["closedByPullRequestsReferences"]["nodes"] if p["state"] == "OPEN"]
            if fixes:
                if owned("Status", current, state):
                    want["Status"] = "In review"
                auto_blocked = "auto: fix in PR " + ", ".join(f"#{n}" for n in fixes)
            elif status and status == state.get("Status") and status in ("Done", "In review"):
                want["Status"] = "Backlog"
    if blocked_unknown:
        pass
    elif auto_blocked and (not blocked or blocked.startswith("auto:")):
        want["Blocked by"] = auto_blocked
    elif not auto_blocked and blocked.startswith("auto:"):
        want["Blocked by"] = None
    return {k: v for k, v in want.items() if (current.get(k) or None) != v}


def set_value(project_id, fields, item_id, name, value):
    field = fields[name]
    head = f'projectId:"{project_id}", itemId:"{item_id}", fieldId:"{field["id"]}"'
    if value is None:
        return f"clearProjectV2ItemFieldValue(input:{{{head}}}){{ clientMutationId }}"
    if "options" in field:
        option = next((o["id"] for o in field["options"] if o["name"] == value), None)
        if option is None:
            raise GqlError(f"field {name!r} has no option {value!r}")
        payload = f'{{singleSelectOptionId:"{option}"}}'
    else:
        payload = f"{{text:{json.dumps(value)}}}"
    return f"updateProjectV2ItemFieldValue(input:{{{head}, value:{payload}}}){{ clientMutationId }}"


def encode_state(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def run_batches(groups, failures):
    """groups: list of (label, [mutation, ...]); an item's mutations always share one batch."""
    batches, cur, size = [], [], 0
    for group in groups:
        if cur and size + len(group[1]) > BATCH:
            batches.append(cur)
            cur, size = [], 0
        cur.append(group)
        size += len(group[1])
    if cur:
        batches.append(cur)
    for batch in batches:
        flat = [(label, m) for label, muts in batch for m in muts]
        query = "mutation{ " + " ".join(f"e{k}: {m}" for k, (_, m) in enumerate(flat)) + " }"
        try:
            data = gql(query, allow_partial=True)
        except GqlError as exc:
            for label, _ in flat:
                failures.append(f"{label}: {exc}")
            continue
        for k, (label, _) in enumerate(flat):
            if data.get(f"e{k}") is None:
                failures.append(f"{label}: write returned null")


def main():
    failures = []
    project_id, fields, items = load_project()
    if STATE_FIELD not in fields and not DRY:
        data = gql(f'mutation{{ createProjectV2Field(input:{{projectId:"{project_id}", dataType:TEXT, name:"{STATE_FIELD}"}}){{ '
                   f'projectV2Field{{ ... on ProjectV2Field{{ id name }} }} }} }}')
        created = (data.get("createProjectV2Field") or {}).get("projectV2Field")
        if not created or not created.get("id"):
            raise GqlError("could not create the Sync state field")
        fields[STATE_FIELD] = created
        print(f"created field {STATE_FIELD!r}")
    has_state = STATE_FIELD in fields

    on_board = {it["content"].get("id") for it in items if it.get("content")}
    missing = [n for n in open_nodes(OPEN_ISSUES, "issues") + open_nodes(OPEN_PRS, "pullRequests") if n["id"] not in on_board]
    added = 0
    for node in missing:
        print(f"add #{node['number']} (Triage)")
        if DRY:
            continue
        try:
            data = gql(f'mutation{{ addProjectV2ItemById(input:{{projectId:"{project_id}", contentId:"{node["id"]}"}}){{ item{{ id }} }} }}')
            item_id = ((data.get("addProjectV2ItemById") or {}).get("item") or {}).get("id")
            if not item_id:
                raise GqlError("add returned no item")
            muts = [set_value(project_id, fields, item_id, "Status", "Triage")]
            if has_state:
                muts.append(set_value(project_id, fields, item_id, STATE_FIELD, encode_state({"Status": "Triage"})))
            run_batches([(f"add #{node['number']}", muts)], failures)
            added += 1
        except GqlError as exc:
            failures.append(f"add #{node['number']}: {exc}")
    if added:
        project_id, fields, items = load_project()
        has_state = STATE_FIELD in fields

    groups, nedits = [], 0
    digest = {"triage": [], "decisions": {}, "red": [], "review": {}}
    for item in items:
        content = item.get("content")
        if not content or content.get("repository", {}).get("nameWithOwner") != REPO:
            continue
        current = values(item)
        state = parse_state(current.get(STATE_FIELD)) if has_state else {}
        label = ("PR#" if content["__typename"] == "PullRequest" else "#") + str(content["number"])
        new_state, muts = dict(state), []
        for field_name, value in desired(item, current, state).items():
            print(f"{label}: {field_name}: {current.get(field_name)!r} -> {value!r}")
            try:
                muts.append(set_value(project_id, fields, item["id"], field_name, value))
            except GqlError as exc:
                failures.append(f"{label}: {exc}")
                continue
            if field_name in STATE_KEYS:
                if value is None:
                    new_state.pop(field_name, None)
                else:
                    new_state[field_name] = value
            current[field_name] = value
        if muts and has_state and new_state != state:
            muts.append(set_value(project_id, fields, item["id"], STATE_FIELD, encode_state(new_state)))
        if muts:
            nedits += len(muts)
            groups.append((label, muts))
        if content.get("state") == "OPEN":
            if current.get("Status") == "Triage":
                digest["triage"].append(label)
            if current.get("Status") == "Needs decision":
                digest["decisions"].setdefault(current.get("Waiting on") or "unassigned", []).append(label)
            if (current.get("Blocked by") or "").startswith("auto: CI red"):
                digest["red"].append(label)
            if content["__typename"] == "PullRequest" and current.get("Status") == "In review" and current.get("Waiting on"):
                digest["review"].setdefault(current["Waiting on"], []).append(label)
    if groups and not DRY:
        run_batches(groups, failures)

    lines = [f"{added} added, {nedits} field updates{' (dry run)' if DRY else ''}",
             "To triage: " + (", ".join(digest["triage"]) or "none")]
    lines += [f"Decisions waiting on {who}: " + ", ".join(labels) for who, labels in sorted(digest["decisions"].items())]
    lines += [f"Reviews waiting on {who}: " + ", ".join(labels) for who, labels in sorted(digest["review"].items())]
    lines.append("Red CI (24-hour rule): " + (", ".join(digest["red"]) or "none"))
    if DRY and missing:
        lines[0] = f"{len(missing)} to add, {nedits} field updates (dry run)"
    if failures:
        lines.append(f"FAILED writes: {len(failures)}")
        lines += [f"FAIL {f}" for f in failures]
    print("\n" + "\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write("## AERead board digest\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GqlError as exc:
        print(f"FATAL: {exc}")
        sys.exit(1)
