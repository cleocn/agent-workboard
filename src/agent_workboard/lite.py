"""Small, useful Agent Workboard baseline.

MVP-LITE deliberately keeps orchestration thin: SQLite state, leases, one writer,
two review stages, human gates, hold/block and an event timeline.
"""

import argparse
import datetime
import html
import ipaddress
import json
import os
import pkgutil
import sqlite3
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import unquote, urlsplit


SCHEMA_VERSION = "MVP-LITE-v1"
MANAGEMENT_CONTRACT_VERSION = "AWB-WORKITEM-MGMT-v1"
TEMPLATE_CONTRACT_VERSION = "AWB-MANAGEMENT-v1"
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
                     management=None, request_id=None, actor_id="orchestrator"):
    if not work_item_id.startswith(item_type + "-"):
        raise LiteError("WorkItem id and type must match")
    if item_type not in ("TI", "FE", "R", "WA", "AWB"):
        raise LiteError("unsupported WorkItem type")
    if mode not in ("STANDARD", "READ_ONLY_DIAGNOSIS"):
        raise LiteError("unsupported mode")
    request_id = request_id or _id("create")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        basic_payload = {"type": item_type, "title": title, "mode": mode, "priority": priority}
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
            "INSERT INTO work_items(work_item_id,work_item_type,title,mode,priority,current_role,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'PLANNER',?,?)",
            (work_item_id, item_type, title, mode, priority, now, now),
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


def acquire_claim(database, work_item_id, task_id, agent_id, role, expires_at,
                  request_id=None):
    request_id = request_id or _id("claim")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        now = _now()
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
        connection.commit()
        return {"claimId": claim_id, "generation": generation}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_claim(database, work_item_id, agent_id, request_id=None):
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
                    request_id=None):
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


def transition(database, work_item_id, action, agent_id, request_id=None,
               local_tests_passed=False, submission=None, quality_baseline=None):
    request_id = request_id or _id(action)
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
        if _management_from_events(connection, work_item_id) is None:
            raise LiteError("management envelope must be backfilled before transition")
        now = _now()
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
        _event(connection, work_item_id, request_id, action.upper(), "AGENT", agent_id, payload)
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
    if isinstance(value, dict) and value.get("protocolVersion") == "AWB-REVIEW-v1":
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
    if latest and latest.get("round") == 3 and latest.get("result") == "REVISE":
        next_step = "run the single convergence review"
    elif latest and latest.get("round") == 4 and latest.get("result") == "CONVERGENCE_REVISE":
        next_step = "perform the single minimal convergence revision"
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
    blocking_result = result in ("REVISE", "CONVERGENCE_REVISE", "BLOCKED")
    if blocking_result and not post_review_open:
        result = "PASS" if result != "BLOCKED" else "WAITING_HUMAN"
    if result == "PASS" and post_review_open:
        raise LiteError("PASS cannot leave an open blocking Finding")
    if round_number == 5 and result == "REVISE":
        # The result is retained for audit, but runtime routes to a human instead of round 6.
        pass
    return {
        "protocolVersion": "AWB-REVIEW-v1", "stage": expected_stage,
        "round": round_number, "reviewerMode": mode, "result": result,
        "findings": valid, "resolvedFindingIds": resolved_ids,
        "nonBlockingSuggestions": suggestions,
        "summary": incoming.get("summary", ""),
    }


