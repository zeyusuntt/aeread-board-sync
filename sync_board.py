"""Keep the AERead board current from GitHub facts. No model calls; deterministic rules only.

Usage: python3 sync_board.py [--dry-run]

What it does on every run:
- adds any open issue or PR of aeread-org/AERead that is not on the board (Status: Triage);
- PRs: draft -> In progress, ready -> In review, merged or closed -> Done;
  "Waiting on" follows requested reviewers, or the author after changes are requested;
  a failing check other than the kernel-review approval gate is written to "Blocked by" as "auto: CI red: <checks>";
- issues: closed -> Done; open with an open PR that closes it -> In review, "auto: fix in PR #n";
- prints a digest: items to triage, decisions waiting per person, red PRs, review requests.

It only moves Status between machine-managed states, so a human-set
Needs decision / Design review / Blocked survives until the item closes.
It only rewrites "Blocked by" text that it wrote itself (prefix "auto:").
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


def gql(query, **variables):
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args += ["-F" if isinstance(value, int) else "-f", f"{key}={value}"]
    out = json.loads(subprocess.run(args, capture_output=True, text=True, check=False).stdout or "{}")
    for error in out.get("errors", []):
        print("GRAPHQL ERROR:", error.get("message", "")[:200])
    return out.get("data") or {}


ITEMS = """query($org:String!, $num:Int!, $after:String){ organization(login:$org){ projectV2(number:$num){
  id fields(first:30){ nodes{ ... on ProjectV2FieldCommon{ id name } ... on ProjectV2SingleSelectField{ options{ id name } } } }
  items(first:100, after:$after){ pageInfo{ hasNextPage endCursor } nodes{ id
    fieldValues(first:20){ nodes{
      ... on ProjectV2ItemFieldSingleSelectValue{ name field{ ... on ProjectV2FieldCommon{ name } } }
      ... on ProjectV2ItemFieldTextValue{ text field{ ... on ProjectV2FieldCommon{ name } } } } }
    content{ __typename
      ... on Issue{ id number state repository{ nameWithOwner }
        closedByPullRequestsReferences(first:5, includeClosedPrs:false){ nodes{ number state } } }
      ... on PullRequest{ id number state isDraft merged reviewDecision author{ login } repository{ nameWithOwner }
        reviewRequests(first:10){ nodes{ requestedReviewer{ ... on User{ login } } } }
        commits(last:1){ nodes{ commit{ statusCheckRollup{ contexts(first:40){ nodes{
          ... on CheckRun{ name conclusion status } ... on StatusContext{ context state } } } } } } } } } } } } } }"""

OPEN_CONTENT = """query($owner:String!, $name:String!){ repository(owner:$owner, name:$name){
  issues(states:OPEN, first:100){ nodes{ id number } }
  pullRequests(states:OPEN, first:100){ nodes{ id number } } } }"""


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
    fields = {f["name"]: f for f in project["fields"]["nodes"] if f}
    return project["id"], fields, items


def values(item):
    out = {}
    for node in item["fieldValues"]["nodes"]:
        if node and node.get("field"):
            out[node["field"]["name"]] = node.get("name", node.get("text"))
    return out


def red_checks(pr):
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if not rollup:
        return []
    red = []
    for check in rollup["contexts"]["nodes"]:
        name = check.get("name") or check.get("context")
        bad = check.get("conclusion") in ("FAILURE", "TIMED_OUT", "STARTUP_FAILURE") or check.get("state") in ("FAILURE", "ERROR")
        if bad and name not in APPROVAL_GATES:
            red.append(name)
    return sorted(set(red))


def desired(item, current):
    content = item["content"]
    kind = content["__typename"]
    status = current.get("Status")
    want = {}
    blocked = current.get("Blocked by") or ""
    auto_blocked = None
    if kind == "PullRequest":
        if content["merged"] or content["state"] != "OPEN":
            want["Status"] = "Done"
        elif status in MACHINE_STATES:
            want["Status"] = "In progress" if content["isDraft"] else "In review"
        if content["state"] == "OPEN":
            reviewers = [n["requestedReviewer"]["login"] for n in content["reviewRequests"]["nodes"] if n["requestedReviewer"]]
            waiting = [PEOPLE[r] for r in reviewers if r in PEOPLE]
            # A human-set reason such as "Landing order" outranks a review request.
            if current.get("Waiting on") in (None, *PEOPLE.values()):
                if waiting:
                    want["Waiting on"] = waiting[0]
                elif content["reviewDecision"] == "CHANGES_REQUESTED" and content["author"]["login"] in PEOPLE:
                    want["Waiting on"] = PEOPLE[content["author"]["login"]]
            red = red_checks(content)
            if red:
                auto_blocked = "auto: CI red: " + ", ".join(red)[:100]
    elif kind == "Issue":
        if content["state"] != "OPEN":
            want["Status"] = "Done"
        else:
            fixes = [p["number"] for p in content["closedByPullRequestsReferences"]["nodes"] if p["state"] == "OPEN"]
            if fixes:
                if status in MACHINE_STATES:
                    want["Status"] = "In review"
                auto_blocked = "auto: fix in PR " + ", ".join(f"#{n}" for n in fixes)
    if auto_blocked and (not blocked or blocked.startswith("auto:")):
        want["Blocked by"] = auto_blocked
    elif not auto_blocked and blocked.startswith("auto:"):
        want["Blocked by"] = ""
    return {k: v for k, v in want.items() if current.get(k) != v and not (v == "" and not current.get(k))}


def set_value(project_id, fields, item_id, name, value):
    field = fields[name]
    if "options" in field:
        option = next(o["id"] for o in field["options"] if o["name"] == value)
        payload = f'{{singleSelectOptionId:"{option}"}}'
    else:
        payload = f"{{text:{json.dumps(value)}}}"
    return (f'updateProjectV2ItemFieldValue(input:{{projectId:"{project_id}", itemId:"{item_id}", '
            f'fieldId:"{field["id"]}", value:{payload}}}){{ clientMutationId }}')


def main():
    project_id, fields, items = load_project()
    on_board = {it["content"].get("id") for it in items if it.get("content")}
    owner, name = REPO.split("/")
    repo = gql(OPEN_CONTENT, owner=owner, name=name)["repository"]
    missing = [n for n in repo["issues"]["nodes"] + repo["pullRequests"]["nodes"] if n["id"] not in on_board]
    for node in missing:
        print(f"add #{node['number']} (Triage)")
        if not DRY:
            data = gql(f'mutation{{ addProjectV2ItemById(input:{{projectId:"{project_id}", contentId:"{node["id"]}"}}){{ item{{ id }} }} }}')
            item_id = data["addProjectV2ItemById"]["item"]["id"]
            gql("mutation{ " + set_value(project_id, fields, item_id, "Status", "Triage") + " }")

    edits, digest = [], {"triage": [], "decisions": {}, "red": [], "review": {}}
    for item in items:
        content = item.get("content")
        if not content or content.get("repository", {}).get("nameWithOwner") != REPO:
            continue
        current = values(item)
        label = ("PR#" if content["__typename"] == "PullRequest" else "#") + str(content["number"])
        for field_name, value in desired(item, current).items():
            print(f"{label}: {field_name}: {current.get(field_name)!r} -> {value!r}")
            edits.append(set_value(project_id, fields, item["id"], field_name, value))
            current[field_name] = value
        if content.get("state") == "OPEN":
            if current.get("Status") == "Triage":
                digest["triage"].append(label)
            if current.get("Status") == "Needs decision":
                digest["decisions"].setdefault(current.get("Waiting on") or "unassigned", []).append(label)
            if (current.get("Blocked by") or "").startswith("auto: CI red"):
                digest["red"].append(label)
            if content["__typename"] == "PullRequest" and current.get("Status") == "In review" and current.get("Waiting on"):
                digest["review"].setdefault(current["Waiting on"], []).append(label)
    if edits and not DRY:
        for start in range(0, len(edits), 15):
            chunk = edits[start:start + 15]
            gql("mutation{ " + " ".join(f"e{k}: {e}" for k, e in enumerate(chunk)) + " }")
    lines = [f"{len(missing)} added, {len(edits)} field updates{' (dry run)' if DRY else ''}",
             "To triage: " + (", ".join(digest["triage"]) or "none")]
    lines += [f"Decisions waiting on {who}: " + ", ".join(labels) for who, labels in sorted(digest["decisions"].items())]
    lines += [f"Reviews waiting on {who}: " + ", ".join(labels) for who, labels in sorted(digest["review"].items())]
    lines.append("Red CI (24-hour rule): " + (", ".join(digest["red"]) or "none"))
    print("\n" + "\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as handle:
            handle.write("## AERead board digest\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")


if __name__ == "__main__":
    main()
