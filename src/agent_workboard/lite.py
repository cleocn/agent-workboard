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
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit


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


def _event(connection, work_item_id, request_id, event_type, actor_kind, actor_id, payload):
    existing = connection.execute(
        "SELECT event_type, payload_json FROM events WHERE request_id=?", (request_id,)
    ).fetchone()
    encoded = _json(payload)
    if existing:
        if existing["event_type"] != event_type or existing["payload_json"] != encoded:
            raise LiteError("request_id was already used with different content")
        return False
    connection.execute(
        "INSERT INTO events(work_item_id,request_id,event_type,actor_kind,actor_id,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (work_item_id, request_id, event_type, actor_kind, actor_id, encoded, _now()),
    )
    return True


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
        connection.execute(
            "INSERT INTO work_items(work_item_id,work_item_type,title,mode,priority,current_role,"
            "created_at,updated_at,human_gate_policy) VALUES(?,?,?,?,?,'PLANNER',?,?,?)",
            (work_item_id, item_type, title, mode, priority, now, now, policy),
        )
        for task in normalized["tasks"]:
            connection.execute(
                "INSERT INTO tasks(task_id,work_item_id,seq,title,owner_role,status,required,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (task["taskId"], work_item_id, task["seq"], task["title"], task["ownerRole"],
                 task["status"], int(task["required"]), now, now),
            )
        _event(connection, work_item_id, request_id, "WORK_ITEM_CREATED", "SYSTEM", actor_id,
               create_payload)
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
    for actual, contract in zip(database_tasks, normalized["tasks"][:len(database_tasks)]):
        if (actual["task_id"], actual["seq"], actual["owner_role"]) != (
            contract["taskId"], contract["seq"], contract["ownerRole"]
        ):
            raise LiteError("management task identity does not match existing WorkItem")
        if allow_task_amendment:
            connection.execute(
                "UPDATE tasks SET title=?,required=?,updated_at=? WHERE task_id=?",
                (contract["title"], int(contract["required"]), now, actual["task_id"]),
            )
        elif bool(actual["required"]) != contract["required"]:
            raise LiteError("management task required flag does not match existing WorkItem")
        contract["status"] = actual["status"]
        if not allow_task_amendment:
            contract["title"] = actual["title"]
    if allow_task_amendment:
        for contract in normalized["tasks"][len(database_tasks):]:
            connection.execute(
                "INSERT INTO tasks(task_id,work_item_id,seq,title,owner_role,status,required,evidence_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'NOT_STARTED',?,'[]',?,?)",
                (contract["taskId"], item["work_item_id"], contract["seq"], contract["title"],
                 contract["ownerRole"], int(contract["required"]), now, now),
            )
            contract["status"] = "NOT_STARTED"
        database_tasks = [dict(row) for row in connection.execute(
            "SELECT * FROM tasks WHERE work_item_id=? ORDER BY seq", (item["work_item_id"],)
        )]
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
    return normalized


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
        normalized = _align_management_with_item(connection, item, management, now)
        _event(connection, work_item_id, request_id, "WORK_ITEM_MANAGEMENT_BACKFILLED", "AGENT",
               agent_id, {"management": normalized, "basis": basis})
        connection.execute(
            "UPDATE work_items SET row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (now, work_item_id),
        )
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
        if previous is None:
            raise LiteError("management envelope must be backfilled before amendment")
        now = _now()
        normalized = _align_management_with_item(
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
        _event(connection, work_item_id, request_id, "WORK_ITEM_MANAGEMENT_AMENDED", "HUMAN",
               human_id, {"management": normalized, "changes": changes, "reason": reason,
                          "authorizedBy": human_id})
        connection.execute(
            "UPDATE work_items SET row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (now, work_item_id),
        )
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _expire_claims(connection, work_item_id, now):
    connection.execute(
        "UPDATE claims SET status='EXPIRED',released_at=? "
        "WHERE work_item_id=? AND status='ACTIVE' AND expires_at<=?",
        (now, work_item_id, now),
    )


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
        _validate_orchestrator_fence(
            connection, work_item_id, orchestrator_id, orchestrator_generation, now
        )
        _expire_claims(connection, work_item_id, now)
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
        connection.execute(
            "INSERT INTO claims VALUES(?,?,?,?,? ,?,'ACTIVE',?,?,NULL)",
            (claim_id, work_item_id, task_id, agent_id, role, generation, now, expires_at),
        )
        connection.execute(
            "UPDATE work_items SET queue_state='CLAIMED',current_role=?,row_version=row_version+1,updated_at=? "
            "WHERE work_item_id=?", (role, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "CLAIM_ACQUIRED", "AGENT", agent_id,
               {"claimId": claim_id, "taskId": task_id, "role": role, "generation": generation})
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
        now = _now()
        connection.execute("UPDATE claims SET status='RELEASED',released_at=? WHERE claim_id=?", (now, claim["claim_id"]))
        queue = item["queue_state"] if item["queue_state"] in ("WAITING_HUMAN", "HELD", "BLOCKED") else "CLAIMABLE"
        connection.execute(
            "UPDATE work_items SET queue_state=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (queue, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "CLAIM_RELEASED", "AGENT", agent_id,
               {"claimId": claim["claim_id"], "generation": claim["generation"]})
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
        claim = connection.execute(
            "SELECT * FROM claims WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, agent_id),
        ).fetchone()
        if claim is None or claim["role"] not in ("PLANNER", "IMPLEMENTER"):
            raise LiteError("repository lock requires Planner or Implementer claim")
        now = _now()
        connection.execute(
            "UPDATE repository_locks SET status='EXPIRED',released_at=? "
            "WHERE repository_key=? AND status='ACTIVE' AND expires_at<=?",
            (now, repository_key, now),
        )
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
        connection.execute(
            "INSERT INTO repository_locks VALUES(?,?,?,?,?,'ACTIVE',?,?,NULL)",
            (lock_id, repository_key, work_item_id, agent_id, generation, now, expires_at),
        )
        _event(connection, work_item_id, request_id, "REPOSITORY_LOCK_ACQUIRED", "AGENT", agent_id,
               {"repositoryKey": repository_key, "generation": generation})
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
        lock = connection.execute(
            "SELECT * FROM repository_locks WHERE repository_key=? AND work_item_id=? "
            "AND agent_id=? AND status='ACTIVE'",
            (repository_key, work_item_id, agent_id),
        ).fetchone()
        if lock is None:
            raise LiteError("active repository lock is not owned by agent")
        now = _now()
        connection.execute(
            "UPDATE repository_locks SET status='RELEASED',released_at=? WHERE lock_id=?",
            (now, lock["lock_id"]),
        )
        _event(connection, work_item_id, request_id, "REPOSITORY_LOCK_RELEASED", "AGENT", agent_id,
               {"repositoryKey": repository_key, "generation": lock["generation"]})
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
        allowed = {
            "NOT_STARTED": {"IN_PROGRESS"},
            "IN_PROGRESS": {"WAITING_ACCEPTANCE", "COMPLETED", "BLOCKED", "CANCELLED"},
            "WAITING_ACCEPTANCE": {"IN_PROGRESS", "COMPLETED"},
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
        now = _now()
        connection.execute(
            "UPDATE tasks SET status=?,evidence_json=?,updated_at=? WHERE task_id=? AND work_item_id=?",
            (status, _json(full_evidence), now, task_id, work_item_id),
        )
        queue = "BLOCKED" if status == "BLOCKED" else "CLAIMED"
        blocked = "TASK_BLOCKED" if status == "BLOCKED" else None
        connection.execute(
            "UPDATE work_items SET queue_state=?,blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (queue, blocked, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "TASK_STATUS_CHANGED", "AGENT", agent_id,
               {"taskId": task_id, "from": task["status"], "status": status,
                "evidence": full_evidence})
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


def _release_active(connection, work_item_id, now):
    connection.execute(
        "UPDATE claims SET status='RELEASED',released_at=? WHERE work_item_id=? AND status='ACTIVE'",
        (now, work_item_id),
    )


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
               usage_policy="BEST_EFFORT", plan_artifact=None):
    _usage_sync_boundary(database, work_item_id, usage_policy)
    request_id = request_id or _id(action)
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope must be backfilled before transition")
        now = _now()
        artifact_event = None
        if action == "submit_plan":
            _active_claim(connection, work_item_id, "PLANNER", agent_id)
            if item["state"] != "DRAFT":
                raise LiteError("submit_plan requires DRAFT")
            pending = connection.execute(
                "SELECT count(*) FROM tasks WHERE work_item_id=? AND owner_role='PLANNER' AND required=1 AND status<>'COMPLETED'",
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
            _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
            if item["state"] != "PLAN_REVIEW_APPROVED":
                raise LiteError("implementation requires approved plan")
            new_state, queue, role = "IMPLEMENTING", "CLAIMED", "IMPLEMENTER"
            release_after = False
        elif action == "submit_implementation":
            _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
            if item["state"] != "IMPLEMENTING":
                raise LiteError("submit_implementation requires IMPLEMENTING")
            pending = connection.execute(
                "SELECT count(*) FROM tasks WHERE work_item_id=? AND owner_role='IMPLEMENTER' AND required=1 AND status<>'COMPLETED'",
                (work_item_id,),
            ).fetchone()[0]
            if pending or not local_tests_passed:
                raise LiteError("implementation tasks and local tests must pass")
            quality_baseline = _validate_quality_baseline(
                connection, work_item_id, quality_baseline
            )
            new_state, queue, role = "IMPLEMENTATION_COMPLETED", "CLAIMABLE", "REVIEWER"
            release_after = True
        else:
            raise LiteError("unknown transition")
        if release_after and connection.execute(
            "SELECT 1 FROM repository_locks WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (work_item_id, agent_id),
        ).fetchone():
            raise LiteError("release repository lock before submitting work")
        if release_after:
            _release_active(connection, work_item_id, now)
        connection.execute(
            "UPDATE work_items SET state=?,queue_state=?,current_role=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (new_state, queue, role, now, work_item_id),
        )
        payload = {"from": item["state"], "to": new_state}
        if submission is not None:
            payload["submission"] = submission
        if quality_baseline is not None:
            payload["qualityBaseline"] = quality_baseline
        if artifact_event is not None:
            payload["planArtifact"] = artifact_event
        _event(connection, work_item_id, request_id, action.upper(), "AGENT", agent_id, payload)
        if artifact_event is not None:
            _event(connection, work_item_id, request_id + "-artifact", "PLAN_ARTIFACT_HEAD",
                   "AGENT", agent_id, artifact_event)
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


def _approved_gate_preconditions(connection, item, stage):
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


def _approve_gate(connection, item, stage, now):
    _approved_gate_preconditions(connection, item, stage)
    if stage == "PLAN":
        state, queue, role, held, closed = (
            "PLAN_REVIEW_APPROVED", "CLAIMABLE", "IMPLEMENTER", None, None
        )
    else:
        state, queue, role, held, closed = (
            "FINAL_ACCEPTANCE_APPROVED", "HELD", None, "TERMINAL_STATE", now
        )
    connection.execute(
        "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,"
        "blocked_reason=NULL,closed_at=?,row_version=row_version+1,updated_at=? "
        "WHERE work_item_id=?",
        (state, queue, role, held, closed, now, item["work_item_id"]),
    )


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
        _active_claim(connection, work_item_id, "REVIEWER", reviewer_agent_id)
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
        stored_decision = "APPROVED" if review["result"] == "PASS" else "REJECTED"
        now = _now()
        review_id = _id("review")
        connection.execute(
            "INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
            (review_id, work_item_id, stage, reviewer_agent_id, stored_decision, _json(review), now),
        )
        _release_active(connection, work_item_id, now)
        result = review["result"]
        round_number = review["round"]
        policy = item["human_gate_policy"] if "human_gate_policy" in item.keys() else "MANUAL"
        if result == "PASS" and item["mode"] == "STANDARD":
            state, queue, role = item["state"], "WAITING_HUMAN", None
        elif result == "PASS":
            state, queue, role = "PLAN_REVIEW_APPROVED", "HELD", None
        elif result == "BLOCKED":
            state, queue, role = item["state"], "BLOCKED", None
        elif result == "WAITING_HUMAN" or (round_number == 5 and result == "REVISE"):
            state, queue, role = item["state"], "WAITING_HUMAN", None
        elif (round_number == 3 and
              result in ("REVISE", "REVISE_TO_PLANNER")):
            state, queue, role = item["state"], "CLAIMABLE", "REVIEWER"
        else:
            state = "DRAFT" if stage == "PLAN" else "IMPLEMENTING"
            queue, role = "CLAIMABLE", author_role
            connection.execute(
                "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND owner_role=?",
                (now, work_item_id, author_role),
            )
        reviewer_status = None
        if result == "PASS" and (stage == "FINAL" or item["mode"] == "READ_ONLY_DIAGNOSIS"):
            reviewer_status = "COMPLETED"
        elif queue == "BLOCKED":
            reviewer_status = "BLOCKED"
        elif queue == "WAITING_HUMAN" and result != "PASS":
            reviewer_status = "WAITING_ACCEPTANCE"
        if reviewer_status:
            connection.execute(
                "UPDATE tasks SET status=?,evidence_json=?,updated_at=? WHERE work_item_id=? AND owner_role='REVIEWER'",
                (reviewer_status, _json([{"reviewRound": round_number, "result": result}]), now,
                 work_item_id),
            )
        auto_approved = False
        auto_gate_failure = None
        if (result == "PASS" and item["mode"] == "STANDARD" and
                policy == "AUTO_ON_PASS" and item["queue_state"] == "CLAIMED" and
                item["held_reason"] is None and item["blocked_reason"] is None):
            try:
                _approve_gate(connection, item, stage, now)
                auto_approved = True
            except LiteError as exc:
                # The independent PASS remains recorded, but runtime drift or
                # incomplete quality evidence deliberately falls back to HUMAN.
                auto_gate_failure = str(exc)
        if not auto_approved:
            connection.execute(
                "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,"
                "blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
                (state, queue, role, "TARGET_REACHED" if queue == "HELD" else None,
                 "REVIEW_BLOCKED" if queue == "BLOCKED" else None, now, work_item_id),
            )
        review_payload = {"decision": stored_decision, "review": review,
                          "requestFingerprint": fingerprint}
        if auto_gate_failure:
            review_payload["autoGate"] = {
                "status": "FAIL_CLOSED", "reason": auto_gate_failure,
            }
        _event(connection, work_item_id, request_id, "AGENT_{0}_REVIEW".format(stage), "AGENT",
               reviewer_agent_id, review_payload)
        if auto_approved:
            review_event = connection.execute(
                "SELECT event_id FROM events WHERE request_id=?", (request_id,)
            ).fetchone()[0]
            payload = {
                "policy": policy, "stage": stage, "reviewId": review_id,
                "reviewRound": round_number, "reviewRequestId": request_id,
                "reviewEventId": review_event,
                "idempotencyRequestId": request_id,
            }
            payload.update(_auto_gate_context(connection, work_item_id))
            _event(
                connection, work_item_id, "auto-gate-" + _sha(request_id + ":" + stage),
                "AUTO_GATE_APPROVED", "SYSTEM", "auto-gate", payload,
            )
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
        lock = connection.execute(
            "SELECT * FROM repository_locks WHERE lock_id=? AND work_item_id=? "
            "AND agent_id=? AND status='ACTIVE'", (lock_id, work_item_id, agent_id),
        ).fetchone()
        if lock is not None:
            now = _now()
            connection.execute(
                "UPDATE repository_locks SET status='RELEASED',released_at=? WHERE lock_id=?",
                (now, lock_id),
            )
            _event(connection, work_item_id, request_id, "REPOSITORY_LOCK_RELEASED",
                   "SYSTEM", "plan-amend", {
                       "repositoryKey": lock["repository_key"],
                       "generation": lock["generation"], "purpose": "PLAN_AMEND",
                       "reviewerAgentId": agent_id,
                   })
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
        connection.execute(
            "UPDATE repository_locks SET status='EXPIRED',released_at=? "
            "WHERE repository_key=? AND status='ACTIVE' AND expires_at<=?",
            (now, repository_key, now),
        )
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
        connection.execute(
            "INSERT INTO repository_locks VALUES(?,?,?,?,?,'ACTIVE',?,?,NULL)",
            (lock_id, repository_key, work_item_id, reviewer_agent_id, generation,
             now, expires_at),
        )
        _event(connection, work_item_id, "plan-amend-lock-" + _sha(request_id),
               "REPOSITORY_LOCK_ACQUIRED", "SYSTEM", "plan-amend", {
                   "repositoryKey": repository_key, "generation": generation,
                   "purpose": "PLAN_AMEND", "reviewerAgentId": reviewer_agent_id,
                   "path": relative, "baseRevision": head["revision"],
                   "baseSha256": head["sha256"], "requestId": request_id,
               })
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
            _active_claim(connection, work_item_id, "REVIEWER", reviewer_agent_id)
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
            connection.execute(
                "INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
                (_id("review"), work_item_id, "PLAN", reviewer_agent_id,
                 "REJECTED", _json(review), now),
            )
            _release_active(connection, work_item_id, now)
            connection.execute(
                "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? "
                "AND owner_role='REVIEWER' AND status='IN_PROGRESS'", (now, work_item_id),
            )
            connection.execute(
                "UPDATE work_items SET state='PLAN_REVIEW_PENDING',queue_state='CLAIMABLE',"
                "current_role='REVIEWER',held_reason=NULL,blocked_reason=NULL,"
                "row_version=row_version+1,updated_at=? WHERE work_item_id=?",
                (now, work_item_id),
            )
            _event(connection, work_item_id, request_id, "AGENT_PLAN_REVIEW", "AGENT",
                   reviewer_agent_id, {"decision": "REJECTED", "review": review,
                                       "requestFingerprint": fingerprint})
            _event(connection, work_item_id, request_id + "-artifact", "PLAN_ARTIFACT_HEAD",
                   "AGENT", reviewer_agent_id, new_head)
            connection.execute(
                "UPDATE repository_locks SET status='RELEASED',released_at=? WHERE lock_id=?",
                (now, lock_id),
            )
            _event(connection, work_item_id, request_id + "-release",
                   "REPOSITORY_LOCK_RELEASED", "SYSTEM", "plan-amend", {
                       "repositoryKey": repository_key, "generation": lock["generation"],
                       "purpose": "PLAN_AMEND", "reviewerAgentId": reviewer_agent_id,
                   })
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
        if item["mode"] != "STANDARD" or item["queue_state"] != "WAITING_HUMAN":
            raise LiteError("human gate is not waiting")
        expected = "PLAN_REVIEW_PENDING" if stage == "PLAN" else "IMPLEMENTATION_COMPLETED"
        if item["state"] != expected or decision not in ("APPROVED", "REJECTED"):
            raise LiteError("human gate stage or decision is invalid")
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope is required before human gate")
        if decision == "APPROVED":
            _approved_gate_preconditions(connection, item, stage)
        now = _now()
        connection.execute(
            "INSERT INTO human_gates VALUES(?,?,?,?,?,?,?)",
            (_id("gate"), work_item_id, stage, human_id, decision, reason, now),
        )
        if decision == "APPROVED":
            _approve_gate(connection, item, stage, now)
            state = None
        elif stage == "PLAN":
            state = "PLAN_REVIEW_APPROVED" if decision == "APPROVED" else "DRAFT"
            queue, role, held, closed = "CLAIMABLE", "IMPLEMENTER" if decision == "APPROVED" else "PLANNER", None, None
            if decision == "REJECTED":
                connection.execute(
                    "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND owner_role='PLANNER'",
                    (now, work_item_id),
                )
        else:
            state, queue, role, held, closed = "IMPLEMENTING", "CLAIMABLE", "IMPLEMENTER", None, None
            connection.execute(
                "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND owner_role='IMPLEMENTER'",
                (now, work_item_id),
            )
        if state is not None:
            connection.execute(
                "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,closed_at=?,"
                "row_version=row_version+1,updated_at=? WHERE work_item_id=?",
                (state, queue, role, held, closed, now, work_item_id),
            )
        _event(connection, work_item_id, request_id, "HUMAN_{0}_GATE".format(stage), "HUMAN",
               human_id, {"decision": decision, "reason": reason})
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_hold(database, work_item_id, human_id, held, reason="USER_PAUSED", request_id=None):
    request_id = request_id or _id("hold")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        if item["state"] == "FINAL_ACCEPTANCE_APPROVED":
            raise LiteError("terminal item cannot be resumed")
        if connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE'", (work_item_id,)
        ).fetchone():
            raise LiteError("release active claim before hold/resume")
        queue = "HELD" if held else ("WAITING_HUMAN" if item["current_role"] is None else "CLAIMABLE")
        now = _now()
        connection.execute(
            "UPDATE work_items SET queue_state=?,held_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (queue, reason if held else None, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "WORK_ITEM_HELD" if held else "WORK_ITEM_RESUMED",
               "HUMAN", human_id, {"reason": reason})
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
        connection.execute(
            "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND task_id=?",
            (now, work_item_id, task_id),
        )
        remaining = connection.execute(
            "SELECT owner_role FROM tasks WHERE work_item_id=? AND status='BLOCKED' ORDER BY seq LIMIT 1",
            (work_item_id,),
        ).fetchone()
        queue = "BLOCKED" if remaining else "CLAIMABLE"
        role = remaining["owner_role"] if remaining else (
            item["current_role"] or task["owner_role"]
        )
        connection.execute(
            "UPDATE work_items SET queue_state=?,current_role=?,blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (queue, role, "TASK_BLOCKED" if remaining else None, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "TASK_UNBLOCKED", "HUMAN", human_id,
               {"taskId": task_id, "reason": reason})
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
    """Recover one precisely bound orphaned PLAN Reviewer task.

    This is intentionally not a general task reset.  Every refusal rolls the
    transaction back and returns a stable structured result; unexpected faults
    are rolled back and re-raised so callers cannot mistake them for recovery.
    """
    values = (work_item_id, task_id, human_id, reason, request_id)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        return _review_task_recovery_result(
            "REFUSED", "INPUT_REQUIRED", work_item_id, task_id, human_id,
            reason, request_id,
        )
    work_item_id, task_id, human_id, reason, request_id = (
        value.strip() for value in values
    )
    connection = open_database(database)

    def refused(code):
        connection.rollback()
        return _review_task_recovery_result(
            "REFUSED", code, work_item_id, task_id, human_id, reason, request_id,
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            "SELECT * FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = _event_payload(replay)
            exact = (
                replay["event_type"] == "HUMAN_REVIEW_TASK_RECOVERED" and
                replay["actor_kind"] == "HUMAN" and replay["actor_id"] == human_id and
                replay["work_item_id"] == work_item_id and
                payload.get("protocolVersion") == REVIEW_TASK_RECOVERY_PROTOCOL and
                payload.get("workItemId") == work_item_id and
                payload.get("taskId") == task_id and payload.get("humanId") == human_id and
                payload.get("reason") == reason and payload.get("requestId") == request_id and
                payload.get("from") == "IN_PROGRESS" and
                payload.get("to") == "NOT_STARTED"
            )
            if not exact:
                return refused("REQUEST_ID_CONFLICT")
            binding = payload.get("binding")
            connection.rollback()
            return _review_task_recovery_result(
                "NO_OP", "EXACT_REPLAY", work_item_id, task_id, human_id,
                reason, request_id, binding,
            )

        try:
            item = _item(connection, work_item_id)
        except LiteError:
            return refused("WORK_ITEM_NOT_FOUND")
        if item["state"] == "FINAL_ACCEPTANCE_APPROVED" or item["closed_at"] is not None:
            return refused("TERMINAL_WORK_ITEM")
        management = _management_from_events(connection, work_item_id)
        if not isinstance(management, dict):
            return refused("MANAGEMENT_REQUIRED")

        reviewer_tasks = connection.execute(
            "SELECT * FROM tasks WHERE work_item_id=? AND owner_role='REVIEWER' ORDER BY seq",
            (work_item_id,),
        ).fetchall()
        if len(reviewer_tasks) != 1 or reviewer_tasks[0]["task_id"] != task_id:
            return refused("REVIEWER_TASK_NOT_UNIQUE")
        task = reviewer_tasks[0]
        if task["status"] != "IN_PROGRESS":
            return refused("TASK_NOT_ORPHANED")
        in_progress = connection.execute(
            "SELECT task_id FROM tasks WHERE work_item_id=? AND status='IN_PROGRESS'",
            (work_item_id,),
        ).fetchall()
        if len(in_progress) != 1 or in_progress[0]["task_id"] != task_id:
            return refused("IN_PROGRESS_TASK_AMBIGUOUS")
        if connection.execute(
            "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE' LIMIT 1",
            (work_item_id,),
        ).fetchone() is not None:
            return refused("ACTIVE_CLAIM")
        if connection.execute(
            "SELECT 1 FROM repository_locks WHERE work_item_id=? AND status='ACTIVE' LIMIT 1",
            (work_item_id,),
        ).fetchone() is not None:
            return refused("ACTIVE_REPOSITORY_WRITER")

        implementer_tasks = connection.execute(
            "SELECT * FROM tasks WHERE work_item_id=? AND owner_role='IMPLEMENTER' ORDER BY seq",
            (work_item_id,),
        ).fetchall()
        if len(implementer_tasks) != 1:
            return refused("IMPLEMENTER_TASK_NOT_UNIQUE")
        implementer_status = implementer_tasks[0]["status"]
        implementing_shape = (
            item["state"] == "IMPLEMENTING" and item["queue_state"] == "CLAIMABLE" and
            item["current_role"] == "IMPLEMENTER" and item["held_reason"] is None and
            item["blocked_reason"] is None and
            implementer_status not in ("COMPLETED", "CANCELLED")
        )
        deviation_shape = (
            item["state"] == "DRAFT" and item["queue_state"] == "BLOCKED" and
            item["current_role"] == "PLANNER" and item["held_reason"] is None and
            item["blocked_reason"] == "PLAN_DEVIATION" and
            implementer_status == "BLOCKED"
        )
        if not (implementing_shape or deviation_shape):
            return refused("UNSUPPORTED_RUNTIME_SHAPE")

        projection = _review_projection(connection, work_item_id)
        if (projection["PLAN"]["latestResult"] != "PASS" or
                projection["PLAN"]["openFindings"]):
            return refused("PLAN_REVIEW_NOT_PASS_OPEN_ZERO")
        if connection.execute(
            "SELECT 1 FROM reviews WHERE work_item_id=? AND stage='FINAL' LIMIT 1",
            (work_item_id,),
        ).fetchone() is not None:
            return refused("FINAL_REVIEW_ALREADY_EXISTS")

        task_events = []
        for row in connection.execute(
                "SELECT * FROM events WHERE work_item_id=? AND event_type='TASK_STATUS_CHANGED' "
                "ORDER BY event_id", (work_item_id,)):
            payload = _event_payload(row)
            if payload.get("taskId") == task_id:
                task_events.append((row, payload))
        if not task_events or task_events[-1][1].get("status") != "IN_PROGRESS":
            return refused("TASK_EVENT_NOT_BOUND")
        task_event, task_payload = task_events[-1]
        reviewer_agent_id = task_event["actor_id"]
        if (task_event["actor_kind"] != "AGENT" or
                task_payload.get("from") != "NOT_STARTED"):
            return refused("TASK_EVENT_NOT_BOUND")

        claims = []
        for claim in connection.execute(
                "SELECT * FROM claims WHERE work_item_id=? AND task_id=? AND agent_id=? "
                "AND role='REVIEWER'", (work_item_id, task_id, reviewer_agent_id)):
            acquired_event = connection.execute(
                "SELECT * FROM events WHERE work_item_id=? AND event_type='CLAIM_ACQUIRED' "
                "AND actor_kind='AGENT' AND actor_id=? ORDER BY event_id",
                (work_item_id, reviewer_agent_id),
            ).fetchall()
            acquired_event = [
                row for row in acquired_event
                if _event_payload(row).get("claimId") == claim["claim_id"] and
                _event_payload(row).get("taskId") == task_id and
                _event_payload(row).get("generation") == claim["generation"]
            ]
            if (claim["status"] == "RELEASED" and claim["released_at"] and
                    len(acquired_event) == 1 and
                    claim["acquired_at"] == acquired_event[0]["created_at"] and
                    acquired_event[0]["event_id"] < task_event["event_id"]):
                claims.append((claim, acquired_event[0]))
        if len(claims) != 1:
            return refused("RELEASED_REVIEWER_CLAIM_NOT_UNIQUE")
        claim, claim_event = claims[0]

        review_candidates = []
        for review in connection.execute(
                "SELECT rowid AS review_rowid,* FROM reviews WHERE work_item_id=? "
                "AND stage='PLAN' ORDER BY created_at,rowid", (work_item_id,)):
            decoded = _decoded_review(review)
            if (review["reviewer_agent_id"] != reviewer_agent_id or
                    decoded.get("protocolVersion") != "AWB-REVIEW-v1" or
                    decoded.get("result") != "PASS" or decoded.get("findings") != []):
                continue
            matching_events = []
            for event in connection.execute(
                    "SELECT * FROM events WHERE work_item_id=? AND event_type='AGENT_PLAN_REVIEW' "
                    "AND actor_kind='AGENT' AND actor_id=? ORDER BY event_id",
                    (work_item_id, reviewer_agent_id)):
                event_payload = _event_payload(event)
                if (event_payload.get("decision") == "APPROVED" and
                        event_payload.get("review") == decoded and
                        event["created_at"] == review["created_at"] and
                        event["event_id"] > task_event["event_id"]):
                    matching_events.append((event, event_payload))
            if len(matching_events) != 1:
                continue
            review_event, review_payload = matching_events[0]
            gate_events = []
            for gate_event in connection.execute(
                    "SELECT * FROM events WHERE work_item_id=? AND event_type IN "
                    "('AUTO_GATE_APPROVED','HUMAN_PLAN_GATE') ORDER BY event_id",
                    (work_item_id,)):
                if gate_event["event_id"] <= review_event["event_id"]:
                    continue
                gate_payload = _event_payload(gate_event)
                if gate_event["event_type"] == "AUTO_GATE_APPROVED":
                    valid_gate = (
                        gate_event["actor_kind"] == "SYSTEM" and
                        gate_payload.get("stage") == "PLAN" and
                        gate_payload.get("reviewId") == review["review_id"] and
                        gate_payload.get("reviewRequestId") == review_event["request_id"] and
                        gate_payload.get("reviewEventId") == review_event["event_id"] and
                        gate_payload.get("reviewRound") == decoded.get("round")
                    )
                else:
                    human_gate = connection.execute(
                        "SELECT 1 FROM human_gates WHERE work_item_id=? AND stage='PLAN' "
                        "AND human_id=? AND decision='APPROVED' AND reason=? AND created_at=?",
                        (work_item_id, gate_event["actor_id"], gate_payload.get("reason"),
                         gate_event["created_at"]),
                    ).fetchone()
                    valid_gate = (
                        gate_event["actor_kind"] == "HUMAN" and
                        gate_payload.get("decision") == "APPROVED" and
                        human_gate is not None
                    )
                if valid_gate:
                    gate_events.append(gate_event)
            for gate_event in gate_events:
                starts = connection.execute(
                    "SELECT * FROM events WHERE work_item_id=? AND event_type='START_IMPLEMENTATION' "
                    "AND event_id>? ORDER BY event_id", (work_item_id, gate_event["event_id"]),
                ).fetchall()
                if len(starts) == 1 and starts[0]["actor_kind"] == "AGENT" and _event_payload(
                        starts[0]) == {
                            "from": "PLAN_REVIEW_APPROVED", "to": "IMPLEMENTING",
                        }:
                    review_candidates.append((review, review_event, gate_event, starts[0]))
        if len(review_candidates) != 1:
            return refused("PLAN_EVIDENCE_NOT_UNIQUE")
        review, review_event, gate_event, start_event = review_candidates[0]
        if claim["released_at"] != review["created_at"]:
            return refused("CLAIM_REVIEW_TIMELINE_DRIFT")

        binding = {
            "reviewerAgentId": reviewer_agent_id,
            "claimId": claim["claim_id"],
            "claimGeneration": claim["generation"],
            "claimEventId": claim_event["event_id"],
            "taskStatusEventId": task_event["event_id"],
            "planReviewId": review["review_id"],
            "planReviewRound": _decoded_review(review).get("round"),
            "planReviewRequestId": review_event["request_id"],
            "planReviewEventId": review_event["event_id"],
            "planGateEventId": gate_event["event_id"],
            "startImplementationEventId": start_event["event_id"],
        }
        payload = {
            "protocolVersion": REVIEW_TASK_RECOVERY_PROTOCOL,
            "workItemId": work_item_id,
            "taskId": task_id,
            "from": "IN_PROGRESS",
            "to": "NOT_STARTED",
            "humanId": human_id,
            "reason": reason,
            "requestId": request_id,
            "binding": binding,
        }
        now = _now()
        updated_task = connection.execute(
            "UPDATE tasks SET status='NOT_STARTED',updated_at=? "
            "WHERE work_item_id=? AND task_id=? AND status='IN_PROGRESS'",
            (now, work_item_id, task_id),
        )
        if updated_task.rowcount != 1:
            return refused("TASK_NOT_ORPHANED")
        connection.execute(
            "UPDATE work_items SET row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (now, work_item_id),
        )
        _event(
            connection, work_item_id, request_id, "HUMAN_REVIEW_TASK_RECOVERED",
            "HUMAN", human_id, payload,
        )
        connection.commit()
        return _review_task_recovery_result(
            "OK", "RECOVERED", work_item_id, task_id, human_id, reason,
            request_id, binding,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def report_plan_deviation(database, work_item_id, task_id, agent_id, evidence,
                          request_id=None):
    if not isinstance(evidence, dict) or not evidence.get("reason") or not evidence.get("impact"):
        raise LiteError("PLAN_DEVIATION requires reason and impact evidence")
    request_id = request_id or _id("plan-deviation")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        claim = _active_claim(connection, work_item_id, "IMPLEMENTER", agent_id)
        if item["state"] != "IMPLEMENTING" or claim["task_id"] != task_id:
            raise LiteError("PLAN_DEVIATION requires active implementation")
        now = _now()
        connection.execute(
            "UPDATE repository_locks SET status='RELEASED',released_at=? "
            "WHERE work_item_id=? AND agent_id=? AND status='ACTIVE'",
            (now, work_item_id, agent_id),
        )
        _release_active(connection, work_item_id, now)
        connection.execute(
            "UPDATE tasks SET status='BLOCKED',evidence_json=?,updated_at=? "
            "WHERE work_item_id=? AND task_id=?",
            (_json([evidence]), now, work_item_id, task_id),
        )
        connection.execute(
            "UPDATE work_items SET state='DRAFT',queue_state='BLOCKED',current_role='PLANNER',"
            "blocked_reason='PLAN_DEVIATION',row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "PLAN_DEVIATION", "AGENT", agent_id,
               {"taskId": task_id, "evidence": evidence, "roundsPreserved": True})
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    release = sub.add_parser("release")
    release.add_argument("work_item_id")
    release.add_argument("--agent", required=True)
    lock = sub.add_parser("lock")
    lock.add_argument("work_item_id")
    lock.add_argument("--repository", required=True)
    lock.add_argument("--agent", required=True)
    lock.add_argument("--ttl", type=int, default=900)
    unlock = sub.add_parser("unlock")
    unlock.add_argument("work_item_id")
    unlock.add_argument("--repository", required=True)
    unlock.add_argument("--agent", required=True)
    task = sub.add_parser("task")
    task.add_argument("work_item_id")
    task.add_argument("task_id")
    task.add_argument("--agent", required=True)
    task.add_argument("--status", required=True, choices=("IN_PROGRESS", "BLOCKED", "WAITING_ACCEPTANCE", "COMPLETED", "CANCELLED"))
    task.add_argument("--evidence", action="append", default=[])
    transition_parser = sub.add_parser("transition")
    transition_parser.add_argument("work_item_id")
    transition_parser.add_argument("action", choices=("submit_plan", "start_implementation", "submit_implementation"))
    transition_parser.add_argument("--agent", required=True)
    transition_parser.add_argument("--local-tests-passed", action="store_true")
    transition_parser.add_argument("--submission-file")
    transition_parser.add_argument("--quality-file")
    transition_parser.add_argument("--plan-artifact")
    transition_parser.add_argument("--request-id")
    review = sub.add_parser("review")
    review.add_argument("work_item_id")
    review.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    review.add_argument("--agent", required=True)
    review.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    review.add_argument("--summary")
    review.add_argument("--review-file")
    review.add_argument("--replacement-file")
    review.add_argument("--request-id")
    gate = sub.add_parser("gate")
    gate.add_argument("work_item_id")
    gate.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    gate.add_argument("--human", required=True)
    gate.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    gate.add_argument("--reason", required=True)
    for command in ("hold", "resume"):
        hold = sub.add_parser(command)
        hold.add_argument("work_item_id")
        hold.add_argument("--human", required=True)
        hold.add_argument("--reason", default="USER_PAUSED")
    unblock = sub.add_parser("unblock")
    unblock.add_argument("work_item_id")
    unblock.add_argument("task_id")
    unblock.add_argument("--human", required=True)
    unblock.add_argument("--reason", required=True)
    recover = sub.add_parser("recover-review-task")
    recover.add_argument("work_item_id")
    recover.add_argument("task_id")
    recover.add_argument("--human")
    recover.add_argument("--reason")
    recover.add_argument("--request-id")
    backfill = sub.add_parser("management-backfill")
    backfill.add_argument("work_item_id")
    backfill.add_argument("--agent", required=True)
    backfill.add_argument("--management-file", required=True)
    backfill.add_argument("--basis", required=True)
    amend = sub.add_parser("management-amend")
    amend.add_argument("work_item_id")
    amend.add_argument("--human", required=True)
    amend.add_argument("--management-file", required=True)
    amend.add_argument("--reason", required=True)
    deviation = sub.add_parser("plan-deviation")
    deviation.add_argument("work_item_id")
    deviation.add_argument("task_id")
    deviation.add_argument("--agent", required=True)
    deviation.add_argument("--evidence-file", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _print(initialize_database(args.database))
        elif args.command == "create":
            _print(create_work_item(
                args.database, args.work_item_id, args.type, args.title, args.mode, args.priority,
                management=_load_json_file(args.management_file, "management file"),
                creation_risk=_load_json_file(args.risk_file, "risk file"),
                human_review=({"auto-on-pass": "AUTO_ON_PASS", "manual": "MANUAL"}.get(
                    args.human_review
                )),
                decision_actor=args.decision_actor,
            ))
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
            _print(acquire_claim(args.database, args.work_item_id, args.task_id, args.agent,
                                 args.role, expires, session_id=args.session_id,
                                 usage_provider=args.usage_provider, model=args.model,
                                 orchestrator_id=args.orchestrator_id,
                                 orchestrator_generation=args.orchestrator_generation,
                                 usage_policy=args.usage_policy))
        elif args.command == "release":
            release_claim(args.database, args.work_item_id, args.agent,
                          usage_policy=args.usage_policy)
            _print(get_work_item(args.database, args.work_item_id))
        elif args.command == "lock":
            if args.ttl < 1:
                raise LiteError("ttl must be positive")
            expires = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=args.ttl)).replace(microsecond=0).isoformat()
            _print(acquire_repository_lock(args.database, args.work_item_id, args.repository, args.agent, expires))
        elif args.command == "unlock":
            release_repository_lock(args.database, args.work_item_id, args.repository, args.agent)
            _print(get_work_item(args.database, args.work_item_id))
        elif args.command == "task":
            set_task_status(args.database, args.work_item_id, args.task_id, args.agent,
                            args.status, args.evidence, usage_policy=args.usage_policy)
            _print(get_work_item(args.database, args.work_item_id))
        elif args.command == "transition":
            _print(transition(
                args.database, args.work_item_id, args.action, args.agent,
                local_tests_passed=args.local_tests_passed,
                submission=_load_json_file(args.submission_file, "submission file"),
                quality_baseline=_load_json_file(args.quality_file, "quality file"),
                usage_policy=args.usage_policy,
                request_id=args.request_id,
                plan_artifact=({"projectRoot": args.project_root,
                                "path": args.plan_artifact}
                               if args.plan_artifact else None),
            ))
        elif args.command == "review":
            review_summary = _load_json_file(args.review_file, "review file") if args.review_file else args.summary
            if review_summary is None:
                raise LiteError("review requires --summary or --review-file")
            if args.replacement_file:
                if args.stage != "PLAN" or args.decision != "REJECTED" or not args.review_file:
                    raise LiteError("replacement-file requires a structured rejected PLAN review")
                if not args.project_root or not args.repository_key:
                    raise LiteError("replacement-file requires configured project identity")
                _print(amend_plan_review(
                    args.database, args.work_item_id, args.agent, args.replacement_file,
                    args.project_root, args.repository_key, review_summary,
                    args.request_id, usage_policy=args.usage_policy,
                ))
            else:
                _print(record_agent_review(
                    args.database, args.work_item_id, args.stage, args.agent,
                    args.decision, review_summary, request_id=args.request_id,
                    usage_policy=args.usage_policy,
                ))
        elif args.command == "gate":
            _print(record_human_gate(
                args.database, args.work_item_id, args.stage, args.human,
                args.decision, args.reason, usage_policy=args.usage_policy,
            ))
        elif args.command in ("hold", "resume"):
            _print(set_hold(args.database, args.work_item_id, args.human, args.command == "hold", args.reason))
        elif args.command == "unblock":
            _print(unblock_task(args.database, args.work_item_id, args.task_id, args.human, args.reason))
        elif args.command == "recover-review-task":
            result = recover_review_task(
                args.database, args.work_item_id, args.task_id, args.human,
                args.reason, args.request_id,
            )
            _print(result)
            if result["status"] == "REFUSED":
                return 2
        elif args.command == "management-backfill":
            _print(backfill_management(
                args.database, args.work_item_id, args.agent,
                _load_json_file(args.management_file, "management file"), args.basis,
            ))
        elif args.command == "management-amend":
            _print(amend_management(
                args.database, args.work_item_id, args.human,
                _load_json_file(args.management_file, "management file"), args.reason,
            ))
        elif args.command == "plan-deviation":
            _print(report_plan_deviation(
                args.database, args.work_item_id, args.task_id, args.agent,
                _load_json_file(args.evidence_file, "evidence file"),
            ))
        elif args.command == "serve":
            server = make_server(args.database, args.host, args.port)
            try:
                print("Agent Workboard Lite listening on http://{0}:{1}".format(*server.server_address))
                server.serve_forever()
            finally:
                server.server_close()
        return 0
    except LiteError as exc:
        print("error: {0}".format(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
