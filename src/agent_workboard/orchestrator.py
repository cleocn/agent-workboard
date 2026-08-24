"""Local, provider-neutral WorkItem coordinator leases.

The host runtime owns processes and Agent sessions.  This module only offers
transactional SQLite coordination primitives and stable JSON results.
"""

import datetime
import hashlib
import json
import re
import sqlite3
import uuid

from .lite import LiteError, _management_from_events, _now, open_database


ORCHESTRATOR_SCHEMA_VERSION = "AWB-ORCHESTRATOR-v1"
PROTOCOL_VERSION = ORCHESTRATOR_SCHEMA_VERSION
ACTIVITY_PROTOCOL_VERSION = "AWB-ACTIVITY-v1"
DEFAULT_TTL = 900
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EXPECTED_SCHEMA_FINGERPRINT = None


def schema_sql():
    return """
INSERT INTO schema_meta(key,value) VALUES
  ('orchestrator_schema_version','AWB-ORCHESTRATOR-v1');

CREATE TABLE orchestrator_instances (
  orchestrator_id TEXT PRIMARY KEY,
  registered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE orchestrator_leases (
  lease_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  orchestrator_id TEXT NOT NULL REFERENCES orchestrator_instances(orchestrator_id),
  generation INTEGER NOT NULL CHECK (generation > 0),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED')),
  acquired_at TEXT NOT NULL,
  renewed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE UNIQUE INDEX one_active_orchestrator_per_work_item
  ON orchestrator_leases(work_item_id) WHERE status = 'ACTIVE';
CREATE INDEX orchestrator_leases_by_instance
  ON orchestrator_leases(orchestrator_id, status, work_item_id);

CREATE TABLE orchestrator_events (
  orchestrator_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL UNIQUE,
  operation TEXT NOT NULL,
  event_type TEXT NOT NULL,
  orchestrator_id TEXT REFERENCES orchestrator_instances(orchestrator_id),
  work_item_id TEXT REFERENCES work_items(work_item_id),
  lease_id TEXT REFERENCES orchestrator_leases(lease_id),
  generation INTEGER,
  payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
  created_at TEXT NOT NULL
);

CREATE INDEX orchestrator_events_timeline
  ON orchestrator_events(work_item_id, orchestrator_event_id);
CREATE TRIGGER orchestrator_events_no_update
BEFORE UPDATE ON orchestrator_events BEGIN
  SELECT RAISE(ABORT, 'orchestrator_events are immutable');
END;
CREATE TRIGGER orchestrator_events_no_delete
BEFORE DELETE ON orchestrator_events BEGIN
  SELECT RAISE(ABORT, 'orchestrator_events are immutable');
END;
""".strip()


def _normalize_schema_sql(value):
    value = value or ""
    normalized = []
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        if character.isspace():
            index += 1
            continue
        if value.startswith("--", index):
            newline = value.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if value.startswith("/*", index):
            closing = value.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            continue
        if character in ("'", '"', "`"):
            quote = character
            normalized.append(character)
            index += 1
            while index < length:
                character = value[index]
                normalized.append(character)
                index += 1
                if character == quote:
                    if index < length and value[index] == quote:
                        normalized.append(value[index])
                        index += 1
                    else:
                        break
            continue
        if character == "[":
            normalized.append(character)
            index += 1
            while index < length:
                character = value[index]
                normalized.append(character)
                index += 1
                if character == "]":
                    if index < length and value[index] == "]":
                        normalized.append(value[index])
                        index += 1
                    else:
                        break
            continue
        normalized.append(character.lower())
        index += 1
    return "".join(normalized)


def _schema_fingerprint(connection):
    names = {
        "orchestrator_instances", "orchestrator_leases", "orchestrator_events",
        "one_active_orchestrator_per_work_item", "orchestrator_leases_by_instance",
        "orchestrator_events_timeline", "orchestrator_events_no_update",
        "orchestrator_events_no_delete",
    }
    return {
        (row[0], row[1]): _normalize_schema_sql(row[2])
        for row in connection.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name IN ({0})".format(
                ",".join("?" for _ in names)
            ), sorted(names)
        )
    }


def _expected_schema_fingerprint():
    global _EXPECTED_SCHEMA_FINGERPRINT
    if _EXPECTED_SCHEMA_FINGERPRINT is None:
        reference = sqlite3.connect(":memory:")
        try:
            reference.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            reference.executescript(schema_sql())
            _EXPECTED_SCHEMA_FINGERPRINT = _schema_fingerprint(reference)
        finally:
            reference.close()
    return _EXPECTED_SCHEMA_FINGERPRINT


