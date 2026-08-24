"""Declarative workflow state, invariant and repair kernel.

The module is deliberately pure with respect to persistence: it reads no files,
opens no database and performs no writes.  Adapters pass canonical rows in and
materialize the returned :class:`TransitionPlan` in one transaction.
"""

from __future__ import absolute_import

import hashlib
import json
import re


CHECK_PROTOCOL = "AWB-WORKFLOW-CHECK-v1"
KERNEL_PROTOCOL = "AWB-TRANSITION-KERNEL-v1"
REPAIR_PROTOCOL = "AWB-WORKFLOW-REPAIR-v1"
MUTATION_RECEIPT_PROTOCOL = "AWB-MUTATION-RECEIPT-v1"

RESET_ORPHAN_REVIEWER_TASK = "RESET_ORPHAN_REVIEWER_TASK_AFTER_PLAN_REVISION"
EXPIRE_AND_RECONCILE_ACTIVITY = "EXPIRE_AND_RECONCILE_ACTIVITY"

TERMINAL_STATE = "FINAL_ACCEPTANCE_APPROVED"
TERMINAL_QUEUE = "HELD"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def content_fingerprint(value):
    return sha256(_json(value))


class FrozenDict(dict):
    """JSON-compatible recursively immutable mapping."""

    def _immutable(self, *unused_args, **unused_kwargs):
        raise TypeError("frozen mapping is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable


def _freeze(value):
    if isinstance(value, dict):
        frozen = dict.__new__(FrozenDict)
        dict.__init__(frozen, ((key, _freeze(item)) for key, item in value.items()))
        return frozen
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class TransitionPlan(object):
    """Recursively immutable complete transition description."""

    __slots__ = (
        "operation", "from_fingerprint", "intent_fingerprint",
        "preconditions", "writes", "post_state", "post_invariants",
        "audit_event", "receipt", "next_step", "_sealed",
    )

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("TransitionPlan is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, operation, from_fingerprint, intent, preconditions,
                 writes, post_state, post_invariants, audit_event, receipt,
                 next_step):
        self.operation = operation
        self.from_fingerprint = from_fingerprint
        self.intent_fingerprint = content_fingerprint(intent)
        self.preconditions = tuple(preconditions)
        self.writes = tuple(writes)
        self.post_state = _freeze(post_state)
        self.post_invariants = tuple(post_invariants)
        self.audit_event = _freeze(audit_event)
        self.receipt = _freeze(receipt)
        self.next_step = _freeze(next_step)
        self._sealed = True

    def as_dict(self):
        return {
            "protocolVersion": KERNEL_PROTOCOL,
            "operation": self.operation,
            "fromFingerprint": self.from_fingerprint,
            "intentFingerprint": self.intent_fingerprint,
            "preconditions": list(self.preconditions),
            "writes": [write.as_dict() if isinstance(write, WriteOp) else write
                       for write in self.writes],
            "postState": _thaw(self.post_state),
            "postInvariants": list(self.post_invariants),
            "auditEvent": _thaw(self.audit_event),
            "receipt": _thaw(self.receipt),
            "nextStep": _thaw(self.next_step),
        }


class WriteOp(object):
    """One immutable, compare-and-set persistence operation.

    Public adapters describe intent only.  The kernel emits these semantic
    operations and :func:`apply_transition_plan` is the sole compiler from a
    lifecycle write to SQL.
    """

    __slots__ = ("action", "table", "values", "where", "expected_rows", "_sealed")

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("WriteOp is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, action, table, values, where=(), expected_rows=1):
        if action not in ("INSERT", "UPDATE", "DELETE"):
            raise ValueError("write action is invalid")
        if table not in MANAGED_TABLES:
            raise ValueError("write table is not managed by the kernel")
        if not values and action != "DELETE":
            raise ValueError("write values cannot be empty")
        if action != "INSERT" and not where:
            raise ValueError("unbounded lifecycle write is forbidden")
        if expected_rows is not None and (type(expected_rows) is not int or
                                           expected_rows < 0):
            raise ValueError("expected_rows is invalid")
        self.action = action
        self.table = table
        self.values = tuple((key, _freeze(value)) for key, value in values)
        self.where = tuple((key, _freeze(value)) for key, value in where)
        self.expected_rows = expected_rows
        self._sealed = True

    def as_dict(self):
        return {
            "action": self.action, "table": self.table,
            "values": [[key, _thaw(value)] for key, value in self.values],
            "where": [[key, _thaw(value)] for key, value in self.where],
            "expectedRows": self.expected_rows,
        }


MANAGED_TABLES = frozenset((
    "work_items", "tasks", "claims", "repository_locks",
    "orchestrator_leases", "reviews", "human_gates",
    "human_gate_reviews", "events",
))
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _columns(items):
    columns = [item[0] for item in items]
    if (len(columns) != len(set(columns)) or
            any(not _IDENTIFIER.match(column) for column in columns)):
        raise ValueError("write columns are invalid")
    return columns


def apply_transition_plan(connection, snapshot, plan, snapshot_loader):
    """Materialize a complete plan and enforce its pre/post invariants.

    The caller must already hold ``BEGIN IMMEDIATE``.  No commit occurs here;
    the public adapter owns only transaction framing and receipt formatting.
    """
    if not isinstance(plan, TransitionPlan):
        raise ValueError("transition plan is required")
    if snapshot.get("projectionFingerprint") != plan.from_fingerprint:
        raise RuntimeError("TRANSITION_FROM_FINGERPRINT_CONFLICT")
    if plan.operation == RESET_ORPHAN_REVIEWER_TASK:
        violations = invariant_violations(snapshot)
        if (not violations or any(value["code"] != "ORPHAN_REVIEWER_TASK"
                                  for value in violations)):
            raise RuntimeError("PRE_INVARIANT_VIOLATION:REPAIR_PROOF_DRIFT")
    elif plan.operation != "CREATE_WORK_ITEM":
        assert_invariants(
            snapshot, phase="pre",
            allow_time_split=plan.operation == EXPIRE_AND_RECONCILE_ACTIVITY,
        )
    for write in plan.writes:
        if not isinstance(write, WriteOp):
            raise ValueError("transition plan contains a non-WriteOp")
        values = list(write.values)
        where = list(write.where)
        value_columns = _columns(values)
        where_columns = _columns(where)
        if write.action == "INSERT":
            placeholders = ",".join("?" for unused in values)
            statement = "INSERT INTO {0}({1}) VALUES({2})".format(
                write.table, ",".join(value_columns), placeholders)
            parameters = [value for unused, value in values]
        elif write.action == "UPDATE":
            statement = "UPDATE {0} SET {1} WHERE {2}".format(
                write.table,
                ",".join("{0}=?".format(column) for column in value_columns),
                " AND ".join("{0}=?".format(column) for column in where_columns),
            )
            parameters = ([value for unused, value in values] +
                          [value for unused, value in where])
        else:
            statement = "DELETE FROM {0} WHERE {1}".format(
                write.table,
                " AND ".join("{0}=?".format(column) for column in where_columns),
            )
            parameters = [value for unused, value in where]
        changed = connection.execute(statement, parameters).rowcount
        if write.expected_rows is not None and changed != write.expected_rows:
            raise RuntimeError("TRANSITION_COMPARE_AND_SET_CONFLICT")
    post = snapshot_loader()
    assert_invariants(post, phase="post")
    expected = plan.post_state or {}
    work_item = post.get("workItem", {})
    for key, value in expected.items():
        if key in work_item and work_item[key] != value:
            raise RuntimeError("TRANSITION_POST_STATE_MISMATCH:{0}".format(key))
    return post


# This is the closed public lifecycle edge universe.  Adapters may reject an
# edge for a particular state, but must never invent an unregistered edge.
TRANSITION_MATRIX = (
    "CREATE_WORK_ITEM", "BACKFILL_MANAGEMENT", "AMEND_MANAGEMENT",
    "CLAIM_ROLE", "RELEASE_ROLE", "ACQUIRE_WRITER", "RELEASE_WRITER",
    "SUBMIT_PLAN", "BEGIN_IMPLEMENTATION", "SUBMIT_IMPLEMENTATION",
    "PLAN_REVIEW", "FINAL_REVIEW", "PLAN_REVIEW_AMEND", "HUMAN_GATE",
    "HOLD", "RESUME", "BLOCK", "UNBLOCK", "PLAN_DEVIATION",
    "WORKFLOW_ADVANCE",
    "CANDIDATE_PREPARE", "CANDIDATE_FREEZE", "CANDIDATE_BUILD",
    "CANDIDATE_QUARANTINE", "CANDIDATE_FINALIZE",
    "ORCHESTRATOR_REGISTER", "ORCHESTRATOR_CLAIM", "ORCHESTRATOR_RENEW",
    "ORCHESTRATOR_RELEASE", "ORCHESTRATOR_RECOVER",
    EXPIRE_AND_RECONCILE_ACTIVITY, "PUBLICATION_AUTHORIZE",
    "PUBLICATION_POSTFLIGHT", "PUBLICATION_RETRY",
    RESET_ORPHAN_REVIEWER_TASK, "TERMINAL_ACTIVITY_CLEANUP",
)

# Closed registration used by adapters and the static acceptance guard.  A new
# public lifecycle mutation cannot silently invent an edge: it must first name
# one of the matrix intents here and gain table/model coverage.
PUBLIC_MUTATION_INTENTS = {
    "lite.create_work_item": "CREATE_WORK_ITEM",
    "lite.backfill_management": "BACKFILL_MANAGEMENT",
    "lite.amend_management": "AMEND_MANAGEMENT",
    "lite.acquire_claim": "CLAIM_ROLE",
    "lite.release_claim": "RELEASE_ROLE",
    "lite.acquire_repository_lock": "ACQUIRE_WRITER",
    "lite.release_repository_lock": "RELEASE_WRITER",
    "lite.set_task_status": "BLOCK",
    "lite.transition.submit_plan": "SUBMIT_PLAN",
    "lite.transition.start_implementation": "BEGIN_IMPLEMENTATION",
    "lite.transition.submit_implementation": "SUBMIT_IMPLEMENTATION",
    "lite.record_agent_review.PLAN": "PLAN_REVIEW",
    "lite.record_agent_review.FINAL": "FINAL_REVIEW",
    "lite.amend_plan_review": "PLAN_REVIEW_AMEND",
    "lite.record_human_gate": "HUMAN_GATE",
    "lite.set_hold.hold": "HOLD",
    "lite.set_hold.resume": "RESUME",
    "lite.unblock_task": "UNBLOCK",
    "lite.report_plan_deviation": "PLAN_DEVIATION",
    "lite.workflow_advance": "WORKFLOW_ADVANCE",
    "lite.workflow_repair": RESET_ORPHAN_REVIEWER_TASK,
    "orchestrator.register": "ORCHESTRATOR_REGISTER",
    "orchestrator.claim": "ORCHESTRATOR_CLAIM",
    "orchestrator.claim_next": "ORCHESTRATOR_CLAIM",
    "orchestrator.renew": "ORCHESTRATOR_RENEW",
    "orchestrator.release": "ORCHESTRATOR_RELEASE",
    "orchestrator.recover": "ORCHESTRATOR_RECOVER",
    "orchestrator.reconcile_expired": EXPIRE_AND_RECONCILE_ACTIVITY,
    "candidate.publication_authorize": "PUBLICATION_AUTHORIZE",
    "candidate.publication_postflight": "PUBLICATION_POSTFLIGHT",
    "candidate.publication_retry": "PUBLICATION_RETRY",
    "candidate.prepare": "CANDIDATE_PREPARE",
    "candidate.freeze": "CANDIDATE_FREEZE",
    "candidate.build": "CANDIDATE_BUILD",
    "candidate.quarantine": "CANDIDATE_QUARANTINE",
    "candidate.finalize": "CANDIDATE_FINALIZE",
}


def effective_status(row, evaluation_time):
    if row.get("status") != "ACTIVE":
        return "INACTIVE"
    return "LIVE" if row.get("expires_at") > evaluation_time else "STALE"


def canonical_projection(item, tasks, claims, writers, leases, reviews,
                         event_head, history_digest, artifact_head=None,
                         artifact_sha=None, evaluation_time=None,
                         review_events=None, gate_events=None):
    """Return a stable, content-minimized snapshot used by every adapter."""
    evaluation_time = evaluation_time or ""

    def activity(row, identity, owner):
        result = {
            "id": row[identity], "status": row["status"],
            "owner": row[owner], "generation": row["generation"],
            "expiresAt": row["expires_at"], "releasedAt": row["released_at"],
            "effectiveStatus": effective_status(row, evaluation_time),
        }
        if "task_id" in row:
            result["taskId"] = row["task_id"]
            result["role"] = row["role"]
        if "repository_key" in row:
            result["repositoryKey"] = row["repository_key"]
        return result

    projection = {
        "workItem": {
            key: item.get(key) for key in (
                "work_item_id", "mode", "state", "queue_state",
                "current_role", "held_reason", "blocked_reason",
                "row_version", "closed_at",
            )
        },
        "tasks": [{key: row.get(key) for key in (
            "task_id", "seq", "owner_role", "status", "required",
        )} for row in sorted(tasks, key=lambda value: value["seq"])],
        "claims": [activity(row, "claim_id", "agent_id") for row in sorted(
            claims, key=lambda value: (value["generation"], value["claim_id"]))],
        "writers": [activity(row, "lock_id", "agent_id") for row in sorted(
            writers, key=lambda value: (value["generation"], value["lock_id"]))],
        "leases": [activity(row, "lease_id", "orchestrator_id") for row in sorted(
            leases, key=lambda value: (value["generation"], value["lease_id"]))],
        "reviews": [dict({key: row.get(key) for key in (
            "review_id", "stage", "reviewer_agent_id", "decision", "created_at",
        )}, review=row.get("review")) for row in reviews],
        "reviewEvents": list(review_events or ()),
        "gateEvents": list(gate_events or ()),
        "eventHead": event_head,
        "historyDigest": history_digest,
        "artifactHead": artifact_head,
        "artifactSha256": artifact_sha,
        "evaluationTime": evaluation_time,
    }
    fingerprint_input = dict(projection)
    # The transaction clock determines LIVE/STALE but is not itself mutable
    # projection state.  Binding the raw clock would make every exact apply
    # stale merely because it starts a second later.
    fingerprint_input.pop("evaluationTime", None)
    projection["projectionFingerprint"] = content_fingerprint(fingerprint_input)
    return projection


def _violation(code, location, repairability="AMBIGUOUS"):
    return {"code": code, "location": location,
            "repairability": repairability}


def invariant_violations(snapshot, allow_time_split=False):
    """Evaluate global lifecycle invariants without exposing stored content."""
    item = snapshot["workItem"]
    tasks = snapshot["tasks"]
    task_by_id = {row["task_id"]: row for row in tasks}
    live_claims = [row for row in snapshot["claims"]
                   if row["effectiveStatus"] == "LIVE"]
    stale_claims = [row for row in snapshot["claims"]
                    if row["effectiveStatus"] == "STALE"]
    live_writers = [row for row in snapshot["writers"]
                    if row["effectiveStatus"] == "LIVE"]
    live_leases = [row for row in snapshot["leases"]
                   if row["effectiveStatus"] == "LIVE"]
    stale_activity = [row for collection in (
        snapshot["claims"], snapshot["writers"], snapshot["leases"]
    ) for row in collection if row["effectiveStatus"] == "STALE"]
    violations = []

    review_events = snapshot.get("reviewEvents", [])
    request_ids = [row.get("requestId") for row in review_events]
    if any(not value for value in request_ids) or len(request_ids) != len(set(request_ids)):
        violations.append(_violation("REVIEW_REQUEST_BINDING_MISMATCH", "reviews"))
    for stage, event_type, external_stage in (
            ("PLAN", "AGENT_PLAN_REVIEW", "PLAN"),
            ("FINAL", "AGENT_FINAL_REVIEW", "IMPLEMENTATION")):
        stage_rows = [row for row in snapshot.get("reviews", [])
                      if row.get("stage") == stage]
        stage_events = [row for row in review_events
                        if row.get("eventType") == event_type]
        if len(stage_rows) != len(stage_events):
            violations.append(_violation("REVIEW_ROW_EVENT_COUNT_MISMATCH",
                                         "reviews:" + stage))
            continue
        open_findings = set()
        resolved_findings = set()
        for index, pair in enumerate(zip(stage_rows, stage_events), 1):
            row, event = pair
            review = row.get("review")
            event_review = event.get("review")
            if (not isinstance(review, dict) or review != event_review or
                    event.get("actorKind") != "AGENT" or
                    event.get("actorId") != row.get("reviewer_agent_id") or
                    event.get("createdAt") != row.get("created_at") or
                    event.get("decision") != row.get("decision")):
                violations.append(_violation("REVIEW_ROW_EVENT_MISMATCH",
                                             "review:" + str(row.get("review_id"))))
                continue
            result = review.get("result")
            expected_decision = "APPROVED" if result == "PASS" else "REJECTED"
            expected_mode = "CONVERGENCE" if index == 4 else "ORDINARY"
            native_event = event.get("recordFormat") == "AWB-REVIEW-EVENT-v7"
            known_legacy_event = event.get("recordFormat") in (
                "AWB-REVIEW-EVENT-v6",
                "AWB-REVIEW-EVENT-v3-v6",
                "AWB-REVIEW-SUMMARY-EVENT-v3-v6",
            )
            if (not native_event and not known_legacy_event) or (
                    review.get("stage") != external_stage or
                    review.get("round") != index or
                    review.get("reviewerMode") != expected_mode or
                    row.get("decision") != expected_decision or
                    (native_event and not event.get("requestFingerprint"))):
                violations.append(_violation("REVIEW_STAGE_ROUND_RESULT_MISMATCH",
                                             "review:" + str(row.get("review_id"))))
            resolved = review.get("resolvedFindingIds")
            findings = review.get("findings")
            if (not isinstance(resolved, list) or not isinstance(findings, list) or
                    len(resolved) != len(set(resolved)) or
                    any(value not in open_findings for value in resolved)):
                violations.append(_violation("REVIEW_FINDING_LIFECYCLE_MISMATCH",
                                             "review:" + str(row.get("review_id"))))
                continue
            for finding_id in resolved:
                open_findings.discard(finding_id)
                resolved_findings.add(finding_id)
            new_ids = []
            for finding in findings:
                if not isinstance(finding, dict) or not finding.get("id"):
                    new_ids.append(None)
                else:
                    new_ids.append(finding["id"])
            if (None in new_ids or len(new_ids) != len(set(new_ids)) or
                    any(value in resolved_findings for value in new_ids)):
                violations.append(_violation("REVIEW_FINDING_LIFECYCLE_MISMATCH",
                                             "review:" + str(row.get("review_id"))))
            open_findings.update(value for value in new_ids if value is not None)
            if result == "PASS" and open_findings:
                violations.append(_violation("REVIEW_PASS_WITH_OPEN_FINDINGS",
                                             "review:" + str(row.get("review_id"))))
            if review.get("protocolVersion") == "AWB-REVIEW-v2":
                artifact = event.get("artifactAtReview")
                ordered_artifact = ({key: artifact.get(key) for key in (
                    "path", "revision", "sha256", "editorAgentId")}
                    if isinstance(artifact, dict) else None)
                if review.get("reviewedArtifact") != ordered_artifact:
                    violations.append(_violation("REVIEW_ARTIFACT_ORDER_MISMATCH",
                                                 "review:" + str(row.get("review_id"))))
            if (stage == "FINAL" and review.get("reviewedCandidate") is not None and
                    review.get("reviewedCandidate") != event.get("candidateAtReview")):
                violations.append(_violation("REVIEW_CANDIDATE_ORDER_MISMATCH",
                                             "review:" + str(row.get("review_id"))))

    for gate in snapshot.get("gateEvents", []):
        gate_stage = (gate.get("stage") if gate.get("eventType") == "AUTO_GATE_APPROVED"
                      else "PLAN" if gate.get("eventType") == "HUMAN_PLAN_GATE"
                      else "FINAL")
        expected_type = "AGENT_{0}_REVIEW".format(gate_stage)
        preceding = [row for row in review_events
                     if row.get("eventType") == expected_type and
                     row.get("eventId", 0) < gate.get("eventId", 0)]
        latest = preceding[-1] if preceding else None
        approved = (gate.get("eventType") == "AUTO_GATE_APPROVED" or
                    gate.get("decision") == "APPROVED")
        if approved and (latest is None or
                         not isinstance(latest.get("review"), dict) or
                         latest["review"].get("result") != "PASS"):
            violations.append(_violation("REVIEW_GATE_ORDER_MISMATCH",
                                         "event:" + str(gate.get("eventId"))))
        if gate.get("eventType") == "AUTO_GATE_APPROVED" and latest is not None:
            bindings = (("reviewEventId", "eventId"),
                        ("reviewRequestId", "requestId"),
                        ("reviewRound", None))
            for gate_key, review_key in bindings:
                expected = (latest.get(review_key) if review_key is not None else
                            (latest.get("review") or {}).get("round"))
                if gate.get(gate_key) != expected:
                    violations.append(_violation("REVIEW_GATE_BINDING_MISMATCH",
                                                 "event:" + str(gate.get("eventId"))))
                    break

    if len(live_claims) > 1:
        violations.append(_violation("MULTIPLE_LIVE_AGENT_CLAIMS", "claims"))
    in_progress = [row for row in tasks if row["status"] == "IN_PROGRESS"]
    for task in in_progress:
        matches = [row for row in live_claims
                   if row.get("taskId") == task["task_id"] and
                   row.get("role") == task["owner_role"]]
        if len(matches) != 1:
            stale_matches = [row for row in stale_claims
                             if row.get("taskId") == task["task_id"] and
                             row.get("role") == task["owner_role"]]
            if not (allow_time_split and len(stale_matches) == 1):
                code = ("ORPHAN_REVIEWER_TASK" if task["owner_role"] == "REVIEWER"
                        else "IN_PROGRESS_WITHOUT_LIVE_CLAIM")
                repairability = ("DETERMINISTIC" if code == "ORPHAN_REVIEWER_TASK"
                                 else "AMBIGUOUS")
                violations.append(_violation(code, "task:" + task["task_id"],
                                             repairability))
    for claim in live_claims:
        task = task_by_id.get(claim.get("taskId"))
        if (task is None or task["status"] != "IN_PROGRESS" or
                task["owner_role"] != claim.get("role")):
            violations.append(_violation("LIVE_CLAIM_WITHOUT_IN_PROGRESS_TASK",
                                         "claim:" + claim["id"]))

    queue = item["queue_state"]
    role = item["current_role"]
    if queue == "CLAIMED":
        if len(live_claims) != 1 or role != live_claims[0].get("role"):
            if not (allow_time_split and len(live_claims) == 0 and
                    len(stale_claims) == 1):
                violations.append(_violation("CLAIMED_ROUTE_MISMATCH", "workItem"))
    elif queue == "CLAIMABLE":
        if live_claims:
            violations.append(_violation("CLAIMABLE_HAS_LIVE_CLAIM", "workItem"))
        if role is None:
            violations.append(_violation("CLAIMABLE_WITHOUT_ROLE", "workItem"))
    elif queue in ("WAITING_HUMAN", "HELD", "BLOCKED"):
        if live_claims or live_writers:
            violations.append(_violation("NONCLAIMABLE_HAS_LIVE_AGENT_ACTIVITY",
                                         "workItem"))
        if role is not None:
            violations.append(_violation("NONCLAIMABLE_HAS_CURRENT_ROLE", "workItem"))

    for writer in live_writers:
        if not any(claim["owner"] == writer["owner"] for claim in live_claims):
            violations.append(_violation("LIVE_WRITER_WITHOUT_OWNER_CLAIM",
                                         "writer:" + writer["id"]))

    if item["state"] == TERMINAL_STATE:
        if (queue != TERMINAL_QUEUE or item.get("closed_at") is None or role is not None):
            violations.append(_violation("TERMINAL_ROUTE_MISMATCH", "workItem"))
        if live_claims or live_writers or live_leases or in_progress:
            violations.append(_violation("TERMINAL_HAS_LIVE_ACTIVITY", "workItem"))

    if stale_activity and not allow_time_split:
        violations.append(_violation("STALE_ACTIVITY_REQUIRES_RECONCILIATION",
                                     "activity", "DETERMINISTIC"))
    return violations


def assert_invariants(snapshot, phase="post", allow_time_split=False):
    violations = invariant_violations(snapshot, allow_time_split=allow_time_split)
    if violations:
        error = RuntimeError("{0}_INVARIANT_VIOLATION:{1}".format(
            phase.upper(), violations[0]["code"]))
        error.violations = violations
        raise error
    return True


def review_route(stage, result, round_number, mode, policy, release_review=False):
    """Declare the complete business and task route for a review result."""
    if stage not in ("PLAN", "FINAL") or mode not in (
            "STANDARD", "READ_ONLY_DIAGNOSIS"):
        raise ValueError("unknown review edge")
    if result not in ("PASS", "REVISE", "REVISE_TO_PLANNER",
                      "CONVERGENCE_REVISE", "BLOCKED",
                      "WAITING_HUMAN", "AMENDED"):
        raise ValueError("unknown review result")
    if round_number not in (1, 2, 3, 4, 5):
        raise ValueError("unknown review round")
    allowed = (
        {"PASS", "REVISE", "REVISE_TO_PLANNER", "BLOCKED"}
        if round_number <= 3 else
        {"PASS", "CONVERGENCE_REVISE", "WAITING_HUMAN", "BLOCKED", "AMENDED"}
        if round_number == 4 else
        {"PASS", "REVISE", "BLOCKED", "WAITING_HUMAN"}
    )
    if result not in allowed:
        raise ValueError("review result is not declared for this round")
    author_role = "PLANNER" if stage == "PLAN" else "IMPLEMENTER"
    if result == "PASS":
        if mode == "READ_ONLY_DIAGNOSIS":
            return {"state": "PLAN_REVIEW_APPROVED", "queue": "HELD",
                    "role": None, "authorTask": "KEEP",
                    "reviewerTask": "COMPLETED", "autoEligible": False}
        return {"state": ("PLAN_REVIEW_PENDING" if stage == "PLAN"
                           else "IMPLEMENTATION_COMPLETED"),
                "queue": "WAITING_HUMAN", "role": None,
                "authorTask": "KEEP",
                "reviewerTask": ("COMPLETED" if stage == "FINAL" else "NOT_STARTED"),
                "autoEligible": policy == "AUTO_ON_PASS" and not release_review,
                "publicationReady": bool(stage == "FINAL" and release_review)}
    if result == "BLOCKED":
        return {"state": None, "queue": "BLOCKED", "role": None,
                "authorTask": "KEEP", "reviewerTask": "BLOCKED",
                "autoEligible": False}
    if result == "WAITING_HUMAN" or round_number == 5:
        return {"state": None, "queue": "WAITING_HUMAN", "role": None,
                "authorTask": "KEEP", "reviewerTask": "WAITING_ACCEPTANCE",
                "autoEligible": False}
    if round_number == 3 or result == "AMENDED":
        return {"state": None, "queue": "CLAIMABLE", "role": "REVIEWER",
                "authorTask": "KEEP", "reviewerTask": "NOT_STARTED",
                "autoEligible": False}
    return {"state": "DRAFT" if stage == "PLAN" else "IMPLEMENTING",
            "queue": "CLAIMABLE", "role": author_role,
            "authorTask": "NOT_STARTED", "reviewerTask": "NOT_STARTED",
            "autoEligible": False}


def build_transition_plan(operation, snapshot, intent, writes, post_state,
                          audit_event, receipt, next_step,
                          preconditions=None, post_invariants=None):
    if operation not in TRANSITION_MATRIX:
        raise ValueError("unregistered transition edge")
    return TransitionPlan(
        operation, snapshot["projectionFingerprint"], intent,
        preconditions or ("EXACT_FROM_FINGERPRINT", "EXACT_FENCE",
                          "EXACT_REQUEST_ID"),
        writes, post_state,
        post_invariants or ("TASK_CLAIM_BIDIRECTIONAL", "ROUTE_CONSISTENT",
                            "REVIEW_LIFECYCLE_CONSISTENT",
                            "TERMINAL_ACTIVITY_CLOSED", "MONOTONIC_HEADS"),
        audit_event, receipt, next_step,
    )


def _event_write(intent, event_type, payload):
    values = []
    if intent.get("eventId") is not None:
        values.append(("event_id", intent["eventId"]))
    values.extend((
        ("work_item_id", intent["workItemId"]),
        ("request_id", intent["requestId"]),
        ("event_type", event_type),
        ("actor_kind", intent["actorKind"]),
        ("actor_id", intent["actorId"]),
        ("payload_json", _json(payload)),
        ("created_at", intent["now"]),
    ))
    return WriteOp("INSERT", "events", tuple(values))


def _route_write(snapshot, intent, queue, role):
    item = snapshot["workItem"]
    return WriteOp("UPDATE", "work_items", (
        ("queue_state", queue), ("current_role", role),
        ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"]),
    ), (("work_item_id", intent["workItemId"]),
        ("row_version", item["row_version"])))


def plan_claim_role(snapshot, intent):
    """Declare the complete claim/task/route/audit post-state."""
    payload = {
        "claimId": intent["claimId"], "taskId": intent["taskId"],
        "role": intent["role"], "generation": intent["generation"],
        "reconciledActivity": [],
    }
    writes = (
        WriteOp("INSERT", "claims", (
            ("claim_id", intent["claimId"]),
            ("work_item_id", intent["workItemId"]),
            ("task_id", intent["taskId"]),
            ("agent_id", intent["actorId"]),
            ("role", intent["role"]),
            ("generation", intent["generation"]),
            ("status", "ACTIVE"), ("acquired_at", intent["now"]),
            ("expires_at", intent["expiresAt"]), ("released_at", None),
        )),
        WriteOp("UPDATE", "tasks", (
            ("status", "IN_PROGRESS"), ("updated_at", intent["now"]),
        ), (("work_item_id", intent["workItemId"]),
            ("task_id", intent["taskId"]), ("status", "NOT_STARTED"))),
        _route_write(snapshot, intent, "CLAIMED", intent["role"]),
        _event_write(intent, "CLAIM_ACQUIRED", payload),
    )
    return build_transition_plan(
        "CLAIM_ROLE", snapshot, intent, writes,
        {"queue_state": "CLAIMED", "current_role": intent["role"]},
        {"type": "CLAIM_ACQUIRED", "payload": payload},
        {"status": "OK", "claimId": intent["claimId"],
         "generation": intent["generation"]},
        {"action": "CONTINUE_CLAIMED_TASK",
         "arguments": {"workItem": intent["workItemId"],
                       "taskId": intent["taskId"]}},
    )


def plan_release_role(snapshot, intent):
    payload = {"claimId": intent["claimId"],
               "generation": intent["generation"]}
    writes = (
        WriteOp("UPDATE", "claims", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
            ("generation", intent["generation"]))),
        WriteOp("UPDATE", "tasks", (
            ("status", "NOT_STARTED"), ("updated_at", intent["now"]),
        ), (("work_item_id", intent["workItemId"]),
            ("task_id", intent["taskId"]), ("status", "IN_PROGRESS"))),
        _route_write(snapshot, intent, intent["queue"], intent["role"]),
        _event_write(intent, "CLAIM_RELEASED", payload),
    )
    return build_transition_plan(
        "RELEASE_ROLE", snapshot, intent, writes,
        {"queue_state": intent["queue"], "current_role": intent["role"]},
        {"type": "CLAIM_RELEASED", "payload": payload},
        {"status": "OK"}, {"action": "CLAIM_ROLE", "arguments": {
            "workItem": intent["workItemId"], "role": intent["role"]}},
    )


def plan_writer(snapshot, intent, acquire):
    operation = "ACQUIRE_WRITER" if acquire else "RELEASE_WRITER"
    if acquire:
        payload = intent.get("payload", {
            "repositoryKey": intent["repositoryKey"],
            "generation": intent["generation"], "reconciledActivity": []})
        writes = (
            WriteOp("INSERT", "repository_locks", (
                ("lock_id", intent["lockId"]),
                ("repository_key", intent["repositoryKey"]),
                ("work_item_id", intent["workItemId"]),
                ("agent_id", intent.get("ownerId", intent["actorId"])),
                ("generation", intent["generation"]), ("status", "ACTIVE"),
                ("acquired_at", intent["now"]),
                ("expires_at", intent["expiresAt"]), ("released_at", None),
            )),
            _route_write(snapshot, intent,
                         snapshot["workItem"]["queue_state"],
                         snapshot["workItem"]["current_role"]),
            _event_write(intent, "REPOSITORY_LOCK_ACQUIRED", payload),
        )
        next_step = {"action": "CONTINUE_CLAIMED_TASK", "arguments": {
            "workItem": intent["workItemId"]}}
    else:
        payload = intent.get("payload", {
            "repositoryKey": intent["repositoryKey"],
            "generation": intent["generation"]})
        writes = (
            WriteOp("UPDATE", "repository_locks", (
                ("status", "RELEASED"), ("released_at", intent["now"]),
            ), (("lock_id", intent["lockId"]), ("status", "ACTIVE"),
                ("generation", intent["generation"]))),
            _route_write(snapshot, intent,
                         snapshot["workItem"]["queue_state"],
                         snapshot["workItem"]["current_role"]),
            _event_write(intent, "REPOSITORY_LOCK_RELEASED", payload),
        )
        next_step = {"action": "RELEASE_ROLE", "arguments": {
            "workItem": intent["workItemId"]}}
    return build_transition_plan(
        operation, snapshot, intent, writes, {},
        {"type": "REPOSITORY_LOCK_" + ("ACQUIRED" if acquire else "RELEASED"),
         "payload": payload},
        {"status": "OK"}, next_step,
    )


def plan_expire_and_reconcile(snapshot, intent):
    """Declare one exact time-induced activity bundle normalization."""
    table_keys = {
        "AGENT_CLAIM": ("claims", "claim_id"),
        "REPOSITORY_WRITER": ("repository_locks", "lock_id"),
        "ORCHESTRATOR_LEASE": ("orchestrator_leases", "lease_id"),
    }
    writes = []
    claim_tasks = []
    for resource in intent["expectedActivity"]:
        kind = resource["kind"]
        if kind not in table_keys:
            raise ValueError("unknown activity kind")
        table, key = table_keys[kind]
        writes.append(WriteOp("UPDATE", table, (
            ("status", "EXPIRED"), ("released_at", intent["now"]),
        ), ((key, resource["resourceId"]),
            ("work_item_id", intent["workItemId"]), ("status", "ACTIVE"),
            ("generation", resource["generation"]),
            ("expires_at", resource["expiresAt"]))))
        if kind == "AGENT_CLAIM":
            claim_tasks.append(resource.get("taskId"))
    terminal = snapshot["workItem"]["state"] == TERMINAL_STATE
    if claim_tasks and not terminal:
        if len(claim_tasks) != 1 or claim_tasks[0] is None:
            raise ValueError("ambiguous activity task route")
        writes.append(WriteOp("UPDATE", "tasks", (
            ("status", "NOT_STARTED"), ("updated_at", intent["now"]),
        ), (("work_item_id", intent["workItemId"]),
            ("task_id", claim_tasks[0]), ("status", "IN_PROGRESS"))))
        queue, role = "CLAIMABLE", intent["nextRole"]
    else:
        queue = snapshot["workItem"]["queue_state"]
        role = snapshot["workItem"]["current_role"]
    writes.append(_route_write(snapshot, intent, queue, role))
    payload = {
        "request": intent["request"],
        "requestFingerprint": intent["requestFingerprint"],
        "fromFingerprint": snapshot["projectionFingerprint"],
        "expectedActivity": intent["expectedActivity"],
        "result": intent["result"],
    }
    writes.append(_event_write(intent, "ACTIVITY_EXPIRED_AND_RECONCILED",
                               payload))
    return build_transition_plan(
        EXPIRE_AND_RECONCILE_ACTIVITY, snapshot, intent, writes,
        {"queue_state": queue, "current_role": role},
        {"type": "ACTIVITY_EXPIRED_AND_RECONCILED", "payload": payload},
        intent["result"], {"action": "NONE", "arguments": {}},
        preconditions=("EXACT_FROM_FINGERPRINT", "EXACT_ACTIVITY_BUNDLE",
                       "EXACT_OWNER_GENERATION", "NOT_AFTER", "EXACT_REQUEST_ID"),
    )


def plan_orchestrator_lease(snapshot, intent):
    operation = intent["operation"]
    if operation not in ("ORCHESTRATOR_CLAIM", "ORCHESTRATOR_RENEW",
                         "ORCHESTRATOR_RELEASE", "ORCHESTRATOR_RECOVER"):
        raise ValueError("unknown orchestrator lease operation")
    if operation in ("ORCHESTRATOR_CLAIM", "ORCHESTRATOR_RECOVER"):
        writes = [WriteOp("INSERT", "orchestrator_leases", (
            ("lease_id", intent["leaseId"]),
            ("work_item_id", intent["workItemId"]),
            ("orchestrator_id", intent["actorId"]),
            ("generation", intent["generation"]), ("status", "ACTIVE"),
            ("acquired_at", intent["now"]), ("renewed_at", intent["now"]),
            ("expires_at", intent["expiresAt"]), ("released_at", None),
        ))]
        event_type = ("ORCHESTRATOR_LEASE_ACQUIRED" if operation ==
                      "ORCHESTRATOR_CLAIM" else "ORCHESTRATOR_LEASE_RECOVERED")
    elif operation == "ORCHESTRATOR_RENEW":
        writes = [WriteOp("UPDATE", "orchestrator_leases", (
            ("renewed_at", intent["now"]), ("expires_at", intent["expiresAt"]),
        ), (("lease_id", intent["leaseId"]), ("work_item_id", intent["workItemId"]),
            ("orchestrator_id", intent["actorId"]),
            ("generation", intent["generation"]), ("status", "ACTIVE")))]
        event_type = "ORCHESTRATOR_LEASE_RENEWED"
    else:
        writes = [WriteOp("UPDATE", "orchestrator_leases", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("lease_id", intent["leaseId"]), ("work_item_id", intent["workItemId"]),
            ("orchestrator_id", intent["actorId"]),
            ("generation", intent["generation"]), ("status", "ACTIVE")))]
        event_type = "ORCHESTRATOR_LEASE_RELEASED"
    writes.append(_route_write(
        snapshot, intent, snapshot["workItem"]["queue_state"],
        snapshot["workItem"]["current_role"],
    ))
    payload = {"leaseId": intent["leaseId"],
               "generation": intent["generation"]}
    writes.append(_event_write(intent, event_type, payload))
    return build_transition_plan(
        operation, snapshot, intent, writes, {},
        {"type": event_type, "payload": payload},
        {"status": "OK", "leaseId": intent["leaseId"],
         "generation": intent["generation"]},
        {"action": "CONTINUE" if operation != "ORCHESTRATOR_RELEASE" else "NONE",
         "arguments": {"workItem": intent["workItemId"]}},
    )


