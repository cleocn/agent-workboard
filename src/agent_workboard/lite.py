"""Small, useful Agent Workboard baseline.

MVP-LITE deliberately keeps orchestration thin: SQLite state, leases, one writer,
two review stages, human gates, hold/block and an event timeline.
"""

import argparse
import datetime
import hashlib
import html
import ipaddress
import json
import os
import pkgutil
import re
import sqlite3
import stat
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit

from . import workflow as workflow_kernel


SCHEMA_VERSION = "MVP-LITE-v1"
MANAGEMENT_CONTRACT_VERSION = "AWB-WORKITEM-MGMT-v1"
TEMPLATE_CONTRACT_VERSION = "AWB-MANAGEMENT-v1"
AUTO_GATE_SCHEMA_VERSION = "AWB-AUTO-GATE-v1"
CREATION_RISK_PROTOCOL = "AWB-CREATION-RISK-v1"
REVIEW_TASK_RECOVERY_PROTOCOL = "AWB-REVIEW-TASK-RECOVERY-v1"
HUMAN_GATE_POLICIES = ("AUTO_ON_PASS", "MANUAL")
CREATION_RISK_KINDS = ("REMOTE", "DESTRUCTIVE", "ANOMALOUS_STATE")
USAGE_POLICIES = ("OFF", "BEST_EFFORT")
PLAN_ARTIFACT_PROTOCOL = "AWB-PLAN-ARTIFACT-v1"
REVIEW_V2_PROTOCOL = "AWB-REVIEW-v2"
MUTATION_RECEIPT_PROTOCOL = "AWB-MUTATION-RECEIPT-v1"
WORKFLOW_ADVANCE_PROTOCOL = "AWB-WORKFLOW-ADVANCE-v1"
PLAN_AMEND_CATEGORIES = (
    "INTERNAL_CONTRADICTION", "COMMAND_OR_PATH", "TEST_OMISSION",
    "ACCEPTANCE_EXPRESSION", "IMPLEMENTATION_ORDER", "DUPLICATE_EVIDENCE",
)
BUSY_TIMEOUT_MS = 5000
DEFAULT_SCHEMA = None
# Compatibility-only default.  Installed projects should use `awb ... --project`
# so the stable/development identity checks are applied before opening SQLite.
DEFAULT_DATABASE = os.path.join(os.getcwd(), ".awb", "workboard.db")


class LiteError(Exception):
    pass


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _id(prefix):
    return "{0}-{1}".format(prefix, uuid.uuid4().hex)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _normalize_schema_sql(value):
    """Canonicalize SQL without changing quoted text."""
    value = value or ""
    normalized = []
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            index += 1
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = len(value) if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            index = len(value) if closing < 0 else closing + 2
            continue
        if character in ("'", '"', "`"):
            quote = character
            normalized.append(character)
            index += 1
            while index < len(value):
                character = value[index]
                normalized.append(character)
                index += 1
                if character == quote:
                    if index < len(value) and value[index] == quote:
                        normalized.append(value[index])
                        index += 1
                    else:
                        break
            continue
        if character == "[":
            normalized.append(character)
            index += 1
            while index < len(value):
                character = value[index]
                normalized.append(character)
                index += 1
                if character == "]":
                    if index < len(value) and value[index] == "]":
                        normalized.append(value[index])
                        index += 1
                    else:
                        break
            continue
        normalized.append(character.lower())
        index += 1
    return "".join(normalized)


def human_gate_schema_sql():
    return """
ALTER TABLE work_items ADD COLUMN human_gate_policy TEXT NOT NULL DEFAULT 'MANUAL'
  CHECK (human_gate_policy IN ('AUTO_ON_PASS','MANUAL'));
INSERT INTO schema_meta(key,value) VALUES
  ('gate_policy_schema_version','AWB-AUTO-GATE-v1');
""".strip()


def human_gate_schema_state(connection):
    marker = connection.execute(
        "SELECT value FROM schema_meta WHERE key='gate_policy_schema_version'"
    ).fetchone()
    columns = [row[1] for row in connection.execute("PRAGMA table_info(work_items)")]
    present = "human_gate_policy" in columns
    if marker is None and not present:
        return "ABSENT"
    table = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
    ).fetchone()
    normalized = _normalize_schema_sql(table[0] if table else "")
    expected = "human_gate_policytextnotnulldefault'MANUAL'check(human_gate_policyin('AUTO_ON_PASS','MANUAL'))"
    invalid_values = (connection.execute(
        "SELECT count(*) FROM work_items WHERE human_gate_policy NOT IN ('AUTO_ON_PASS','MANUAL')"
    ).fetchone()[0] if present else 1)
    if (marker and marker[0] == AUTO_GATE_SCHEMA_VERSION and present and
            expected in normalized and not invalid_values):
        return "INSTALLED"
    return "INVALID"


_SENSITIVE_RISK = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]+|password\s*[:=]|secret\s*[:=]|"
    r"api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|credential\s*[:=])"
)


def normalize_creation_risk(value):
    """Validate the Agent's pre-create classification before any DB transaction."""
    if value is None:
        value = {"protocolVersion": CREATION_RISK_PROTOCOL, "signals": []}
    if not isinstance(value, dict) or set(value) != {"protocolVersion", "signals"}:
        raise LiteError("creation risk must contain only protocolVersion and signals")
    if value.get("protocolVersion") != CREATION_RISK_PROTOCOL:
        raise LiteError("creation risk protocolVersion is invalid")
    signals = value.get("signals")
    if not isinstance(signals, list):
        raise LiteError("creation risk signals must be a list")
    normalized = []
    seen = set()
    for signal in signals:
        if not isinstance(signal, dict) or set(signal) != {"kind", "source", "evidence"}:
            raise LiteError("creation risk signal fields are invalid")
        kind = signal.get("kind")
        source = signal.get("source")
        evidence = signal.get("evidence")
        if kind not in CREATION_RISK_KINDS:
            raise LiteError("creation risk kind is invalid")
        if (not isinstance(source, str) or not source.strip() or len(source.strip()) > 120 or
                not isinstance(evidence, str) or not evidence.strip() or
                len(evidence.strip()) > 240):
            raise LiteError("creation risk source and evidence must be short non-empty strings")
        source, evidence = source.strip(), evidence.strip()
        if _SENSITIVE_RISK.search(source) or _SENSITIVE_RISK.search(evidence):
            raise LiteError("creation risk must not contain credentials or secrets")
        key = (kind, source, evidence)
        if key in seen:
            raise LiteError("creation risk signals must not be duplicated")
        seen.add(key)
        normalized.append({"kind": kind, "source": source, "evidence": evidence})
    normalized.sort(key=lambda entry: (entry["kind"], entry["source"], entry["evidence"]))
    return {"protocolVersion": CREATION_RISK_PROTOCOL, "signals": normalized}


