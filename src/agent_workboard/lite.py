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


def create_work_item(database, work_item_id, item_type, title, mode="STANDARD", priority="P2",
                     request_id=None, actor_id="orchestrator"):
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
        create_payload = {"type": item_type, "title": title, "mode": mode, "priority": priority}
        replay = connection.execute(
            "SELECT event_type,payload_json FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay:
            if replay["event_type"] != "WORK_ITEM_CREATED" or replay["payload_json"] != _json(create_payload):
                raise LiteError("request_id was already used with different content")
            connection.rollback()
            return get_work_item(database, work_item_id)
        now = _now()
        connection.execute(
            "INSERT INTO work_items(work_item_id,work_item_type,title,mode,priority,current_role,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'PLANNER',?,?)",
            (work_item_id, item_type, title, mode, priority, now, now),
        )
        tasks = [(1, "规划与诊断", "PLANNER")]
        if mode == "STANDARD":
            tasks.extend([(2, "实施与本地测试", "IMPLEMENTER"), (3, "批量最终复审", "REVIEWER")])
        else:
            tasks.append((2, "规划复审", "REVIEWER"))
        for seq, task_title, role in tasks:
            connection.execute(
                "INSERT INTO tasks(task_id,work_item_id,seq,title,owner_role,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("{0}-T{1:02d}".format(work_item_id, seq), work_item_id, seq, task_title, role, now, now),
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
        return item
    finally:
        connection.close()


def list_work_items(database):
    connection = open_database(database)
    try:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM work_items ORDER BY "
            "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, "
            "updated_at, work_item_id"
        )]
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
        now = _now()
        connection.execute(
            "UPDATE tasks SET status=?,evidence_json=?,updated_at=? WHERE task_id=? AND work_item_id=?",
            (status, _json(evidence or []), now, task_id, work_item_id),
        )
        queue = "BLOCKED" if status == "BLOCKED" else "CLAIMED"
        blocked = "TASK_BLOCKED" if status == "BLOCKED" else None
        connection.execute(
            "UPDATE work_items SET queue_state=?,blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (queue, blocked, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "TASK_STATUS_CHANGED", "AGENT", agent_id,
               {"taskId": task_id, "status": status})
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


def transition(database, work_item_id, action, agent_id, request_id=None, local_tests_passed=False):
    request_id = request_id or _id(action)
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        item = _item(connection, work_item_id)
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
        _event(connection, work_item_id, request_id, action.upper(), "AGENT", agent_id,
               {"from": item["state"], "to": new_state})
        connection.commit()
        return get_work_item(database, work_item_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
        author_role = "PLANNER" if stage == "PLAN" else "IMPLEMENTER"
        author = connection.execute(
            "SELECT agent_id FROM claims WHERE work_item_id=? AND role=? ORDER BY generation DESC LIMIT 1",
            (work_item_id, author_role),
        ).fetchone()
        if author is not None and author[0] == reviewer_agent_id:
            raise LiteError("author cannot review own work")
        now = _now()
        connection.execute(
            "INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
            (_id("review"), work_item_id, stage, reviewer_agent_id, decision, summary, now),
        )
        _release_active(connection, work_item_id, now)
        if decision == "REJECTED":
            state = "DRAFT" if stage == "PLAN" else "IMPLEMENTING"
            queue = "CLAIMABLE"
            role = author_role
        elif item["mode"] == "STANDARD":
            state, queue, role = item["state"], "WAITING_HUMAN", None
        else:
            state, queue, role = "PLAN_REVIEW_APPROVED", "HELD", None
        connection.execute(
            "UPDATE work_items SET state=?,queue_state=?,current_role=?,held_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            (state, queue, role, "TARGET_REACHED" if queue == "HELD" else None, now, work_item_id),
        )
        _event(connection, work_item_id, request_id, "AGENT_{0}_REVIEW".format(stage), "AGENT",
               reviewer_agent_id, {"decision": decision, "summary": summary})
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
        review = connection.execute(
            "SELECT decision FROM reviews WHERE work_item_id=? AND stage=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (work_item_id, stage),
        ).fetchone()
        if decision == "APPROVED" and (review is None or review[0] != "APPROVED"):
            raise LiteError("human approval requires approved Agent review")
        now = _now()
        connection.execute(
            "INSERT INTO human_gates VALUES(?,?,?,?,?,?,?)",
            (_id("gate"), work_item_id, stage, human_id, decision, reason, now),
        )
        if stage == "PLAN":
            state = "PLAN_REVIEW_APPROVED" if decision == "APPROVED" else "DRAFT"
            queue, role, held, closed = "CLAIMABLE", "IMPLEMENTER" if decision == "APPROVED" else "PLANNER", None, None
        elif decision == "APPROVED":
            state, queue, role, held, closed = "FINAL_ACCEPTANCE_APPROVED", "HELD", None, "TERMINAL_STATE", now
            connection.execute(
                "UPDATE tasks SET status='COMPLETED',updated_at=? WHERE work_item_id=? AND owner_role='REVIEWER'",
                (now, work_item_id),
            )
        else:
            state, queue, role, held, closed = "IMPLEMENTING", "CLAIMABLE", "IMPLEMENTER", None, None
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
            "SELECT status FROM tasks WHERE work_item_id=? AND task_id=?", (work_item_id, task_id)
        ).fetchone()
        if task is None or task[0] != "BLOCKED":
            raise LiteError("task is not blocked")
        now = _now()
        connection.execute(
            "UPDATE tasks SET status='NOT_STARTED',updated_at=? WHERE work_item_id=? AND task_id=?",
            (now, work_item_id, task_id),
        )
        remaining = connection.execute(
            "SELECT count(*) FROM tasks WHERE work_item_id=? AND status='BLOCKED'", (work_item_id,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE work_items SET queue_state=?,blocked_reason=?,row_version=row_version+1,updated_at=? WHERE work_item_id=?",
            ("BLOCKED" if remaining else "CLAIMABLE", "TASK_BLOCKED" if remaining else None, now, work_item_id),
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
                '<span>{1}</span><small>{2} · {3} · {4}</small></a>'.format(
                    html.escape(row["work_item_id"]), html.escape(row["title"]),
                    html.escape(row["priority"]), html.escape(row["queue_state"]),
                    html.escape(row["current_role"] or "-")
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
    review = sub.add_parser("review")
    review.add_argument("work_item_id")
    review.add_argument("--stage", required=True, choices=("PLAN", "FINAL"))
    review.add_argument("--agent", required=True)
    review.add_argument("--decision", required=True, choices=("APPROVED", "REJECTED"))
    review.add_argument("--summary", required=True)
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
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            _print(initialize_database(args.database))
        elif args.command == "create":
            _print(create_work_item(args.database, args.work_item_id, args.type, args.title, args.mode, args.priority))
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
            _print(transition(args.database, args.work_item_id, args.action, args.agent, local_tests_passed=args.local_tests_passed))
        elif args.command == "review":
            _print(record_agent_review(args.database, args.work_item_id, args.stage, args.agent, args.decision, args.summary))
        elif args.command == "gate":
            _print(record_human_gate(args.database, args.work_item_id, args.stage, args.human, args.decision, args.reason))
        elif args.command in ("hold", "resume"):
            _print(set_hold(args.database, args.work_item_id, args.human, args.command == "hold", args.reason))
        elif args.command == "unblock":
            _print(unblock_task(args.database, args.work_item_id, args.task_id, args.human, args.reason))
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