def plan_task_status(snapshot, intent):
    status = intent["status"]
    if status not in ("BLOCKED", "CANCELLED"):
        raise ValueError("task status is not a lifecycle edge")
    writes = [WriteOp("UPDATE", "tasks", (
        ("status", status), ("evidence_json", _json(intent["evidence"])),
        ("updated_at", intent["now"]),
    ), (("task_id", intent["taskId"]),
        ("work_item_id", intent["workItemId"]),
        ("status", intent["fromStatus"]))) ]
    writes.append(WriteOp("UPDATE", "claims", (
        ("status", "RELEASED"), ("released_at", intent["now"]),
    ), (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
        ("generation", intent["claimGeneration"]))))
    for writer in intent.get("writers", ()):
        writes.append(WriteOp("UPDATE", "repository_locks", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("lock_id", writer["lockId"]), ("status", "ACTIVE"),
            ("generation", writer["generation"]))))
    queue = "BLOCKED" if status == "BLOCKED" else "CLAIMABLE"
    item = snapshot["workItem"]
    writes.append(WriteOp("UPDATE", "work_items", (
        ("queue_state", queue),
        ("current_role", None if status == "BLOCKED" else intent["role"]),
        ("blocked_reason", "TASK_BLOCKED" if status == "BLOCKED" else None),
        ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"]),
    ), (("work_item_id", intent["workItemId"]),
        ("row_version", item["row_version"]))))
    payload = {"taskId": intent["taskId"], "from": intent["fromStatus"],
               "status": status, "evidence": intent["evidence"]}
    writes.append(_event_write(intent, "TASK_STATUS_CHANGED", payload))
    return build_transition_plan(
        "BLOCK", snapshot, intent, writes,
        {"queue_state": queue,
         "current_role": None if status == "BLOCKED" else intent["role"]},
        {"type": "TASK_STATUS_CHANGED", "payload": payload},
        {"status": "OK"},
        {"action": "HUMAN_UNBLOCK" if status == "BLOCKED" else "CLAIM_ROLE",
         "arguments": {"workItem": intent["workItemId"]}},
    )