def schema_state(connection):
    marker = connection.execute(
        "SELECT value FROM schema_meta WHERE key='orchestrator_schema_version'"
    ).fetchone()
    expected = {
        "orchestrator_instances", "orchestrator_leases", "orchestrator_events",
        "one_active_orchestrator_per_work_item", "orchestrator_leases_by_instance",
        "orchestrator_events_timeline", "orchestrator_events_no_update",
        "orchestrator_events_no_delete",
    }
    present = set(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'orchestrator_%' "
        "OR name='one_active_orchestrator_per_work_item'"
    ))
    if marker is None and not present:
        return "ABSENT"
    columns = {
        "orchestrator_instances": ["orchestrator_id", "registered_at", "last_seen_at"],
        "orchestrator_leases": [
            "lease_id", "work_item_id", "orchestrator_id", "generation", "status",
            "acquired_at", "renewed_at", "expires_at", "released_at",
        ],
        "orchestrator_events": [
            "orchestrator_event_id", "request_id", "operation", "event_type",
            "orchestrator_id", "work_item_id", "lease_id", "generation",
            "payload_json", "created_at",
        ],
    }
    actual = {}
    for table in columns:
        actual[table] = [row[1] for row in connection.execute(
            "PRAGMA table_info({0})".format(table)
        )]
    protections_valid = _schema_fingerprint(connection) == _expected_schema_fingerprint()
    if (marker and marker[0] == ORCHESTRATOR_SCHEMA_VERSION and present == expected and
            all(actual[name] == value for name, value in columns.items()) and
            protections_valid):
        return "INSTALLED"
    return "INVALID"


def schema_installed(connection):
    return schema_state(connection) == "INSTALLED"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timestamp(value=None):
    if value is None:
        return _now()
    if isinstance(value, datetime.datetime):
        return value.replace(microsecond=0).isoformat()
    return value


def _expires(now, ttl):
    parsed = datetime.datetime.fromisoformat(now)
    return (parsed + datetime.timedelta(seconds=ttl)).replace(microsecond=0).isoformat()


def _validate_id(value, field):
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise LiteError("{0} must be 1-128 safe printable characters".format(field))


def _lease(row, now=None):
    if row is None:
        return None
    value = {key: row[key] for key in (
        "lease_id", "work_item_id", "orchestrator_id", "generation", "status",
        "acquired_at", "renewed_at", "expires_at", "released_at",
    )}
    if now is not None:
        value.update(_activity_projection(
            "ORCHESTRATOR_LEASE", row, now,
            terminal=False,
        ))
    return value


_ACTIVITY_KINDS = {
    "claim": ("AGENT_CLAIM", "claims", "claim_id", "agent_id"),
    "repository-writer": (
        "REPOSITORY_WRITER", "repository_locks", "lock_id", "agent_id"
    ),
    "orchestrator-lease": (
        "ORCHESTRATOR_LEASE", "orchestrator_leases", "lease_id", "orchestrator_id"
    ),
}
_SAFE_ACTIONS = {
    "AGENT_CLAIM": "RELEASE_CLAIM_BY_OWNER",
    "REPOSITORY_WRITER": "RELEASE_REPOSITORY_WRITER_BY_OWNER",
    "ORCHESTRATOR_LEASE": "RELEASE_ORCHESTRATOR_LEASE_BY_OWNER",
}


def _effective_status(status, expires_at, now):
    if status != "ACTIVE":
        return "INACTIVE"
    return "LIVE" if expires_at > now else "STALE"


def _activity_projection(kind, row, now, terminal):
    effective = _effective_status(row["status"], row["expires_at"], now)
    if effective == "LIVE":
        reason = "LIVE_ACTIVITY_HELD"
        safe = _SAFE_ACTIONS[kind]
    elif effective == "STALE":
        reason = ("TERMINAL_ACTIVITY_RESIDUE" if terminal else
                  "EXPIRED_ACTIVITY_RESIDUE")
        safe = "RECONCILE_EXPIRED_ACTIVITY"
    else:
        reason, safe = None, "NONE"
    return {
        "persistedStatus": row["status"], "effectiveStatus": effective,
        "terminal": bool(terminal), "reasonCode": reason, "safeAction": safe,
    }


def _activity_row(kind_key, row, now, terminal):
    kind, unused_table, resource_column, owner_column = _ACTIVITY_KINDS[kind_key]
    value = {
        "kind": kind, "resourceId": row[resource_column],
        "workItemId": row["work_item_id"],
        "ownerKind": "ORCHESTRATOR" if kind == "ORCHESTRATOR_LEASE" else "AGENT",
        "ownerId": row[owner_column], "generation": row["generation"],
        "expiresAt": row["expires_at"],
    }
    value.update(_activity_projection(kind, row, now, terminal))
    return value


