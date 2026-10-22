"""Offline tests for sync_board.py: gql is replaced by an in-memory fake board."""
import json
import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sync_board as sync  # noqa: E402

REPO = "aeread-org/AERead"
STATUS_OPTS = ["Triage", "Backlog", "Ready", "In progress", "In review", "Needs decision", "Design review", "Blocked", "Done"]


def check(name, conclusion="SUCCESS", completed="2026-01-01T00:00:00Z", started="2026-01-01T00:00:00Z", status="COMPLETED"):
    return {"name": name, "conclusion": conclusion, "status": status, "startedAt": started, "completedAt": completed}


def pending(name, started="2026-01-02T00:00:00Z"):
    return {"name": name, "conclusion": None, "status": "IN_PROGRESS", "startedAt": started, "completedAt": None}


def issue(number, state="OPEN", fixes=()):
    return {"__typename": "Issue", "id": f"I_{number}", "number": number, "state": state,
            "repository": {"nameWithOwner": REPO},
            "closedByPullRequestsReferences": {"nodes": [{"number": n, "state": "OPEN"} for n in fixes]}}


def pr(number, state="OPEN", draft=False, merged=False, decision=None, author="zeyusuntt", reviewers=(),
       checks=(), more_checks=False):
    return {"__typename": "PullRequest", "id": f"PR_{number}", "number": number, "state": state, "isDraft": draft,
            "merged": merged, "reviewDecision": decision,
            "author": {"login": author} if author else None,
            "repository": {"nameWithOwner": REPO},
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": r}} for r in reviewers]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {
                "pageInfo": {"hasNextPage": more_checks, "endCursor": "x"}, "nodes": list(checks)}}}}]}}