def plan_submit(snapshot, intent):
    operation = intent["operation"]
    if operation not in ("SUBMIT_PLAN", "SUBMIT_IMPLEMENTATION"):
        raise ValueError("unknown submission operation")
    writes = [
        WriteOp("UPDATE", "tasks", (
            ("status", "COMPLETED"), ("updated_at", intent["now"]),
        ), (("task_id", intent["taskId"]), ("work_item_id", intent["workItemId"]),
            ("status", "IN_PROGRESS"))),
        WriteOp("UPDATE", "repository_locks", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("lock_id", intent["lockId"]), ("status", "ACTIVE"),
            ("generation", intent["lockGeneration"]))),
        WriteOp("UPDATE", "claims", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
            ("generation", intent["claimGeneration"]))),
    ]
    item = snapshot["workItem"]
    writes.append(WriteOp("UPDATE", "work_items", (
        ("state", intent["state"]), ("queue_state", "CLAIMABLE"),
        ("current_role", "REVIEWER"),
        ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"]),
    ), (("work_item_id", intent["workItemId"]),
        ("row_version", item["row_version"]))))
    events = (
        (intent["requestId"] + "-task-complete", "TASK_STATUS_CHANGED", {
            "taskId": intent["taskId"], "from": "IN_PROGRESS",
            "status": "COMPLETED", "evidence": []}),
        (intent["requestId"] + "-writer-release", "REPOSITORY_LOCK_RELEASED", {
            "repositoryKey": intent["repositoryKey"],
            "generation": intent["lockGeneration"]}),
        (intent["requestId"] + "-claim-release", "CLAIM_RELEASED", {
            "claimId": intent["claimId"], "taskId": intent["taskId"],
            "role": intent["role"], "generation": intent["claimGeneration"]}),
        (intent["requestId"] + "-submit", operation, intent["payload"]),
    )
    for event_request, event_type, payload in events:
        event_intent = dict(intent); event_intent["requestId"] = event_request
        writes.append(_event_write(event_intent, event_type, payload))
    if operation == "SUBMIT_PLAN":
        event_intent = dict(intent)
        event_intent["requestId"] = intent["requestId"] + "-artifact"
        writes.append(_event_write(event_intent, "PLAN_ARTIFACT_HEAD",
                                   intent["artifact"]))
    if intent.get("advanceReceipt") is not None:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["requestId"]
        event_intent["eventId"] = intent["advanceReceipt"]["eventId"]
        writes.append(_event_write(event_intent, "WORKFLOW_ADVANCED", {
            "requestFingerprint": intent["advanceFingerprint"],
            "receipt": intent["advanceReceipt"],
        }))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        {"state": intent["state"], "queue_state": "CLAIMABLE",
         "current_role": "REVIEWER"},
        {"type": operation, "payload": intent["payload"]},
        {"status": "OK"}, {"action": "CLAIM_ROLE", "arguments": {
            "workItem": intent["workItemId"], "role": "REVIEWER"}},
    )