def open_database(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise LiteError("database is missing; run lite init")
    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout={0}".format(BUSY_TIMEOUT_MS))
    version = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if version is None or version[0] != SCHEMA_VERSION:
        connection.close()
        raise LiteError("database is not MVP-LITE-v1")
    return connection


def _resource_text(name):
    data = pkgutil.get_data("agent_workboard", "resources/" + name)
    if data is None:
        raise LiteError("required package resource is missing: {0}".format(name))
    return data.decode("utf-8")


def initialize_database(path, schema_path=DEFAULT_SCHEMA):
    path = os.path.abspath(path)
    if os.path.lexists(path) and (not os.path.isfile(path) or os.path.getsize(path) != 0):
        raise LiteError("refusing to initialize an existing non-empty target")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if schema_path is None:
        schema = _resource_text("spec/mvp_lite_v1_1/schema.sql")
    else:
        with open(schema_path, "r", encoding="utf-8") as handle:
            schema = handle.read()
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(schema)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LiteError("integrity check failed")
        connection.commit()
    except Exception:
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            candidate = path + suffix
            if os.path.isfile(candidate):
                os.unlink(candidate)
        raise
    finally:
        if connection:
            connection.close()
    return {"status": "ok", "schemaVersion": SCHEMA_VERSION, "database": path}


def _item(connection, work_item_id):
    row = connection.execute(
        "SELECT * FROM work_items WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    if row is None:
        raise LiteError("WorkItem does not exist")
    return row


def _nonempty_strings(value, field, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise LiteError("{0} must be a {1}list".format(field, "non-empty " if not allow_empty else ""))
    if any(not isinstance(entry, str) or not entry.strip() for entry in value):
        raise LiteError("{0} entries must be non-empty strings".format(field))
    return list(value)


def _expected_roles(mode):
    return {"PLANNER", "REVIEWER"} if mode == "READ_ONLY_DIAGNOSIS" else {
        "PLANNER", "IMPLEMENTER", "REVIEWER"
    }


def _normalize_management(work_item_id, item_type, title, mode, priority, management, now):
    if not isinstance(management, dict):
        raise LiteError("management envelope is required")
    if management.get("contractVersion") != MANAGEMENT_CONTRACT_VERSION:
        raise LiteError("management contractVersion is invalid")
    if management.get("templateContractVersion") != TEMPLATE_CONTRACT_VERSION:
        raise LiteError("management templateContractVersion is invalid")
    scope = _nonempty_strings(management.get("scope"), "scope")
    out_of_scope = _nonempty_strings(management.get("outOfScope"), "outOfScope", allow_empty=True)
    authorization = management.get("authorization")
    if not isinstance(authorization, dict) or set(authorization) != {"allowed", "forbidden"}:
        raise LiteError("authorization must contain only allowed and forbidden")
    allowed = _nonempty_strings(authorization["allowed"], "authorization.allowed")
    forbidden = _nonempty_strings(authorization["forbidden"], "authorization.forbidden")
    safety = _nonempty_strings(management.get("safetyConstraints"), "safetyConstraints")
    tasks = management.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise LiteError("management tasks must be non-empty")
    normalized_tasks = []
    roles = set()
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            raise LiteError("management task must be an object")
        expected_id = "{0}-T{1:02d}".format(work_item_id, index)
        if task.get("taskId") != expected_id or task.get("seq") != index:
            raise LiteError("management task ids and seq must be continuous from T01")
        role = task.get("ownerRole")
        if role not in ("PLANNER", "IMPLEMENTER", "REVIEWER"):
            raise LiteError("management task ownerRole is invalid")
        if not isinstance(task.get("title"), str) or not task["title"].strip():
            raise LiteError("management task title is required")
        if not isinstance(task.get("required"), bool):
            raise LiteError("management task required must be boolean")
        acceptance = _nonempty_strings(task.get("acceptance"), "task acceptance")
        closure = _nonempty_strings(
            task.get("closureEvidenceRequired"), "task closureEvidenceRequired"
        )
        status = task.get("status", "NOT_STARTED")
        if status not in (
            "NOT_STARTED", "IN_PROGRESS", "BLOCKED", "WAITING_ACCEPTANCE",
            "COMPLETED", "CANCELLED",
        ):
            raise LiteError("management task status is invalid")
        roles.add(role)
        normalized_tasks.append({
            "taskId": expected_id, "seq": index, "title": task["title"].strip(),
            "ownerRole": role, "required": task["required"], "status": status,
            "acceptance": acceptance, "closureEvidenceRequired": closure,
        })
    if roles != _expected_roles(mode):
        raise LiteError("management task roles do not match mode")
    acceptance = management.get("acceptance")
    closure = management.get("closure")
    for value, field in ((acceptance, "acceptance"), (closure, "closure")):
        if not isinstance(value, list) or not value:
            raise LiteError("management {0} must be non-empty".format(field))
        ids = []
        for entry in value:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
                raise LiteError("management {0} entries require id".format(field))
            if not isinstance(entry.get("criterion"), str) or not entry["criterion"].strip():
                raise LiteError("management {0} entries require criterion".format(field))
            ids.append(entry["id"])
        if len(ids) != len(set(ids)):
            raise LiteError("management {0} ids must be unique".format(field))
    progress_method = management.get("progressMethod", "REQUIRED_TASKS")
    stages = management.get("stages", [])
    if progress_method == "WEIGHTED":
        if item_type != "FE" or not isinstance(stages, list) or not stages:
            raise LiteError("weighted progress is only valid for FE with stages")
        stage_ids = [stage.get("id") for stage in stages if isinstance(stage, dict)]
        weights = [stage.get("weight") for stage in stages if isinstance(stage, dict)]
        if len(stage_ids) != len(stages) or any(not isinstance(value, str) or not value for value in stage_ids):
            raise LiteError("weighted stages require stable ids")
        if len(stage_ids) != len(set(stage_ids)) or any(not isinstance(value, int) or value <= 0 for value in weights):
            raise LiteError("weighted stage ids and weights are invalid")
        if sum(weights) != 100:
            raise LiteError("weighted stage weights must total 100")
        completions = [stage.get("completion", 0) for stage in stages]
        if any(type(value) is not int or value < 0 or value > 100 for value in completions):
            raise LiteError("weighted stage completion must be an integer from 0 to 100")
    elif progress_method != "REQUIRED_TASKS" or stages:
        raise LiteError("progressMethod is invalid")
    return {
        "contractVersion": MANAGEMENT_CONTRACT_VERSION,
        "templateContractVersion": TEMPLATE_CONTRACT_VERSION,
        "workItemId": work_item_id, "type": item_type, "title": title,
        "mode": mode, "priority": priority, "createdAt": now, "updatedAt": now,
        "state": "DRAFT", "queueState": "CLAIMABLE",
        "scope": scope, "outOfScope": out_of_scope,
        "authorization": {"allowed": allowed, "forbidden": forbidden},
        "safetyConstraints": safety, "tasks": normalized_tasks,
        "acceptance": acceptance, "closure": closure,
        "progressMethod": progress_method, "stages": stages,
        "currentTask": normalized_tasks[0]["taskId"],
        "nextStep": "claim {0} as PLANNER".format(normalized_tasks[0]["taskId"]),
        "statusHistory": [{
            "at": now, "actor": "ORCHESTRATOR", "event": "WORK_ITEM_CREATED",
            "state": "DRAFT", "queueState": "CLAIMABLE",
        }],
    }


def _management_from_events(connection, work_item_id):
    management = None
    for row in connection.execute(
        "SELECT event_type,payload_json FROM events WHERE work_item_id=? ORDER BY event_id",
        (work_item_id,),
    ):
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            continue
        if row["event_type"] == "WORK_ITEM_CREATED" and isinstance(payload.get("management"), dict):
            management = payload["management"]
        elif row["event_type"] in ("WORK_ITEM_MANAGEMENT_BACKFILLED", "WORK_ITEM_MANAGEMENT_AMENDED"):
            if isinstance(payload.get("management"), dict):
                management = payload["management"]
    return management


def _progress(tasks, management=None):
    required = [task for task in tasks if task["required"]]
    completed = sum(1 for task in required if task["status"] == "COMPLETED")
    counts = {status: 0 for status in (
        "NOT_STARTED", "IN_PROGRESS", "BLOCKED", "WAITING_ACCEPTANCE", "COMPLETED", "CANCELLED"
    )}
    for task in tasks:
        counts[task["status"]] += 1
    percent = int(100 * completed / len(required)) if required else 0
    result = {"method": "REQUIRED_TASKS", "completed": completed, "total": len(required),
              "percent": percent, "statusCounts": counts}
    if management and management.get("progressMethod") == "WEIGHTED":
        stages = management.get("stages", [])
        result = {"method": "WEIGHTED", "percent": int(sum(
            stage["weight"] * stage.get("completion", 0) for stage in stages
        ) / 100), "stages": stages, "statusCounts": counts,
            "completed": completed, "total": len(required)}
    return result


def _current_and_next(item, tasks, active_claim, review_state=None):
    if item["state"] == "FINAL_ACCEPTANCE_APPROVED":
        return None, "NONE"
    if item["queue_state"] == "BLOCKED":
        return None, "human unblock required"
    if item["queue_state"] == "WAITING_HUMAN":
        stage = "PLAN" if item["state"] == "PLAN_REVIEW_PENDING" else "FINAL"
        return None, "human {0} gate decision".format(stage)
    if item["queue_state"] == "HELD":
        return None, "human resume required"
    if active_claim:
        return active_claim["task_id"], "continue {0}".format(active_claim["task_id"])
    if review_state and review_state.get("nextStep"):
        return None, review_state["nextStep"]
    role = item["current_role"]
    candidates = [task for task in tasks if task["owner_role"] == role and task["status"] != "COMPLETED"]
    task = min(candidates, key=lambda value: value["seq"]) if candidates else None
    return (task["task_id"] if task else None,
            "claim {0} as {1}".format(task["task_id"], role) if task else "NONE")


def create_work_item(database, work_item_id, item_type, title, mode="STANDARD", priority="P2",
                     management=None, request_id=None, actor_id="orchestrator",
                     human_review=None, creation_risk=None, decision_actor=None):
    if not work_item_id.startswith(item_type + "-"):
        raise LiteError("WorkItem id and type must match")
    if item_type not in ("TI", "FE", "R", "WA", "AWB"):
        raise LiteError("unsupported WorkItem type")
    if mode not in ("STANDARD", "READ_ONLY_DIAGNOSIS"):
        raise LiteError("unsupported mode")
    risk = normalize_creation_risk(creation_risk)
    if human_review is not None and human_review not in HUMAN_GATE_POLICIES:
        raise LiteError("human review policy is invalid")
    if mode == "READ_ONLY_DIAGNOSIS" and human_review == "AUTO_ON_PASS":
        raise LiteError("AUTO_ON_PASS is only valid for STANDARD WorkItems")
    if risk["signals"] and human_review is None:
        raise LiteError("risk signals require an explicit human review choice before create")
    if risk["signals"] and (not isinstance(decision_actor, str) or not decision_actor.strip()):
        raise LiteError("risk choice requires a decision actor")
    policy = (human_review or "AUTO_ON_PASS") if mode == "STANDARD" else "MANUAL"
    policy_source = "EXPLICIT" if human_review is not None else "DEFAULT"
    request_id = request_id or _id("create")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if human_gate_schema_state(connection) != "INSTALLED":
            raise LiteError("human gate policy schema is not installed; run awb migrate")
        basic_payload = {
            "type": item_type, "title": title, "mode": mode, "priority": priority,
            "humanGatePolicy": policy, "policySource": policy_source,
            "creationRisk": risk,
        }
        if risk["signals"]:
            basic_payload.update({
                "riskPromptDecision": policy,
                "decisionActor": decision_actor.strip(),
                "riskActionAuthorized": False,
            })
        replay = connection.execute(
            "SELECT event_type,payload_json FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay:
            stored = json.loads(replay["payload_json"])
            if replay["event_type"] != "WORK_ITEM_CREATED" or any(
                stored.get(key) != value for key, value in basic_payload.items()
            ):
                raise LiteError("request_id was already used with different content")
            stored_management = stored.get("management")
            if (stored_management is None) != (management is None):
                raise LiteError("request_id was already used with different content")
            if stored_management is not None:
                candidate = _normalize_management(
                    work_item_id, item_type, title, mode, priority, management,
                    stored_management["createdAt"],
                )
                comparable = (
                    "contractVersion", "templateContractVersion", "scope", "outOfScope",
                    "authorization", "safetyConstraints", "tasks", "acceptance", "closure",
                    "progressMethod", "stages",
                )
                if any(candidate.get(key) != stored_management.get(key) for key in comparable):
                    raise LiteError("request_id was already used with different content")
            connection.rollback()
            return get_work_item(database, work_item_id)
        now = _now()
        normalized = _normalize_management(
            work_item_id, item_type, title, mode, priority, management, now
        )
        create_payload = dict(basic_payload)
        create_payload["management"] = normalized
        task_rows = []
        for task in normalized["tasks"]:
            task_rows.append((
                ("task_id", task["taskId"]), ("work_item_id", work_item_id),
                ("seq", task["seq"]), ("title", task["title"]),
                ("owner_role", task["ownerRole"]), ("status", task["status"]),
                ("required", int(task["required"])), ("created_at", now),
                ("updated_at", now),
            ))
        intent = {
            "operation": "CREATE_WORK_ITEM", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "SYSTEM",
            "actorId": actor_id, "now": now, "payload": create_payload,
            "item": (
                ("work_item_id", work_item_id), ("work_item_type", item_type),
                ("title", title), ("mode", mode), ("priority", priority),
                ("current_role", "PLANNER"), ("created_at", now),
                ("updated_at", now), ("human_gate_policy", policy),
            ),
            "tasks": task_rows,
        }
        snapshot, plan = workflow_kernel.plan_create_work_item(intent)
        _kernel_apply(connection, work_item_id, snapshot, plan)
        connection.commit()
        return get_work_item(database, work_item_id)
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise LiteError("cannot create WorkItem: {0}".format(exc))
    finally:
        connection.close()


def get_work_item(database, work_item_id):
    connection = open_database(database)
    try:
        item = dict(_item(connection, work_item_id))
        policy = item.get("human_gate_policy", "MANUAL")
        item["humanGatePolicy"] = policy
        creation = connection.execute(
            "SELECT payload_json FROM events WHERE work_item_id=? AND event_type='WORK_ITEM_CREATED' "
            "ORDER BY event_id LIMIT 1", (work_item_id,),
        ).fetchone()
        try:
            creation_payload = json.loads(creation[0]) if creation else {}
        except (TypeError, ValueError):
            creation_payload = {}
        item["humanGatePolicySource"] = creation_payload.get("policySource", "MIGRATED")
        item["tasks"] = [dict(row) for row in connection.execute(
            "SELECT * FROM tasks WHERE work_item_id=? ORDER BY seq", (work_item_id,)
        )]
        item["activeClaim"] = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone()
        if item["activeClaim"] is not None:
            item["activeClaim"] = dict(item["activeClaim"])
        management = _management_from_events(connection, work_item_id)
        item["managementBackfillRequired"] = management is None
        task_contracts = {
            task["taskId"]: task for task in (management or {}).get("tasks", [])
        }
        for task in item["tasks"]:
            contract = task_contracts.get(task["task_id"], {})
            task["acceptance"] = contract.get("acceptance", [])
            task["closureEvidenceRequired"] = contract.get("closureEvidenceRequired", [])
        item["progress"] = _progress(item["tasks"], management)
        review_state = _review_projection(connection, work_item_id)
        item["reviewConvergence"] = review_state
        item["planArtifact"] = _plan_artifact_head(connection, work_item_id)
        current, next_step = _current_and_next(
            item, item["tasks"], item["activeClaim"], review_state
        )
        item["currentTask"] = current
        item["nextStep"] = next_step
        item["statusHistory"] = []
        for row in connection.execute(
            "SELECT event_type,actor_kind,actor_id,payload_json,created_at FROM events "
            "WHERE work_item_id=? ORDER BY event_id", (work_item_id,),
        ):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            if (row["event_type"] == "WORK_ITEM_CREATED" or
                    row["event_type"].startswith("WORK_ITEM_MANAGEMENT_")):
                payload = {key: value for key, value in payload.items() if key != "management"}
            item["statusHistory"].append({
                "at": row["created_at"], "actorKind": row["actor_kind"],
                "actorId": row["actor_id"], "event": row["event_type"], "payload": payload,
            })
        if management is not None:
            management = dict(management)
            management.update({
                "state": item["state"], "queueState": item["queue_state"],
                "updatedAt": item["updated_at"], "progress": item["progress"],
                "currentTask": current, "nextStep": next_step,
                "statusHistory": item["statusHistory"],
            })
            current_tasks = {task["task_id"]: task for task in item["tasks"]}
            for contract in management.get("tasks", []):
                actual = current_tasks.get(contract["taskId"])
                if actual:
                    contract["status"] = actual["status"]
                    contract["evidence"] = json.loads(actual["evidence_json"])
                    contract["updatedAt"] = actual["updated_at"]
            item["management"] = management
        else:
            item["management"] = None
        return item
    finally:
        connection.close()


def list_work_items(database):
    connection = open_database(database)
    try:
        rows = [dict(row) for row in connection.execute(
            "SELECT * FROM work_items ORDER BY "
            "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, "
            "updated_at, work_item_id"
        )]
        for row in rows:
            row["humanGatePolicy"] = row.get("human_gate_policy", "MANUAL")
            tasks = [dict(task) for task in connection.execute(
                "SELECT * FROM tasks WHERE work_item_id=? ORDER BY seq", (row["work_item_id"],)
            )]
            row["progress"] = _progress(tasks, _management_from_events(connection, row["work_item_id"]))
            active = connection.execute(
                "SELECT * FROM claims WHERE work_item_id=? AND status='ACTIVE'", (row["work_item_id"],)
            ).fetchone()
            current, next_step = _current_and_next(row, tasks, dict(active) if active else None,
                                                    _review_projection(connection, row["work_item_id"]))
            row["currentTask"], row["nextStep"] = current, next_step
        return rows
    finally:
        connection.close()


def _align_management_with_item(connection, item, management, now, allow_task_amendment=False):
    normalized = _normalize_management(
        item["work_item_id"], item["work_item_type"], item["title"], item["mode"],
        item["priority"], management, now,
    )
    database_tasks = [dict(row) for row in connection.execute(
        "SELECT * FROM tasks WHERE work_item_id=? ORDER BY seq", (item["work_item_id"],)
    )]
    if ((not allow_task_amendment and len(database_tasks) != len(normalized["tasks"])) or
            len(normalized["tasks"]) < len(database_tasks)):
        raise LiteError("management tasks do not match existing WorkItem")
    task_changes = []
    for actual, contract in zip(database_tasks, normalized["tasks"][:len(database_tasks)]):
        if (actual["task_id"], actual["seq"], actual["owner_role"]) != (
            contract["taskId"], contract["seq"], contract["ownerRole"]
        ):
            raise LiteError("management task identity does not match existing WorkItem")
        if allow_task_amendment:
            task_changes.append({"kind": "UPDATE", "taskId": actual["task_id"],
                                 "title": contract["title"],
                                 "required": contract["required"]})
            actual.update({"title": contract["title"],
                           "required": int(contract["required"]),
                           "updated_at": now})
        elif bool(actual["required"]) != contract["required"]:
            raise LiteError("management task required flag does not match existing WorkItem")
        contract["status"] = actual["status"]
        if not allow_task_amendment:
            contract["title"] = actual["title"]
    if allow_task_amendment:
        for contract in normalized["tasks"][len(database_tasks):]:
            task_changes.append({"kind": "INSERT", "taskId": contract["taskId"],
                                 "seq": contract["seq"],
                                 "title": contract["title"],
                                 "ownerRole": contract["ownerRole"],
                                 "required": contract["required"]})
            contract["status"] = "NOT_STARTED"
            database_tasks.append({
                "task_id": contract["taskId"], "work_item_id": item["work_item_id"],
                "seq": contract["seq"], "title": contract["title"],
                "owner_role": contract["ownerRole"], "status": "NOT_STARTED",
                "required": int(contract["required"]), "evidence_json": "[]",
                "created_at": now, "updated_at": now,
            })
    normalized.update({
        "createdAt": item["created_at"], "updatedAt": now, "state": item["state"],
        "queueState": item["queue_state"],
    })
    active = connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? AND status='ACTIVE'", (item["work_item_id"],)
    ).fetchone()
    current, next_step = _current_and_next(
        item, database_tasks, dict(active) if active else None,
        _review_projection(connection, item["work_item_id"]),
    )
    normalized["currentTask"], normalized["nextStep"] = current, next_step
    normalized["statusHistory"] = [{
        "at": item["created_at"], "actor": "SYSTEM", "event": "LEGACY_HISTORY_PRESERVED"
    }]
    return normalized, task_changes


def backfill_management(database, work_item_id, agent_id, management, basis, request_id=None):
    if not isinstance(basis, str) or not basis.strip():
        raise LiteError("management backfill requires an authorization basis")
    request_id = request_id or _id("management-backfill")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        if _management_from_events(connection, work_item_id) is not None:
            raise LiteError("management envelope already exists")
        claim = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, agent_id),
        ).fetchone()
        if claim is None or claim["role"] not in ("PLANNER", "IMPLEMENTER"):
            raise LiteError("management backfill requires active Planner or Implementer claim")
        now = _now()
        normalized, unused_changes = _align_management_with_item(
            connection, item, management, now
        )
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        intent = {
            "operation": "BACKFILL_MANAGEMENT", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "AGENT", "actorId": agent_id,
            "now": now, "taskChanges": (),
            "payload": {"management": normalized, "basis": basis},
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_management(snapshot, intent),
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def amend_management(database, work_item_id, human_id, management, reason, request_id=None):
    if not isinstance(reason, str) or not reason.strip():
        raise LiteError("management amendment requires a reason")
    request_id = request_id or _id("management-amend")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        previous = _management_from_events(connection, work_item_id)
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        if previous is None:
            raise LiteError("management envelope must be backfilled before amendment")
        now = _now()
        normalized, task_changes = _align_management_with_item(
            connection, item, management, now, allow_task_amendment=True
        )
        protected = (
            "scope", "outOfScope", "authorization", "safetyConstraints", "tasks",
            "acceptance", "closure", "progressMethod", "stages",
        )
        changes = {
            key: {"before": previous.get(key), "after": normalized.get(key)}
            for key in protected if previous.get(key) != normalized.get(key)
        }
        if not changes:
            raise LiteError("management amendment has no changes")
        intent = {
            "operation": "AMEND_MANAGEMENT", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN", "actorId": human_id,
            "now": now, "taskChanges": task_changes,
            "payload": {"management": normalized, "changes": changes,
                        "reason": reason, "authorizedBy": human_id},
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_management(snapshot, intent),
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _validate_orchestrator_fence(connection, work_item_id, orchestrator_id,
                                 orchestrator_generation, now):
    """Authorize a new Agent dispatch without coupling in-flight Agent work.

    Databases without the extension and WorkItems with zero lease history retain
    the legacy no-fence path.  After the first lease, omission is intentionally
    not a bypass: the exact current ACTIVE and unexpired fence is mandatory.
    """
    supplied = (orchestrator_id is not None, orchestrator_generation is not None)
    if supplied[0] != supplied[1]:
        raise LiteError("orchestrator-id and orchestrator-generation must be supplied together")
    try:
        from .orchestrator import schema_state
        state = schema_state(connection)
    except sqlite3.Error:
        state = "ABSENT"
    if state == "INVALID":
        raise LiteError("orchestrator schema is invalid")
    if state == "ABSENT":
        if any(supplied):
            raise LiteError("orchestrator fence requires installed schema")
        return
    history = connection.execute(
        "SELECT count(*) FROM orchestrator_leases WHERE work_item_id=?",
        (work_item_id,),
    ).fetchone()[0]
    if history == 0:
        if any(supplied):
            raise LiteError("orchestrator fence has no lease history")
        return
    if not all(supplied):
        raise LiteError("current orchestrator fence is required")
    if type(orchestrator_generation) is not int or orchestrator_generation < 1:
        raise LiteError("orchestrator-generation must be positive")
    active = connection.execute(
        "SELECT 1 FROM orchestrator_leases WHERE work_item_id=? AND orchestrator_id=? "
        "AND generation=? AND status='ACTIVE' AND expires_at>?",
        (work_item_id, orchestrator_id, orchestrator_generation, now),
    ).fetchone()
    if active is None:
        raise LiteError("stale or inactive orchestrator fence")


def acquire_claim(database, work_item_id, task_id, agent_id, role, expires_at,
                  request_id=None, session_id=None, usage_provider=None, model=None,
                  sessions_root=None, orchestrator_id=None,
                  orchestrator_generation=None, usage_policy="BEST_EFFORT"):
    if usage_policy not in USAGE_POLICIES:
        raise LiteError("usage policy is invalid")
    usage_values = (session_id, usage_provider, model)
    if any(value is not None for value in usage_values) and not all(usage_values):
        raise LiteError("session-id, usage-provider, and model must be supplied together")
    baseline = identity = None
    if all(usage_values) and usage_policy == "BEST_EFFORT":
        try:
            from .usage import prepare_interval_boundary
            baseline, identity, adapter = prepare_interval_boundary(
                database, usage_provider, session_id, sessions_root)
        except Exception as exc:
            raise LiteError("usage binding baseline failed: {0}".format(exc))
    request_id = request_id or _id("claim")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        now = _now()
        if not isinstance(expires_at, str) or expires_at <= now:
            raise LiteError("claim expiry must be in the future")
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        _validate_orchestrator_fence(
            connection, work_item_id, orchestrator_id, orchestrator_generation, now
        )
        stale = connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE' "
            "AND expires_at<=? LIMIT 1", (work_item_id, now),
        ).fetchone()
        if stale is not None:
            raise LiteError("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED")
        reconciled = []
        active = connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone()
        if active:
            raise LiteError("WorkItem is already claimed")
        if item["queue_state"] not in ("CLAIMABLE", "CLAIMED"):
            raise LiteError("WorkItem is not claimable")
        if item["current_role"] != role:
            raise LiteError("WorkItem is not assigned to this role")
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=? AND work_item_id=?", (task_id, work_item_id)
        ).fetchone()
        if task is None or task["owner_role"] != role:
            raise LiteError("task role does not match")
        if task["status"] != "NOT_STARTED":
            raise LiteError("claim requires an exact NOT_STARTED task")
        if role == "REVIEWER" and item["state"] == "PLAN_REVIEW_PENDING":
            artifact_head = _plan_artifact_head(connection, work_item_id)
            if artifact_head is not None:
                if artifact_head.get("editorAgentId") == agent_id:
                    raise LiteError("latest plan artifact editor cannot review own revision")
                used = connection.execute(
                    "SELECT 1 FROM reviews WHERE work_item_id=? AND stage='PLAN' "
                    "AND reviewer_agent_id=? LIMIT 1", (work_item_id, agent_id),
                ).fetchone()
                if used is not None:
                    raise LiteError("opt-in PLAN requires a fresh Reviewer for every round")
        generation = connection.execute(
            "SELECT coalesce(max(generation),0)+1 FROM claims WHERE work_item_id=?", (work_item_id,)
        ).fetchone()[0]
        claim_id = _id("claim")
        intent = {
            "operation": "CLAIM_ROLE", "workItemId": work_item_id,
            "taskId": task_id, "claimId": claim_id, "actorKind": "AGENT",
            "actorId": agent_id, "role": role, "generation": generation,
            "expiresAt": expires_at, "requestId": request_id, "now": now,
            "orchestratorId": orchestrator_id,
            "orchestratorGeneration": orchestrator_generation,
        }
        plan = workflow_kernel.plan_claim_role(snapshot, intent)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        if all(usage_values) and usage_policy == "BEST_EFFORT":
            try:
                from .usage import record_binding
                claim = connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
                record_binding(connection, claim, usage_provider, session_id, model,
                               baseline=baseline,
                               adapter_version=adapter.adapter_version,
                               parser_version=adapter.parser_version,
                               role_observed=identity.get("agent_role") if identity else None)
            except Exception as exc:
                raise LiteError("usage binding failed: {0}".format(exc))
        connection.commit()
        result = {"claimId": claim_id, "generation": generation}
        if usage_policy == "OFF":
            result["usageStatus"] = "DISABLED"
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _usage_sync_boundary(database, work_item_id, usage_policy="BEST_EFFORT"):
    if usage_policy == "OFF":
        return {"status": "DISABLED", "policy": "OFF", "writes": 0}
    if usage_policy not in USAGE_POLICIES:
        raise LiteError("usage policy is invalid")
    try:
        from .usage import best_effort_sync
        return best_effort_sync(database, work_item_id)
    except Exception:
        return {"status": "coverage-gap", "reasonCode": "SYNC_IMPORT_FAILED"}


def release_claim(database, work_item_id, agent_id, request_id=None,
                  usage_policy="BEST_EFFORT"):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    request_id = request_id or _id("release")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        claim = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone()
        if claim is None or claim["agent_id"] != agent_id:
            raise LiteError("active claim is not owned by agent")
        if connection.execute(
            "SELECT 1 FROM repository_locks WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, agent_id),
        ).fetchone():
            raise LiteError("release repository lock before WorkItem claim")
        queue = item["queue_state"] if item["queue_state"] in ("WAITING_HUMAN", "HELD", "BLOCKED") else "CLAIMABLE"
        intent = {
            "operation": "RELEASE_ROLE", "workItemId": work_item_id,
            "taskId": claim["task_id"], "claimId": claim["claim_id"],
            "actorKind": "AGENT", "actorId": agent_id,
            "generation": claim["generation"], "requestId": request_id,
            "now": now, "queue": queue, "role": item["current_role"],
        }
        plan = workflow_kernel.plan_release_role(snapshot, intent)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def acquire_repository_lock(database, work_item_id, repository_key, agent_id, expires_at,
                            request_id=None):
    request_id = request_id or _id("repo-lock")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        claim = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, agent_id),
        ).fetchone()
        if claim is None or claim["role"] not in ("PLANNER", "IMPLEMENTER"):
            raise LiteError("repository lock requires Planner or Implementer claim")
        if not isinstance(expires_at, str) or expires_at <= now:
            raise LiteError("repository writer expiry must be in the future")
        stale = connection.execute(
            "SELECT lock_id,work_item_id,agent_id,generation,expires_at "
            "FROM repository_locks WHERE repository_key=? AND status='ACTIVE' "
            "AND expires_at<=?", (repository_key, now),
        ).fetchall()
        if stale:
            raise LiteError("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED")
        if connection.execute(
            "SELECT 1 FROM repository_locks WHERE repository_key=? AND status='ACTIVE'",
            (repository_key,),
        ).fetchone():
            raise LiteError("repository already has an active writer")
        generation = connection.execute(
            "SELECT coalesce(max(generation),0)+1 FROM repository_locks WHERE repository_key=?",
            (repository_key,),
        ).fetchone()[0]
        lock_id = _id("repo")
        intent = {
            "operation": "ACQUIRE_WRITER", "workItemId": work_item_id,
            "repositoryKey": repository_key, "lockId": lock_id,
            "actorKind": "AGENT", "actorId": agent_id,
            "generation": generation, "expiresAt": expires_at,
            "requestId": request_id, "now": now,
        }
        plan = workflow_kernel.plan_writer(snapshot, intent, True)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        connection.commit()
        return {"lockId": lock_id, "generation": generation}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_repository_lock(database, work_item_id, repository_key, agent_id,
                            request_id=None):
    request_id = request_id or _id("repo-release")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        lock = connection.execute(
            "SELECT * FROM repository_locks WHERE repository_key=? AND work_item_id=? "
            "AND agent_id=? AND status='ACTIVE'",
            (repository_key, work_item_id, agent_id),
        ).fetchone()
        if lock is None:
            raise LiteError("active repository lock is not owned by agent")
        intent = {
            "operation": "RELEASE_WRITER", "workItemId": work_item_id,
            "repositoryKey": repository_key, "lockId": lock["lock_id"],
            "actorKind": "AGENT", "actorId": agent_id,
            "generation": lock["generation"], "requestId": request_id,
            "now": now,
        }
        plan = workflow_kernel.plan_writer(snapshot, intent, False)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_task_status(database, work_item_id, task_id, agent_id, status, evidence=None,
                    request_id=None, usage_policy="BEST_EFFORT"):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    if status not in ("IN_PROGRESS", "BLOCKED", "WAITING_ACCEPTANCE", "COMPLETED", "CANCELLED"):
        raise LiteError("unsupported task status")
    request_id = request_id or _id("task")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        claim = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND task_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, task_id, agent_id),
        ).fetchone()
        if claim is None:
            raise LiteError("task mutation requires its active claim")
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope must be backfilled before task mutation")
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=? AND work_item_id=?", (task_id, work_item_id)
        ).fetchone()
        if task["owner_role"] == "REVIEWER":
            raise LiteError("REVIEWER_TASK_MANAGED_BY_CLAIM")
        if task["status"] == "IN_PROGRESS" and status == "IN_PROGRESS":
            connection.rollback()
            return
        if status in ("COMPLETED", "WAITING_ACCEPTANCE"):
            raise LiteError("USE_WORKFLOW_ADVANCE")
        allowed = {
            "NOT_STARTED": {"IN_PROGRESS"},
            "IN_PROGRESS": {"BLOCKED", "CANCELLED"},
            "WAITING_ACCEPTANCE": set(),
            "BLOCKED": set(), "COMPLETED": set(), "CANCELLED": set(),
        }
        if status not in allowed[task["status"]]:
            raise LiteError("illegal task status transition")
        evidence = evidence or []
        if status in ("BLOCKED", "WAITING_ACCEPTANCE", "COMPLETED", "CANCELLED") and not evidence:
            raise LiteError("task status requires evidence")
        if status == "CANCELLED" and task["required"]:
            raise LiteError("required task cannot be cancelled without a management amendment")
        if status == "IN_PROGRESS" and connection.execute(
            "SELECT 1 FROM tasks WHERE work_item_id=? AND task_id<>? AND status='IN_PROGRESS'",
            (work_item_id, task_id),
        ).fetchone():
            raise LiteError("WorkItem already has an IN_PROGRESS task")
        try:
            previous_evidence = json.loads(task["evidence_json"])
        except (TypeError, ValueError):
            previous_evidence = []
        full_evidence = previous_evidence + list(evidence)
        writers = [{"lockId": row["lock_id"], "generation": row["generation"]}
                   for row in connection.execute(
                       "SELECT lock_id,generation FROM repository_locks "
                       "WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
                       (work_item_id, agent_id))]
        intent = {
            "operation": "BLOCK", "workItemId": work_item_id,
            "taskId": task_id, "fromStatus": task["status"],
            "status": status, "evidence": full_evidence,
            "claimId": claim["claim_id"],
            "claimGeneration": claim["generation"], "writers": writers,
            "role": task["owner_role"], "actorKind": "AGENT",
            "actorId": agent_id, "requestId": request_id, "now": now,
        }
        plan = workflow_kernel.plan_task_status(snapshot, intent)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _active_claim(connection, work_item_id, role, agent_id):
    row = connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? AND role=? AND agent_id=? AND status='ACTIVE'",
        (work_item_id, role, agent_id),
    ).fetchone()
    if row is None:
        raise LiteError("transition requires active role claim")
    return row