class World:
    def __init__(self, state_field=True, page_size=100):
        self.fields = {
            "Status": {"id": "F_status", "options": STATUS_OPTS},
            "Waiting on": {"id": "F_wait", "options": ["Chenyu", "Zeyu", "Yuchen"]},
            "Blocked by": {"id": "F_block"},
        }
        if state_field:
            self.fields["Sync state"] = {"id": "F_sync"}
        self.items = {}
        self.contents = {}
        self.open_issues, self.open_prs = [], []
        self.page_size = page_size
        self.calls = []            # every mutation call: list of (itemId, fieldName)
        self.mutation_queries = []
        self.fail = lambda item_id, field: False
        self.created_fields = 0

    def add(self, content, **values):
        item_id = f"ITEM_{content['id']}"
        self.items[item_id] = {"content": content, "values": dict(values)}
        self.contents[content["id"]] = content
        if content["state"] == "OPEN":
            (self.open_issues if content["__typename"] == "Issue" else self.open_prs).append(
                {"id": content["id"], "number": content["number"]})
        return item_id

    def unboarded(self, content):
        self.contents[content["id"]] = content
        if content["state"] == "OPEN":
            (self.open_issues if content["__typename"] == "Issue" else self.open_prs).append(
                {"id": content["id"], "number": content["number"]})

    def vals(self, content_id):
        return self.items[f"ITEM_{content_id}"]["values"]

    def _field_by_id(self, fid):
        return next(n for n, f in self.fields.items() if f["id"] == fid)

    def _project(self):
        nodes = []
        for name, f in self.fields.items():
            node = {"id": f["id"], "name": name}
            if "options" in f:
                node["options"] = [{"id": f"{f['id']}:{o}", "name": o} for o in f["options"]]
            nodes.append(node)
        items = []
        for item_id, it in self.items.items():
            fv = []
            for fname, val in it["values"].items():
                if val is None:
                    continue
                if "options" in self.fields[fname]:
                    fv.append({"name": val, "field": {"name": fname}})
                else:
                    fv.append({"text": val, "field": {"name": fname}})
            items.append({"id": item_id, "fieldValues": {"nodes": fv}, "content": it["content"]})
        return {"id": "P1", "fields": {"nodes": nodes},
                "items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": items}}

    def _page(self, nodes, after):
        start = int(after or 0)
        chunk = nodes[start:start + self.page_size]
        more = start + self.page_size < len(nodes)
        return {"pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page_size)}, "nodes": chunk}

    def gql(self, query, allow_partial=False, **variables):
        if "projectV2(number" in query:
            return {"organization": {"projectV2": self._project()}}
        if "issues(states:OPEN" in query:
            return {"repository": {"issues": self._page(self.open_issues, variables.get("after"))}}
        if "pullRequests(states:OPEN" in query:
            return {"repository": {"pullRequests": self._page(self.open_prs, variables.get("after"))}}
        if "createProjectV2Field" in query:
            self.created_fields += 1
            self.fields["Sync state"] = {"id": "F_sync"}
            return {"createProjectV2Field": {"projectV2Field": {"id": "F_sync", "name": "Sync state"}}}
        if "addProjectV2ItemById" in query:
            cid = re.search(r'contentId:"(.*?)"', query).group(1)
            item_id = f"ITEM_{cid}"
            self.items[item_id] = {"content": self.contents[cid], "values": {}}
            return {"addProjectV2ItemById": {"item": {"id": item_id}}}
        assert query.startswith("mutation{"), query
        self.mutation_queries.append(query)
        out, batch = {}, []
        pat_set = re.compile(r'(\w+): updateProjectV2ItemFieldValue\(input:\{projectId:"[^"]*", itemId:"(.*?)", '
                             r'fieldId:"(.*?)", value:\{(singleSelectOptionId|text):(.*?)\}\}\)\{ clientMutationId \}')
        pat_clear = re.compile(r'(\w+): clearProjectV2ItemFieldValue\(input:\{projectId:"[^"]*", itemId:"(.*?)", '
                               r'fieldId:"(.*?)"\}\)\{ clientMutationId \}')
        for m in pat_set.finditer(query):
            alias, item_id, fid, kind, raw = m.groups()
            fname = self._field_by_id(fid)
            batch.append((alias, item_id, fname, json.loads(raw) if kind == "text" else raw.strip('"').split(":", 1)[1]))
        for m in pat_clear.finditer(query):
            alias, item_id, fid = m.groups()
            batch.append((alias, item_id, self._field_by_id(fid), None))
        assert batch, f"unparsed mutation: {query}"
        for alias, item_id, fname, value in batch:
            self.calls.append((item_id, fname))
            if self.fail(item_id, fname):
                out[alias] = None
                continue
            self.items[item_id]["values"][fname] = value
            out[alias] = {"clientMutationId": None}
        return out


@pytest.fixture
def run(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def go(world, dry=False):
        monkeypatch.setattr(sync, "gql", world.gql)
        monkeypatch.setattr(sync, "DRY", dry)
        return sync.main()
    return go


def state(world, content_id):
    return json.loads(world.vals(content_id).get("Sync state") or "{}")


# ---- 1. errors ------------------------------------------------------------------------------

def fake_run(monkeypatch, stdout, code=0, stderr=""):
    monkeypatch.setattr(sync.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=stdout, stderr=stderr))


def test_gql_raises_on_nonzero_exit(monkeypatch):
    fake_run(monkeypatch, "", code=1, stderr="boom")
    with pytest.raises(Exception):
        sync.gql("query{x}")


def test_gql_raises_on_unparsable_output(monkeypatch):
    fake_run(monkeypatch, "<html>502</html>")
    with pytest.raises(Exception):
        sync.gql("query{x}")


def test_gql_raises_on_graphql_errors(monkeypatch):
    fake_run(monkeypatch, json.dumps({"data": {"a": 1}, "errors": [{"message": "nope"}]}))
    with pytest.raises(Exception):
        sync.gql("query{x}")


def test_gql_returns_data(monkeypatch):
    fake_run(monkeypatch, json.dumps({"data": {"a": 1}}))
    assert sync.gql("query{x}") == {"a": 1}


def test_gql_partial_mutation_returns_data_with_null_alias(monkeypatch):
    fake_run(monkeypatch, json.dumps({"data": {"e0": {"clientMutationId": None}, "e1": None}, "errors": [{"message": "bad"}]}), code=1)
    assert sync.gql("mutation{x}", allow_partial=True) == {"e0": {"clientMutationId": None}, "e1": None}


def test_failed_alias_keeps_going_and_exits_one(run, capsys):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    w.add(pr(2, reviewers=["ychenfang"]), Status="Triage")
    w.fail = lambda item_id, field: item_id == "ITEM_PR_1" and field == "Status"
    assert run(w) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "PR#1" in out
    assert w.vals("PR_2")["Status"] == "In review"           # other item still written
    assert w.vals("PR_2")["Waiting on"] == "Yuchen"


def test_all_writes_ok_exits_zero(run):
    w = World()
    w.add(pr(1), Status="Triage")
    assert run(w) == 0


def test_whole_batch_exception_is_reported(run, capsys):
    w = World()
    w.add(pr(1), Status="Triage")
    real = w.gql

    def flaky(query, **kw):
        if query.startswith("mutation{"):
            raise sync.GqlError("network down")
        return real(query, **kw)
    w.gql = flaky
    assert run(w) == 1
    assert "FAIL" in capsys.readouterr().out


# ---- 2. pagination --------------------------------------------------------------------------

def test_open_issues_and_prs_are_paged(run):
    w = World(page_size=2)
    for n in range(1, 6):
        w.unboarded(issue(n))
    for n in range(10, 15):
        w.unboarded(pr(n))
    assert run(w) == 0
    assert len(w.items) == 10


def test_checks_query_asks_for_100_with_pageinfo():
    assert "contexts(first:100" in sync.ITEMS and "pageInfo" in sync.ITEMS.split("contexts(first:100")[1][:60]


def test_truncated_checks_leave_blocker_unchanged(run):
    w = World()
    w.add(pr(1, checks=[check("test")], more_checks=True), Status="In review", **{"Blocked by": "auto: CI red: test"})
    assert run(w) == 0
    assert w.vals("PR_1")["Blocked by"] == "auto: CI red: test"


def test_truncated_checks_do_not_set_blocker(run):
    w = World()
    w.add(pr(1, checks=[check("test", "FAILURE")], more_checks=True), Status="In review")
    run(w)
    assert not w.vals("PR_1").get("Blocked by")


# ---- 3. provenance --------------------------------------------------------------------------

def test_sync_state_field_created_when_missing(run):
    w = World(state_field=False)
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    assert run(w) == 0
    assert w.created_fields == 1
    assert state(w, "PR_1") == {"Status": "In review", "Waiting on": "Chenyu"}


def test_dry_run_does_not_create_field_or_write(run):
    w = World(state_field=False)
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    assert run(w, dry=True) == 0
    assert w.created_fields == 0 and not w.mutation_queries


def test_sync_state_written_after_values(run):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    run(w)
    assert len(w.mutation_queries) == 2
    first, second = w.mutation_queries
    assert "F_status" in first and "F_wait" in first and "F_sync" not in first
    assert "F_sync" in second and "F_status" not in second
    assert state(w, "PR_1") == {"Status": "In review", "Waiting on": "Chenyu"}


def test_waiting_cleared_when_request_gone_and_value_is_own(run):
    w = World()
    w.add(pr(1), Status="In review", **{"Waiting on": "Chenyu", "Sync state": '{"Status":"In review","Waiting on":"Chenyu"}'})
    run(w)
    assert not w.vals("PR_1").get("Waiting on")
    assert "Waiting on" not in state(w, "PR_1")


def test_waiting_kept_when_person_set(run):
    w = World()
    w.add(pr(1), Status="In review", **{"Waiting on": "Chenyu"})
    run(w)
    assert w.vals("PR_1")["Waiting on"] == "Chenyu"


def test_waiting_kept_when_state_names_someone_else(run):
    w = World()
    w.add(pr(1), Status="In review", **{"Waiting on": "Yuchen", "Sync state": '{"Waiting on":"Chenyu"}'})
    run(w)
    assert w.vals("PR_1")["Waiting on"] == "Yuchen"


def test_person_waiting_not_overwritten_by_review_request(run):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="In review", **{"Waiting on": "Zeyu"})
    run(w)
    assert w.vals("PR_1")["Waiting on"] == "Zeyu"