def plan_begin(snapshot, intent):
    operation = "WORKFLOW_ADVANCE"
    writes = [
        WriteOp("INSERT", "claims", (
            ("claim_id", intent["claimId"]), ("work_item_id", intent["workItemId"]),
            ("task_id", intent["taskId"]), ("agent_id", intent["actorId"]),
            ("role", intent["role"]), ("generation", intent["claimGeneration"]),
            ("status", "ACTIVE"), ("acquired_at", intent["now"]),
            ("expires_at", intent["expiresAt"]), ("released_at", None),
        )),
        WriteOp("UPDATE", "tasks", (
            ("status", "IN_PROGRESS"), ("updated_at", intent["now"]),
        ), (("task_id", intent["taskId"]), ("work_item_id", intent["workItemId"]),
            ("status", "NOT_STARTED"))),
    ]
    if intent.get("lockId") is not None:
        writes.append(WriteOp("INSERT", "repository_locks", (
            ("lock_id", intent["lockId"]),
            ("repository_key", intent["repositoryKey"]),
            ("work_item_id", intent["workItemId"]),
            ("agent_id", intent["actorId"]),
            ("generation", intent["lockGeneration"]), ("status", "ACTIVE"),
            ("acquired_at", intent["now"]), ("expires_at", intent["expiresAt"]),
            ("released_at", None),
        )))
    item = snapshot["workItem"]
    writes.append(WriteOp("UPDATE", "work_items", (
        ("state", "IMPLEMENTING" if intent["beginAction"] == "BEGIN_IMPLEMENTATION"
         else item["state"]), ("queue_state", "CLAIMED"),
        ("current_role", intent["role"]),
        ("row_version", item["row_version"] + 1), ("updated_at", intent["now"]),
    ), (("work_item_id", intent["workItemId"]),
        ("row_version", item["row_version"]))))
    event_index = 0
    events = [("-claim", "CLAIM_ACQUIRED", {"claimId": intent["claimId"],
               "taskId": intent["taskId"], "role": intent["role"],
               "generation": intent["claimGeneration"], "reconciledActivity": []})]
    events.append(("-task", "TASK_STATUS_CHANGED", {
        "taskId": intent["taskId"], "from": "NOT_STARTED",
        "status": "IN_PROGRESS", "evidence": []}))
    if intent.get("lockId") is not None:
        events.append(("-writer", "REPOSITORY_LOCK_ACQUIRED", {
            "repositoryKey": intent["repositoryKey"],
            "generation": intent["lockGeneration"], "reconciledActivity": []}))
    if intent["beginAction"] == "BEGIN_IMPLEMENTATION":
        events.append(("-start", "START_IMPLEMENTATION", {
            "from": item["state"], "to": "IMPLEMENTING"}))
    for suffix, event_type, payload in events:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["requestId"] + suffix
        event_intent["eventId"] = intent["firstEventId"] + event_index
        writes.append(_event_write(event_intent, event_type, payload))
        event_index += 1
    event_intent = dict(intent)
    event_intent["eventId"] = intent["advanceReceipt"]["eventId"]
    writes.append(_event_write(event_intent, "WORKFLOW_ADVANCED", {
        "requestFingerprint": intent["advanceFingerprint"],
        "receipt": intent["advanceReceipt"],
    }))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        {"state": "IMPLEMENTING" if intent["beginAction"] == "BEGIN_IMPLEMENTATION"
         else item["state"], "queue_state": "CLAIMED", "current_role": intent["role"]},
        {"type": "WORKFLOW_ADVANCED"}, intent["advanceReceipt"],
        {"action": "CONTINUE_CLAIMED_TASK",
         "arguments": {"workItem": intent["workItemId"], "taskId": intent["taskId"]}},
    )