def activity_snapshot(connection, now=None, work_item_id=None, include_inactive=False):
    """Return all three activity classes from one snapshot and one UTC clock."""
    current = _timestamp(now)
    terminal = {
        row[0]: row[1] == "FINAL_ACCEPTANCE_APPROVED"
        for row in connection.execute("SELECT work_item_id,state FROM work_items")
    }
    rows = []
    for key, (unused_kind, table, unused_resource, unused_owner) in _ACTIVITY_KINDS.items():
        if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            continue
        query = "SELECT * FROM " + table
        values = []
        if work_item_id:
            query += " WHERE work_item_id=?"
            values.append(work_item_id)
        query += " ORDER BY work_item_id,generation"
        for row in connection.execute(query, values):
            value = _activity_row(key, row, current, terminal.get(row["work_item_id"], False))
            if include_inactive or value["effectiveStatus"] != "INACTIVE":
                rows.append(value)
    rows.sort(key=lambda value: (value["kind"], value["workItemId"],
                                 value["generation"], value["resourceId"]))
    counts = {
        "liveAgentClaims": 0, "staleAgentClaims": 0,
        "liveRepositoryWriters": 0, "staleRepositoryWriters": 0,
        "liveOrchestratorLeases": 0, "staleOrchestratorLeases": 0,
    }
    prefixes = {
        "AGENT_CLAIM": "AgentClaims", "REPOSITORY_WRITER": "RepositoryWriters",
        "ORCHESTRATOR_LEASE": "OrchestratorLeases",
    }
    for value in rows:
        if value["effectiveStatus"] in ("LIVE", "STALE"):
            counts[value["effectiveStatus"].lower() + prefixes[value["kind"]]] += 1
    counts.update({
        "activeClaims": counts["liveAgentClaims"],
        "activeWriters": counts["liveRepositoryWriters"],
        "activeOrchestratorLeases": counts["liveOrchestratorLeases"],
    })
    return {"clock": current, "counts": counts, "resources": rows}


def activity_fingerprint(resources):
    selected = [{key: row[key] for key in (
        "kind", "resourceId", "workItemId", "ownerId", "generation", "expiresAt"
    )} for row in resources if row.get("effectiveStatus") == "STALE"]
    return hashlib.sha256(
        _json(sorted(selected, key=lambda value: (
            value["kind"], value["workItemId"], value["resourceId"]
        ))).encode("utf-8")
    ).hexdigest()


def list_activity(database, work_item_id=None, effective_status=None):
    if effective_status and effective_status not in ("LIVE", "STALE", "INACTIVE"):
        raise LiteError("effective-status is invalid")
    connection, refusal = _open("ACTIVITY_LIST", database)
    if refusal:
        return refusal
    try:
        snapshot = activity_snapshot(
            connection, work_item_id=work_item_id, include_inactive=True
        )
        resources = snapshot["resources"]
        if effective_status:
            resources = [row for row in resources
                         if row["effectiveStatus"] == effective_status]
        return {"protocolVersion": ACTIVITY_PROTOCOL_VERSION, "operation": "LIST",
                "status": "OK", "reasonCode": None,
                "clock": snapshot["clock"], "counts": snapshot["counts"],
                "resources": resources, "nextStep": _next("NONE")}
    finally:
        connection.close()


def show_activity(database, kind, resource_id):
    if kind not in _ACTIVITY_KINDS:
        raise LiteError("activity kind is invalid")
    _validate_id(resource_id, "resource-id")
    connection, refusal = _open("ACTIVITY_SHOW", database)
    if refusal:
        return refusal
    try:
        unused_kind, table, resource_column, unused_owner = _ACTIVITY_KINDS[kind]
        row = connection.execute(
            "SELECT * FROM {0} WHERE {1}=?".format(table, resource_column),
            (resource_id,),
        ).fetchone()
        if row is None:
            return {"protocolVersion": ACTIVITY_PROTOCOL_VERSION, "operation": "SHOW",
                    "status": "REFUSED", "reasonCode": "RESOURCE_NOT_FOUND",
                    "resource": None, "nextStep": _next("STOP")}
        terminal = connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?", (row["work_item_id"],)
        ).fetchone()[0] == "FINAL_ACCEPTANCE_APPROVED"
        return {"protocolVersion": ACTIVITY_PROTOCOL_VERSION, "operation": "SHOW",
                "status": "OK", "reasonCode": None,
                "resource": _activity_row(kind, row, _timestamp(), terminal),
                "nextStep": _next("NONE")}
    finally:
        connection.close()


def _activity_event(connection, work_item_id, request_id, event_type, payload, now,
                    actor_id="activity-reconciler"):
    connection.execute(
        "INSERT INTO events(work_item_id,request_id,event_type,actor_kind,"
        "actor_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (work_item_id, request_id, event_type, "SYSTEM", actor_id,
         _json(payload), now),
    )


def _reconcile_result(status, reason=None, resource=None, next_action="NONE"):
    return {"protocolVersion": ACTIVITY_PROTOCOL_VERSION,
            "operation": "RECONCILE_EXPIRED", "status": status,
            "reasonCode": reason, "resource": resource,
            "nextStep": _next(next_action)}