def test_own_waiting_follows_new_reviewer(run):
    w = World()
    w.add(pr(1, reviewers=["ychenfang"]), Status="In review",
          **{"Waiting on": "Chenyu", "Sync state": '{"Waiting on":"Chenyu"}'})
    run(w)
    assert w.vals("PR_1")["Waiting on"] == "Yuchen"
    assert state(w, "PR_1")["Waiting on"] == "Yuchen"


def test_waiting_cleared_when_pr_closed_and_own(run):
    w = World()
    w.add(pr(1, state="MERGED", merged=True, reviewers=["LEE-CHENYU"]), Status="In review",
          **{"Waiting on": "Chenyu", "Sync state": '{"Status":"In review","Waiting on":"Chenyu"}'})
    run(w)
    assert w.vals("PR_1")["Status"] == "Done"
    assert not w.vals("PR_1").get("Waiting on")


def test_waiting_kept_when_pr_closed_and_person_set(run):
    w = World()
    w.add(pr(1, state="CLOSED"), Status="In review", **{"Waiting on": "Chenyu"})
    run(w)
    assert w.vals("PR_1")["Status"] == "Done"
    assert w.vals("PR_1")["Waiting on"] == "Chenyu"


def test_closed_item_goes_to_done_even_over_person_status(run):
    w = World()
    w.add(issue(1, state="CLOSED"), Status="Needs decision")
    w.add(pr(2, state="CLOSED"), Status="Blocked")
    run(w)
    assert w.vals("I_1")["Status"] == "Done" and w.vals("PR_2")["Status"] == "Done"