def plan_legacy_transition(snapshot, intent):
    operation = intent["operation"]
    if operation not in ("SUBMIT_PLAN", "BEGIN_IMPLEMENTATION",
                         "SUBMIT_IMPLEMENTATION"):
        raise ValueError("unknown transition operation")
    writes = []
    if operation != "BEGIN_IMPLEMENTATION":
        writes.append(WriteOp("UPDATE", "tasks", (
            ("status", "COMPLETED"), ("updated_at", intent["now"]),
        ), (("task_id", intent["taskId"]), ("work_item_id", intent["workItemId"]),
            ("status", "IN_PROGRESS"))))
        writes.append(WriteOp("UPDATE", "claims", (
            ("status", "RELEASED"), ("released_at", intent["now"]),
        ), (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
            ("generation", intent["claimGeneration"]))))
        for writer in intent.get("writers", ()):
            writes.append(WriteOp("UPDATE", "repository_locks", (
                ("status", "RELEASED"), ("released_at", intent["now"]),
            ), (("lock_id", writer["lockId"]), ("status", "ACTIVE"),
                ("generation", writer["generation"]))))
    item = snapshot["workItem"]
    writes.append(WriteOp("UPDATE", "work_items", (
        ("state", intent["state"]), ("queue_state", intent["queue"]),
        ("current_role", intent["role"]),
        ("row_version", item["row_version"] + 1), ("updated_at", intent["now"]),
    ), (("work_item_id", intent["workItemId"]),
        ("row_version", item["row_version"]))))
    writes.append(_event_write(intent, intent["eventType"], intent["payload"]))
    if intent.get("artifact") is not None:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["requestId"] + "-artifact"
        writes.append(_event_write(event_intent, "PLAN_ARTIFACT_HEAD",
                                   intent["artifact"]))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        {"state": intent["state"], "queue_state": intent["queue"],
         "current_role": intent["role"]},
        {"type": intent["eventType"], "payload": intent["payload"]},
        {"status": "OK"}, {"action": "CONTINUE", "arguments": {
            "workItem": intent["workItemId"]}},
    )