def reconcile_expired(database, work_item_id, kind, resource_id, owner,
                      generation, request_id, now=None):
    """Expire one exact stale resource, with immutable request replay evidence."""
    if kind not in _ACTIVITY_KINDS:
        raise LiteError("activity kind is invalid")
    for value, field in ((work_item_id, "work-item"), (resource_id, "resource-id"),
                         (owner, "owner"), (request_id, "request-id")):
        _validate_id(value, field)
    if type(generation) is not int or generation < 1:
        raise LiteError("generation must be positive")
    request = {"operation": "RECONCILE_EXPIRED", "workItemId": work_item_id,
               "kind": kind, "resourceId": resource_id, "owner": owner,
               "generation": generation}
    fingerprint = hashlib.sha256(_json(request).encode("utf-8")).hexdigest()
    connection, refusal = _open("RECONCILE_EXPIRED", database)
    if refusal:
        return refusal
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = connection.execute(
            "SELECT payload_json FROM events WHERE request_id=?", (request_id,)
        ).fetchone()
        if replay is not None:
            payload = json.loads(replay[0])
            if (payload.get("requestFingerprint") != fingerprint or
                    payload.get("request") != request):
                connection.rollback()
                return _reconcile_result("REFUSED", "REQUEST_ID_REUSED", None,
                                         "USE_NEW_REQUEST_ID")
            result = dict(payload["result"])
            result["status"] = "NO_OP"
            result["nextStep"] = _next("NONE")
            connection.rollback()
            return result
        current = _timestamp(now)
        item = connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if item is None:
            connection.rollback()
            return _reconcile_result("REFUSED", "WORK_ITEM_NOT_FOUND", None, "STOP")
        projected_kind, table, resource_column, owner_column = _ACTIVITY_KINDS[kind]
        row = connection.execute(
            "SELECT * FROM {0} WHERE {1}=?".format(table, resource_column),
            (resource_id,),
        ).fetchone()
        if row is None or row["work_item_id"] != work_item_id:
            connection.rollback()
            return _reconcile_result("REFUSED", "RESOURCE_NOT_FOUND", None, "STOP")
        terminal = item["state"] == "FINAL_ACCEPTANCE_APPROVED"
        projected = _activity_row(kind, row, current, terminal)
        if row[owner_column] != owner:
            connection.rollback()
            return _reconcile_result("REFUSED", "WRONG_OWNER", projected, "VERIFY_OWNER")
        if row["generation"] != generation:
            connection.rollback()
            return _reconcile_result("REFUSED", "WRONG_GENERATION", projected,
                                     "VERIFY_GENERATION")
        if row["status"] != "ACTIVE" or row["expires_at"] > current:
            connection.rollback()
            reason = ("LIVE_ACTIVITY_HELD" if row["status"] == "ACTIVE" else
                      "RESOURCE_NOT_STALE")
            action = (_SAFE_ACTIONS[projected_kind] if reason == "LIVE_ACTIVITY_HELD" else
                      "NONE")
            return _reconcile_result("REFUSED", reason, projected, action)
        snapshot = activity_snapshot(connection, current)
        conflicts = [value for value in snapshot["resources"]
                     if value["effectiveStatus"] == "LIVE" and
                     value["workItemId"] == work_item_id]
        if kind == "repository-writer":
            repository_key = row["repository_key"]
            live_writers = connection.execute(
                "SELECT * FROM repository_locks WHERE repository_key=? AND status='ACTIVE' "
                "AND expires_at>?", (repository_key, current)
            ).fetchall()
            conflicts.extend(_activity_row("repository-writer", value, current, False)
                             for value in live_writers)
        if conflicts:
            connection.rollback()
            return _reconcile_result("REFUSED", "CONFLICTING_LIVE_ACTIVITY", projected,
                                     "STOP_LIVE_ACTIVITY_OWNER")
        connection.execute(
            "UPDATE {0} SET status='EXPIRED',released_at=? WHERE {1}=? AND status='ACTIVE'"
            .format(table, resource_column), (current, resource_id),
        )
        updated = connection.execute(
            "SELECT * FROM {0} WHERE {1}=?".format(table, resource_column),
            (resource_id,),
        ).fetchone()
        result = _reconcile_result(
            "OK", None, _activity_row(kind, updated, current, terminal), "NONE"
        )
        _activity_event(connection, work_item_id, request_id, "ACTIVITY_RECONCILED",
                        {"request": request, "requestFingerprint": fingerprint,
                         "result": result}, current)
        connection.commit()
        return result
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return _reconcile_result("CONFLICT", "DATABASE_BUSY", None,
                                     "RETRY_SAME_REQUEST")
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_terminal_activity(connection, work_item_id, now, request_id,
                                reviewer_claim=None):
    """Close all persisted ACTIVE activity in the caller's final-gate transaction."""
    snapshot = activity_snapshot(connection, now, work_item_id=work_item_id)
    changed = []
    for value in snapshot["resources"]:
        if value["persistedStatus"] != "ACTIVE":
            continue
        kind_key = {value[0]: key for key, value in _ACTIVITY_KINDS.items()}[value["kind"]]
        unused_kind, table, resource_column, unused_owner = _ACTIVITY_KINDS[kind_key]
        after = "RELEASED" if value["effectiveStatus"] == "LIVE" else "EXPIRED"
        connection.execute(
            "UPDATE {0} SET status=?,released_at=? WHERE {1}=? AND status='ACTIVE'"
            .format(table, resource_column), (after, now, value["resourceId"]),
        )
        record = dict(value)
        record["afterStatus"] = after
        record["mutated"] = True
        changed.append(record)
    if reviewer_claim is not None:
        changed.append({
            "kind": "AGENT_CLAIM",
            "resourceId": reviewer_claim["claimId"],
            "workItemId": reviewer_claim["workItemId"],
            "taskId": reviewer_claim["taskId"],
            "ownerKind": "AGENT",
            "ownerId": reviewer_claim["agentId"],
            "role": reviewer_claim["role"],
            "generation": reviewer_claim["generation"],
            "releasedAt": reviewer_claim["releasedAt"],
            "persistedStatus": "RELEASED",
            "beforeStatus": "RELEASED",
            "effectiveStatus": "INACTIVE",
            "afterStatus": "RELEASED",
            "terminal": True,
            "reasonCode": None,
            "safeAction": "NONE",
            "mutated": False,
            "source": "FINAL_REVIEW_CLAIM",
        })
    _activity_event(
        connection, work_item_id, request_id, "TERMINAL_ACTIVITY_RECONCILED",
        {"triggerRequestId": request_id, "resources": changed}, now,
        actor_id="terminal-reconciler",
    )
    return changed