def _latest_submission_baseline(connection, work_item_id):
    rows = connection.execute(
        "SELECT payload_json FROM events WHERE work_item_id=? AND event_type='SUBMIT_IMPLEMENTATION' "
        "ORDER BY event_id DESC", (work_item_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if isinstance(payload.get("qualityBaseline"), dict):
            return payload["qualityBaseline"]
    return None


def _validate_quality_baseline(connection, work_item_id, baseline):
    if not isinstance(baseline, dict):
        raise LiteError("submit_implementation requires a quality baseline")
    list_fields = (
        "passedAcceptance", "tests", "modifiedScope", "knownNonBlockingIssues",
        "addressedFindingIds", "complexityChanges", "regressions", "acceptanceRegressions",
        "closureEvidence",
    )
    if any(not isinstance(baseline.get(field), list) for field in list_fields):
        raise LiteError("quality baseline list fields are incomplete")
    if not baseline["passedAcceptance"] or not baseline["tests"] or not baseline["modifiedScope"]:
        raise LiteError("quality baseline requires passed acceptance, tests and modified scope")
    if baseline.get("testsWeakened") is not False:
        raise LiteError("tests must not be deleted, skipped, relaxed or weakened")
    if baseline["regressions"] or baseline["acceptanceRegressions"]:
        raise LiteError("quality regression stops automatic implementation")
    if baseline.get("planDeviation"):
        raise LiteError("plan deviation must be reported instead of submitted")
    tests = baseline["tests"]
    if any(not isinstance(test, dict) or not test.get("command") or test.get("result") != "PASS"
           for test in tests):
        raise LiteError("all recorded tests must have a command and PASS result")
    acceptance_evidence = {}
    for entry in baseline["passedAcceptance"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("evidence"):
            raise LiteError("passedAcceptance requires id and evidence")
        acceptance_evidence[entry["id"]] = entry["evidence"]
    closure_evidence = {}
    for entry in baseline["closureEvidence"]:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("evidence"):
            raise LiteError("closureEvidence requires id and evidence")
        closure_evidence[entry["id"]] = entry["evidence"]
    management = _management_from_events(connection, work_item_id)
    required_acceptance = {entry["id"] for entry in management["acceptance"]}
    required_closure = {entry["id"] for entry in management["closure"]}
    if not required_acceptance.issubset(acceptance_evidence):
        raise LiteError("quality baseline does not cover WorkItem acceptance")
    if not required_closure.issubset(closure_evidence):
        raise LiteError("quality baseline does not cover WorkItem closure evidence")
    open_ids = {
        finding["id"] for finding in _review_projection(connection, work_item_id)["IMPLEMENTATION"]["openFindings"]
    }
    addressed = set(baseline["addressedFindingIds"])
    if not addressed.issubset(open_ids):
        raise LiteError("addressedFindingIds may only reference open implementation Findings")
    if open_ids and not addressed:
        raise LiteError("implementation revision must identify addressed open Findings")
    valid_trace = required_acceptance | open_ids
    for change in baseline["complexityChanges"]:
        if not isinstance(change, dict) or not change.get("kind") or not change.get("name"):
            raise LiteError("complexity change requires kind and name")
        traces = change.get("traceTo")
        traces = [traces] if isinstance(traces, str) else traces
        if not isinstance(traces, list) or not traces or not set(traces).intersection(valid_trace):
            raise LiteError("complexity change is not traceable to acceptance or an open Finding")
    previous = _latest_submission_baseline(connection, work_item_id)
    if previous:
        previous_ids = {entry["id"] for entry in previous["passedAcceptance"]}
        if not previous_ids.issubset(acceptance_evidence):
            raise LiteError("previously passed acceptance regressed")
    normalized = dict(baseline)
    normalized["passedAcceptance"] = baseline["passedAcceptance"]
    return normalized


def _validate_revision_submission(connection, work_item_id, stage, submission):
    projection = _review_projection(connection, work_item_id)[stage]
    open_ids = {finding["id"] for finding in projection["openFindings"]}
    if not open_ids:
        if submission and submission.get("complexityChanges"):
            raise LiteError("new complexity requires acceptance or an open Finding")
        return
    if not isinstance(submission, dict):
        raise LiteError("revision submission must identify addressed Findings")
    addressed = submission.get("addressedFindingIds", [])
    if not isinstance(addressed, list) or not set(addressed).issubset(open_ids) or not addressed:
        raise LiteError("revision may only address open Findings")
    for change in submission.get("complexityChanges", []):
        traces = change.get("traceTo", []) if isinstance(change, dict) else []
        traces = [traces] if isinstance(traces, str) else traces
        if not set(traces).intersection(open_ids):
            raise LiteError("revision complexity must trace to an open Finding")


def _plan_artifact_head(connection, work_item_id):
    rows = connection.execute(
        "SELECT payload_json FROM events WHERE work_item_id=? "
        "AND event_type='PLAN_ARTIFACT_HEAD' ORDER BY event_id DESC",
        (work_item_id,),
    ).fetchall()
    for row in rows:
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("protocolVersion") == PLAN_ARTIFACT_PROTOCOL:
            return value
    return None


def _regular_plan_path(project_root, relative_path):
    if (not isinstance(project_root, str) or not project_root or
            not isinstance(relative_path, str) or not relative_path or
            os.path.isabs(relative_path) or "\\" in relative_path):
        raise LiteError("plan artifact requires a project-relative path")
    normalized = os.path.normpath(relative_path)
    if normalized in ("", ".", "..") or normalized.startswith(".." + os.sep):
        raise LiteError("plan artifact escapes project root")
    root = os.path.realpath(os.path.abspath(project_root))
    absolute = os.path.abspath(os.path.join(root, normalized))
    try:
        if os.path.commonpath((root, absolute)) != root:
            raise LiteError("plan artifact escapes project root")
    except ValueError:
        raise LiteError("plan artifact escapes project root")
    cursor = root
    for component in normalized.split(os.sep):
        cursor = os.path.join(cursor, component)
        if os.path.islink(cursor):
            raise LiteError("plan artifact contains a symbolic link")
    if not os.path.isfile(absolute):
        raise LiteError("plan artifact is not a regular file")
    return root, normalized.replace(os.sep, "/"), absolute


def _artifact_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _next_plan_artifact(connection, work_item_id, agent_id, artifact):
    if not isinstance(artifact, dict) or set(artifact) != {"projectRoot", "path"}:
        raise LiteError("plan artifact input is invalid")
    unused_root, relative, absolute = _regular_plan_path(
        artifact["projectRoot"], artifact["path"]
    )
    head = _plan_artifact_head(connection, work_item_id)
    if head and head.get("path") != relative:
        raise LiteError("plan artifact path cannot change across revisions")
    digest = _artifact_sha(absolute)
    if head and head.get("sha256") == digest:
        raise LiteError("plan artifact revision must change bytes")
    return {
        "protocolVersion": PLAN_ARTIFACT_PROTOCOL, "policy": "REVIEWER_AMEND",
        "path": relative, "revision": (head.get("revision", 0) + 1 if head else 1),
        "sha256": digest, "editorAgentId": agent_id,
    }


def transition(database, work_item_id, action, agent_id, request_id=None,
               local_tests_passed=False, submission=None, quality_baseline=None,
               usage_policy="BEST_EFFORT", plan_artifact=None,
               candidate_id=None, candidate_fingerprint=None):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    request_id = request_id or _id(action)
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope must be backfilled before transition")
        artifact_event = None
        if action == "submit_plan":
            active_claim = _active_claim(connection, work_item_id, "PLANNER", agent_id)
            if item["state"] != "DRAFT":
                raise LiteError("submit_plan requires DRAFT")
            pending = connection.execute(
                "SELECT count(*) FROM tasks WHERE work_item_id=? AND owner_role='PLANNER' "
                "AND required=1 AND status NOT IN ('IN_PROGRESS','COMPLETED')",
                (work_item_id,),
            ).fetchone()[0]
            if pending:
                raise LiteError("planning tasks are incomplete")
            _validate_revision_submission(connection, work_item_id, "PLAN", submission)
            artifact_head = _plan_artifact_head(connection, work_item_id)
            if artifact_head is not None and plan_artifact is None:
                raise LiteError("opt-in plan revisions require --plan-artifact")
            artifact_event = (_next_plan_artifact(
                connection, work_item_id, agent_id, plan_artifact
            ) if plan_artifact is not None else None)
            new_state, queue, role = "PLAN_REVIEW_PENDING", "CLAIMABLE", "REVIEWER"
            release_after = True
        elif action == "start_implementation":
            active_claim = _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
            if item["state"] != "PLAN_REVIEW_APPROVED":
                raise LiteError("implementation requires approved plan")
            new_state, queue, role = "IMPLEMENTING", "CLAIMED", "IMPLEMENTER"
            release_after = False
        elif action == "submit_implementation":
            active_claim = _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
            if item["state"] != "IMPLEMENTING":
                raise LiteError("submit_implementation requires IMPLEMENTING")
            pending = connection.execute(
                "SELECT count(*) FROM tasks WHERE work_item_id=? AND owner_role='IMPLEMENTER' "
                "AND required=1 AND status NOT IN ('IN_PROGRESS','COMPLETED')",
                (work_item_id,),
            ).fetchone()[0]
            if pending or not local_tests_passed:
                raise LiteError("implementation tasks and local tests must pass")
            quality_baseline = _validate_quality_baseline(
                connection, work_item_id, quality_baseline
            )
            from .candidate import release_submission_candidate
            release_candidate = release_submission_candidate(connection, work_item_id)
            if release_candidate is not None:
                if (candidate_id != release_candidate["candidateId"] or
                        candidate_fingerprint != release_candidate["candidateFingerprint"]):
                    raise LiteError("Release submission requires the exact FROZEN+BUILT candidate")
                modified = sorted(quality_baseline.get("modifiedScope", []))
                if modified != sorted(release_candidate["changedPaths"]):
                    raise LiteError("Release quality modifiedScope must equal the frozen allowlist")
            elif candidate_id is not None or candidate_fingerprint is not None:
                raise LiteError("ordinary WorkItem must not provide candidate flags")
            new_state, queue, role = "IMPLEMENTATION_COMPLETED", "CLAIMABLE", "REVIEWER"
            release_after = True
        else:
            raise LiteError("unknown transition")
        payload = {"from": item["state"], "to": new_state}
        if submission is not None:
            payload["submission"] = submission
        if quality_baseline is not None:
            payload["qualityBaseline"] = quality_baseline
        if action == "submit_implementation" and release_candidate is not None:
            payload["reviewedCandidate"] = release_candidate
        if artifact_event is not None:
            payload["planArtifact"] = artifact_event
        writers = [{"lockId": row["lock_id"], "generation": row["generation"]}
                   for row in connection.execute(
                       "SELECT lock_id,generation FROM repository_locks "
                       "WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
                       (work_item_id, agent_id))]
        intent = {
            "operation": {"submit_plan": "SUBMIT_PLAN",
                          "start_implementation": "BEGIN_IMPLEMENTATION",
                          "submit_implementation": "SUBMIT_IMPLEMENTATION"}[action],
            "workItemId": work_item_id, "taskId": active_claim["task_id"],
            "claimId": active_claim["claim_id"],
            "claimGeneration": active_claim["generation"], "writers": writers,
            "state": new_state, "queue": queue, "role": role,
            "eventType": action.upper(), "payload": payload,
            "artifact": artifact_event, "actorKind": "AGENT",
            "actorId": agent_id, "requestId": request_id, "now": now,
        }
        plan = workflow_kernel.plan_legacy_transition(snapshot, intent)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _decoded_review(row):
    try:
        value = json.loads(row["summary"])
    except (TypeError, ValueError):
        value = None
    if (isinstance(value, dict) and
            value.get("protocolVersion") in ("AWB-REVIEW-v1", REVIEW_V2_PROTOCOL)):
        return value
    # Public b3-b6 accepted this closed pre-protocol JSON shape.  Preserve every
    # verifiable field and infer only the stage that was stored separately in
    # the review row.  Unknown near-matches remain LEGACY/round=None and are
    # rejected by the invariant validator instead of being blanket-skipped.
    legacy_required = {
        "result", "round", "reviewerMode", "findings",
        "resolvedFindingIds", "nonBlockingSuggestions",
    }
    if (isinstance(value, dict) and legacy_required.issubset(value) and
            value.get("result") in ("PASS", "REVISE", "BLOCKED") and
            type(value.get("round")) is int and
            value.get("reviewerMode") in ("ORDINARY", "CONVERGENCE") and
            isinstance(value.get("findings"), list) and
            isinstance(value.get("resolvedFindingIds"), list) and
            isinstance(value.get("nonBlockingSuggestions"), list)):
        normalized = dict(value)
        normalized.update({
            "protocolVersion": "AWB-LEGACY-REVIEW-v1",
            "stage": ("IMPLEMENTATION" if row["stage"] == "FINAL" else "PLAN"),
        })
        return normalized
    return {
        "protocolVersion": "LEGACY", "stage": "IMPLEMENTATION" if row["stage"] == "FINAL" else "PLAN",
        "result": "PASS" if row["decision"] == "APPROVED" else "REVISE",
        "reviewerMode": "ORDINARY", "round": None, "findings": [],
        "resolvedFindingIds": [], "nonBlockingSuggestions": [], "legacySummary": row["summary"],
    }


def _review_history(connection, work_item_id, stage):
    return [(dict(row), _decoded_review(row)) for row in connection.execute(
        "SELECT * FROM reviews WHERE work_item_id=? AND stage=? ORDER BY created_at,rowid",
        (work_item_id, stage),
    )]


def _review_stage_projection(history):
    open_findings = {}
    resolved = set()
    ordinary = 0
    convergence = 0
    for row, review in history:
        if review.get("reviewerMode") == "CONVERGENCE":
            convergence += 1
        else:
            ordinary += 1
        for finding_id in review.get("resolvedFindingIds", []):
            open_findings.pop(finding_id, None)
            resolved.add(finding_id)
        for finding in review.get("findings", []):
            if finding.get("status", "OPEN") == "OPEN":
                open_findings[finding["id"]] = finding
    latest = history[-1][1] if history else None
    next_step = None
    if (latest and latest.get("round") == 3 and
            latest.get("result") in ("REVISE", "REVISE_TO_PLANNER", "AMENDED")):
        next_step = "run the single convergence review"
    elif (latest and latest.get("round") == 4 and
          latest.get("result") in ("CONVERGENCE_REVISE", "AMENDED")):
        next_step = ("run the final ordinary review" if latest.get("result") == "AMENDED" else
                     "perform the single minimal convergence revision")
    return {
        "ordinaryRoundsUsed": ordinary, "convergenceUsed": bool(convergence),
        "totalRoundsUsed": len(history), "latestResult": latest.get("result") if latest else None,
        "openFindings": list(open_findings.values()), "resolvedFindingIds": sorted(resolved),
        "nextStep": next_step,
    }


def _review_projection(connection, work_item_id):
    plan = _review_stage_projection(_review_history(connection, work_item_id, "PLAN"))
    implementation = _review_stage_projection(_review_history(connection, work_item_id, "FINAL"))
    next_step = implementation.get("nextStep") or plan.get("nextStep")
    return {"PLAN": plan, "IMPLEMENTATION": implementation, "nextStep": next_step}


def _finding_error(finding, expected_stage):
    required = ("id", "stage", "violatedContract", "evidence", "impact", "closeCondition")
    if not isinstance(finding, dict):
        return "finding is not an object"
    for field in required:
        value = finding.get(field)
        if value is None or value == "" or value == [] or value == {}:
            return "finding is missing {0}".format(field)
    if finding["stage"] != expected_stage:
        return "finding stage does not match review stage"
    return None


def _normalize_review(connection, work_item_id, stage, decision, summary):
    history = _review_history(connection, work_item_id, stage)
    round_number = len(history) + 1
    if round_number > 5:
        raise LiteError("review round limit exhausted; human decision required")
    expected_stage = "PLAN" if stage == "PLAN" else "IMPLEMENTATION"
    expected_mode = "CONVERGENCE" if round_number == 4 else "ORDINARY"
    artifact_head = _plan_artifact_head(connection, work_item_id) if stage == "PLAN" else None
    opt_in = artifact_head is not None
    if isinstance(summary, dict):
        incoming = dict(summary)
    else:
        try:
            incoming = json.loads(summary)
        except (TypeError, ValueError):
            incoming = None
    if not isinstance(incoming, dict):
        incoming = {
            "result": "PASS" if decision == "APPROVED" else "REVISE",
            "reviewerMode": expected_mode, "findings": [], "resolvedFindingIds": [],
            "nonBlockingSuggestions": ([] if decision == "APPROVED" else [{
                "source": "INCOMPLETE_REVIEW", "content": str(summary),
                "reason": "substantive review lacked a complete blocking Finding",
            }]),
            "summary": str(summary),
        }
    result = incoming.get("result", "PASS" if decision == "APPROVED" else "REVISE")
    mode = incoming.get("reviewerMode", expected_mode)
    if mode != expected_mode:
        raise LiteError("reviewerMode does not match the persisted review round")
    if opt_in:
        if round_number <= 3:
            allowed = {"PASS", "REVISE_TO_PLANNER", "BLOCKED"}
        elif round_number == 4:
            allowed = {"PASS", "WAITING_HUMAN", "BLOCKED"}
        else:
            allowed = {"PASS", "WAITING_HUMAN"}
    else:
        allowed = ({"PASS", "CONVERGENCE_REVISE", "WAITING_HUMAN", "BLOCKED"}
                   if mode == "CONVERGENCE" else {"PASS", "REVISE", "BLOCKED"})
    if result not in allowed:
        raise LiteError("review result is invalid for this round")
    previous = _review_stage_projection(history)
    prior_open = {finding["id"]: finding for finding in previous["openFindings"]}
    prior_resolved = set(previous["resolvedFindingIds"])
    resolved_ids = incoming.get("resolvedFindingIds", [])
    if not isinstance(resolved_ids, list) or len(resolved_ids) != len(set(resolved_ids)):
        raise LiteError("resolvedFindingIds must be a unique list")
    if any(finding_id not in prior_open for finding_id in resolved_ids):
        raise LiteError("resolvedFindingIds may only close open findings")
    valid = []
    suggestion_input = incoming.get("nonBlockingSuggestions", [])
    suggestions = list(suggestion_input) if isinstance(suggestion_input, list) else [{
        "source": "INVALID_SUGGESTIONS", "content": suggestion_input,
        "reason": "nonBlockingSuggestions must be a list",
    }]
    finding_input = incoming.get("findings", [])
    if not isinstance(finding_input, list):
        suggestions.append({"source": "INVALID_FINDINGS", "content": finding_input,
                            "reason": "findings must be a list"})
        finding_input = []
    seen = set()
    for finding in finding_input:
        reason = _finding_error(finding, expected_stage)
        finding_id = finding.get("id") if isinstance(finding, dict) else None
        if finding_id in seen:
            reason = "finding id is duplicated"
        seen.add(finding_id)
        if reason is None and round_number >= 2:
            is_new_or_reopened = finding_id not in prior_open
            if is_new_or_reopened:
                if finding.get("origin") not in ("REVISION_REGRESSION", "NEWLY_AVAILABLE_EVIDENCE"):
                    reason = "later finding lacks an allowed origin"
                elif not finding.get("priorUnavailableReason"):
                    reason = "later finding lacks priorUnavailableReason"
            if round_number == 5 and finding_id not in prior_open:
                reason = "round 5 cannot introduce an unrelated finding"
        if reason is None:
            normalized_finding = dict(finding)
            normalized_finding["status"] = "OPEN"
            normalized_finding.setdefault("origin", "INITIAL")
            normalized_finding.setdefault("priorUnavailableReason", "not applicable")
            valid.append(normalized_finding)
        else:
            suggestions.append({"source": "INVALID_FINDING", "reason": reason, "content": finding})
    post_review_open = (set(prior_open) - set(resolved_ids)) | {
        finding["id"] for finding in valid
    }
    blocking_result = result in (
        "REVISE", "REVISE_TO_PLANNER", "CONVERGENCE_REVISE", "BLOCKED"
    )
    if blocking_result and not post_review_open:
        result = "PASS" if result != "BLOCKED" else "WAITING_HUMAN"
    if result == "PASS" and post_review_open:
        raise LiteError("PASS cannot leave an open blocking Finding")
    if round_number == 5 and result == "REVISE":
        # The result is retained for audit, but runtime routes to a human instead of round 6.
        pass
    if opt_in:
        reviewed = incoming.get("reviewedArtifact")
        expected_artifact = {key: artifact_head[key] for key in (
            "path", "revision", "sha256", "editorAgentId"
        )}
        if reviewed != expected_artifact:
            raise LiteError("PLAN review must identify the exact current artifact")
    normalized = {
        "protocolVersion": (REVIEW_V2_PROTOCOL if opt_in else "AWB-REVIEW-v1"),
        "stage": expected_stage,
        "round": round_number, "reviewerMode": mode, "result": result,
        "findings": valid, "resolvedFindingIds": resolved_ids,
        "nonBlockingSuggestions": suggestions,
        "summary": incoming.get("summary", ""),
    }
    if opt_in:
        normalized["reviewedArtifact"] = expected_artifact
        normalized["amendments"] = []
    if stage == "FINAL":
        submission = connection.execute(
            "SELECT payload_json FROM events WHERE work_item_id=? "
            "AND event_type='SUBMIT_IMPLEMENTATION' ORDER BY event_id DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        try:
            reviewed_candidate = json.loads(submission[0]).get("reviewedCandidate") if submission else None
        except (TypeError, ValueError):
            reviewed_candidate = None
        if reviewed_candidate is not None:
            if incoming.get("reviewedCandidate") != reviewed_candidate:
                raise LiteError("Release review must identify the exact submitted candidate")
            normalized["reviewedCandidate"] = reviewed_candidate
    return normalized


def _review_request_fingerprint(work_item_id, stage, reviewer_agent_id, decision, summary):
    return _sha(_json({
        "workItemId": work_item_id, "stage": stage, "reviewer": reviewer_agent_id,
        "decision": decision, "summary": summary,
    }))


def _quality_baseline_is_auto_safe(baseline, management):
    if not isinstance(baseline, dict):
        return False
    list_fields = (
        "passedAcceptance", "tests", "modifiedScope", "regressions",
        "acceptanceRegressions", "closureEvidence",
    )
    if any(not isinstance(baseline.get(field), list) for field in list_fields):
        return False
    acceptance = {
        entry.get("id") for entry in baseline["passedAcceptance"]
        if isinstance(entry, dict) and entry.get("id") and entry.get("evidence")
    }
    closure = {
        entry.get("id") for entry in baseline["closureEvidence"]
        if isinstance(entry, dict) and entry.get("id") and entry.get("evidence")
    }
    required_acceptance = {entry["id"] for entry in management.get("acceptance", [])}
    required_closure = {entry["id"] for entry in management.get("closure", [])}
    return bool(
        baseline["passedAcceptance"] and baseline["tests"] and
        baseline["modifiedScope"] and baseline["closureEvidence"] and
        required_acceptance.issubset(acceptance) and required_closure.issubset(closure) and
        baseline.get("testsWeakened") is False and not baseline.get("planDeviation") and
        not baseline["regressions"] and not baseline["acceptanceRegressions"] and
        all(isinstance(test, dict) and test.get("command") and test.get("result") == "PASS"
            for test in baseline["tests"])
    )


def _approved_gate_preconditions(connection, item, stage,
                                 pending_publication_postflight=False):
    expected = "PLAN_REVIEW_PENDING" if stage == "PLAN" else "IMPLEMENTATION_COMPLETED"
    if item["mode"] != "STANDARD" or item["state"] != expected:
        raise LiteError("gate stage is invalid")
    review = connection.execute(
        "SELECT * FROM reviews WHERE work_item_id=? AND stage=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", (item["work_item_id"], stage),
    ).fetchone()
    if review is None or _decoded_review(review)["result"] != "PASS":
        raise LiteError("approval requires approved Agent review")
    projection = _review_projection(connection, item["work_item_id"])
    projected_stage = "PLAN" if stage == "PLAN" else "IMPLEMENTATION"
    if projection[projected_stage]["openFindings"]:
        raise LiteError("approval requires no open {0} Findings".format(projected_stage.lower()))
    if stage == "FINAL":
        submission = connection.execute(
            "SELECT event_id,payload_json FROM events WHERE work_item_id=? "
            "AND event_type='SUBMIT_IMPLEMENTATION' ORDER BY event_id DESC LIMIT 1",
            (item["work_item_id"],),
        ).fetchone()
        try:
            release_candidate = json.loads(submission["payload_json"]).get("reviewedCandidate") if submission else None
        except (TypeError, ValueError):
            release_candidate = None
        if release_candidate is not None:
            ready = connection.execute(
                "SELECT event_id FROM events WHERE work_item_id=? AND event_type='PUBLICATION_READY' "
                "ORDER BY event_id DESC LIMIT 1", (item["work_item_id"],)
            ).fetchone()
            postflight = connection.execute(
                "SELECT event_id FROM events WHERE work_item_id=? "
                "AND event_type='PUBLICATION_POSTFLIGHT_ACCEPTED' ORDER BY event_id DESC LIMIT 1",
                (item["work_item_id"],),
            ).fetchone()
            if (ready is None or
                    (not pending_publication_postflight and
                     (postflight is None or postflight["event_id"] < ready["event_id"]))):
                raise LiteError("Release FINAL requires exact accepted publication postflight")
        pending = connection.execute(
            "SELECT count(*) FROM tasks WHERE work_item_id=? AND required=1 AND status<>'COMPLETED'",
            (item["work_item_id"],),
        ).fetchone()[0]
        if pending:
            raise LiteError("final approval requires all required tasks completed")
        baseline = _latest_submission_baseline(connection, item["work_item_id"])
        management = _management_from_events(connection, item["work_item_id"])
        if not _quality_baseline_is_auto_safe(baseline, management or {}):
            raise LiteError("final approval requires passing implementation quality evidence")
    return review


def _reviewer_claim_identity(row):
    if row is None:
        raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
    return {
        "claimId": row["claim_id"], "workItemId": row["work_item_id"],
        "taskId": row["task_id"], "agentId": row["agent_id"],
        "role": row["role"], "generation": row["generation"],
        "releasedAt": row["released_at"], "status": row["status"],
    }


def _validated_final_reviewer_claim(connection, work_item_id):
    event = connection.execute(
        "SELECT * FROM events WHERE work_item_id=? AND event_type='AGENT_FINAL_REVIEW' "
        "ORDER BY event_id DESC LIMIT 1", (work_item_id,),
    ).fetchone()
    if event is None:
        raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
    payload = _event_payload(event)
    review = payload.get("review", {})
    if payload.get("decision") != "APPROVED" or review.get("result") != "PASS":
        raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
    binding = payload.get("reviewerClaim")
    if binding is not None:
        if not isinstance(binding, dict):
            raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
        required = {
            "claimId", "workItemId", "taskId", "agentId", "role",
            "generation", "releasedAt", "status",
        }
        if set(binding) != required:
            raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
        row = connection.execute(
            "SELECT c.* FROM claims c JOIN tasks t ON t.task_id=c.task_id "
            "AND t.work_item_id=c.work_item_id AND t.owner_role='REVIEWER' "
            "WHERE c.claim_id=? AND c.work_item_id=? AND c.task_id=? "
            "AND c.agent_id=? AND c.role='REVIEWER' AND c.generation=? "
            "AND c.status='RELEASED' AND c.released_at=?",
            (binding["claimId"], work_item_id, binding["taskId"],
             event["actor_id"], binding["generation"], binding["releasedAt"]),
        ).fetchone()
        if (row is None or binding["workItemId"] != work_item_id or
                binding["agentId"] != event["actor_id"] or
                binding["role"] != "REVIEWER" or binding["status"] != "RELEASED"):
            raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
        return _reviewer_claim_identity(row)
    rows = connection.execute(
        "SELECT c.* FROM claims c JOIN tasks t ON t.task_id=c.task_id "
        "AND t.work_item_id=c.work_item_id AND t.owner_role='REVIEWER' "
        "WHERE c.work_item_id=? AND c.agent_id=? AND c.role='REVIEWER' "
        "AND c.status='RELEASED' AND c.released_at=?",
        (work_item_id, event["actor_id"], event["created_at"]),
    ).fetchall()
    if len(rows) != 1:
        raise LiteError("FINAL_REVIEW_CLAIM_BINDING_INVALID")
    return _reviewer_claim_identity(rows[0])


def _auto_gate_context(connection, work_item_id):
    row = connection.execute(
        "SELECT payload_json FROM events WHERE work_item_id=? AND event_type='WORK_ITEM_CREATED' "
        "ORDER BY event_id LIMIT 1", (work_item_id,),
    ).fetchone()
    try:
        payload = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        payload = {}
    risk = payload.get("creationRisk", {})
    signals = risk.get("signals", []) if isinstance(risk, dict) else []
    return {
        "policySource": payload.get("policySource", "MIGRATED"),
        "creationRiskKinds": sorted(set(
            signal.get("kind") for signal in signals if isinstance(signal, dict) and
            signal.get("kind") in CREATION_RISK_KINDS
        )),
        "riskPromptDecision": payload.get("riskPromptDecision"),
        "riskActionAuthorized": False,
    }


def _review_materialized(connection, work_item_id, request_id):
    """Fault-injection seam after a complete review plan and before commit."""
    return None


def _terminal_resource_projection(snapshot, reviewer_claim):
    resources = []
    for collection, kind, owner_kind, safe in (
            ("writers", "REPOSITORY_WRITER", "AGENT",
             "RELEASE_REPOSITORY_WRITER_BY_OWNER"),
            ("leases", "ORCHESTRATOR_LEASE", "ORCHESTRATOR",
             "RELEASE_ORCHESTRATOR_LEASE_BY_OWNER")):
        for value in snapshot[collection]:
            if value["status"] != "ACTIVE":
                continue
            projected = {
                "kind": kind, "resourceId": value["id"],
                "workItemId": snapshot["workItem"]["work_item_id"],
                "ownerKind": owner_kind, "ownerId": value["owner"],
                "generation": value["generation"],
                "expiresAt": value["expiresAt"],
                "persistedStatus": "ACTIVE",
                "effectiveStatus": value["effectiveStatus"],
                "terminal": False,
                "reasonCode": "LIVE_ACTIVITY_HELD" if
                value["effectiveStatus"] == "LIVE" else "EXPIRED_ACTIVITY_RESIDUE",
                "safeAction": safe if value["effectiveStatus"] == "LIVE" else
                "RECONCILE_EXPIRED_ACTIVITY",
                "afterStatus": "RELEASED" if value["effectiveStatus"] == "LIVE"
                else "EXPIRED", "mutated": True,
            }
            resources.append(projected)
    resources.append({
        "kind": "AGENT_CLAIM", "resourceId": reviewer_claim["claimId"],
        "workItemId": reviewer_claim["workItemId"],
        "taskId": reviewer_claim["taskId"], "ownerKind": "AGENT",
        "ownerId": reviewer_claim["agentId"], "role": reviewer_claim["role"],
        "generation": reviewer_claim["generation"],
        "releasedAt": reviewer_claim["releasedAt"],
        "persistedStatus": "RELEASED", "beforeStatus": "RELEASED",
        "effectiveStatus": "INACTIVE", "afterStatus": "RELEASED",
        "terminal": True, "reasonCode": None, "safeAction": "NONE",
        "mutated": False, "source": "FINAL_REVIEW_CLAIM",
    })
    return resources


def record_agent_review(database, work_item_id, stage, reviewer_agent_id, decision, summary,
                        request_id=None, usage_policy="BEST_EFFORT"):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    request_id = request_id or _id("review")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        fingerprint = _review_request_fingerprint(
            work_item_id, stage, reviewer_agent_id, decision, summary
        )
        replay = connection.execute(
            "SELECT event_type,actor_kind,actor_id,payload_json FROM events WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if replay:
            try:
                replay_payload = json.loads(replay["payload_json"])
            except (TypeError, ValueError):
                replay_payload = {}
            expected_event = "AGENT_{0}_REVIEW".format(stage)
            if (replay["event_type"] != expected_event or replay["actor_kind"] != "AGENT" or
                    replay["actor_id"] != reviewer_agent_id or
                    replay_payload.get("requestFingerprint") != fingerprint):
                raise LiteError("request_id was already used with different content")
            connection.rollback()
            return get_work_item(database, work_item_id)
        item = _item(connection, work_item_id)
        now = _now()
        snapshot = _kernel_assert(connection, work_item_id, phase="pre",
                                  evaluation_time=now)
        reviewer_claim_row = _active_claim(
            connection, work_item_id, "REVIEWER", reviewer_agent_id
        )
        expected = "PLAN_REVIEW_PENDING" if stage == "PLAN" else "IMPLEMENTATION_COMPLETED"
        if item["state"] != expected or decision not in ("APPROVED", "REJECTED"):
            raise LiteError("review stage or decision is invalid")
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope is required before review")
        author_role = "PLANNER" if stage == "PLAN" else "IMPLEMENTER"
        author = connection.execute(
            "SELECT agent_id FROM claims WHERE work_item_id=? AND role=? ORDER BY generation DESC LIMIT 1",
            (work_item_id, author_role),
        ).fetchone()
        if author is not None and author[0] == reviewer_agent_id:
            raise LiteError("author cannot review own work")
        review = _normalize_review(connection, work_item_id, stage, decision, summary)
        reviewed_candidate = review.get("reviewedCandidate") if stage == "FINAL" else None
        release_review = reviewed_candidate is not None
        stored_decision = "APPROVED" if review["result"] == "PASS" else "REJECTED"
        review_id = _id("review")
        result = review["result"]
        round_number = review["round"]
        policy = item["human_gate_policy"] if "human_gate_policy" in item.keys() else "MANUAL"
        route = workflow_kernel.review_route(
            stage, result, round_number, item["mode"], policy,
            release_review=release_review,
        )
        state = route["state"] or item["state"]
        queue, role = route["queue"], route["role"]
        reviewer_status = route["reviewerTask"]
        auto_approved = False
        auto_gate_failure = None
        final_reviewer_claim = _reviewer_claim_identity(dict(reviewer_claim_row))
        final_reviewer_claim.update({"status": "RELEASED", "releasedAt": now})
        if (result == "PASS" and item["mode"] == "STANDARD" and
                policy == "AUTO_ON_PASS" and item["queue_state"] == "CLAIMED" and
                item["held_reason"] is None and item["blocked_reason"] is None and
                not release_review):
            if stage == "FINAL":
                baseline = _latest_submission_baseline(connection, work_item_id)
                management = _management_from_events(connection, work_item_id) or {}
                pending = connection.execute(
                    "SELECT count(*) FROM tasks WHERE work_item_id=? AND required=1 "
                    "AND owner_role<>'REVIEWER' AND status<>'COMPLETED'",
                    (work_item_id,),
                ).fetchone()[0]
                if pending or not _quality_baseline_is_auto_safe(baseline, management):
                    auto_gate_failure = (
                        "final approval requires passing implementation quality evidence")
                else:
                    auto_approved = True
            else:
                auto_approved = True
        if release_review and result == "PASS":
            state, queue, role = "IMPLEMENTATION_COMPLETED", "WAITING_HUMAN", None
        review_payload = {"eventProtocolVersion": "AWB-REVIEW-EVENT-v7",
                          "decision": stored_decision, "review": review,
                          "requestFingerprint": fingerprint}
        if stage == "FINAL":
            review_payload["reviewerClaim"] = final_reviewer_claim
        if auto_gate_failure:
            review_payload["autoGate"] = {
                "status": "FAIL_CLOSED", "reason": auto_gate_failure,
            }
        next_event_id = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        ready_payload = None
        if release_review and result == "PASS":
            ready_payload = {
                "protocolVersion": "AWB-PUBLICATION-READY-v1",
                "candidateId": reviewed_candidate["candidateId"],
                "candidateFingerprint": reviewed_candidate["candidateFingerprint"],
                "buildFingerprint": reviewed_candidate["buildFingerprint"],
                "submissionEventId": connection.execute(
                    "SELECT event_id FROM events WHERE work_item_id=? "
                    "AND event_type='SUBMIT_IMPLEMENTATION' ORDER BY event_id DESC LIMIT 1",
                    (work_item_id,),
                ).fetchone()[0],
                "reviewId": review_id, "reviewEventId": next_event_id,
                "reviewRequestId": request_id, "reviewRound": round_number,
                "reviewerAgentId": reviewer_agent_id,
                "rowVersion": item["row_version"] + 1,
            }
            ready_payload["readyFingerprint"] = _sha(_json(ready_payload))
        auto_payload = None
        terminal_resources = None
        if auto_approved:
            auto_payload = {
                "policy": policy, "stage": stage, "reviewId": review_id,
                "reviewRound": round_number, "reviewRequestId": request_id,
                "reviewEventId": next_event_id,
                "idempotencyRequestId": request_id,
            }
            auto_payload.update(_auto_gate_context(connection, work_item_id))
            if stage == "FINAL":
                terminal_resources = _terminal_resource_projection(
                    snapshot, final_reviewer_claim)
        reviewer_task = next(row for row in snapshot["tasks"]
                             if row["owner_role"] == "REVIEWER" and
                             row["status"] == "IN_PROGRESS")
        author_task = next((row for row in snapshot["tasks"]
                            if row["owner_role"] == author_role), None)
        next_step = {"action": ("NONE" if auto_approved and stage == "FINAL" else
                                "CLAIM_ROLE" if queue == "CLAIMABLE" else
                                "HUMAN_GATE" if queue == "WAITING_HUMAN" else
                                "HUMAN_UNBLOCK" if queue == "BLOCKED" else "NONE"),
                     "arguments": {"workItem": work_item_id}}
        intent = {
            "operation": "PLAN_REVIEW" if stage == "PLAN" else "FINAL_REVIEW",
            "workItemId": work_item_id, "stage": stage,
            "reviewId": review_id, "review": review,
            "storedDecision": stored_decision, "claimId": reviewer_claim_row["claim_id"],
            "claimGeneration": reviewer_claim_row["generation"],
            "reviewerTaskId": reviewer_task["task_id"],
            "reviewerStatus": reviewer_status,
            "reviewEvidence": [{"reviewRound": round_number, "result": result}],
            "authorRole": author_role,
            "authorTaskId": (author_task["task_id"] if
                             route["authorTask"] == "NOT_STARTED" else None),
            "state": state, "queue": queue, "role": role,
            "autoApproved": auto_approved, "reviewPayload": review_payload,
            "reviewRequestId": request_id, "reviewEventId": next_event_id,
            "readyPayload": ready_payload,
            "readyRequestId": request_id + "-publication-ready",
            "readyEventId": next_event_id + 1 if ready_payload else None,
            "terminalResources": terminal_resources,
            "terminalRequestId": request_id + "-terminal-activity",
            "terminalEventId": next_event_id + 1 if terminal_resources else None,
            "autoPayload": auto_payload,
            "autoRequestId": "auto-gate-" + _sha(request_id + ":" + stage),
            "autoEventId": (next_event_id + (2 if terminal_resources else 1)
                            if auto_payload else None),
            "actorKind": "AGENT", "actorId": reviewer_agent_id,
            "requestId": request_id, "now": now, "nextStep": next_step,
        }
        plan = workflow_kernel.plan_review(snapshot, intent)
        _kernel_apply(connection, work_item_id, snapshot, plan,
                      evaluation_time=now)
        _review_materialized(connection, work_item_id, request_id)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _atomic_plan_bytes(path, raw):
    temporary = path + ".plan-amend-" + uuid.uuid4().hex
    try:
        with open(temporary, "wb") as handle:
            handle.write(raw)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _release_plan_amend_lock(database, lock_id, work_item_id, agent_id, request_id):
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _kernel_assert(connection, work_item_id, phase="pre")
        lock = connection.execute(
            "SELECT * FROM repository_locks WHERE lock_id=? AND work_item_id=? "
            "AND agent_id=? AND status='ACTIVE'", (lock_id, work_item_id, agent_id),
        ).fetchone()
        if lock is not None:
            now = _now()
            intent = {
                "operation": "RELEASE_WRITER", "workItemId": work_item_id,
                "requestId": request_id, "actorKind": "SYSTEM",
                "actorId": "plan-amend", "now": now,
                "repositoryKey": lock["repository_key"], "lockId": lock_id,
                "generation": lock["generation"],
                "payload": {"repositoryKey": lock["repository_key"],
                            "generation": lock["generation"],
                            "purpose": "PLAN_AMEND",
                            "reviewerAgentId": agent_id},
            }
            _kernel_apply(connection, work_item_id, snapshot,
                          workflow_kernel.plan_writer(snapshot, intent, False),
                          evaluation_time=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def amend_plan_review(database, work_item_id, reviewer_agent_id, replacement_file,
                      project_root, repository_key, review_input, request_id,
                      usage_policy="BEST_EFFORT"):
    """Apply one bounded PLAN amendment under a package-owned exact writer lock."""
    _usage_sync_boundary(database, work_item_id, usage_policy)
    if not isinstance(request_id, str) or not request_id.strip():
        raise LiteError("PLAN_AMEND requires request-id")
    if not isinstance(repository_key, str) or not repository_key.strip():
        raise LiteError("PLAN_AMEND requires repository identity")
    if not isinstance(review_input, dict):
        raise LiteError("PLAN_AMEND requires a structured review")
    _, unused_relative, replacement_file = _regular_plan_path(
        os.path.dirname(os.path.abspath(replacement_file)),
        os.path.basename(replacement_file),
    )
    with open(replacement_file, "rb") as handle:
        replacement = handle.read()
    replacement_sha = _sha(replacement)
    fingerprint = _sha(_json({
        "workItemId": work_item_id, "reviewer": reviewer_agent_id,
        "replacementSha256": replacement_sha, "review": review_input,
        "repositoryKey": repository_key,
    }))

    # Replay is checked before acquiring a new lock or touching the file.
    connection = open_database(database)
    try:
        replay = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = _event_payload(replay)
            head = _plan_artifact_head(connection, work_item_id)
            if (replay["event_type"] != "AGENT_PLAN_REVIEW" or
                    replay["actor_id"] != reviewer_agent_id or
                    payload.get("requestFingerprint") != fingerprint or
                    payload.get("review", {}).get("result") != "AMENDED" or
                    not head or head.get("sha256") != replacement_sha):
                raise LiteError("request_id was already used with different content")
            _, unused, official = _regular_plan_path(project_root, head["path"])
            if _artifact_sha(official) != replacement_sha:
                raise LiteError("exact PLAN_AMEND replay conflicts with artifact bytes")
            return get_work_item(database, work_item_id)
    finally:
        connection.close()

    # Validate the immutable base and acquire the visible exact lock first.
    connection = open_database(database)
    lock_id = None
    old_bytes = None
    official = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        snapshot = _kernel_assert(connection, work_item_id, project_root, phase="pre")
        claim = _active_claim(connection, work_item_id, "REVIEWER", reviewer_agent_id)
        if item["state"] != "PLAN_REVIEW_PENDING":
            raise LiteError("PLAN_AMEND requires pending PLAN review")
        head = _plan_artifact_head(connection, work_item_id)
        if head is None or head.get("policy") != "REVIEWER_AMEND":
            raise LiteError("PLAN_AMEND requires an opt-in plan artifact")
        _, relative, official = _regular_plan_path(project_root, head["path"])
        with open(official, "rb") as handle:
            old_bytes = handle.read()
        if _sha(old_bytes) != head.get("sha256"):
            raise LiteError("plan artifact bytes drifted from runtime head")
        if replacement_sha == head.get("sha256"):
            raise LiteError("PLAN_AMEND replacement must change bytes")
        if head.get("editorAgentId") == reviewer_agent_id:
            raise LiteError("latest plan artifact editor cannot review own revision")
        if connection.execute(
            "SELECT 1 FROM reviews WHERE work_item_id=? AND stage='PLAN' "
            "AND reviewer_agent_id=?", (work_item_id, reviewer_agent_id),
        ).fetchone():
            raise LiteError("opt-in PLAN requires a fresh Reviewer for every round")
        history = _review_history(connection, work_item_id, "PLAN")
        round_number = len(history) + 1
        if round_number > 4:
            raise LiteError("round 5 cannot amend or create round 6")
        expected_mode = "CONVERGENCE" if round_number == 4 else "ORDINARY"
        if (review_input.get("protocolVersion") != REVIEW_V2_PROTOCOL or
                review_input.get("stage") != "PLAN" or
                review_input.get("result") != "AMENDED" or
                review_input.get("reviewerMode") != expected_mode):
            raise LiteError("PLAN_AMEND review envelope is invalid")
        reviewed = {key: head[key] for key in (
            "path", "revision", "sha256", "editorAgentId"
        )}
        if review_input.get("reviewedArtifact") != reviewed:
            raise LiteError("PLAN_AMEND base artifact identity is stale")
        amendments = review_input.get("amendments")
        if not isinstance(amendments, list) or not amendments:
            raise LiteError("PLAN_AMEND requires amendment summaries")
        management = _management_from_events(connection, work_item_id) or {}
        acceptance_ids = {entry.get("id") for entry in management.get("acceptance", [])}
        open_ids = {entry["id"] for entry in _review_stage_projection(history)["openFindings"]}
        allowed_trace = acceptance_ids | open_ids
        for amendment in amendments:
            if (not isinstance(amendment, dict) or
                    set(amendment) != {"category", "summary", "traceTo"} or
                    amendment.get("category") not in PLAN_AMEND_CATEGORIES or
                    not isinstance(amendment.get("summary"), str) or
                    not amendment["summary"].strip() or
                    not isinstance(amendment.get("traceTo"), list) or
                    not amendment["traceTo"] or
                    not set(amendment["traceTo"]).intersection(allowed_trace)):
                raise LiteError("PLAN_AMEND amendment is unapproved or untraceable")
        if review_input.get("findings") not in (None, []):
            raise LiteError("AMENDED cannot introduce a blocking Finding")
        resolved = review_input.get("resolvedFindingIds", [])
        if (not isinstance(resolved, list) or len(resolved) != len(set(resolved)) or
                not set(resolved).issubset(open_ids) or open_ids - set(resolved)):
            raise LiteError("AMENDED must close every open Finding it changes")
        now = _now()
        stale_locks = connection.execute(
            "SELECT lock_id,work_item_id,agent_id,generation,expires_at "
            "FROM repository_locks WHERE repository_key=? AND status='ACTIVE' "
            "AND expires_at<=?", (repository_key, now),
        ).fetchall()
        if stale_locks:
            raise LiteError("EXPIRED_ACTIVITY_RECONCILIATION_REQUIRED")
        if connection.execute(
            "SELECT 1 FROM repository_locks WHERE repository_key=? AND status='ACTIVE'",
            (repository_key,),
        ).fetchone():
            raise LiteError("repository already has an active writer")
        generation = connection.execute(
            "SELECT coalesce(max(generation),0)+1 FROM repository_locks WHERE repository_key=?",
            (repository_key,),
        ).fetchone()[0]
        lock_id = _id("repo")
        expires_at = (datetime.datetime.now(datetime.timezone.utc) +
                      datetime.timedelta(minutes=5)).replace(microsecond=0).isoformat()
        lock_request = "plan-amend-lock-" + _sha(request_id)
        intent = {
            "operation": "ACQUIRE_WRITER", "workItemId": work_item_id,
            "requestId": lock_request, "actorKind": "SYSTEM",
            "actorId": "plan-amend", "now": now,
            "ownerId": reviewer_agent_id, "repositoryKey": repository_key,
            "lockId": lock_id, "generation": generation,
            "expiresAt": expires_at,
            "payload": {"repositoryKey": repository_key,
                        "generation": generation, "purpose": "PLAN_AMEND",
                        "reviewerAgentId": reviewer_agent_id,
                        "path": relative, "baseRevision": head["revision"],
                        "baseSha256": head["sha256"],
                        "requestId": request_id,
                        "reconciledActivity": []},
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_writer(snapshot, intent, True),
                      project_root=project_root, evaluation_time=now)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    try:
        _atomic_plan_bytes(official, replacement)
        connection = open_database(database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            item = _item(connection, work_item_id)
            snapshot = _kernel_assert(connection, work_item_id, project_root, phase="pre")
            reviewer_claim = _active_claim(
                connection, work_item_id, "REVIEWER", reviewer_agent_id
            )
            lock = connection.execute(
                "SELECT * FROM repository_locks WHERE lock_id=? AND repository_key=? "
                "AND work_item_id=? AND agent_id=? AND status='ACTIVE'",
                (lock_id, repository_key, work_item_id, reviewer_agent_id),
            ).fetchone()
            if lock is None:
                raise LiteError("exact PLAN_AMEND lock is missing")
            current_head = _plan_artifact_head(connection, work_item_id)
            if current_head != head or _artifact_sha(official) != replacement_sha:
                raise LiteError("PLAN_AMEND artifact or runtime head changed")
            now = _now()
            new_head = dict(head)
            new_head.update({"revision": head["revision"] + 1,
                             "sha256": replacement_sha,
                             "editorAgentId": reviewer_agent_id})
            review = {
                "protocolVersion": REVIEW_V2_PROTOCOL, "stage": "PLAN",
                "round": round_number, "reviewerMode": expected_mode,
                "result": "AMENDED", "findings": [],
                "resolvedFindingIds": list(resolved),
                "nonBlockingSuggestions": review_input.get("nonBlockingSuggestions", []),
                "summary": review_input.get("summary", ""),
                "reviewedArtifact": reviewed, "amendments": amendments,
            }
            reviewer_task = next(row for row in snapshot["tasks"]
                                 if row["owner_role"] == "REVIEWER" and
                                 row["status"] == "IN_PROGRESS")
            intent = {
                "operation": "PLAN_REVIEW_AMEND", "workItemId": work_item_id,
                "requestId": request_id, "actorKind": "AGENT",
                "actorId": reviewer_agent_id, "now": now,
                "reviewId": _id("review"), "review": review,
                "claimId": reviewer_claim["claim_id"],
                "claimGeneration": reviewer_claim["generation"],
                "reviewerTaskId": reviewer_task["task_id"],
                "lockId": lock_id, "lockGeneration": lock["generation"],
                "reviewPayload": {"decision": "REJECTED", "review": review,
                                  "requestFingerprint": fingerprint},
                "artifactPayload": new_head,
                "releasePayload": {"repositoryKey": repository_key,
                                   "generation": lock["generation"],
                                   "purpose": "PLAN_AMEND",
                                   "reviewerAgentId": reviewer_agent_id},
            }
            _kernel_apply(connection, work_item_id, snapshot,
                          workflow_kernel.plan_plan_amend(snapshot, intent),
                          project_root=project_root, evaluation_time=now)
            try:
                connection.commit()
            except Exception:
                connection.rollback()
                check = open_database(database)
                try:
                    event = check.execute(
                        "SELECT payload_json FROM events WHERE request_id=?", (request_id,)
                    ).fetchone()
                    committed = bool(event and _event_payload(event).get(
                        "requestFingerprint") == fingerprint)
                finally:
                    check.close()
                if committed:
                    return get_work_item(database, work_item_id)
                raise
        finally:
            connection.close()
        return get_work_item(database, work_item_id)
    except Exception:
        check = open_database(database)
        try:
            committed_event = check.execute(
                "SELECT payload_json FROM events WHERE request_id=?", (request_id,)
            ).fetchone()
            already_committed = bool(
                committed_event and _event_payload(committed_event).get(
                    "requestFingerprint") == fingerprint
            )
        finally:
            check.close()
        if already_committed:
            # The runtime head/review/release transaction is authoritative.
            # Never compensate committed bytes merely because projection failed.
            raise
        # Do not overwrite a third party's conflicting content.  Exact new bytes
        # are ours and can be compensated to the exact old head.
        if official is not None and os.path.isfile(official):
            current = _artifact_sha(official)
            if current == replacement_sha:
                _atomic_plan_bytes(official, old_bytes)
            elif current != _sha(old_bytes):
                try:
                    _release_plan_amend_lock(
                        database, lock_id, work_item_id, reviewer_agent_id,
                        "plan-amend-conflict-release-" + _sha(request_id),
                    )
                finally:
                    raise LiteError("PLAN_AMEND compensation found conflicting artifact bytes")
        _release_plan_amend_lock(
            database, lock_id, work_item_id, reviewer_agent_id,
            "plan-amend-failure-release-" + _sha(request_id),
        )
        raise


def record_human_gate(database, work_item_id, stage, human_id, decision, reason,
                      request_id=None, usage_policy="BEST_EFFORT"):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    request_id = request_id or _id("human")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        snapshot = _kernel_assert(connection, work_item_id, phase="pre")
        if item["mode"] != "STANDARD" or item["queue_state"] != "WAITING_HUMAN":
            raise LiteError("human gate is not waiting")
        expected = "PLAN_REVIEW_PENDING" if stage == "PLAN" else "IMPLEMENTATION_COMPLETED"
        if item["state"] != expected or decision not in ("APPROVED", "REJECTED"):
            raise LiteError("human gate stage or decision is invalid")
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope is required before human gate")
        if stage == "FINAL":
            submission = connection.execute(
                "SELECT payload_json FROM events WHERE work_item_id=? "
                "AND event_type='SUBMIT_IMPLEMENTATION' ORDER BY event_id DESC LIMIT 1",
                (work_item_id,),
            ).fetchone()
            try:
                release_candidate = json.loads(submission[0]).get("reviewedCandidate") if submission else None
            except (TypeError, ValueError):
                release_candidate = None
            if release_candidate is not None and decision == "REJECTED":
                raise LiteError("USE_RELEASE_PUBLICATION_RETRY")
        if decision == "APPROVED":
            _approved_gate_preconditions(connection, item, stage)
        reviewer_claim = None
        if decision == "APPROVED" and stage == "FINAL":
            reviewer_claim = _validated_final_reviewer_claim(
                connection, work_item_id
            )
        now = _now()
        intent = {
            "operation": "HUMAN_GATE", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN", "actorId": human_id,
            "now": now, "stage": stage, "decision": decision, "reason": reason,
            "gateId": _id("gate"),
            "terminalResources": (_terminal_resource_projection(
                snapshot, reviewer_claim) if reviewer_claim else []),
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_human_gate(snapshot, intent),
                      evaluation_time=now)
        if decision == "APPROVED" and stage == "FINAL":
            _terminal_activity_materialized(
                connection, work_item_id, request_id + "-terminal-activity"
            )
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _terminal_activity_materialized(connection, work_item_id, request_id):
    """Fault-injection seam after terminal activity materialization."""
    return None


def set_hold(database, work_item_id, human_id, held, reason="USER_PAUSED", request_id=None):
    request_id = request_id or _id("hold")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        snapshot = _kernel_assert(connection, work_item_id, phase="pre")
        if item["state"] == "FINAL_ACCEPTANCE_APPROVED":
            raise LiteError("terminal item cannot be resumed")
        if connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone():
            raise LiteError("release active claim before hold/resume")
        now = _now()
        intent = {
            "operation": "HOLD" if held else "RESUME",
            "workItemId": work_item_id, "requestId": request_id,
            "actorKind": "HUMAN", "actorId": human_id, "now": now,
            "held": held, "reason": reason,
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_hold(snapshot, intent),
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def unblock_task(database, work_item_id, task_id, human_id, reason, request_id=None):
    request_id = request_id or _id("unblock")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        snapshot = _kernel_assert(connection, work_item_id, phase="pre")
        if item["queue_state"] != "BLOCKED":
            raise LiteError("WorkItem is not blocked")
        if connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone():
            raise LiteError("blocked WorkItem must not have active claim")
        task = connection.execute(
            "SELECT status,owner_role FROM tasks WHERE work_item_id=? AND task_id=?",
            (work_item_id, task_id),
        ).fetchone()
        if task is None or task[0] != "BLOCKED":
            raise LiteError("task is not blocked")
        now = _now()
        intent = {
            "operation": "UNBLOCK", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "HUMAN",
            "actorId": human_id, "now": now,
            "taskId": task_id, "reason": reason,
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_unblock(snapshot, intent),
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _review_task_recovery_result(status, reason_code, work_item_id, task_id,
                                 human_id, reason, request_id, binding=None):
    result = {
        "protocolVersion": REVIEW_TASK_RECOVERY_PROTOCOL,
        "operation": "RECOVER_REVIEW_TASK",
        "status": status,
        "reasonCode": reason_code,
        "workItemId": work_item_id,
        "taskId": task_id,
        "humanId": human_id,
        "requestId": request_id,
    }
    if binding is not None:
        result["binding"] = binding
    return result


def _event_payload(row):
    try:
        value = json.loads(row["payload_json"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def recover_review_task(database, work_item_id, task_id, human_id, reason,
                        request_id):
    """Refuse the removed ad-hoc reset and route callers to exact repair."""
    return _review_task_recovery_result(
        "REFUSED", "USE_WORKFLOW_CHECK_AND_EXACT_REPAIR", work_item_id,
        task_id, human_id, reason, request_id,
    )


def report_plan_deviation(database, work_item_id, task_id, agent_id, evidence,
                          request_id=None):
    if not isinstance(evidence, dict) or not evidence.get("reason") or not evidence.get("impact"):
        raise LiteError("PLAN_DEVIATION requires reason and impact evidence")
    request_id = request_id or _id("plan-deviation")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        snapshot = _kernel_assert(connection, work_item_id, phase="pre")
        claim = _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
        if item["state"] != "IMPLEMENTING" or claim["task_id"] != task_id:
            raise LiteError("PLAN_DEVIATION requires active implementation")
        now = _now()
        intent = {
            "operation": "PLAN_DEVIATION", "workItemId": work_item_id,
            "requestId": request_id, "actorKind": "AGENT", "actorId": agent_id,
            "now": now, "taskId": task_id, "evidence": evidence,
        }
        _kernel_apply(connection, work_item_id, snapshot,
                      workflow_kernel.plan_plan_deviation(snapshot, intent),
                      evaluation_time=now)
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _structured_next(action, arguments=None, required_inputs=None, risk_class="LOCAL_SAFE"):
    value = {"action": action, "arguments": arguments or {},
             "requiredInputs": required_inputs or [], "riskClass": risk_class}
    value["fingerprint"] = _sha(_json(value))
    return value


def _workflow_snapshot(connection, work_item_id, agent_id, role, repository_key,
                       orchestrator_id=None, orchestrator_generation=None,
                       project_root=None):
    item = _item(connection, work_item_id)
    tasks = connection.execute(
        "SELECT * FROM tasks WHERE work_item_id=? AND owner_role=? "
        "ORDER BY seq", (work_item_id, role)
    ).fetchall()
    task = tasks[0] if len(tasks) == 1 else None
    claim = connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
    ).fetchone()
    writer = connection.execute(
        "SELECT * FROM repository_locks WHERE repository_key=? AND status='ACTIVE'",
        (repository_key,),
    ).fetchone()
    publication_step = None
    if project_root and item["queue_state"] == "WAITING_HUMAN":
        ready = connection.execute(
            "SELECT * FROM events WHERE work_item_id=? AND event_type='PUBLICATION_READY' "
            "ORDER BY event_id DESC LIMIT 1", (work_item_id,),
        ).fetchone()
        authorization = connection.execute(
            "SELECT * FROM events WHERE work_item_id=? AND event_type='PUBLICATION_AUTHORIZED' "
            "ORDER BY event_id DESC LIMIT 1", (work_item_id,),
        ).fetchone()
        invalidation = connection.execute(
            "SELECT event_id FROM events WHERE work_item_id=? "
            "AND event_type='PUBLICATION_READY_INVALIDATED' ORDER BY event_id DESC LIMIT 1",
            (work_item_id,),
        ).fetchone()
        if (ready is not None and authorization is not None and
                (invalidation is None or invalidation["event_id"] < ready["event_id"])):
            try:
                ready_payload = json.loads(ready["payload_json"])
                authorization_payload = json.loads(authorization["payload_json"])
                exact = authorization_payload.get("candidateFingerprint")
                if exact != ready_payload.get("candidateFingerprint"):
                    raise LiteError("publication identity drift")
                from .candidate import _verify_current_build
                _verify_current_build(project_root, work_item_id, {
                    "candidateId": ready_payload["candidateId"],
                    "candidateFingerprint": exact,
                    "buildFingerprint": ready_payload["buildFingerprint"],
                })
                publication_step = _structured_next(
                    "EXECUTE_EXACT_AUTHORIZED_PUBLICATION", {
                        "readyFingerprint": ready_payload["readyFingerprint"],
                        "authorizationRequestId": authorization["request_id"],
                        "candidateFingerprint": exact,
                    }, risk_class="REMOTE",
                )
            except (KeyError, TypeError, ValueError, LiteError):
                publication_step = _structured_next(
                    "HUMAN_INSPECT_PUBLICATION_IDENTITY", risk_class="HUMAN"
                )
    if item["state"] == "FINAL_ACCEPTANCE_APPROVED":
        step = _structured_next("NONE")
        status, reason = "NO_OP", None
    elif publication_step is not None:
        step = publication_step
        status = "REFUSED" if step["riskClass"] == "REMOTE" else "WAITING_HUMAN"
        reason = ("REMOTE_STEP_NOT_CONSUMABLE" if step["riskClass"] == "REMOTE" else
                  "PUBLICATION_IDENTITY_DRIFT")
    elif item["queue_state"] in ("WAITING_HUMAN", "HELD", "BLOCKED"):
        action = {"WAITING_HUMAN": "HUMAN_GATE_DECISION",
                  "HELD": "HUMAN_RESUME", "BLOCKED": "HUMAN_UNBLOCK"}[item["queue_state"]]
        step = _structured_next(action, risk_class="HUMAN")
        status, reason = "WAITING_HUMAN", item["queue_state"]
    elif claim is not None and (claim["agent_id"] != agent_id or claim["role"] != role):
        step = _structured_next("RELEASE_RESOURCE_BY_EXACT_OWNER", {
            "owner": claim["agent_id"], "generation": claim["generation"]
        }, risk_class="AMBIGUOUS")
        status, reason = "REFUSED", "RESOURCE_CONFLICT"
    else:
        state = item["state"]
        active = claim is not None
        action = None
        required = []
        if state == "DRAFT" and role == "PLANNER":
            action = "SUBMIT_PLAN" if active else "BEGIN_PLANNING"
            required = ["--plan-artifact"] if active else []
            if active and _review_stage_projection(_review_history(connection, work_item_id, "PLAN"))["openFindings"]:
                required.append("--submission-file")
        elif state == "PLAN_REVIEW_PENDING" and role == "REVIEWER":
            action = "SUBMIT_PLAN_REVIEW" if active else "BEGIN_PLAN_REVIEW"
            required = ["--review-file", "--decision"] if active else []
        elif state == "PLAN_REVIEW_APPROVED" and role == "IMPLEMENTER":
            action = "BEGIN_IMPLEMENTATION"
        elif state == "IMPLEMENTING" and role == "IMPLEMENTER":
            action = "SUBMIT_IMPLEMENTATION" if active else "BEGIN_IMPLEMENTATION"
            if active:
                required = ["--quality-file", "--local-tests-passed"]
                from .candidate import release_submission_candidate
                if release_submission_candidate(connection, work_item_id) is not None:
                    required += ["--candidate", "--candidate-fingerprint"]
        elif state == "IMPLEMENTATION_COMPLETED" and role == "REVIEWER":
            action = "SUBMIT_IMPLEMENTATION_REVIEW" if active else "BEGIN_IMPLEMENTATION_REVIEW"
            required = ["--review-file", "--decision"] if active else []
        if len(tasks) != 1:
            step = _structured_next("HUMAN_RESOLVE_TASK_AMBIGUITY", risk_class="AMBIGUOUS")
            status, reason = "REFUSED", "AMBIGUOUS_NEXT_STEP"
        elif action is None:
            step = _structured_next("NONE")
            status, reason = "NO_OP", "NO_LOCAL_SAFE_NEXT_STEP"
        else:
            arguments = {"agent": agent_id, "role": role,
                         "repository": repository_key, "taskId": task["task_id"]}
            if orchestrator_id is not None:
                arguments.update({"orchestratorId": orchestrator_id,
                                  "orchestratorGeneration": orchestrator_generation})
            step = _structured_next(action, arguments, required)
            status, reason = "READY", None
    fingerprint_input = {
        "project": connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0],
        "workItemId": work_item_id, "state": item["state"],
        "queueState": item["queue_state"], "currentRole": item["current_role"],
        "task": dict(task) if task else None, "rowVersion": item["row_version"],
        "claim": dict(claim) if claim else None, "writer": dict(writer) if writer else None,
        "orchestratorId": orchestrator_id,
        "orchestratorGeneration": orchestrator_generation,
        "reviewHeads": [dict(row) for row in connection.execute(
            "SELECT stage,review_id,decision,summary,created_at FROM reviews "
            "WHERE work_item_id=? ORDER BY created_at,rowid", (work_item_id,)
        )],
        "planHead": [dict(row) for row in connection.execute(
            "SELECT event_id,event_type,payload_json FROM events WHERE work_item_id=? "
            "AND event_type='PLAN_ARTIFACT_HEAD' ORDER BY event_id DESC LIMIT 1",
            (work_item_id,),
        )],
        "candidateHead": [dict(row) for row in connection.execute(
            "SELECT event_id,event_type,payload_json FROM events WHERE work_item_id=? "
            "AND event_type LIKE 'CANDIDATE_%' ORDER BY event_id DESC LIMIT 1",
            (work_item_id,),
        )],
        "step": step,
    }
    step["fingerprint"] = _sha(_json(fingerprint_input))
    return item, task, claim, writer, {
        "protocolVersion": WORKFLOW_ADVANCE_PROTOCOL, "status": status,
        "reasonCode": reason, "workItemId": work_item_id,
        "rowVersion": item["row_version"], "nextStep": step,
    }


def workflow_status(database, work_item_id, agent_id, role, repository_key,
                    orchestrator_id=None, orchestrator_generation=None,
                    project_root=None):
    if role not in ("PLANNER", "IMPLEMENTER", "REVIEWER"):
        raise LiteError("workflow role is invalid")
    connection = open_database(database)
    try:
        connection.execute("BEGIN")
        unused_item, unused_task, unused_claim, unused_writer, result = _workflow_snapshot(
            connection, work_item_id, agent_id, role, repository_key,
            orchestrator_id, orchestrator_generation, project_root,
        )
        connection.rollback()
        return result
    finally:
        connection.close()


def _workflow_input(project_root, relative, label):
    if not isinstance(relative, str) or not relative or os.path.isabs(relative):
        raise LiteError("{0} must be project-relative".format(label))
    root = os.path.realpath(os.path.abspath(project_root))
    path = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path) or os.path.islink(path):
        raise LiteError("{0} must be a project regular non-symlink file".format(label))
    with open(path, "rb") as handle:
        raw = handle.read()
    return path, raw, _sha(raw)


def workflow_advance(database, project_root, work_item_id, agent_id, role,
                     repository_key, expected_step, expected_row_version, request_id,
                     orchestrator_id=None, orchestrator_generation=None, ttl=900,
                     plan_artifact=None, submission_file=None, quality_file=None,
                     review_file=None, decision=None, local_tests_passed=False,
                     candidate_id=None, candidate_fingerprint=None):
    if ttl < 1 or not request_id:
        raise LiteError("workflow advance requires positive ttl and request-id")
    file_inputs = {}
    for name, value in (("planArtifact", plan_artifact), ("submissionFile", submission_file),
                        ("qualityFile", quality_file), ("reviewFile", review_file)):
        if value:
            path, raw, digest = _workflow_input(project_root, value, name)
            try:
                parsed = json.loads(raw.decode("utf-8")) if name != "planArtifact" else None
            except (UnicodeError, ValueError):
                raise LiteError("{0} is invalid JSON".format(name))
            file_inputs[name] = {"path": path, "relative": value, "raw": raw,
                                 "sha256": digest, "value": parsed}
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _kernel_assert(connection, work_item_id, project_root, phase="pre")
        item, task, claim, writer, status_result = _workflow_snapshot(
            connection, work_item_id, agent_id, role, repository_key,
            orchestrator_id, orchestrator_generation, project_root,
        )
        step = status_result["nextStep"]
        fingerprint = _sha(_json({"workItemId": work_item_id, "agent": agent_id,
                                  "role": role, "expectedStep": expected_step,
                                  "expectedRowVersion": expected_row_version,
                                  "files": {key: value["sha256"] for key, value in file_inputs.items()},
                                  "decision": decision, "localTestsPassed": local_tests_passed,
                                  "ttl": ttl, "orchestratorId": orchestrator_id,
                                  "orchestratorGeneration": orchestrator_generation,
                                  "candidateId": candidate_id,
                                  "candidateFingerprint": candidate_fingerprint}))
        replay = connection.execute("SELECT * FROM events WHERE request_id=?", (request_id,)).fetchone()
        if replay:
            payload = json.loads(replay["payload_json"])
            if replay["event_type"] != "WORKFLOW_ADVANCED" or payload.get("requestFingerprint") != fingerprint:
                raise LiteError("REQUEST_REPLAY_CONFLICT")
            connection.rollback()
            return payload["receipt"]
        if status_result["status"] != "READY" or step.get("riskClass") != "LOCAL_SAFE":
            connection.rollback()
            result = dict(status_result)
            result.update({"status": "REFUSED", "reasonCode": status_result.get("reasonCode") or
                           "NON_LOCAL_SAFE_NEXT_STEP"})
            return result
        if expected_row_version != item["row_version"]:
            raise LiteError("ROW_VERSION_DRIFT")
        if expected_step != step["fingerprint"]:
            raise LiteError("NEXT_STEP_DRIFT")
        for name, value in file_inputs.items():
            info = os.lstat(value["path"])
            with open(value["path"], "rb") as handle:
                current_sha = _sha(handle.read())
            if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or
                    current_sha != value["sha256"]):
                raise LiteError("{0} changed after workflow preflight".format(name))
        expected_flags = set(step.get("requiredInputs", []))
        provided_flags = set()
        if plan_artifact: provided_flags.add("--plan-artifact")
        if submission_file: provided_flags.add("--submission-file")
        if quality_file: provided_flags.add("--quality-file")
        if review_file: provided_flags.add("--review-file")
        if decision: provided_flags.add("--decision")
        if local_tests_passed: provided_flags.add("--local-tests-passed")
        if candidate_id: provided_flags.add("--candidate")
        if candidate_fingerprint: provided_flags.add("--candidate-fingerprint")
        allowed_flags = set(expected_flags)
        if step["action"] == "SUBMIT_PLAN" and "--submission-file" not in expected_flags:
            allowed_flags.add("--submission-file")
        if not expected_flags.issubset(provided_flags) or not provided_flags.issubset(allowed_flags):
            raise LiteError("UNEXPECTED_ADVANCE_INPUT")
        now = _now()
        expires_at = (datetime.datetime.now(datetime.timezone.utc) +
                      datetime.timedelta(seconds=ttl)).replace(microsecond=0).isoformat()
        action = step["action"]
        operation_event = action
        receipt = None
        if action.startswith("BEGIN_"):
            if task is None or task["status"] != "NOT_STARTED" or claim is not None:
                raise LiteError("workflow begin has no eligible task")
            _validate_orchestrator_fence(
                connection, work_item_id, orchestrator_id,
                orchestrator_generation, now)
            claim_generation = connection.execute(
                "SELECT coalesce(max(generation),0)+1 FROM claims WHERE work_item_id=?",
                (work_item_id,),
            ).fetchone()[0]
            claim_id = _id("claim")
            needs_writer = action in ("BEGIN_PLANNING", "BEGIN_IMPLEMENTATION")
            lock_id = lock_generation = None
            if needs_writer:
                if connection.execute(
                    "SELECT 1 FROM repository_locks WHERE repository_key=? "
                    "AND status='ACTIVE'", (repository_key,),
                ).fetchone():
                    raise LiteError("repository already has an active writer")
                lock_generation = connection.execute(
                    "SELECT coalesce(max(generation),0)+1 FROM repository_locks "
                    "WHERE repository_key=?", (repository_key,),
                ).fetchone()[0]
                lock_id = _id("repo")
            begin_snapshot = _kernel_snapshot(connection, work_item_id, project_root,
                                              evaluation_time=now)
            first_event_id = connection.execute(
                "SELECT coalesce(max(event_id),0)+1 FROM events"
            ).fetchone()[0]
            event_count = 2 + int(needs_writer) + int(action == "BEGIN_IMPLEMENTATION")
            advance_event_id = first_event_id + event_count
            next_action = ("SUBMIT_PLAN" if action == "BEGIN_PLANNING" else
                           "SUBMIT_IMPLEMENTATION" if action == "BEGIN_IMPLEMENTATION" else
                           "SUBMIT_PLAN_REVIEW" if action == "BEGIN_PLAN_REVIEW" else
                           "SUBMIT_IMPLEMENTATION_REVIEW")
            receipt = {"protocolVersion": MUTATION_RECEIPT_PROTOCOL,
                       "operation": operation_event, "status": "OK", "reasonCode": None,
                       "workItemId": work_item_id,
                       "state": "IMPLEMENTING" if action == "BEGIN_IMPLEMENTATION" else item["state"],
                       "queueState": "CLAIMED", "currentRole": role,
                       "currentTask": task["task_id"], "eventId": advance_event_id,
                       "rowVersion": item["row_version"] + 1,
                       "nextStep": {"action": next_action, "riskClass": "LOCAL_SAFE",
                                    "arguments": {"workItemId": work_item_id,
                                                  "taskId": task["task_id"],
                                                  "role": role}}}
            begin_intent = {
                "operation": "WORKFLOW_ADVANCE", "beginAction": action,
                "workItemId": work_item_id, "taskId": task["task_id"],
                "role": role, "claimId": claim_id,
                "claimGeneration": claim_generation, "lockId": lock_id,
                "lockGeneration": lock_generation, "repositoryKey": repository_key,
                "expiresAt": expires_at, "firstEventId": first_event_id,
                "actorKind": "AGENT", "actorId": agent_id,
                "requestId": request_id, "now": now,
                "advanceReceipt": receipt, "advanceFingerprint": fingerprint,
            }
            plan = workflow_kernel.plan_begin(begin_snapshot, begin_intent)
            _kernel_apply(connection, work_item_id, begin_snapshot, plan, project_root,
                          evaluation_time=now)
        elif action in ("SUBMIT_PLAN", "SUBMIT_IMPLEMENTATION"):
            if claim is None or claim["agent_id"] != agent_id or writer is None or writer["agent_id"] != agent_id:
                raise LiteError("workflow submit requires exact claim and writer")
            if task["status"] != "IN_PROGRESS":
                raise LiteError("workflow submit requires IN_PROGRESS task")
            payload = {"from": item["state"]}
            if action == "SUBMIT_PLAN":
                artifact = _next_plan_artifact(connection, work_item_id, agent_id,
                                               {"projectRoot": project_root,
                                                "path": plan_artifact})
                _validate_revision_submission(connection, work_item_id, "PLAN",
                                              file_inputs.get("submissionFile", {}).get("value"))
                state, next_role = "PLAN_REVIEW_PENDING", "REVIEWER"
                payload.update({"to": state, "planArtifact": artifact})
            else:
                if not local_tests_passed:
                    raise LiteError("implementation local tests must pass")
                quality = _validate_quality_baseline(connection, work_item_id,
                                                     file_inputs["qualityFile"]["value"])
                from .candidate import release_submission_candidate
                release_candidate = release_submission_candidate(connection, work_item_id)
                if release_candidate:
                    if candidate_id != release_candidate["candidateId"] or candidate_fingerprint != release_candidate["candidateFingerprint"]:
                        raise LiteError("Release submission candidate drift")
                    if sorted(quality.get("modifiedScope", [])) != sorted(release_candidate["changedPaths"]):
                        raise LiteError("Release modifiedScope drift")
                    payload["reviewedCandidate"] = release_candidate
                state, next_role = "IMPLEMENTATION_COMPLETED", "REVIEWER"
                payload.update({"to": state, "qualityBaseline": quality})
            snapshot = _kernel_snapshot(connection, work_item_id, project_root,
                                        evaluation_time=now)
            next_event_id = connection.execute(
                "SELECT coalesce(max(event_id),0)+1 FROM events"
            ).fetchone()[0]
            event_count = 5 if action == "SUBMIT_PLAN" else 4
            advance_event_id = next_event_id + event_count
            reviewer_tasks = [row for row in snapshot["tasks"]
                              if row["owner_role"] == "REVIEWER" and
                              row["status"] != "COMPLETED"]
            current_task = min(reviewer_tasks, key=lambda row: row["seq"])["task_id"]
            receipt = {
                "protocolVersion": MUTATION_RECEIPT_PROTOCOL,
                "operation": operation_event, "status": "OK", "reasonCode": None,
                "workItemId": work_item_id, "state": state,
                "queueState": "CLAIMABLE", "currentRole": "REVIEWER",
                "currentTask": current_task, "eventId": advance_event_id,
                "rowVersion": item["row_version"] + 1,
                "nextStep": {"action": "BEGIN_REVIEW", "riskClass": "LOCAL_SAFE",
                             "arguments": {"workItemId": work_item_id,
                                           "taskId": current_task,
                                           "role": "REVIEWER"}},
            }
            intent = {
                "operation": action, "workItemId": work_item_id,
                "taskId": task["task_id"], "claimId": claim["claim_id"],
                "claimGeneration": claim["generation"], "lockId": writer["lock_id"],
                "lockGeneration": writer["generation"], "role": claim["role"],
                "repositoryKey": repository_key, "state": state, "payload": payload,
                "artifact": artifact if action == "SUBMIT_PLAN" else None,
                "actorKind": "AGENT", "actorId": agent_id,
                "requestId": request_id, "now": now,
                "advanceReceipt": receipt, "advanceFingerprint": fingerprint,
            }
            plan = workflow_kernel.plan_submit(snapshot, intent)
            _kernel_apply(connection, work_item_id, snapshot, plan, project_root,
                          evaluation_time=now)
        else:
            if claim is None or claim["agent_id"] != agent_id or task["status"] != "IN_PROGRESS":
                raise LiteError("workflow review submit requires exact Reviewer claim")
            stage = "PLAN" if action == "SUBMIT_PLAN_REVIEW" else "FINAL"
            if decision not in ("APPROVED", "REJECTED"):
                raise LiteError("workflow review decision is invalid")
            author_role = "PLANNER" if stage == "PLAN" else "IMPLEMENTER"
            author = connection.execute(
                "SELECT agent_id FROM claims WHERE work_item_id=? AND role=? "
                "ORDER BY generation DESC LIMIT 1", (work_item_id, author_role),
            ).fetchone()
            if author is not None and author[0] == agent_id:
                raise LiteError("author cannot review own work")
            review = _normalize_review(connection, work_item_id, stage, decision,
                                       file_inputs["reviewFile"]["value"])
            result = review["result"]
            stored = "APPROVED" if result == "PASS" else "REJECTED"
            review_id = _id("review")
            reviewer_claim = _reviewer_claim_identity(dict(claim))
            reviewer_claim.update({"status": "RELEASED", "releasedAt": now})
            release_review = stage == "FINAL" and review.get("reviewedCandidate") is not None
            round_number = review["round"]
            policy = item["human_gate_policy"]
            route = workflow_kernel.review_route(
                stage, result, round_number, item["mode"], policy,
                release_review=release_review,
            )
            reviewer_status = route["reviewerTask"]
            state = route["state"] or item["state"]
            queue, next_role = route["queue"], route["role"]
            auto_approved = False
            auto_failure = None
            if (result == "PASS" and item["mode"] == "STANDARD" and
                    policy == "AUTO_ON_PASS" and not release_review):
                if stage == "FINAL":
                    baseline = _latest_submission_baseline(connection, work_item_id)
                    management = _management_from_events(connection, work_item_id) or {}
                    pending = connection.execute(
                        "SELECT count(*) FROM tasks WHERE work_item_id=? AND required=1 "
                        "AND owner_role<>'REVIEWER' AND status<>'COMPLETED'",
                        (work_item_id,),
                    ).fetchone()[0]
                    if pending or not _quality_baseline_is_auto_safe(baseline, management):
                        auto_failure = "final approval requires passing implementation quality evidence"
                    else:
                        auto_approved = True
                else:
                    auto_approved = True
            if release_review and result == "PASS":
                state, queue, next_role = "IMPLEMENTATION_COMPLETED", "WAITING_HUMAN", None
            review_payload = {"eventProtocolVersion": "AWB-REVIEW-EVENT-v7",
                              "decision": stored, "review": review,
                              "requestFingerprint": _review_request_fingerprint(work_item_id, stage, agent_id, decision, file_inputs["reviewFile"]["value"]),
                              "reviewerClaim": reviewer_claim}
            if auto_failure:
                review_payload["autoGate"] = {"status": "FAIL_CLOSED",
                                               "reason": auto_failure}
            review_snapshot = _kernel_snapshot(connection, work_item_id, project_root,
                                               evaluation_time=now)
            next_event_id = connection.execute(
                "SELECT coalesce(max(event_id),0)+1 FROM events"
            ).fetchone()[0]
            terminal_resources = (_terminal_resource_projection(
                review_snapshot, reviewer_claim)
                if auto_approved and stage == "FINAL" else None)
            ready = None
            if release_review and result == "PASS":
                submission_event = connection.execute(
                    "SELECT event_id FROM events WHERE work_item_id=? "
                    "AND event_type='SUBMIT_IMPLEMENTATION' ORDER BY event_id DESC LIMIT 1",
                    (work_item_id,),
                ).fetchone()[0]
                ready = {"protocolVersion": "AWB-PUBLICATION-READY-v1",
                         "candidateId": review["reviewedCandidate"]["candidateId"],
                         "candidateFingerprint": review["reviewedCandidate"]["candidateFingerprint"],
                         "buildFingerprint": review["reviewedCandidate"]["buildFingerprint"],
                         "submissionEventId": submission_event, "reviewId": review_id,
                         "reviewEventId": next_event_id,
                         "reviewRequestId": request_id + "-review",
                         "reviewRound": round_number, "reviewerAgentId": agent_id,
                         "rowVersion": item["row_version"] + 1}
                ready["readyFingerprint"] = _sha(_json(ready))
            auto = None
            if auto_approved:
                auto = {"policy": policy, "stage": stage, "reviewId": review_id,
                        "reviewRound": round_number, "reviewRequestId": request_id + "-review",
                        "reviewEventId": next_event_id, "idempotencyRequestId": request_id}
                auto.update(_auto_gate_context(connection, work_item_id))
            optional_count = int(bool(terminal_resources)) + int(bool(ready)) + int(bool(auto))
            advance_event_id = next_event_id + 3 + optional_count
            if auto_approved and stage == "FINAL":
                receipt_state, receipt_queue, receipt_role, current_task = (
                    "FINAL_ACCEPTANCE_APPROVED", "HELD", None, None)
            elif auto_approved:
                implementer_tasks = [row for row in review_snapshot["tasks"]
                                     if row["owner_role"] == "IMPLEMENTER" and
                                     row["status"] != "COMPLETED"]
                current_task = min(implementer_tasks, key=lambda row: row["seq"])["task_id"]
                receipt_state, receipt_queue, receipt_role = (
                    "PLAN_REVIEW_APPROVED", "CLAIMABLE", "IMPLEMENTER")
            else:
                receipt_state, receipt_queue, receipt_role = state, queue, next_role
                candidates = [row for row in review_snapshot["tasks"]
                              if row["owner_role"] == next_role and row["status"] != "COMPLETED"]
                current_task = (min(candidates, key=lambda row: row["seq"])["task_id"]
                                if candidates else None)
            receipt = {"protocolVersion": MUTATION_RECEIPT_PROTOCOL,
                       "operation": operation_event, "status": "OK", "reasonCode": None,
                       "workItemId": work_item_id, "state": receipt_state,
                       "queueState": receipt_queue, "currentRole": receipt_role,
                       "currentTask": current_task, "eventId": advance_event_id,
                       "rowVersion": item["row_version"] + 1,
                       "nextStep": {"action": "NONE", "arguments": {}}}
            author_task = next((row for row in review_snapshot["tasks"]
                                if row["owner_role"] == author_role), None)
            extra_events = [
                {"requestId": request_id + "-task-review", "eventId": next_event_id + 1,
                 "eventType": "TASK_STATUS_CHANGED", "actorKind": "AGENT",
                 "actorId": agent_id, "payload": {"taskId": task["task_id"],
                 "from": "IN_PROGRESS", "status": reviewer_status,
                 "evidence": [{"reviewRound": round_number, "result": result}]}},
                {"requestId": request_id + "-claim-release", "eventId": next_event_id + 2,
                 "eventType": "CLAIM_RELEASED", "actorKind": "AGENT",
                 "actorId": agent_id, "payload": {"claimId": claim["claim_id"],
                 "taskId": claim["task_id"], "role": claim["role"],
                 "generation": claim["generation"]}},
            ]
            optional_event_id = next_event_id + 3
            review_intent = {
                "operation": "PLAN_REVIEW" if stage == "PLAN" else "FINAL_REVIEW",
                "workItemId": work_item_id, "stage": stage, "reviewId": review_id,
                "review": review, "storedDecision": stored, "claimId": claim["claim_id"],
                "claimGeneration": claim["generation"], "reviewerTaskId": task["task_id"],
                "reviewerStatus": reviewer_status,
                "reviewEvidence": [{"reviewRound": round_number, "result": result}],
                "authorRole": author_role, "authorTaskId": (author_task["task_id"] if
                    route["authorTask"] == "NOT_STARTED" else None),
                "state": state, "queue": queue, "role": next_role,
                "autoApproved": auto_approved, "reviewPayload": review_payload,
                "reviewRequestId": request_id + "-review", "reviewEventId": next_event_id,
                "extraEvents": extra_events, "terminalResources": terminal_resources,
                "terminalRequestId": request_id + "-terminal-activity",
                "terminalEventId": optional_event_id if terminal_resources else None,
                "readyPayload": ready, "readyRequestId": request_id + "-publication-ready",
                "readyEventId": optional_event_id if ready else None,
                "autoPayload": auto, "autoRequestId": request_id + "-auto-gate",
                "autoEventId": (optional_event_id + int(bool(terminal_resources or ready))
                                if auto else None),
                "actorKind": "AGENT", "actorId": agent_id,
                "requestId": request_id, "now": now,
                "advanceReceipt": receipt, "advanceFingerprint": fingerprint,
                "nextStep": receipt["nextStep"],
            }
            plan = workflow_kernel.plan_review(review_snapshot, review_intent)
            _kernel_apply(connection, work_item_id, review_snapshot, plan, project_root,
                          evaluation_time=now)
        if receipt is None:
            raise LiteError("UNREGISTERED_WORKFLOW_ADVANCE_EDGE")
        _kernel_assert(connection, work_item_id, project_root, phase="post")
        _workflow_materialized(connection, work_item_id, request_id)
        connection.commit()
        return receipt
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _workflow_materialized(connection, work_item_id, request_id):
    """Fault-injection seam after complete workflow plan and before commit."""
    return None


def _kernel_snapshot(connection, work_item_id, project_root=None,
                     evaluation_time=None):
    """Build the one canonical projection consumed by the workflow kernel."""
    evaluation_time = evaluation_time or _now()
    item = dict(_item(connection, work_item_id))
    tasks = [dict(row) for row in connection.execute(
        "SELECT * FROM tasks WHERE work_item_id=? ORDER BY seq", (work_item_id,)
    )]
    claims = [dict(row) for row in connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? ORDER BY generation,claim_id",
        (work_item_id,),
    )]
    writers = [dict(row) for row in connection.execute(
        "SELECT * FROM repository_locks WHERE work_item_id=? "
        "ORDER BY generation,lock_id", (work_item_id,),
    )]
    try:
        leases = [dict(row) for row in connection.execute(
            "SELECT * FROM orchestrator_leases WHERE work_item_id=? "
            "ORDER BY generation,lease_id", (work_item_id,),
        )]
    except sqlite3.Error:
        leases = []
    reviews = [dict(row) for row in connection.execute(
        "SELECT * FROM reviews WHERE work_item_id=? ORDER BY rowid",
        (work_item_id,),
    )]
    for review_row in reviews:
        review_row["review"] = _decoded_review(review_row)
    events = [dict(row) for row in connection.execute(
        "SELECT event_id,event_type,actor_kind,actor_id,request_id,payload_json,created_at "
        "FROM events WHERE work_item_id=? ORDER BY event_id", (work_item_id,),
    )]
    # A repair receipt binds the post-repair projection.  Exclude only that
    # self-referential value from the history digest so it can be computed in a
    # rolled-back dry materialization and then stored immutably in the request
    # event without changing the projection it identifies.
    history_events = []
    for event in events:
        canonical_event = dict(event)
        if event["event_type"] == "WORKFLOW_REPAIRED":
            payload = _event_payload(event)
            receipt = payload.get("receipt")
            if isinstance(receipt, dict) and "toFingerprint" in receipt:
                payload = dict(payload)
                payload["receipt"] = dict(receipt)
                payload["receipt"].pop("toFingerprint", None)
                canonical_event["payload_json"] = _json(payload)
        history_events.append(canonical_event)
    history_digest = workflow_kernel.content_fingerprint(history_events)
    event_head = events[-1]["event_id"] if events else 0
    artifact_head = _plan_artifact_head(connection, work_item_id)
    artifact_sha = None
    if artifact_head is not None and project_root is not None:
        try:
            unused_root, unused_relative, absolute = _regular_plan_path(
                project_root, artifact_head.get("path")
            )
            artifact_sha = _artifact_sha(absolute)
        except LiteError:
            artifact_sha = "UNAVAILABLE"
    review_events = []
    gate_events = []
    artifact_at_event = None
    candidate_at_event = None
    for event in events:
        payload = _event_payload(event)
        if event["event_type"] == "PLAN_ARTIFACT_HEAD":
            artifact_at_event = payload
            continue
        if event["event_type"] == "SUBMIT_IMPLEMENTATION":
            candidate_at_event = payload.get("reviewedCandidate")
        if event["event_type"] in ("AGENT_PLAN_REVIEW", "AGENT_FINAL_REVIEW"):
            payload_keys = set(payload)
            native_required = {
                "eventProtocolVersion", "decision", "review", "requestFingerprint",
            }
            if (payload.get("eventProtocolVersion") == "AWB-REVIEW-EVENT-v7" and
                    native_required.issubset(payload_keys) and
                    payload_keys.difference(native_required).issubset(
                        {"reviewerClaim", "autoGate"}) and
                    isinstance(payload.get("review"), dict)):
                record_format = "AWB-REVIEW-EVENT-v7"
                event_review = payload.get("review")
            elif (payload_keys in (
                    {"decision", "review", "requestFingerprint"},
                    {"decision", "review", "requestFingerprint", "reviewerClaim"}) and
                  isinstance(payload.get("review"), dict) and
                  isinstance(payload.get("requestFingerprint"), str) and
                  payload.get("requestFingerprint")):
                record_format = "AWB-REVIEW-EVENT-v6"
                event_review = payload.get("review")
            elif (payload_keys == {"decision", "review"} and
                  isinstance(payload.get("review"), dict)):
                record_format = "AWB-REVIEW-EVENT-v3-v6"
                event_review = payload.get("review")
            elif (payload_keys == {"decision", "summary"} and
                  isinstance(payload.get("summary"), str)):
                record_format = "AWB-REVIEW-SUMMARY-EVENT-v3-v6"
                event_review = _decoded_review({
                    "summary": payload["summary"],
                    "stage": ("PLAN" if event["event_type"] ==
                              "AGENT_PLAN_REVIEW" else "FINAL"),
                    "decision": payload.get("decision"),
                })
            else:
                record_format = "UNKNOWN"
                event_review = None
            review_events.append({
                "eventId": event["event_id"],
                "eventType": event["event_type"],
                "actorKind": event["actor_kind"],
                "actorId": event["actor_id"],
                "requestId": event["request_id"],
                "createdAt": event["created_at"],
                "decision": payload.get("decision"),
                "requestFingerprint": payload.get("requestFingerprint"),
                "review": event_review,
                "recordFormat": record_format,
                "rawSummary": payload.get("summary"),
                "artifactAtReview": artifact_at_event,
                "candidateAtReview": candidate_at_event,
            })
        elif event["event_type"] in (
                "HUMAN_PLAN_GATE", "HUMAN_FINAL_GATE", "AUTO_GATE_APPROVED"):
            gate_events.append({
                "eventId": event["event_id"],
                "eventType": event["event_type"],
                "requestId": event["request_id"],
                "decision": payload.get("decision"),
                "stage": payload.get("stage"),
                "reviewId": payload.get("reviewId"),
                "reviewEventId": payload.get("reviewEventId"),
                "reviewRequestId": payload.get("reviewRequestId"),
                "reviewRound": payload.get("reviewRound"),
            })
    return workflow_kernel.canonical_projection(
        item, tasks, claims, writers, leases, reviews, event_head,
        history_digest, artifact_head=artifact_head,
        artifact_sha=artifact_sha, evaluation_time=evaluation_time,
        review_events=review_events, gate_events=gate_events,
    )


def _kernel_assert(connection, work_item_id, project_root=None, phase="post",
                   allow_time_split=False, evaluation_time=None):
    snapshot = _kernel_snapshot(
        connection, work_item_id, project_root,
        evaluation_time=evaluation_time,
    )
    try:
        workflow_kernel.assert_invariants(
            snapshot, phase=phase, allow_time_split=allow_time_split
        )
    except RuntimeError as exc:
        prefix = ("INTERNAL_INVARIANT_VIOLATION" if phase == "post" else
                  "WORKFLOW_INVARIANT_VIOLATION")
        raise LiteError("{0}:{1}".format(prefix, str(exc).split(":")[-1]))
    return snapshot


def _kernel_apply(connection, work_item_id, snapshot, plan, project_root=None,
                  evaluation_time=None):
    """The only public-adapter seam that may materialize lifecycle writes."""
    try:
        return workflow_kernel.apply_transition_plan(
            connection, snapshot, plan,
            lambda: _kernel_snapshot(
                connection, work_item_id, project_root,
                evaluation_time=evaluation_time,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        raise LiteError("INTERNAL_INVARIANT_VIOLATION:{0}".format(exc))


def _stale_activity_recipe(snapshot, work_item_id):
    expected = []
    kinds = (("claims", "AGENT_CLAIM", "AGENT"),
             ("writers", "REPOSITORY_WRITER", "AGENT"),
             ("leases", "ORCHESTRATOR_LEASE", "ORCHESTRATOR"))
    for collection, kind, owner_kind in kinds:
        for row in snapshot[collection]:
            if row["effectiveStatus"] != "STALE":
                continue
            value = {
                "kind": kind, "resourceId": row["id"],
                "workItemId": work_item_id, "ownerKind": owner_kind,
                "ownerId": row["owner"], "generation": row["generation"],
                "expiresAt": row["expiresAt"], "persistedStatus": "ACTIVE",
                "effectiveStatus": "STALE",
            }
            if row.get("taskId") is not None:
                value["taskId"] = row["taskId"]
            if row.get("repositoryKey") is not None:
                value["repositoryKey"] = row["repositoryKey"]
            expected.append(value)
    expected.sort(key=lambda row: (row["kind"], row["resourceId"]))
    if not expected:
        return None, None, None
    live_claims = [row for row in snapshot["claims"]
                   if row["effectiveStatus"] == "LIVE"]
    live_agent_activity = [row for collection in
                           (snapshot["claims"], snapshot["writers"])
                           for row in collection
                           if row["effectiveStatus"] == "LIVE"]
    live_activity = [row for collection in
                     (snapshot["claims"], snapshot["writers"], snapshot["leases"])
                     for row in collection if row["effectiveStatus"] == "LIVE"]
    stale_claims = [row for row in snapshot["claims"]
                    if row["effectiveStatus"] == "STALE"]
    stale_writers = [row for row in snapshot["writers"]
                     if row["effectiveStatus"] == "STALE"]
    # Agent claim/writer resources are one bound ownership bundle.  A LIVE
    # member makes a stale sibling ambiguous, but an independently fenced LIVE
    # Orchestrator lease is not part of that bundle and must be preserved.
    if stale_claims and live_agent_activity:
        return None, None, None
    # Writer-only expiry may preserve its exact live owner claim.  A live claim
    # owned by anyone else is an ambiguous repository hand-off.
    if stale_writers and live_claims and any(
            writer["owner"] != claim["owner"]
            for writer in stale_writers for claim in live_claims):
        return None, None, None
    request_id = "workflow-expire-" + snapshot["projectionFingerprint"][:24]
    not_after = min((row["expiresAt"] for row in live_activity), default=None)
    return expected, request_id, not_after


def _orphan_reviewer_recipe(connection, snapshot, project_root):
    """Recognize only the frozen AWB-024 public-b6 projection split."""
    item = snapshot["workItem"]
    if (item["work_item_id"] != "AWB-024" or item["state"] != "DRAFT" or
            item["queue_state"] != "CLAIMABLE" or
            item["current_role"] != "PLANNER"):
        return None
    tasks = snapshot["tasks"]
    reviewers = [row for row in tasks if row["owner_role"] == "REVIEWER"]
    authors = [row for row in tasks if row["owner_role"] in
               ("PLANNER", "IMPLEMENTER")]
    if (len(reviewers) != 1 or reviewers[0]["status"] != "IN_PROGRESS" or
            any(row["status"] != "NOT_STARTED" for row in authors)):
        return None
    if any(row["status"] == "ACTIVE" for collection in
           (snapshot["claims"], snapshot["writers"], snapshot["leases"])
           for row in collection):
        return None
    review_rows = connection.execute(
        "SELECT * FROM reviews WHERE work_item_id=? AND stage='PLAN' ORDER BY rowid",
        (item["work_item_id"],),
    ).fetchall()
    if len(review_rows) != 1:
        return None
    review = _decoded_review(review_rows[0])
    if (review.get("round") != 1 or review.get("result") != "REVISE_TO_PLANNER" or
            review_rows[0]["decision"] != "REJECTED"):
        return None
    event = connection.execute(
        "SELECT * FROM events WHERE work_item_id=? AND event_type='AGENT_PLAN_REVIEW' "
        "ORDER BY event_id DESC LIMIT 1", (item["work_item_id"],),
    ).fetchone()
    if event is None or event["actor_id"] != review_rows[0]["reviewer_agent_id"]:
        return None
    claim_rows = connection.execute(
        "SELECT * FROM claims WHERE work_item_id=? AND role='REVIEWER' "
        "AND agent_id=? AND status='RELEASED' ORDER BY generation",
        (item["work_item_id"], event["actor_id"]),
    ).fetchall()
    if len(claim_rows) != 1 or claim_rows[0]["task_id"] != reviewers[0]["task_id"]:
        return None
    later = connection.execute(
        "SELECT 1 FROM events WHERE work_item_id=? AND event_id>? AND event_type IN "
        "('AGENT_PLAN_REVIEW','SUBMIT_PLAN','HUMAN_PLAN_GATE',"
        "'AUTO_GATE_APPROVED','WORK_ITEM_MANAGEMENT_AMENDED') LIMIT 1",
        (item["work_item_id"], event["event_id"]),
    ).fetchone()
    if later is not None:
        return None
    head = snapshot.get("artifactHead") or {}
    if (head.get("path") != "docs/work-items/AWB-024-v0.3.1b7-preview-release.md" or
            snapshot.get("artifactSha256") !=
            "c406cffab3dc3ec91853637f08f2d7d80f45632457bc59e9d9e40019af07c09a"):
        return None
    return {"taskId": reviewers[0]["task_id"],
            "reviewId": review_rows[0]["review_id"],
            "reviewEventId": event["event_id"],
            "claimId": claim_rows[0]["claim_id"]}


def _check_one(connection, project_root, work_item_id, evaluation_time=None):
    snapshot = _kernel_snapshot(connection, work_item_id, project_root,
                                evaluation_time=evaluation_time)
    violations = workflow_kernel.invariant_violations(snapshot)
    recipe = _orphan_reviewer_recipe(connection, snapshot, project_root)
    if recipe is not None:
        # The closed recipe is stronger than the generic invariant classifier.
        request_id = "workflow-repair-" + snapshot["projectionFingerprint"][:24]
        return workflow_kernel.repair_result(
            work_item_id, snapshot, violations,
            recipe=workflow_kernel.RESET_ORPHAN_REVIEWER_TASK,
            request_id=request_id,
        )
    expected, request_id, not_after = _stale_activity_recipe(
        snapshot, work_item_id
    )
    if expected is not None:
        return workflow_kernel.repair_result(
            work_item_id, snapshot, violations,
            request_id=request_id, expected_activity=expected,
            not_after=not_after,
        )
    return workflow_kernel.repair_result(work_item_id, snapshot, violations)


def workflow_check(database, project_root, work_item_id=None):
    """Read-only invariant check for one WorkItem or the complete board."""
    connection = open_database(database)
    try:
        connection.execute("BEGIN")
        if work_item_id is not None:
            result = _check_one(connection, project_root, work_item_id)
            connection.rollback()
            return result
        identifiers = [row[0] for row in connection.execute(
            "SELECT work_item_id FROM work_items ORDER BY work_item_id"
        )]
        results = [_check_one(connection, project_root, value)
                   for value in identifiers]
        connection.rollback()
        status = ("PASS" if all(row["status"] == "PASS" for row in results)
                  else "WAITING_HUMAN" if any(row["status"] == "WAITING_HUMAN"
                                               for row in results)
                  else "VIOLATION")
        digest = workflow_kernel.content_fingerprint([
            {"workItemId": row["workItemId"],
             "projectionFingerprint": row["projectionFingerprint"],
             "status": row["status"]} for row in results
        ])
        return {
            "protocolVersion": workflow_kernel.CHECK_PROTOCOL,
            "operation": "CHECK_ALL", "status": status,
            "projectionFingerprint": digest, "results": results,
            "nextStep": {"action": ("NONE" if status == "PASS" else
                                    "CONSUME_EACH_EXACT_RESULT"),
                         "arguments": {}},
        }
    finally:
        connection.close()


def workflow_repair(database, project_root, work_item_id, action,
                    fingerprint, request_id, human_id):
    if action != workflow_kernel.RESET_ORPHAN_REVIEWER_TASK:
        return {
            "protocolVersion": workflow_kernel.REPAIR_PROTOCOL,
            "operation": "REPAIR", "status": "WAITING_HUMAN",
            "reasonCode": "UNREGISTERED_OR_AMBIGUOUS_REPAIR",
            "workItemId": work_item_id,
            "nextStep": {"action": "HUMAN_INSPECT_WORKFLOW_PROJECTION",
                         "arguments": {}},
        }
    if not human_id or not request_id or not fingerprint:
        raise LiteError("workflow repair requires exact proof and HUMAN identity")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = _event_payload(replay)
            if (replay["event_type"] != "WORKFLOW_REPAIRED" or
                    replay["actor_kind"] != "HUMAN" or
                    replay["actor_id"] != human_id or
                    payload.get("action") != action or
                    payload.get("fromFingerprint") != fingerprint):
                raise LiteError("REQUEST_REPLAY_CONFLICT")
            replay_receipt = dict(payload["receipt"])
            if not replay_receipt.get("toFingerprint"):
                raise LiteError("INCOMPLETE_REPAIR_RECEIPT")
            connection.rollback()
            return replay_receipt
        check = _check_one(connection, project_root, work_item_id)
        expected = check.get("nextStep", {}).get("arguments", {})
        if (check.get("repairability") != "DETERMINISTIC" or
                check.get("projectionFingerprint") != fingerprint or
                expected.get("action") != action or
                expected.get("requestId") != request_id):
            connection.rollback()
            return {
                "protocolVersion": workflow_kernel.REPAIR_PROTOCOL,
                "operation": "REPAIR", "status": "REFUSED",
                "reasonCode": "STALE_REPAIR_PROOF", "workItemId": work_item_id,
                "nextStep": {"action": "RUN_WORKFLOW_CHECK", "arguments": {
                    "workItem": work_item_id}},
            }
        snapshot = _kernel_snapshot(connection, work_item_id, project_root)
        proof = _orphan_reviewer_recipe(connection, snapshot, project_root)
        if proof is None:
            raise LiteError("STALE_REPAIR_PROOF")
        item = _item(connection, work_item_id)
        now = _now()
        event_id = connection.execute(
            "SELECT coalesce(max(event_id),0)+1 FROM events"
        ).fetchone()[0]
        receipt = {
            "protocolVersion": MUTATION_RECEIPT_PROTOCOL,
            "operation": "WORKFLOW_REPAIRED", "status": "OK",
            "workItemId": work_item_id, "eventId": event_id,
            "rowVersion": item["row_version"] + 1,
            "fromFingerprint": fingerprint,
            "nextStep": {"action": "RUN_WORKFLOW_CHECK",
                         "arguments": {"workItem": work_item_id}},
        }
        payload = {
            "protocolVersion": workflow_kernel.REPAIR_PROTOCOL,
            "action": action, "fromFingerprint": fingerprint,
            "proof": proof, "changedProjection": ["REVIEWER_TASK_STATUS"],
            "historyPreserved": True, "receipt": receipt,
        }
        intent = {
            "operation": workflow_kernel.RESET_ORPHAN_REVIEWER_TASK,
            "workItemId": work_item_id, "requestId": request_id,
            "actorKind": "HUMAN", "actorId": human_id, "now": now,
            "taskId": proof["taskId"], "eventId": event_id, "payload": payload,
        }
        # Determine the exact post state without committing any write.  The
        # canonical history digest intentionally ignores only this receipt's
        # self-reference, so the final materialization has the same fingerprint.
        connection.execute("SAVEPOINT workflow_repair_receipt")
        trial_post = _kernel_apply(
            connection, work_item_id, snapshot,
            workflow_kernel.plan_orphan_repair(snapshot, intent),
            project_root=project_root, evaluation_time=now,
        )
        connection.execute("ROLLBACK TO workflow_repair_receipt")
        connection.execute("RELEASE workflow_repair_receipt")
        receipt["toFingerprint"] = trial_post["projectionFingerprint"]
        payload["receipt"] = dict(receipt)
        post = _kernel_apply(
            connection, work_item_id, snapshot,
            workflow_kernel.plan_orphan_repair(snapshot, intent),
            project_root=project_root, evaluation_time=now,
        )
        if post["projectionFingerprint"] != receipt["toFingerprint"]:
            raise LiteError("INTERNAL_INVARIANT_VIOLATION:REPAIR_RECEIPT_DRIFT")
        _workflow_repair_materialized(connection, work_item_id, request_id)
        connection.commit()
        return receipt
    except RuntimeError as exc:
        connection.rollback()
        raise LiteError("INTERNAL_INVARIANT_VIOLATION:{0}".format(exc))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _workflow_repair_materialized(connection, work_item_id, request_id):
    """Fault-injection seam after exact repair materialization and before commit."""
    return None


def timeline(database, work_item_id):
    connection = open_database(database)
    try:
        _item(connection, work_item_id)
        return [dict(row) for row in connection.execute(
            "SELECT * FROM events WHERE work_item_id=? ORDER BY event_id", (work_item_id,)
        )]
    finally:
        connection.close()


def _public_item(database, work_item_id):
    item = get_work_item(database, work_item_id)
    for task in item["tasks"]:
        try:
            task["evidence"] = json.loads(task.pop("evidence_json"))
        except (TypeError, ValueError):
            task["evidence"] = []
    return item


def _board_html(database):
    states = (
        "DRAFT", "PLAN_REVIEW_PENDING", "PLAN_REVIEW_APPROVED",
        "IMPLEMENTING", "IMPLEMENTATION_COMPLETED", "FINAL_ACCEPTANCE_APPROVED",
    )
    labels = {
        "DRAFT": "草稿", "PLAN_REVIEW_PENDING": "待规划复审",
        "PLAN_REVIEW_APPROVED": "规划复审通过", "IMPLEMENTING": "实施中",
        "IMPLEMENTATION_COMPLETED": "实施完成", "FINAL_ACCEPTANCE_APPROVED": "最终验收通过",
    }
    items = list_work_items(database)
    columns = []
    for state in states:
        active = [item for item in items if item["state"] == state and item["queue_state"] != "HELD"]
        held = [item for item in items if item["state"] == state and item["queue_state"] == "HELD"]

        def cards(rows):
            if not rows:
                return '<p class="empty">无</p>'
            return "".join(
                '<a class="card" href="/v1/work-items/{0}"><strong>{0}</strong>'
                '<span>{1}</span><small>{2} · {3} · {4}</small>'
                '<small>{5}% · {6}</small></a>'.format(
                    html.escape(row["work_item_id"]), html.escape(row["title"]),
                    html.escape(row["priority"]), html.escape(row["queue_state"]),
                    html.escape(row["current_role"] or "-"), row["progress"]["percent"],
                    html.escape(row["nextStep"])
                ) for row in rows
            )

        columns.append(
            '<section><h2>{0}</h2>{1}<h3>搁置</h3>{2}</section>'.format(
                labels[state], cards(active), cards(held)
            )
        )
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent Workboard Lite</title>
<style>body{{font-family:system-ui;margin:0;background:#f5f7fa;color:#172033}}header{{padding:18px 22px;background:#172033;color:white}}.board{{display:grid;grid-template-columns:repeat(6,minmax(220px,1fr));gap:12px;padding:14px;overflow:auto}}section{{background:white;border-radius:10px;padding:12px;min-height:240px}}h2{{font-size:16px;margin:0 0 10px}}h3{{font-size:13px;border-top:1px solid #ddd;padding-top:10px}}.card{{display:block;color:inherit;text-decoration:none;border:1px solid #d9e0e8;border-radius:8px;padding:9px;margin:8px 0}}.card span,.card small{{display:block;margin-top:4px}}.card small,.empty{{color:#64748b;font-size:12px}}</style></head>
<body><header><strong>Agent Workboard · MVP-LITE-v1</strong></header><main class="board">{0}</main></body></html>""".format("".join(columns))


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class LiteRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentWorkboardLite/1"
    sys_version = ""

    def _headers(self, content_type, length, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _json_response(self, status, value):
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self._headers("application/json; charset=utf-8", len(body), status)
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment:
            self._json_response(400, {"error": "query_not_supported"})
            return
        try:
            path = unquote(parsed.path, errors="strict")
        except (UnicodeError, ValueError):
            self._json_response(400, {"error": "invalid_path"})
            return
        try:
            if path == "/healthz":
                connection = open_database(self.server.database)
                try:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    connection.close()
                self._json_response(200, {"status": "ok", "schemaVersion": SCHEMA_VERSION, "integrity": integrity})
                return
            if path == "/":
                body = _board_html(self.server.database).encode("utf-8")
                self._headers("text/html; charset=utf-8", len(body))
                self.wfile.write(body)
                return
            if path == "/v1/work-items":
                self._json_response(200, list_work_items(self.server.database))
                return
            prefix = "/v1/work-items/"
            if path.startswith(prefix):
                suffix = path[len(prefix):]
                if not suffix or "/" in suffix or suffix in (".", ".."):
                    raise LiteError("invalid WorkItem path")
                self._json_response(200, _public_item(self.server.database, suffix))
                return
            self._json_response(404, {"error": "not_found"})
        except LiteError as exc:
            status = 404 if str(exc) == "WorkItem does not exist" else 400
            self._json_response(status, {"error": str(exc)})

    def _not_allowed(self):
        self._json_response(405, {"error": "method_not_allowed"})

    do_POST = _not_allowed
    do_PUT = _not_allowed
    do_PATCH = _not_allowed
    do_DELETE = _not_allowed

    def log_message(self, fmt, *args):
        return


def make_server(database, host="127.0.0.1", port=8787):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise LiteError("host must be a literal loopback IP")
    if not address.is_loopback:
        raise LiteError("refusing non-loopback listener")
    connection = open_database(database)
    connection.close()
    server = _ThreadingHTTPServer((host, port), LiteRequestHandler)
    server.database = database
    return server


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _reason_code(message):
    explicit = re.match(r"^[A-Z][A-Z0-9_]+$", str(message))
    if explicit:
        return str(message)
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(message).upper()).strip("_")
    return normalized[:96] or "MUTATION_REFUSED"


def mutation_receipt(database, work_item_id, request_id, operation):
    connection = open_database(database)
    try:
        item = _item(connection, work_item_id)
        event = connection.execute(
            "SELECT event_id,event_type FROM events WHERE request_id=?", (request_id,)
        ).fetchone() if request_id else None
    finally:
        connection.close()
    projection = get_work_item(database, work_item_id)
    action = projection.get("nextStep") or "NONE"
    return {
        "protocolVersion": MUTATION_RECEIPT_PROTOCOL,
        "operation": event["event_type"] if event else operation,
        "status": "OK", "reasonCode": None, "workItemId": work_item_id,
        "state": item["state"], "queueState": item["queue_state"],
        "currentRole": item["current_role"], "currentTask": projection.get("currentTask"),
        "eventId": event["event_id"] if event else None,
        "rowVersion": item["row_version"],
        "nextStep": {"action": action, "arguments": {}},
    }


def mutation_error_receipt(database, work_item_id, operation, message):
    state = queue = role = task = row_version = None
    if work_item_id:
        try:
            projection = get_work_item(database, work_item_id)
            state, queue = projection["state"], projection["queue_state"]
            role, task = projection["current_role"], projection.get("currentTask")
            row_version = projection["row_version"]
        except LiteError:
            pass
    code = _reason_code(message)
    return {
        "protocolVersion": MUTATION_RECEIPT_PROTOCOL, "operation": operation,
        "status": "REFUSED", "reasonCode": code, "workItemId": work_item_id,
        "state": state, "queueState": queue, "currentRole": role,
        "currentTask": task, "eventId": None, "rowVersion": row_version,
        "nextStep": {"action": "RESOLVE_" + code, "arguments": {}},
    }


def _mutation_print(database, work_item_id, request_id, operation, full, result=None):
    _print(result if full else mutation_receipt(database, work_item_id, request_id, operation))


def _load_json_file(path, label):
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise LiteError("cannot read {0}: {1}".format(label, exc))
    if not isinstance(value, dict):
        raise LiteError("{0} must contain a JSON object".format(label))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-workboard-lite")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--project-root")
    parser.add_argument("--repository-key")
    parser.add_argument("--usage-policy", choices=USAGE_POLICIES, default="BEST_EFFORT")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    create = sub.add_parser("create")
    create.add_argument("work_item_id")
    create.add_argument("--type", required=True, choices=("TI", "FE", "R", "WA", "AWB"))
    create.add_argument("--title", required=True)
    create.add_argument("--mode", choices=("STANDARD", "READ_ONLY_DIAGNOSIS"), default="STANDARD")
    create.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")
    create.add_argument("--management-file", required=True)
    create.add_argument("--risk-file", required=True)
    create.add_argument("--human-review", choices=("auto-on-pass", "manual"))
    create.add_argument("--decision-actor")
    create.add_argument("--request-id")
    create.add_argument("--full", action="store_true")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("work_item_id")
    timeline_parser = sub.add_parser("timeline")
    timeline_parser.add_argument("work_item_id")
    claim = sub.add_parser("claim")
    claim.add_argument("work_item_id")
    claim.add_argument("task_id")
    claim.add_argument("--agent", required=True)
    claim.add_argument("--role", required=True, choices=("PLANNER", "IMPLEMENTER", "REVIEWER", "ORCHESTRATOR"))
    claim.add_argument("--ttl", type=int, default=900)
    claim.add_argument("--session-id")
    claim.add_argument("--usage-provider")
    claim.add_argument("--model")
    claim.add_argument("--orchestrator-id")
    claim.add_argument("--orchestrator-generation", type=int)
    claim.add_argument("--request-id")
    claim.add_argument("--full", action="store_true")
    release = sub.add_parser("release")
    release.add_argument("work_item_id")
    release.add_argument("--agent", required=True)
    release.add_argument("--request-id")
    release.add_argument("--full", action="store_true")
    lock = sub.add_parser("lock")
    lock.add_argument("work_item_id")
    lock.add_argument("--repository", required=True)
    lock.add_argument("--agent", required=True)
    lock.add_argument("--ttl", type=int, default=900)
    lock.add_argument("--request-id")
    lock.add_argument("--full", action="store_true")
    unlock = sub.add_parser("unlock")
    unlock.add_argument("work_item_id")
    unlock.add_argument("--repository", required=True)
    unlock.add_argument("--agent", required=True)
    unlock.add_argument("--request-id")
    unlock.add_argument("--full", action="store_true")
    task = sub.add_parser("task")
    task.add_argument("work_item_id")
    task.add_argument("task_id")
    task.add_argument("--agent", required=True)
    task.add_argument("--status", required=True, choices=("IN_PROGRESS", "BLOCKED", "WAITING_ACCEPTANCE", "COMPLETED", "CANCELLED"))
    task.add_argument("--evidence", action="append", default=[])
    task.add_argument("--request-id")
    task.add_argument("--full", action="store_true")
    transition_parser = sub.add_parser("transition")
    transition_parser.add_argument("work_item_id")
    transition_parser.add_argument("action", choices=("submit_plan", "start_implementation", "submit_implementation"))
    transition_parser.add_argument("--agent", required=True)
    transition_parser.add_argument("--local-tests-passed", action="store_true")
    transition_parser.add_argument("--submission-file")
    transition_parser.add_argument("--quality-file")
    transition_parser.add_argument("--plan-artifact")
    transition_parser.add_argument("--request-id")
    transition_parser.add_argument("--candidate")
    transition_parser.add_argument("--candidate-fingerprint")
    transition_parser.add_argument("--full", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("work_item_id")
    review.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    review.add_argument("--agent", required=True)
    review.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    review.add_argument("--summary")
    review.add_argument("--review-file")
    review.add_argument("--replacement-file")
    review.add_argument("--request-id")
    review.add_argument("--full", action="store_true")
    gate = sub.add_parser("gate")
    gate.add_argument("work_item_id")
    gate.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    gate.add_argument("--human", required=True)
    gate.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    gate.add_argument("--reason", required=True)
    gate.add_argument("--request-id")
    gate.add_argument("--full", action="store_true")
    for command in ("hold", "resume"):
        hold = sub.add_parser(command)
        hold.add_argument("work_item_id")
        hold.add_argument("--human", required=True)
        hold.add_argument("--reason", default="USER_PAUSED")
        hold.add_argument("--request-id")
        hold.add_argument("--full", action="store_true")
    unblock = sub.add_parser("unblock")
    unblock.add_argument("work_item_id")
    unblock.add_argument("task_id")
    unblock.add_argument("--human", required=True)
    unblock.add_argument("--reason", required=True)
    unblock.add_argument("--request-id")
    unblock.add_argument("--full", action="store_true")
    recover = sub.add_parser("recover-review-task")
    recover.add_argument("work_item_id")
    recover.add_argument("task_id")
    recover.add_argument("--human")
    recover.add_argument("--reason")
    recover.add_argument("--request-id")
    recover.add_argument("--full", action="store_true")
    backfill = sub.add_parser("management-backfill")
    backfill.add_argument("work_item_id")
    backfill.add_argument("--agent", required=True)
    backfill.add_argument("--management-file", required=True)
    backfill.add_argument("--basis", required=True)
    backfill.add_argument("--request-id")
    backfill.add_argument("--full", action="store_true")
    amend = sub.add_parser("management-amend")
    amend.add_argument("work_item_id")
    amend.add_argument("--human", required=True)
    amend.add_argument("--management-file", required=True)
    amend.add_argument("--reason", required=True)
    amend.add_argument("--request-id")
    amend.add_argument("--full", action="store_true")
    deviation = sub.add_parser("plan-deviation")
    deviation.add_argument("work_item_id")
    deviation.add_argument("task_id")
    deviation.add_argument("--agent", required=True)
    deviation.add_argument("--evidence-file", required=True)
    deviation.add_argument("--request-id")
    deviation.add_argument("--full", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _print(initialize_database(args.database))
        elif args.command == "create":
            request_id = args.request_id or _id("create")
            result = create_work_item(
                args.database, args.work_item_id, args.type, args.title, args.mode, args.priority,
                management=_load_json_file(args.management_file, "management file"),
                creation_risk=_load_json_file(args.risk_file, "risk file"),
                human_review=({"auto-on-pass": "AUTO_ON_PASS", "manual": "MANUAL"}.get(
                    args.human_review
                )),
                decision_actor=args.decision_actor,
                request_id=request_id,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            "WORK_ITEM_CREATED", args.full, result)
        elif args.command == "list":
            _print(list_work_items(args.database))
        elif args.command == "show":
            _print(get_work_item(args.database, args.work_item_id))
        elif args.command == "timeline":
            _print(timeline(args.database, args.work_item_id))
        elif args.command == "claim":
            if args.ttl < 1:
                raise LiteError("ttl must be positive")
            expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=args.ttl)).replace(microsecond=0).isoformat()
            request_id = args.request_id or _id("claim")
            acquire_claim(args.database, args.work_item_id, args.task_id, args.agent,
                          args.role, expires, request_id=request_id,
                          session_id=args.session_id, usage_provider=args.usage_provider,
                          model=args.model, orchestrator_id=args.orchestrator_id,
                          orchestrator_generation=args.orchestrator_generation,
                          usage_policy=args.usage_policy)
            result = get_work_item(args.database, args.work_item_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "CLAIM_ACQUIRED", args.full, result)
        elif args.command == "release":
            request_id = args.request_id or _id("release")
            release_claim(args.database, args.work_item_id, args.agent,
                          request_id=request_id, usage_policy=args.usage_policy)
            result = get_work_item(args.database, args.work_item_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "CLAIM_RELEASED", args.full, result)
        elif args.command == "lock":
            if args.ttl < 1:
                raise LiteError("ttl must be positive")
            expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=args.ttl)).replace(microsecond=0).isoformat()
            request_id = args.request_id or _id("repo-lock")
            acquire_repository_lock(args.database, args.work_item_id, args.repository,
                                    args.agent, expires, request_id=request_id)
            result = get_work_item(args.database, args.work_item_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "REPOSITORY_LOCK_ACQUIRED", args.full, result)
        elif args.command == "unlock":
            request_id = args.request_id or _id("repo-release")
            release_repository_lock(args.database, args.work_item_id, args.repository,
                                    args.agent, request_id=request_id)
            result = get_work_item(args.database, args.work_item_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "REPOSITORY_LOCK_RELEASED", args.full, result)
        elif args.command == "task":
            request_id = args.request_id or _id("task")
            set_task_status(args.database, args.work_item_id, args.task_id, args.agent,
                            args.status, args.evidence, request_id=request_id,
                            usage_policy=args.usage_policy)
            result = get_work_item(args.database, args.work_item_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "TASK_STATUS_CHANGED", args.full, result)
        elif args.command == "transition":
            request_id = args.request_id or _id(args.action)
            result = transition(
                args.database, args.work_item_id, args.action, args.agent,
                local_tests_passed=args.local_tests_passed,
                submission=_load_json_file(args.submission_file, "submission file"),
                quality_baseline=_load_json_file(args.quality_file, "quality file"),
                usage_policy=args.usage_policy,
                request_id=request_id,
                plan_artifact=({"projectRoot": args.project_root,
                                "path": args.plan_artifact}
                               if args.plan_artifact else None),
                candidate_id=args.candidate,
                candidate_fingerprint=args.candidate_fingerprint,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            args.action.upper(), args.full, result)
        elif args.command == "review":
            review_summary = _load_json_file(args.review_file, "review file") if args.review_file else args.summary
            if review_summary is None:
                raise LiteError("review requires --summary or --review-file")
            if args.replacement_file:
                if args.stage != "PLAN" or args.decision != "REJECTED" or not args.review_file:
                    raise LiteError("replacement-file requires a structured rejected PLAN review")
                if not args.project_root or not args.repository_key:
                    raise LiteError("replacement-file requires configured project identity")
                result = amend_plan_review(
                    args.database, args.work_item_id, args.agent, args.replacement_file,
                    args.project_root, args.repository_key, review_summary,
                    args.request_id, usage_policy=args.usage_policy,
                )
            else:
                request_id = args.request_id or _id("review")
                result = record_agent_review(
                    args.database, args.work_item_id, args.stage, args.agent,
                    args.decision, review_summary, request_id=request_id,
                    usage_policy=args.usage_policy,
                )
            request_id = args.request_id or request_id
            _mutation_print(args.database, args.work_item_id, request_id,
                            "AGENT_{0}_REVIEW".format(args.stage), args.full, result)
        elif args.command == "gate":
            request_id = args.request_id or _id("human")
            result = record_human_gate(
                args.database, args.work_item_id, args.stage, args.human,
                args.decision, args.reason, request_id=request_id,
                usage_policy=args.usage_policy,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            "HUMAN_{0}_GATE".format(args.stage), args.full, result)
        elif args.command in ("hold", "resume"):
            request_id = args.request_id or _id("hold")
            result = set_hold(args.database, args.work_item_id, args.human,
                              args.command == "hold", args.reason, request_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "WORK_ITEM_HELD" if args.command == "hold" else "WORK_ITEM_RESUMED",
                            args.full, result)
        elif args.command == "unblock":
            request_id = args.request_id or _id("unblock")
            result = unblock_task(args.database, args.work_item_id, args.task_id,
                                  args.human, args.reason, request_id)
            _mutation_print(args.database, args.work_item_id, request_id,
                            "TASK_UNBLOCKED", args.full, result)
        elif args.command == "recover-review-task":
            result = recover_review_task(
                args.database, args.work_item_id, args.task_id, args.human,
                args.reason, args.request_id,
            )
            if args.full:
                _print(get_work_item(args.database, args.work_item_id))
            elif result["status"] == "REFUSED":
                _print(result)
            else:
                _print(mutation_receipt(args.database, args.work_item_id,
                                        args.request_id, "REVIEW_TASK_RECOVERED"))
            if result["status"] == "REFUSED":
                return 2
        elif args.command == "management-backfill":
            request_id = args.request_id or _id("management-backfill")
            result = backfill_management(
                args.database, args.work_item_id, args.agent,
                _load_json_file(args.management_file, "management file"), args.basis,
                request_id=request_id,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            "WORK_ITEM_MANAGEMENT_BACKFILLED", args.full, result)
        elif args.command == "management-amend":
            request_id = args.request_id or _id("management-amend")
            result = amend_management(
                args.database, args.work_item_id, args.human,
                _load_json_file(args.management_file, "management file"), args.reason,
                request_id=request_id,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            "WORK_ITEM_MANAGEMENT_AMENDED", args.full, result)
        elif args.command == "plan-deviation":
            request_id = args.request_id or _id("plan-deviation")
            result = report_plan_deviation(
                args.database, args.work_item_id, args.task_id, args.agent,
                _load_json_file(args.evidence_file, "evidence file"),
                request_id=request_id,
            )
            _mutation_print(args.database, args.work_item_id, request_id,
                            "PLAN_DEVIATION", args.full, result)
        elif args.command == "serve":
            server = make_server(args.database, args.host, args.port)
            try:
                print("Agent Workboard Lite listening on http://{0}:{1}".format(*server.server_address))
                server.serve_forever()
            finally:
                server.server_close()
        return 0
    except LiteError as exc:
        work_item_id = getattr(args, "work_item_id", None)
        operation = getattr(args, "command", "MUTATION").upper()
        _print(mutation_error_receipt(args.database, work_item_id, operation, exc))
        print("error: {0}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