def plan_audit_touch(snapshot, intent):
    """Plan candidate/audit mutations that only touch rowVersion and events."""
    operation = intent["operation"]
    writes = []
    for task in intent.get("taskUpdates", ()):
        writes.append(WriteOp("UPDATE", "tasks", tuple(task["values"]),
                              tuple(task["where"]), task.get("expectedRows", 1)))
    item = snapshot["workItem"]
    values = list(intent.get("workItemValues", ()))
    values.extend((("row_version", item["row_version"] + 1),
                   ("updated_at", intent["now"])))
    writes.append(WriteOp("UPDATE", "work_items", tuple(values),
        (("work_item_id", intent["workItemId"]),
         ("row_version", item["row_version"]))))
    writes.append(_event_write(intent, intent["eventType"], intent["payload"]))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        dict(intent.get("postState", {})),
        {"type": intent["eventType"], "payload": intent["payload"]},
        intent.get("receipt", {"status": "OK"}),
        intent.get("nextStep", {"action": "NONE", "arguments": {}}),
    )


def _plan_lifecycle_bundle(snapshot, intent):
    """Build a closed lifecycle bundle for the remaining registered edges.

    This is deliberately private: named kernel builders own lifecycle choices
    and use this helper only after reducing a domain intent to complete writes.
    Public adapters must never supply managed-table write specifications.
    """
    operation = intent["operation"]
    if operation not in TRANSITION_MATRIX:
        raise ValueError("unregistered transition edge")
    writes = []
    for spec in intent.get("writes", ()):
        writes.append(WriteOp(
            spec["action"], spec["table"], tuple(spec.get("values", ())),
            tuple(spec.get("where", ())), spec.get("expectedRows", 1),
        ))
    for event in intent.get("events", ()):
        event_intent = dict(intent)
        event_intent.update({
            "requestId": event["requestId"],
            "eventId": event.get("eventId"),
            "actorKind": event.get("actorKind", intent["actorKind"]),
            "actorId": event.get("actorId", intent["actorId"]),
        })
        writes.append(_event_write(event_intent, event["eventType"],
                                   event["payload"]))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        dict(intent.get("postState", {})),
        intent.get("auditEvent", {}),
        intent.get("receipt", {"status": "OK"}),
        intent.get("nextStep", {"action": "NONE", "arguments": {}}),
    )


def plan_management(snapshot, intent):
    writes = []
    for change in intent.get("taskChanges", ()):
        if change["kind"] == "UPDATE":
            writes.append({"action": "UPDATE", "table": "tasks", "values": (
                ("title", change["title"]),
                ("required", int(change["required"])),
                ("updated_at", intent["now"]),
            ), "where": (("task_id", change["taskId"]),
                         ("work_item_id", intent["workItemId"]))})
        elif change["kind"] == "INSERT":
            writes.append({"action": "INSERT", "table": "tasks", "values": (
                ("task_id", change["taskId"]),
                ("work_item_id", intent["workItemId"]),
                ("seq", change["seq"]), ("title", change["title"]),
                ("owner_role", change["ownerRole"]),
                ("status", "NOT_STARTED"),
                ("required", int(change["required"])),
                ("evidence_json", "[]"), ("created_at", intent["now"]),
                ("updated_at", intent["now"]),
            )})
        else:
            raise ValueError("unknown management task change")
    item = snapshot["workItem"]
    writes.append({"action": "UPDATE", "table": "work_items", "values": (
        ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"]),
    ), "where": (("work_item_id", intent["workItemId"]),
                 ("row_version", item["row_version"]))})
    event_type = ("WORK_ITEM_MANAGEMENT_AMENDED" if
                  intent["operation"] == "AMEND_MANAGEMENT" else
                  "WORK_ITEM_MANAGEMENT_BACKFILLED")
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": ({
        "requestId": intent["requestId"], "eventType": event_type,
        "payload": intent["payload"],
    },)})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_hold(snapshot, intent):
    item = snapshot["workItem"]
    if intent["held"]:
        queue, role = "HELD", None
    elif item["state"] in ("PLAN_REVIEW_PENDING", "IMPLEMENTATION_COMPLETED"):
        queue, role = "WAITING_HUMAN", None
    else:
        queue = "CLAIMABLE"
        role = {"DRAFT": "PLANNER", "PLAN_REVIEW_APPROVED": "IMPLEMENTER",
                "IMPLEMENTING": "IMPLEMENTER"}.get(item["state"])
    bundle = dict(intent)
    bundle.update({
        "eventType": "WORK_ITEM_HELD" if intent["held"] else "WORK_ITEM_RESUMED",
        "payload": {"reason": intent["reason"]},
        "workItemValues": (("queue_state", queue), ("current_role", role),
                           ("held_reason", intent["reason"] if intent["held"] else None)),
        "postState": {"queue_state": queue, "current_role": role},
    })
    return plan_audit_touch(snapshot, bundle)


def plan_unblock(snapshot, intent):
    target = next((row for row in snapshot["tasks"]
                   if row["task_id"] == intent["taskId"]), None)
    if target is None or target["status"] != "BLOCKED":
        raise ValueError("task is not blocked")
    remaining = [row for row in snapshot["tasks"]
                 if row["status"] == "BLOCKED" and
                 row["task_id"] != intent["taskId"]]
    updates = [{"values": (("status", "NOT_STARTED"),
                            ("updated_at", intent["now"])),
                "where": (("work_item_id", intent["workItemId"]),
                          ("task_id", intent["taskId"]), ("status", "BLOCKED"))}]
    if remaining:
        queue, role, blocked = "BLOCKED", sorted(
            remaining, key=lambda row: row["seq"])[0]["owner_role"], "TASK_BLOCKED"
    elif snapshot["workItem"].get("blocked_reason") == "PLAN_DEVIATION":
        queue, role, blocked = "CLAIMABLE", "PLANNER", None
        planner = next(row for row in snapshot["tasks"]
                       if row["owner_role"] == "PLANNER")
        updates.append({"values": (("status", "NOT_STARTED"),
                                   ("updated_at", intent["now"])),
                        "where": (("work_item_id", intent["workItemId"]),
                                  ("task_id", planner["task_id"]))})
    else:
        queue, role, blocked = "CLAIMABLE", target["owner_role"], None
    bundle = dict(intent)
    bundle.update({"eventType": "TASK_UNBLOCKED",
                   "payload": {"taskId": intent["taskId"],
                               "reason": intent["reason"]},
                   "taskUpdates": updates,
                   "workItemValues": (("queue_state", queue),
                                      ("current_role", role),
                                      ("blocked_reason", blocked)),
                   "postState": {"queue_state": queue, "current_role": role}})
    return plan_audit_touch(snapshot, bundle)