def _next(action, **arguments):
    return {"action": action, "arguments": arguments}


def _result(operation, status, lease=None, reason=None, next_step=None, **extra):
    value = {
        "protocolVersion": PROTOCOL_VERSION, "operation": operation,
        "status": status, "lease": _lease(lease) if lease is not None else None,
        "reasonCode": reason,
        "nextStep": next_step or _next("NONE"),
    }
    value.update(extra)
    return value


def _schema_refusal(operation):
    return _result(operation, "REFUSED", reason="SCHEMA_NOT_INSTALLED",
                   next_step=_next("MIGRATE"))


def error_result(operation, reason="INVALID_ARGUMENT"):
    """Return the public envelope for a rejected CLI invocation."""
    return _result(operation.upper().replace("-", "_"), "REFUSED", reason=reason,
                   next_step=_next("FIX_ARGUMENTS_AND_RETRY"))


def _open(operation, database):
    connection = open_database(database)
    state = schema_state(connection)
    if state != "INSTALLED":
        connection.close()
        return None, (_schema_refusal(operation) if state == "ABSENT" else
                      _result(operation, "REFUSED", reason="SCHEMA_INVALID",
                              next_step=_next("RUN_DOCTOR")))
    return connection, None


def _request(operation, **values):
    result = {"operation": operation}
    result.update(values)
    return result


def _replay(connection, request_id, request):
    row = connection.execute(
        "SELECT operation,payload_json FROM orchestrator_events WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    if row["operation"] != request["operation"] or payload.get("request") != request:
        return _result(request["operation"], "REFUSED", reason="REQUEST_ID_REUSED",
                       next_step=_next("USE_NEW_REQUEST_ID"))
    return payload["result"]


def _instance(connection, orchestrator_id, now):
    row = connection.execute(
        "SELECT orchestrator_id FROM orchestrator_instances WHERE orchestrator_id=?",
        (orchestrator_id,),
    ).fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO orchestrator_instances VALUES(?,?,?)",
            (orchestrator_id, now, now),
        )
        return True
    connection.execute(
        "UPDATE orchestrator_instances SET last_seen_at=? WHERE orchestrator_id=?",
        (now, orchestrator_id),
    )
    return False


def _event(connection, request_id, request, event_type, orchestrator_id, result, now,
           work_item_id=None, lease=None):
    lease_value = _lease(lease) if lease is not None else None
    stored_orchestrator = orchestrator_id if connection.execute(
        "SELECT 1 FROM orchestrator_instances WHERE orchestrator_id=?",
        (orchestrator_id,),
    ).fetchone() else None
    stored_work_item = work_item_id if work_item_id and connection.execute(
        "SELECT 1 FROM work_items WHERE work_item_id=?", (work_item_id,)
    ).fetchone() else None
    connection.execute(
        "INSERT INTO orchestrator_events(request_id,operation,event_type,orchestrator_id,"
        "work_item_id,lease_id,generation,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (request_id, request["operation"], event_type, stored_orchestrator, stored_work_item,
         lease_value["lease_id"] if lease_value else None,
         lease_value["generation"] if lease_value else None,
         _json({"request": request, "result": result}), now),
    )