def record_agent_review(database, work_item_id, stage, reviewer_agent_id, decision, summary,
                        request_id=None):
    request_id = request_id or _id("review")
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
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
        connection.execute(
            "INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
            (_id("review"), work_item_id, stage, reviewer_agent_id, stored_decision, _json(review), now),
        )
        _release_active(connection, work_item_id, now)
        result = review["result"]
        round_number = review["round"]
        if result == "PASS" and item["mode"] == "STANDARD":
            state, queue, role = item["state"], "WAITING_HUMAN", None
        elif result == "PASS":
            state, queue, role = "PLAN_REVIEW_APPROVED", "HELD", None
        elif result == "BLOCKED":
            state, queue, role = item["state"], "BLOCKED", None
        elif result == "WAITING_HUMAN" or (round_number == 5 and result == "REVISE"):
            state, queue, role = item["state"], "WAITING_HUMAN", None
        elif round_number == 3 and result == "REVISE":
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
        connection.execute(
            "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (state, queue, role, "TARGET_REACHED" if queue == "HELD" else None,
             "REVIEW_BLOCKED" if queue == "BLOCKED" else None, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "AGENT_{0}_REVIEW".format(stage), "AGENT",
               reviewer_agent_id, {"decision": stored_decision, "review": review})
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_human_gate(database, work_item_id, stage, human_id, decision, reason,
                      request_id=None):
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
        review = connection.execute(
            "SELECT * FROM reviews WHERE work_item_id=? AND stage=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (work_item_id, stage),
        ).fetchone()
        if decision == "APPROVED" and (review is None or _decoded_review(review)["result"] != "PASS"):
            raise LiteError("human approval requires approved Agent review")
        if decision == "APPROVED" and stage == "FINAL":
            pending = connection.execute(
                "SELECT count(*) FROM tasks WHERE work_item_id=? AND required=1 AND status<>'COMPLETED'",
                (work_item_id,),
            ).fetchone()[0]
            if pending:
                raise LiteError("final approval requires all required tasks completed")
            if _review_projection(connection, work_item_id)["IMPLEMENTATION"]["openFindings"]:
                raise LiteError("final approval requires no open implementation Findings")
            if _latest_submission_baseline(connection, work_item_id) is None:
                raise LiteError("final approval requires implementation quality evidence")
        now = _now()
        connection.execute(
            "INSERT INTO human_gates VALUES(?,?,?,?,?,?,?)",
            (_id("gate"), work_item_id, stage, human_id, decision, reason, now),
        )
        if stage == "PLAN":
            state = "PLAN_REVIEW_APPROVED" if decision == "APPROVED" else "DRAFT"
            queue, role, held, closed = "CLAIMABLE", "IMPLEMENTER" if decision == "APPROVED" else "PLANNER", None, None
            if decision == "REJECTED":
                connection.execute(
                    "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND owner_role='PLANNER'",
                    (now, work_item_id),
                )
        elif decision == "APPROVED":
            state, queue, role, held, closed = "FINAL_ACCEPTANCE_APPROVED", "HELD", None, "TERMINAL_STATE", now
            connection.execute(
                "UPDATE tasks SET status='COMPLETED',updated_at=? WHERE work_item_id=? AND owner_role='REVIEWER'",
                (now, work_item_id),
            )
        else:
            state, queue, role, held, closed = "IMPLEMENTING", "CLAIMABLE", "IMPLEMENTER", None, None
            connection.execute(
                "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND owner_role='IMPLEMENTER'",
                (now, work_item_id),
            )
        connection.execute(
            "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,closed_at=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
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
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    create = sub.add_parser("create")
    create.add_argument("work_item_id")
    create.add_argument("--type", required=True, choices=("TI", "FE", "R", "WA", "AWB"))
    create.add_argument("--title", required=True)
    create.add_argument("--mode", choices=("STANDARD", "READ_ONLY_DIAGNOSIS"), default="STANDARD")
    create.add_argument("--priority", choices=("P0", "P1", "P2", "P3"), default="P2")
    create.add_argument("--management-file", required=True)
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
    review = sub.add_parser("review")
    review.add_argument("work_item_id")
    review.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    review.add_argument("--agent", required=True)
    review.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    review.add_argument("--summary")
    review.add_argument("--review-file")
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
            _print(acquire_claim(args.database, args.work_item_id, args.task_id, args.agent, args.role, expires))
        elif args.command == "release":
            release_claim(args.database, args.work_item_id, args.agent)
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
            set_task_status(args.database, args.work_item_id, args.task_id, args.agent, args.status, args.evidence)
            _print(get_work_item(args.database, args.work_item_id))
        elif args.command == "transition":
            _print(transition(
                args.database, args.work_item_id, args.action, args.agent,
                local_tests_passed=args.local_tests_passed,
                submission=_load_json_file(args.submission_file, "submission file"),
                quality_baseline=_load_json_file(args.quality_file, "quality file"),
            ))
        elif args.command == "review":
            review_summary = _load_json_file(args.review_file, "review file") if args.review_file else args.summary
            if review_summary is None:
                raise LiteError("review requires --summary or --review-file")
            _print(record_agent_review(args.database, args.work_item_id, args.stage, args.agent, args.decision, review_summary))
        elif args.command == "gate":
            _print(record_human_gate(args.database, args.work_item_id, args.stage, args.human, args.decision, args.reason))
        elif args.command in ("hold", "resume"):
            _print(set_hold(args.database, args.work_item_id, args.human, args.command == "hold", args.reason))
        elif args.command == "unblock":
            _print(unblock_task(args.database, args.work_item_id, args.task_id, args.human, args.reason))
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