def plan_plan_deviation(snapshot, intent):
    live_claims = [row for row in snapshot["claims"]
                   if row["status"] == "ACTIVE" and
                   row["owner"] == intent["actorId"]]
    if len(live_claims) != 1:
        raise ValueError("implementation claim is not unique")
    claim = live_claims[0]
    writes = []
    for writer in snapshot["writers"]:
        if writer["status"] == "ACTIVE" and writer["owner"] == intent["actorId"]:
            writes.append({"action": "UPDATE", "table": "repository_locks",
                           "values": (("status", "RELEASED"),
                                      ("released_at", intent["now"])),
                           "where": (("lock_id", writer["id"]),
                                     ("status", "ACTIVE"),
                                     ("generation", writer["generation"]))})
    item = snapshot["workItem"]
    writes.extend((
        {"action": "UPDATE", "table": "claims",
         "values": (("status", "RELEASED"), ("released_at", intent["now"])),
         "where": (("claim_id", claim["id"]), ("status", "ACTIVE"),
                   ("generation", claim["generation"]))},
        {"action": "UPDATE", "table": "tasks",
         "values": (("status", "BLOCKED"),
                    ("evidence_json", _json([intent["evidence"]])),
                    ("updated_at", intent["now"])),
         "where": (("work_item_id", intent["workItemId"]),
                   ("task_id", intent["taskId"]), ("status", "IN_PROGRESS"))},
        {"action": "UPDATE", "table": "work_items", "values": (
            ("state", "DRAFT"), ("queue_state", "BLOCKED"),
            ("current_role", None), ("blocked_reason", "PLAN_DEVIATION"),
            ("row_version", item["row_version"] + 1),
            ("updated_at", intent["now"])),
         "where": (("work_item_id", intent["workItemId"]),
                   ("row_version", item["row_version"]))},
    ))
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": ({
        "requestId": intent["requestId"], "eventType": "PLAN_DEVIATION",
        "payload": {"taskId": intent["taskId"], "evidence": intent["evidence"],
                    "roundsPreserved": True}},),
        "postState": {"state": "DRAFT", "queue_state": "BLOCKED",
                      "current_role": None}})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_human_gate(snapshot, intent):
    item = snapshot["workItem"]
    stage, decision = intent["stage"], intent["decision"]
    writes = [{"action": "INSERT", "table": "human_gates", "values": (
        ("gate_id", intent["gateId"]), ("work_item_id", intent["workItemId"]),
        ("stage", stage), ("human_id", intent["actorId"]),
        ("decision", decision), ("reason", intent["reason"]),
        ("created_at", intent["now"]),
    )}]
    events = [{"requestId": intent["requestId"],
               "eventType": "HUMAN_{0}_GATE".format(stage),
               "payload": {"decision": decision, "reason": intent["reason"]}}]
    if decision == "APPROVED" and stage == "PLAN":
        state, queue, role, held, closed = (
            "PLAN_REVIEW_APPROVED", "CLAIMABLE", "IMPLEMENTER", None, None)
    elif decision == "APPROVED":
        state, queue, role, held, closed = (
            TERMINAL_STATE, TERMINAL_QUEUE, None, "TERMINAL_STATE", intent["now"])
        for collection, table, key in (
                ("claims", "claims", "claim_id"),
                ("writers", "repository_locks", "lock_id"),
                ("leases", "orchestrator_leases", "lease_id")):
            for resource in snapshot[collection]:
                if resource["status"] == "ACTIVE":
                    writes.append({"action": "UPDATE", "table": table,
                                   "values": (("status", "RELEASED" if
                                               resource["effectiveStatus"] == "LIVE"
                                               else "EXPIRED"),
                                              ("released_at", intent["now"])),
                                   "where": ((key, resource["id"]),
                                             ("status", "ACTIVE"),
                                             ("generation", resource["generation"]))})
        events.append({"requestId": intent["requestId"] + "-terminal-activity",
                       "eventType": "TERMINAL_ACTIVITY_RECONCILED",
                       "actorKind": "SYSTEM", "actorId": "terminal-reconciler",
                       "payload": {"triggerRequestId": intent["requestId"] +
                                   "-terminal-activity",
                                   "resources": intent["terminalResources"]}})
    else:
        state = "DRAFT" if stage == "PLAN" else "IMPLEMENTING"
        queue, role, held, closed = (
            "CLAIMABLE", "PLANNER" if stage == "PLAN" else "IMPLEMENTER",
            None, None)
        author = next(row for row in snapshot["tasks"]
                      if row["owner_role"] == role)
        writes.append({"action": "UPDATE", "table": "tasks",
                       "values": (("status", "NOT_STARTED"),
                                  ("updated_at", intent["now"])),
                       "where": (("task_id", author["task_id"]),
                                 ("work_item_id", intent["workItemId"]))})
    writes.append({"action": "UPDATE", "table": "work_items", "values": (
        ("state", state), ("queue_state", queue), ("current_role", role),
        ("held_reason", held), ("blocked_reason", None), ("closed_at", closed),
        ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"])),
        "where": (("work_item_id", intent["workItemId"]),
                  ("row_version", item["row_version"]))})
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": events,
                   "postState": {"state": state, "queue_state": queue,
                                 "current_role": role}})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_orphan_repair(snapshot, intent):
    item = snapshot["workItem"]
    bundle = dict(intent)
    bundle.update({"writes": ({
        "action": "UPDATE", "table": "tasks",
        "values": (("status", "NOT_STARTED"), ("updated_at", intent["now"])),
        "where": (("work_item_id", intent["workItemId"]),
                  ("task_id", intent["taskId"]), ("owner_role", "REVIEWER"),
                  ("status", "IN_PROGRESS")),
    }, {
        "action": "UPDATE", "table": "work_items",
        "values": (("row_version", item["row_version"] + 1),
                   ("updated_at", intent["now"])),
        "where": (("work_item_id", intent["workItemId"]),
                  ("row_version", item["row_version"])),
    }), "events": ({"requestId": intent["requestId"],
                     "eventId": intent["eventId"],
                     "eventType": "WORKFLOW_REPAIRED",
                     "payload": intent["payload"]},)})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_publication_retry(snapshot, intent):
    writes = []
    events = [{"requestId": intent["requestId"],
               "eventType": "PUBLICATION_READY_INVALIDATED",
               "payload": intent["payload"]}]
    for task in intent["tasks"]:
        writes.append({"action": "UPDATE", "table": "tasks",
                       "values": (("status", "NOT_STARTED"),
                                  ("updated_at", intent["now"])),
                       "where": (("task_id", task["taskId"]),
                                 ("work_item_id", intent["workItemId"]),
                                 ("status", "COMPLETED"))})
        events.append({"requestId": intent["requestId"] + "-task-" +
                       task["ownerRole"].lower(),
                       "eventType": "TASK_STATUS_CHANGED",
                       "payload": {"taskId": task["taskId"], "from": "COMPLETED",
                                   "status": "NOT_STARTED", "evidence": [],
                                   "purpose": "PUBLICATION_RETRY"}})
    item = snapshot["workItem"]
    writes.append({"action": "UPDATE", "table": "work_items", "values": (
        ("state", "IMPLEMENTING"), ("queue_state", "CLAIMABLE"),
        ("current_role", "IMPLEMENTER"), ("held_reason", None),
        ("blocked_reason", None), ("row_version", item["row_version"] + 1),
        ("updated_at", intent["now"])),
        "where": (("work_item_id", intent["workItemId"]),
                  ("row_version", item["row_version"]))})
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": events,
                   "postState": {"state": "IMPLEMENTING",
                                 "queue_state": "CLAIMABLE",
                                 "current_role": "IMPLEMENTER"}})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_publication_postflight(snapshot, intent):
    item = snapshot["workItem"]
    writes = []
    events = list(intent["baseEvents"])
    if intent["auto"]:
        for collection, table, key in (
                ("claims", "claims", "claim_id"),
                ("writers", "repository_locks", "lock_id"),
                ("leases", "orchestrator_leases", "lease_id")):
            for resource in snapshot[collection]:
                if resource["status"] == "ACTIVE":
                    writes.append({"action": "UPDATE", "table": table,
                                   "values": (("status", "RELEASED" if
                                               resource["effectiveStatus"] == "LIVE"
                                               else "EXPIRED"),
                                              ("released_at", intent["now"])),
                                   "where": ((key, resource["id"]),
                                             ("status", "ACTIVE"),
                                             ("generation", resource["generation"]))})
        events.extend(intent["autoEvents"])
        state, queue, role = TERMINAL_STATE, TERMINAL_QUEUE, None
        values = (("state", state), ("queue_state", queue),
                  ("current_role", role), ("held_reason", "TERMINAL_STATE"),
                  ("blocked_reason", None), ("closed_at", intent["now"]))
    else:
        state, queue, role = item["state"], "WAITING_HUMAN", None
        values = (("queue_state", queue), ("current_role", role))
    writes.append({"action": "UPDATE", "table": "work_items",
                   "values": values + (("row_version", item["row_version"] + 1),
                                       ("updated_at", intent["now"])),
                   "where": (("work_item_id", intent["workItemId"]),
                             ("row_version", item["row_version"]))})
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": events,
                   "postState": {"state": state, "queue_state": queue,
                                 "current_role": role}})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_plan_amend(snapshot, intent):
    item = snapshot["workItem"]
    writes = (
        {"action": "INSERT", "table": "reviews", "values": (
            ("review_id", intent["reviewId"]),
            ("work_item_id", intent["workItemId"]), ("stage", "PLAN"),
            ("reviewer_agent_id", intent["actorId"]), ("decision", "REJECTED"),
            ("summary", _json(intent["review"])), ("created_at", intent["now"]),
        )},
        {"action": "UPDATE", "table": "claims",
         "values": (("status", "RELEASED"), ("released_at", intent["now"])),
         "where": (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
                   ("generation", intent["claimGeneration"]))},
        {"action": "UPDATE", "table": "tasks",
         "values": (("status", "NOT_STARTED"), ("updated_at", intent["now"])),
         "where": (("work_item_id", intent["workItemId"]),
                   ("task_id", intent["reviewerTaskId"]),
                   ("status", "IN_PROGRESS"))},
        {"action": "UPDATE", "table": "repository_locks",
         "values": (("status", "RELEASED"), ("released_at", intent["now"])),
         "where": (("lock_id", intent["lockId"]), ("status", "ACTIVE"),
                   ("generation", intent["lockGeneration"]))},
        {"action": "UPDATE", "table": "work_items", "values": (
            ("state", "PLAN_REVIEW_PENDING"), ("queue_state", "CLAIMABLE"),
            ("current_role", "REVIEWER"), ("held_reason", None),
            ("blocked_reason", None), ("row_version", item["row_version"] + 1),
            ("updated_at", intent["now"])),
         "where": (("work_item_id", intent["workItemId"]),
                   ("row_version", item["row_version"]))},
    )
    events = (
        {"requestId": intent["requestId"], "eventType": "AGENT_PLAN_REVIEW",
         "payload": intent["reviewPayload"]},
        {"requestId": intent["requestId"] + "-artifact",
         "eventType": "PLAN_ARTIFACT_HEAD", "payload": intent["artifactPayload"]},
        {"requestId": intent["requestId"] + "-release",
         "eventType": "REPOSITORY_LOCK_RELEASED",
         "actorKind": "SYSTEM", "actorId": "plan-amend",
         "payload": intent["releasePayload"]},
    )
    bundle = dict(intent)
    bundle.update({"writes": writes, "events": events,
                   "postState": {"state": "PLAN_REVIEW_PENDING",
                                 "queue_state": "CLAIMABLE",
                                 "current_role": "REVIEWER"}})
    return _plan_lifecycle_bundle(snapshot, bundle)