def _mutate(database, request_id, orchestrator_id, request, callback, now=None):
    operation = request["operation"]
    _validate_id(request_id, "request-id")
    _validate_id(orchestrator_id, "orchestrator")
    connection, refusal = _open(operation, database)
    if refusal:
        return refusal
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _replay(connection, request_id, request)
        if replay is not None:
            connection.rollback()
            return replay
        current = _timestamp(now)
        result, event_type, work_item_id, lease, touch = callback(connection, current)
        if touch:
            _instance(connection, orchestrator_id, current)
        if event_type is not None:
            _event(connection, request_id, request, event_type, orchestrator_id, result,
                   current, work_item_id, lease)
        connection.commit()
        return result
    except sqlite3.OperationalError as exc:
        connection.rollback()
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            return _result(operation, "CONFLICT", reason="DATABASE_BUSY",
                           next_step=_next("RETRY_SAME_REQUEST"))
        raise
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def register(database, orchestrator_id, request_id, now=None):
    request = _request("REGISTER", orchestratorId=orchestrator_id)

    def action(connection, current):
        created = connection.execute(
            "SELECT 1 FROM orchestrator_instances WHERE orchestrator_id=?",
            (orchestrator_id,),
        ).fetchone() is None
        result = _result("REGISTER", "OK" if created else "NO_OP",
                         next_step=_next("CLAIM_WORK"))
        return result, "ORCHESTRATOR_REGISTERED", None, None, True

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def _expire_target(connection, work_item_id, now):
    rows = connection.execute(
        "SELECT * FROM orchestrator_leases WHERE work_item_id=? AND status='ACTIVE' "
        "AND expires_at<=?", (work_item_id, now),
    ).fetchall()
    connection.execute(
        "UPDATE orchestrator_leases SET status='EXPIRED',released_at=? "
        "WHERE work_item_id=? AND status='ACTIVE' AND expires_at<=?",
        (now, work_item_id, now),
    )
    return [_activity_row("orchestrator-lease", row, now, False) for row in rows]


def _eligible(connection, work_item_id, now):
    item = connection.execute(
        "SELECT * FROM work_items WHERE work_item_id=?", (work_item_id,)
    ).fetchone()
    if item is None:
        return None, "WORK_ITEM_NOT_FOUND", []
    if _management_from_events(connection, work_item_id) is None:
        return item, "NOT_ELIGIBLE", []
    if (item["state"] == "FINAL_ACCEPTANCE_APPROVED" or
            item["queue_state"] != "CLAIMABLE" or not item["current_role"]):
        return item, "NOT_ELIGIBLE", []
    if connection.execute(
        "SELECT 1 FROM claims WHERE work_item_id=? AND status='ACTIVE' AND expires_at>?",
        (work_item_id, now),
    ).fetchone():
        return item, "NOT_ELIGIBLE", []
    reconciled = _expire_target(connection, work_item_id, now)
    if connection.execute(
        "SELECT 1 FROM orchestrator_leases WHERE work_item_id=? AND status='ACTIVE'",
        (work_item_id,),
    ).fetchone():
        return item, "LEASE_HELD", reconciled
    return item, None, reconciled


def _new_lease(connection, work_item_id, orchestrator_id, ttl, now):
    _instance(connection, orchestrator_id, now)
    generation = connection.execute(
        "SELECT coalesce(max(generation),0)+1 FROM orchestrator_leases WHERE work_item_id=?",
        (work_item_id,),
    ).fetchone()[0]
    lease_id = "orchestrator-lease-" + uuid.uuid4().hex
    connection.execute(
        "INSERT INTO orchestrator_leases VALUES(?,?,?,?,'ACTIVE',?,?,?,NULL)",
        (lease_id, work_item_id, orchestrator_id, generation, now, now,
         _expires(now, ttl)),
    )
    return connection.execute(
        "SELECT * FROM orchestrator_leases WHERE lease_id=?", (lease_id,)
    ).fetchone()


def claim(database, work_item_id, orchestrator_id, ttl, request_id, now=None):
    if ttl < 1:
        raise LiteError("ttl must be positive")
    request = _request("CLAIM", workItemId=work_item_id,
                       orchestratorId=orchestrator_id, ttl=ttl)

    def action(connection, current):
        item, reason, reconciled = _eligible(connection, work_item_id, current)
        if reason:
            status = "CONFLICT" if reason == "LEASE_HELD" else "REFUSED"
            result = _result("CLAIM", status, reason=reason,
                             next_step=_next("SELECT_ANOTHER_OR_RETRY"))
            event_type = None if reason == "NOT_ELIGIBLE" else "ORCHESTRATOR_CLAIM_REFUSED"
            return result, event_type, work_item_id, None, False
        lease = _new_lease(connection, work_item_id, orchestrator_id, ttl, current)
        result = _result("CLAIM", "OK", lease=lease, reconciledActivity=reconciled,
                         next_step=_next("DISPATCH_AGENT", workItemId=work_item_id,
                                         orchestrator=orchestrator_id,
                                         generation=lease["generation"]))
        return result, "ORCHESTRATOR_LEASE_ACQUIRED", work_item_id, lease, False

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def claim_next(database, orchestrator_id, ttl, request_id, now=None):
    if ttl < 1:
        raise LiteError("ttl must be positive")
    request = _request("CLAIM_NEXT", orchestratorId=orchestrator_id, ttl=ttl)

    def action(connection, current):
        rows = connection.execute(
            "SELECT work_item_id FROM work_items ORDER BY "
            "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,"
            "updated_at,work_item_id"
        ).fetchall()
        selected = None
        reconciled = []
        for row in rows:
            _, reason, found_reconciled = _eligible(connection, row["work_item_id"], current)
            reconciled.extend(found_reconciled)
            if reason is None:
                selected = row["work_item_id"]
                break
        if selected is None:
            result = _result("CLAIM_NEXT", "NO_OP", reason="NO_CANDIDATE",
                             next_step=_next("WAIT_OR_CLAIM_EXPLICIT"),
                             reconciledActivity=reconciled)
            return result, "ORCHESTRATOR_NO_CANDIDATE", None, None, True
        lease = _new_lease(connection, selected, orchestrator_id, ttl, current)
        result = _result("CLAIM_NEXT", "OK", lease=lease,
                         reconciledActivity=reconciled,
                         next_step=_next("DISPATCH_AGENT", workItemId=selected,
                                         orchestrator=orchestrator_id,
                                         generation=lease["generation"]))
        return result, "ORCHESTRATOR_LEASE_ACQUIRED", selected, lease, False

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def _fenced(connection, work_item_id, orchestrator_id, generation, now):
    lease = connection.execute(
        "SELECT * FROM orchestrator_leases WHERE work_item_id=? AND status='ACTIVE'",
        (work_item_id,),
    ).fetchone()
    if lease is None:
        return None, "NO_ACTIVE_LEASE"
    if lease["orchestrator_id"] != orchestrator_id:
        return None, "WRONG_OWNER"
    if lease["generation"] != generation:
        return None, "WRONG_GENERATION"
    if lease["expires_at"] <= now:
        return None, "EXPIRED_FENCE"
    return lease, None