def test_reopened_issue_set_done_by_script_goes_back_to_backlog(run):
    w = World()
    w.add(issue(1), Status="Done", **{"Sync state": '{"Status":"Done"}'})
    run(w)
    assert w.vals("I_1")["Status"] == "Backlog"
    assert state(w, "I_1")["Status"] == "Backlog"


def test_reopened_pr_returns_to_in_review_or_in_progress(run):
    w = World()
    w.add(pr(1), Status="Done", **{"Sync state": '{"Status":"Done"}'})
    w.add(pr(2, draft=True), Status="Done", **{"Sync state": '{"Status":"Done"}'})
    run(w)
    assert w.vals("PR_1")["Status"] == "In review"
    assert w.vals("PR_2")["Status"] == "In progress"


def test_person_done_stays_done_when_open(run):
    w = World()
    w.add(issue(1), Status="Done")
    w.add(pr(2), Status="Done", **{"Sync state": '{"Status":"In review"}'})
    run(w)
    assert w.vals("I_1")["Status"] == "Done" and w.vals("PR_2")["Status"] == "Done"


def test_issue_in_review_reverts_to_backlog_when_no_open_closing_pr(run):
    w = World()
    w.add(issue(1), Status="In review", **{"Blocked by": "auto: fix in PR #9", "Sync state": '{"Status":"In review"}'})
    run(w)
    assert w.vals("I_1")["Status"] == "Backlog"
    assert not w.vals("I_1").get("Blocked by")


def test_issue_in_review_by_person_is_kept(run):
    w = World()
    w.add(issue(1), Status="In review")
    run(w)
    assert w.vals("I_1")["Status"] == "In review"


def test_issue_with_closing_pr_goes_in_review(run):
    w = World()
    w.add(issue(1, fixes=[7]), Status="Backlog")
    run(w)
    assert w.vals("I_1")["Status"] == "In review"
    assert w.vals("I_1")["Blocked by"] == "auto: fix in PR #7"
    assert state(w, "I_1") == {"Status": "In review"}


def test_human_status_survives(run):
    w = World()
    w.add(pr(1), Status="Needs decision")
    run(w)
    assert w.vals("PR_1")["Status"] == "Needs decision"