def plan_create_work_item(intent):
    """Declare the only transition whose canonical pre-state is absence."""
    snapshot = {"projectionFingerprint": content_fingerprint({
        "workItemId": intent["workItemId"], "absent": True,
    })}
    item = intent["item"]
    writes = [WriteOp("INSERT", "work_items", tuple(item))]
    for task in intent["tasks"]:
        writes.append(WriteOp("INSERT", "tasks", tuple(task)))
    writes.append(_event_write(intent, "WORK_ITEM_CREATED", intent["payload"]))
    return snapshot, build_transition_plan(
        "CREATE_WORK_ITEM", snapshot, intent, writes,
        {"state": "DRAFT", "queue_state": "CLAIMABLE",
         "current_role": "PLANNER"},
        {"type": "WORK_ITEM_CREATED", "payload": intent["payload"]},
        {"status": "OK"},
        {"action": "CLAIM_ROLE", "arguments": {"role": "PLANNER"}},
    )
def plan_review(snapshot, intent):
    """Declare the complete Reviewer task/claim/review/route lifecycle."""
    operation = "PLAN_REVIEW" if intent["stage"] == "PLAN" else "FINAL_REVIEW"
    writes = [WriteOp("INSERT", "reviews", (
        ("review_id", intent["reviewId"]),
        ("work_item_id", intent["workItemId"]), ("stage", intent["stage"]),
        ("reviewer_agent_id", intent["actorId"]),
        ("decision", intent["storedDecision"]),
        ("summary", _json(intent["review"])), ("created_at", intent["now"]),
    ))]
    writes.append(WriteOp("UPDATE", "claims", (
        ("status", "RELEASED"), ("released_at", intent["now"]),
    ), (("claim_id", intent["claimId"]), ("status", "ACTIVE"),
        ("generation", intent["claimGeneration"]))))
    writes.append(WriteOp("UPDATE", "tasks", (
        ("status", intent["reviewerStatus"]),
        ("evidence_json", _json(intent["reviewEvidence"])),
        ("updated_at", intent["now"]),
    ), (("task_id", intent["reviewerTaskId"]),
        ("work_item_id", intent["workItemId"]), ("owner_role", "REVIEWER"),
        ("status", "IN_PROGRESS"))))
    if intent.get("authorTaskId") is not None:
        writes.append(WriteOp("UPDATE", "tasks", (
            ("status", "NOT_STARTED"), ("updated_at", intent["now"]),
        ), (("task_id", intent["authorTaskId"]),
            ("work_item_id", intent["workItemId"]),
            ("owner_role", intent["authorRole"]), ("status", "COMPLETED"))))
    item = snapshot["workItem"]
    if intent["autoApproved"]:
        if intent["stage"] == "FINAL":
            state, queue, role = TERMINAL_STATE, TERMINAL_QUEUE, None
            held_reason, closed_at = "TERMINAL_STATE", intent["now"]
        else:
            state, queue, role = "PLAN_REVIEW_APPROVED", "CLAIMABLE", "IMPLEMENTER"
            held_reason, closed_at = None, None
        work_values = (
            ("state", state), ("queue_state", queue), ("current_role", role),
            ("held_reason", held_reason), ("blocked_reason", None),
            ("closed_at", closed_at),
            ("row_version", item["row_version"] + 1),
            ("updated_at", intent["now"]),
        )
        if intent["stage"] == "FINAL":
            for collection, table, key in (
                    ("writers", "repository_locks", "lock_id"),
                    ("leases", "orchestrator_leases", "lease_id")):
                for resource in snapshot[collection]:
                    if resource["status"] != "ACTIVE":
                        continue
                    writes.append(WriteOp("UPDATE", table, (
                        ("status", "RELEASED" if resource["effectiveStatus"] == "LIVE"
                         else "EXPIRED"), ("released_at", intent["now"]),
                    ), ((key, resource["id"]), ("status", "ACTIVE"),
                        ("generation", resource["generation"]))))
    else:
        state, queue, role = intent["state"], intent["queue"], intent["role"]
        work_values = (
            ("state", state), ("queue_state", queue), ("current_role", role),
            ("held_reason", "TARGET_REACHED" if queue == "HELD" else None),
            ("blocked_reason", "REVIEW_BLOCKED" if queue == "BLOCKED" else None),
            ("row_version", item["row_version"] + 1),
            ("updated_at", intent["now"]),
        )
    writes.append(WriteOp("UPDATE", "work_items", work_values,
        (("work_item_id", intent["workItemId"]),
         ("row_version", item["row_version"]))))
    event_intent = dict(intent); event_intent["requestId"] = intent["reviewRequestId"]
    event_intent["eventId"] = intent["reviewEventId"]
    writes.append(_event_write(event_intent,
                               "AGENT_{0}_REVIEW".format(intent["stage"]),
                               intent["reviewPayload"]))
    for extra in intent.get("extraEvents", ()):
        event_intent = dict(intent)
        event_intent.update({"requestId": extra["requestId"],
                             "eventId": extra["eventId"],
                             "actorKind": extra["actorKind"],
                             "actorId": extra["actorId"]})
        writes.append(_event_write(event_intent, extra["eventType"],
                                   extra["payload"]))
    if intent.get("terminalResources") is not None:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["terminalRequestId"]
        event_intent["eventId"] = intent["terminalEventId"]
        event_intent["actorKind"] = "SYSTEM"
        event_intent["actorId"] = "terminal-reconciler"
        writes.append(_event_write(event_intent, "TERMINAL_ACTIVITY_RECONCILED", {
            "triggerRequestId": intent["terminalRequestId"],
            "resources": intent["terminalResources"],
        }))
    if intent.get("readyPayload") is not None:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["readyRequestId"]
        event_intent["eventId"] = intent["readyEventId"]
        event_intent["actorKind"] = "SYSTEM"
        event_intent["actorId"] = "publication-gate"
        writes.append(_event_write(event_intent, "PUBLICATION_READY",
                                   intent["readyPayload"]))
    if intent.get("autoPayload") is not None:
        event_intent = dict(intent)
        event_intent["requestId"] = intent["autoRequestId"]
        event_intent["eventId"] = intent["autoEventId"]
        event_intent["actorKind"] = "SYSTEM"
        event_intent["actorId"] = "auto-gate"
        writes.append(_event_write(event_intent, "AUTO_GATE_APPROVED",
                                   intent["autoPayload"]))
    if intent.get("advanceReceipt") is not None:
        event_intent = dict(intent)
        event_intent.update({"requestId": intent["requestId"],
                             "eventId": intent["advanceReceipt"]["eventId"]})
        writes.append(_event_write(event_intent, "WORKFLOW_ADVANCED", {
            "requestFingerprint": intent["advanceFingerprint"],
            "receipt": intent["advanceReceipt"],
        }))
    return build_transition_plan(
        operation, snapshot, intent, writes,
        {"state": state, "queue_state": queue, "current_role": role},
        {"type": "AGENT_{0}_REVIEW".format(intent["stage"]),
         "payload": intent["reviewPayload"]},
        {"status": "OK", "reviewId": intent["reviewId"]},
        intent["nextStep"],
    )
def repair_result(work_item_id, snapshot, violations, recipe=None,
                  request_id=None, expected_activity=None, not_after=None):
    fingerprint = snapshot["projectionFingerprint"]
    if not violations:
        return {
            "protocolVersion": CHECK_PROTOCOL, "operation": "CHECK",
            "status": "PASS", "workItemId": work_item_id,
            "projectionFingerprint": fingerprint, "violations": [],
            "repairability": "NONE",
            "nextStep": {"action": "NONE", "arguments": {}},
        }
    deterministic = recipe or (
        EXPIRE_AND_RECONCILE_ACTIVITY if expected_activity else None)
    if deterministic:
        arguments = {
            "workItem": work_item_id, "fingerprint": fingerprint,
            "requestId": request_id,
        }
        if deterministic == EXPIRE_AND_RECONCILE_ACTIVITY:
            arguments.update({"expectedActivity": expected_activity,
                              "notAfter": not_after})
        else:
            arguments.update({"action": deterministic})
        return {
            "protocolVersion": CHECK_PROTOCOL, "operation": "CHECK",
            "status": "VIOLATION", "workItemId": work_item_id,
            "projectionFingerprint": fingerprint, "violations": violations,
            "repairability": "DETERMINISTIC",
            "nextStep": {"action": deterministic, "arguments": arguments},
        }
    return {
        "protocolVersion": CHECK_PROTOCOL, "operation": "CHECK",
        "status": "VIOLATION", "workItemId": work_item_id,
        "projectionFingerprint": fingerprint, "violations": violations,
        "repairability": "AMBIGUOUS",
        "nextStep": {"action": "HUMAN_INSPECT_WORKFLOW_PROJECTION",
                     "arguments": {"workItem": work_item_id,
                                   "fingerprint": fingerprint}},
    }