def renew(database, work_item_id, orchestrator_id, generation, ttl, request_id, now=None):
    if ttl < 1 or generation < 1:
        raise LiteError("ttl and generation must be positive")
    request = _request("RENEW", workItemId=work_item_id, orchestratorId=orchestrator_id,
                       generation=generation, ttl=ttl)

    def action(connection, current):
        item = connection.execute(
            "SELECT queue_state FROM work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if item is None:
            result = _result("RENEW", "REFUSED", reason="WORK_ITEM_NOT_FOUND",
                             next_step=_next("STOP"))
            return result, "ORCHESTRATOR_RENEW_REFUSED", work_item_id, None, False
        if item["queue_state"] in ("HELD", "BLOCKED"):
            result = _result("RENEW", "REFUSED", reason="NOT_ELIGIBLE",
                             next_step=_next("RELEASE_OR_WAIT_FOR_EXPIRY"))
            return result, "ORCHESTRATOR_RENEW_REFUSED", work_item_id, None, False
        lease, fence_reason = _fenced(
            connection, work_item_id, orchestrator_id, generation, current
        )
        if lease is None:
            result = _result("RENEW", "REFUSED", reason=fence_reason,
                             next_step=_next("STOP_STALE_OWNER"))
            return result, None, work_item_id, None, False
        connection.execute(
            "UPDATE orchestrator_leases SET renewed_at=?,expires_at=? WHERE lease_id=?",
            (current, _expires(current, ttl), lease["lease_id"]),
        )
        lease = connection.execute(
            "SELECT * FROM orchestrator_leases WHERE lease_id=?", (lease["lease_id"],)
        ).fetchone()
        result = _result("RENEW", "OK", lease=lease,
                         next_step=_next("CONTINUE", workItemId=work_item_id,
                                         generation=generation))
        return result, "ORCHESTRATOR_LEASE_RENEWED", work_item_id, lease, True

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def release(database, work_item_id, orchestrator_id, generation, request_id, now=None):
    if generation < 1:
        raise LiteError("generation must be positive")
    request = _request("RELEASE", workItemId=work_item_id,
                       orchestratorId=orchestrator_id, generation=generation)

    def action(connection, current):
        lease, fence_reason = _fenced(
            connection, work_item_id, orchestrator_id, generation, current
        )
        if lease is None:
            result = _result("RELEASE", "REFUSED", reason=fence_reason,
                             next_step=_next("STOP_STALE_OWNER"))
            return result, None, work_item_id, None, False
        connection.execute(
            "UPDATE orchestrator_leases SET status='RELEASED',released_at=? WHERE lease_id=?",
            (current, lease["lease_id"]),
        )
        lease = connection.execute(
            "SELECT * FROM orchestrator_leases WHERE lease_id=?", (lease["lease_id"],)
        ).fetchone()
        result = _result("RELEASE", "OK", lease=lease, next_step=_next("NONE"))
        return result, "ORCHESTRATOR_LEASE_RELEASED", work_item_id, lease, True

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def recover(database, work_item_id, orchestrator_id, ttl, request_id, now=None):
    if ttl < 1:
        raise LiteError("ttl must be positive")
    request = _request("RECOVER", workItemId=work_item_id,
                       orchestratorId=orchestrator_id, ttl=ttl)

    def action(connection, current):
        item = connection.execute(
            "SELECT * FROM work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if item is None:
            result = _result("RECOVER", "REFUSED", reason="WORK_ITEM_NOT_FOUND",
                             next_step=_next("STOP"))
            return result, "ORCHESTRATOR_RECOVER_REFUSED", work_item_id, None, False
        if (item["state"] == "FINAL_ACCEPTANCE_APPROVED" or
                item["queue_state"] not in ("CLAIMABLE", "CLAIMED", "WAITING_HUMAN")):
            result = _result("RECOVER", "REFUSED", reason="NOT_ELIGIBLE",
                             next_step=_next("WAIT_FOR_HUMAN"))
            return result, "ORCHESTRATOR_RECOVER_REFUSED", work_item_id, None, False
        latest = connection.execute(
            "SELECT * FROM orchestrator_leases WHERE work_item_id=? "
            "ORDER BY generation DESC LIMIT 1", (work_item_id,),
        ).fetchone()
        if latest is None or latest["status"] == "RELEASED" or latest["expires_at"] > current:
            reason = "LEASE_HELD" if latest is not None and latest["status"] == "ACTIVE" else "NO_EXPIRED_LEASE"
            result = _result("RECOVER", "CONFLICT" if reason == "LEASE_HELD" else "REFUSED",
                             reason=reason, next_step=_next("WAIT_OR_CLAIM"))
            return result, "ORCHESTRATOR_RECOVER_REFUSED", work_item_id, None, False
        if latest["status"] == "ACTIVE":
            reconciled = _activity_row("orchestrator-lease", latest, current, False)
            connection.execute(
                "UPDATE orchestrator_leases SET status='EXPIRED',released_at=? WHERE lease_id=?",
                (current, latest["lease_id"]),
            )
        if connection.execute(
            "SELECT 1 FROM orchestrator_leases WHERE work_item_id=? AND status='ACTIVE'",
            (work_item_id,),
        ).fetchone():
            result = _result("RECOVER", "CONFLICT", reason="LEASE_HELD",
                             next_step=_next("WAIT_OR_CLAIM"))
            return result, "ORCHESTRATOR_RECOVER_REFUSED", work_item_id, None, False
        lease = _new_lease(connection, work_item_id, orchestrator_id, ttl, current)
        result = _result("RECOVER", "OK", lease=lease,
                         reconciledActivity=([reconciled] if latest["status"] == "ACTIVE" else []),
                         next_step=_next("OBSERVE_OR_DISPATCH", workItemId=work_item_id,
                                         generation=lease["generation"]))
        return result, "ORCHESTRATOR_LEASE_RECOVERED", work_item_id, lease, False

    return _mutate(database, request_id, orchestrator_id, request, action, now)


def list_leases(database, orchestrator_id=None, status=None):
    connection, refusal = _open("LIST", database)
    if refusal:
        return refusal
    try:
        where = []
        values = []
        if orchestrator_id:
            _validate_id(orchestrator_id, "orchestrator")
            where.append("orchestrator_id=?")
            values.append(orchestrator_id)
        if status:
            if status not in ("ACTIVE", "RELEASED", "EXPIRED"):
                raise LiteError("status is invalid")
            where.append("status=?")
            values.append(status)
        query = "SELECT * FROM orchestrator_leases"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY work_item_id,generation"
        current = _timestamp()
        terminal = {row[0]: row[1] == "FINAL_ACCEPTANCE_APPROVED" for row in
                    connection.execute("SELECT work_item_id,state FROM work_items")}
        leases = []
        for row in connection.execute(query, values):
            value = _lease(row)
            value.update(_activity_projection(
                "ORCHESTRATOR_LEASE", row, current,
                terminal.get(row["work_item_id"], False),
            ))
            leases.append(value)
        return _result("LIST", "OK", next_step=_next("NONE"), leases=leases)
    finally:
        connection.close()


def show(database, work_item_id):
    connection, refusal = _open("SHOW", database)
    if refusal:
        return refusal
    try:
        item = connection.execute(
            "SELECT 1 FROM work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if item is None:
            return _result("SHOW", "REFUSED", reason="WORK_ITEM_NOT_FOUND",
                           next_step=_next("STOP"))
        lease_rows = connection.execute(
            "SELECT * FROM orchestrator_leases WHERE work_item_id=? ORDER BY generation",
            (work_item_id,),
        ).fetchall()
        current = _timestamp()
        terminal = connection.execute(
            "SELECT state FROM work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()[0] == "FINAL_ACCEPTANCE_APPROVED"
        leases = []
        for row in lease_rows:
            value = _lease(row)
            value.update(_activity_projection(
                "ORCHESTRATOR_LEASE", row, current, terminal
            ))
            leases.append(value)
        events = []
        for row in connection.execute(
            "SELECT orchestrator_event_id,request_id,operation,event_type,orchestrator_id,"
            "work_item_id,lease_id,generation,created_at FROM orchestrator_events "
            "WHERE work_item_id=? ORDER BY orchestrator_event_id", (work_item_id,),
        ):
            events.append(dict(row))
        active = next((entry for entry in reversed(leases)
                       if entry["effectiveStatus"] == "LIVE"), None)
        return _result("SHOW", "OK", lease=active, next_step=_next("NONE"),
                       leases=leases, events=events)
    finally:
        connection.close()


def active_count(connection):
    if not schema_installed(connection):
        return 0
    now = _timestamp()
    return connection.execute(
        "SELECT count(*) FROM orchestrator_leases WHERE status='ACTIVE' AND expires_at>?",
        (now,),
    ).fetchone()[0]