# ---- 4. CI ----------------------------------------------------------------------------------

def ci(*checks, more=False):
    return sync.ci_state(pr(1, checks=checks, more_checks=more))


def test_ci_latest_run_wins_old_failure_then_success():
    assert ci(check("t", "FAILURE", "2026-01-01T00:00:00Z"), check("t", "SUCCESS", "2026-01-02T00:00:00Z")) == ("ok", [])


def test_ci_latest_run_wins_success_then_failure():
    assert ci(check("t", "SUCCESS", "2026-01-01T00:00:00Z"), check("t", "FAILURE", "2026-01-02T00:00:00Z")) == ("red", ["t"])


@pytest.mark.parametrize("conclusion", ["FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "CANCELLED", "STALE"])
def test_ci_red_conclusions(conclusion):
    assert ci(check("t", conclusion)) == ("red", ["t"])


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_ci_ok_conclusions(conclusion):
    assert ci(check("t", conclusion)) == ("ok", [])


def test_ci_status_context_failure_and_error():
    assert ci({"context": "ci/x", "state": "FAILURE"}) == ("red", ["ci/x"])
    assert ci({"context": "ci/x", "state": "ERROR"}) == ("red", ["ci/x"])
    assert ci({"context": "ci/x", "state": "SUCCESS"}) == ("ok", [])


def test_ci_pending_without_red_is_unknown():
    assert ci(check("a"), pending("b"))[0] == "unknown"
    assert ci({"context": "ci/x", "state": "PENDING"})[0] == "unknown"


def test_ci_pending_rerun_of_failed_check_is_unknown():
    assert ci(check("t", "FAILURE", "2026-01-01T00:00:00Z"), pending("t", "2026-01-02T00:00:00Z"))[0] == "unknown"


def test_ci_pending_with_red_is_red():
    assert ci(check("a", "FAILURE"), pending("b")) == ("red", ["a"])


def test_ci_kernel_review_excluded():
    assert ci(check("kernel-review", "FAILURE")) == ("ok", [])
    assert ci(pending("kernel-review")) == ("ok", [])


def test_ci_truncated_is_unknown():
    assert ci(check("a", "FAILURE"), more=True)[0] == "unknown"


def test_pending_ci_keeps_existing_blocker(run):
    w = World()
    w.add(pr(1, checks=[check("a", "FAILURE"), pending("b")][1:]), Status="In review", **{"Blocked by": "auto: CI red: a"})
    run(w)
    assert w.vals("PR_1")["Blocked by"] == "auto: CI red: a"


def test_cancelled_check_does_not_clear_blocker(run):
    w = World()
    w.add(pr(1, checks=[check("a", "CANCELLED")]), Status="In review", **{"Blocked by": "auto: CI red: a"})
    run(w)
    assert w.vals("PR_1")["Blocked by"] == "auto: CI red: a"


def test_green_ci_clears_auto_blocker_but_not_human_text(run):
    w = World()
    w.add(pr(1, checks=[check("a")]), Status="In review", **{"Blocked by": "auto: CI red: a"})
    w.add(pr(2, checks=[check("a")]), Status="In review", **{"Blocked by": "waiting for Chenyu's ruling"})
    run(w)
    assert not w.vals("PR_1").get("Blocked by")
    assert w.vals("PR_2")["Blocked by"] == "waiting for Chenyu's ruling"


# ---- 5. new items ---------------------------------------------------------------------------

def test_new_items_get_rule_status_and_digest_in_same_run_and_second_run_is_noop(run, capsys):
    w = World()
    w.add(issue(1), Status="Backlog")
    w.unboarded(pr(2, reviewers=["LEE-CHENYU"]))
    w.unboarded(issue(3))
    assert run(w) == 0
    out = capsys.readouterr().out
    assert w.vals("PR_2")["Status"] == "In review"
    assert w.vals("PR_2")["Waiting on"] == "Chenyu"
    assert w.vals("I_3")["Status"] == "Triage"
    assert "To triage: #3" in out
    assert "Reviews waiting on Chenyu: PR#2" in out
    before = len(w.mutation_queries)
    assert run(w) == 0
    assert len(w.mutation_queries) == before            # zero edits on the second run
    assert "0 added, 0 field updates" in capsys.readouterr().out


def test_dry_run_adds_nothing(run):
    w = World()
    w.unboarded(issue(3))
    assert run(w, dry=True) == 0
    assert not w.items and not w.mutation_queries


# ---- 6. null author -------------------------------------------------------------------------

def test_null_author_with_changes_requested_does_not_crash(run):
    w = World()
    w.add(pr(1, author=None, decision="CHANGES_REQUESTED"), Status="In review")
    assert run(w) == 0
    assert not w.vals("PR_1").get("Waiting on")


def test_null_author_changes_requested_clears_own_stale_waiting(run):
    w = World()
    w.add(pr(1, author=None, decision="CHANGES_REQUESTED"), Status="In review",
          **{"Waiting on": "Zeyu", "Sync state": '{"Waiting on":"Zeyu"}'})
    assert run(w) == 0
    assert not w.vals("PR_1").get("Waiting on")


def test_null_author_keeps_person_set_waiting(run):
    w = World()
    w.add(pr(1, author=None, decision="CHANGES_REQUESTED"), Status="In review", **{"Waiting on": "Zeyu"})
    assert run(w) == 0
    assert w.vals("PR_1")["Waiting on"] == "Zeyu"


def test_null_author_still_fills_waiting_from_review_request(run):
    w = World()
    w.add(pr(1, author=None, reviewers=["LEE-CHENYU"]), Status="In review")
    assert run(w) == 0
    assert w.vals("PR_1")["Waiting on"] == "Chenyu"


def test_null_author_clears_own_waiting_when_request_gone(run):
    w = World()
    w.add(pr(1, author=None), Status="In review",
          **{"Waiting on": "Chenyu", "Sync state": '{"Waiting on":"Chenyu"}'})
    assert run(w) == 0
    assert not w.vals("PR_1").get("Waiting on")


def test_changes_requested_waits_on_author(run):
    w = World()
    w.add(pr(1, decision="CHANGES_REQUESTED"), Status="In review")
    run(w)
    assert w.vals("PR_1")["Waiting on"] == "Zeyu"


@pytest.mark.parametrize("name", ["ITEMS", "OPEN_ISSUES", "OPEN_PRS"])
def test_graphql_documents_have_balanced_braces(name):
    q = getattr(sync, name)
    assert q.count("{") == q.count("}") and q.count("(") == q.count(")")


# ---- 7. round two: state provenance, batch errors, CI ordering ------------------------------

def test_failed_value_write_leaves_sync_state_unchanged(run):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    w.fail = lambda item_id, field: field == "Waiting on"
    assert run(w) == 1
    assert w.vals("PR_1")["Status"] == "In review"
    assert not w.vals("PR_1").get("Waiting on")
    assert state(w, "PR_1") == {"Status": "In review"}          # only the confirmed field


def test_all_value_writes_failed_writes_no_state(run):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    w.fail = lambda item_id, field: field != "Sync state"
    assert run(w) == 1
    assert not w.vals("PR_1").get("Sync state")


def test_state_write_failure_is_reported(run, capsys):
    w = World()
    w.add(pr(1), Status="Triage")
    w.fail = lambda item_id, field: field == "Sync state"
    assert run(w) == 1
    assert "Sync state" in capsys.readouterr().out


def test_successful_write_then_next_run_treats_value_as_own(run):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    assert run(w) == 0
    w.contents["PR_1"]["reviewRequests"] = {"nodes": [{"requestedReviewer": {"login": "ychenfang"}}]}
    assert run(w) == 0
    assert w.vals("PR_1")["Waiting on"] == "Yuchen"


@pytest.fixture
def raw_gh(monkeypatch, run):
    """Mutations go through the real sync.gql with a faked gh process; reads use the fake board."""
    def install(w, errors=None, code=0):
        real = sync.gql

        def gql(query, allow_partial=False, **kw):
            if query.startswith("mutation{ e"):
                out = w.gql(query, allow_partial=allow_partial, **kw)
                body = {"data": out}
                if errors:
                    body["errors"] = errors
                fake_run(monkeypatch, json.dumps(body), code=code)
                return real(query, allow_partial=allow_partial, **kw)
            return w.gql(query, allow_partial=allow_partial, **kw)
        monkeypatch.setattr(sync, "gql", gql)
        monkeypatch.setattr(sync, "DRY", False)
        return sync.main()
    return install


def test_subfield_graphql_error_on_nonnull_alias_fails_the_alias(raw_gh, capsys):
    w = World()
    w.add(pr(1, reviewers=["LEE-CHENYU"]), Status="Triage")
    code = raw_gh(w, errors=[{"message": "denied", "path": ["e1", "clientMutationId"]}], code=1)
    assert code == 1
    assert "FAIL" in capsys.readouterr().out
    assert not state(w, "PR_1").get("Waiting on")


def test_nonzero_exit_without_errors_fails_all_aliases(raw_gh, capsys):
    w = World()
    w.add(pr(1), Status="Triage")
    assert raw_gh(w, errors=None, code=1) == 1
    assert "FAIL" in capsys.readouterr().out


def test_pathless_graphql_error_fails_whole_batch(raw_gh):
    w = World()
    w.add(pr(1), Status="Triage")
    assert raw_gh(w, errors=[{"message": "rate limited"}], code=0) == 1


def test_ci_old_success_plus_newer_queued_with_null_times_is_unknown():
    queued = {"name": "test", "conclusion": None, "status": "QUEUED", "startedAt": None, "completedAt": None}
    verdict, _ = sync.ci_state(pr(1, checks=[check("test"), queued]))
    assert verdict == "unknown"


def test_ci_competing_runs_with_null_times_is_unknown():
    a = check("test", "SUCCESS", completed=None, started=None)
    b = check("test", "FAILURE", completed=None, started=None)
    verdict, _ = sync.ci_state(pr(1, checks=[a, b]))
    assert verdict == "unknown"


def test_ci_queued_run_keeps_existing_blocker(run):
    w = World()
    queued = {"name": "test", "conclusion": None, "status": "QUEUED", "startedAt": None, "completedAt": None}
    w.add(pr(1, checks=[check("test"), queued]), Status="In review", **{"Blocked by": "auto: CI red: test"})
    assert run(w) == 0
    assert w.vals("PR_1")["Blocked by"] == "auto: CI red: test"


# ---- third review round ---------------------------------------------------------------------

def _two_aliases(monkeypatch, errors, code):
    body = {"data": {"e0": {"clientMutationId": None}, "e1": {"clientMutationId": None}}}
    if errors:
        body["errors"] = errors
    fake_run(monkeypatch, json.dumps(body), code=code)
    failures = []
    ok = sync.run_batches([("A", ["m0"]), ("B", ["m1"])], failures)
    return ok, failures


@pytest.mark.parametrize("code", [0, 1])
def test_alias_error_fails_only_that_alias(monkeypatch, code):
    ok, failures = _two_aliases(monkeypatch, [{"message": "denied", "path": ["e1", "clientMutationId"]}], code)
    assert ok == [[True], [False]]
    assert len(failures) == 1 and failures[0].startswith("B:")


def test_error_path_outside_the_batch_fails_the_whole_batch(monkeypatch):
    ok, failures = _two_aliases(monkeypatch, [{"message": "odd", "path": ["e999", "clientMutationId"]}], 0)
    assert ok == [[False], [False]]
    assert len(failures) == 2


def test_ci_competing_runs_with_one_missing_timestamp_is_unknown():
    a = check("test", "SUCCESS")
    b = check("test", "FAILURE", completed=None, started=None)
    verdict, _ = sync.ci_state(pr(1, checks=[a, b]))
    assert verdict == "unknown"
