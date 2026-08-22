"""Provider-neutral, observation-only usage event core."""

import csv
import datetime
import hashlib
import json
import math
import os
import pkgutil
import secrets
import uuid
from decimal import Decimal


USAGE_SCHEMA_VERSION = "AWB-USAGE-v1"
USAGE_REPORT_VERSION = "AWB-USAGE-REPORT-v1"
USAGE_EXPORT_VERSION = "AWB-USAGE-EXPORT-v1"
ROLES = ("ORCHESTRATOR", "PLANNER", "IMPLEMENTER", "REVIEWER")
ATTRIBUTION = ("ATTRIBUTED", "UNATTRIBUTED", "SHARED")
COUNTER_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_tokens", "total_tokens",
)
USAGE_EVENT_TYPES = (
    "USAGE_BINDING_RECORDED", "USAGE_SPAN_BEGAN", "USAGE_SPAN_ENDED",
    "AGENT_USAGE_RECORDED", "COUNTER_SEGMENT_STARTED", "QUOTA_SNAPSHOT_RECORDED",
    "USAGE_SYNC_REJECTED", "USAGE_CORRECTED", "COHORT_STARTED", "COHORT_SNAPSHOT",
    "COHORT_SEMANTICS_CHANGED", "COHORT_CONCLUDED",
)


class UsageError(Exception):
    pass


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value):
    if not isinstance(value, bytes):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def schema_installed(connection):
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='usage_schema_version'"
    ).fetchone()
    return bool(row and row[0] == USAGE_SCHEMA_VERSION)


def require_schema(connection):
    if not schema_installed(connection):
        raise UsageError("usage schema is not installed; run awb migrate --project .")


def usage_schema_sql():
    return """
INSERT INTO schema_meta(key,value) VALUES('usage_schema_version','AWB-USAGE-v1');
CREATE TABLE usage_events (
 usage_event_id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
 event_type TEXT NOT NULL CHECK(event_type IN (
 'USAGE_BINDING_RECORDED','USAGE_SPAN_BEGAN','USAGE_SPAN_ENDED',
 'AGENT_USAGE_RECORDED','COUNTER_SEGMENT_STARTED','QUOTA_SNAPSHOT_RECORDED',
 'USAGE_SYNC_REJECTED','USAGE_CORRECTED','COHORT_STARTED','COHORT_SNAPSHOT',
 'COHORT_SEMANTICS_CHANGED','COHORT_CONCLUDED')),
 work_item_id TEXT REFERENCES work_items(work_item_id), task_id TEXT REFERENCES tasks(task_id),
 claim_id TEXT REFERENCES claims(claim_id), provider TEXT, source_session_id TEXT,
 source_snapshot_key TEXT, actor_kind TEXT NOT NULL CHECK(actor_kind IN ('AGENT','HUMAN','SYSTEM')),
 actor_id TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
 observed_at TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX usage_events_work_item ON usage_events(work_item_id,usage_event_id);
CREATE INDEX usage_events_stream ON usage_events(provider,source_session_id,usage_event_id);
CREATE UNIQUE INDEX usage_source_snapshot_once ON usage_events(provider,source_session_id,source_snapshot_key)
 WHERE source_snapshot_key IS NOT NULL;
CREATE TRIGGER usage_events_no_update BEFORE UPDATE ON usage_events BEGIN
 SELECT RAISE(ABORT,'usage_events are immutable'); END;
CREATE TRIGGER usage_events_no_delete BEFORE DELETE ON usage_events BEGIN
 SELECT RAISE(ABORT,'usage_events are immutable'); END;
"""


def install_schema(connection):
    if schema_installed(connection):
        return False
    connection.executescript(usage_schema_sql())
    return True


def validate_counters(raw):
    if not isinstance(raw, dict):
        raise UsageError("usage counters must be an object")
    counters = {}
    for field in COUNTER_FIELDS:
        value = raw.get(field, 0 if field == "cache_write_input_tokens" else None)
        if type(value) is not int or value < 0:
            raise UsageError("usage counter {0} must be a non-negative integer".format(field))
        counters[field] = value
    extensions = raw.get("numeric_extensions", {})
    if not isinstance(extensions, dict):
        raise UsageError("numeric_extensions must be an object")
    for key, value in extensions.items():
        if (not isinstance(key, str) or not key or key in COUNTER_FIELDS or
                type(value) is not int or value < 0):
            raise UsageError("numeric extension is invalid")
    if counters["cached_input_tokens"] > counters["input_tokens"]:
        raise UsageError("cached input exceeds input")
    if counters["cache_write_input_tokens"] > counters["input_tokens"]:
        raise UsageError("cache write input exceeds input")
    if (counters["cached_input_tokens"] + counters["cache_write_input_tokens"] >
            counters["input_tokens"]):
        raise UsageError("cached plus cache-write input exceeds input")
    if counters["reasoning_tokens"] > counters["output_tokens"]:
        raise UsageError("reasoning output exceeds output")
    if counters["total_tokens"] != counters["input_tokens"] + counters["output_tokens"]:
        raise UsageError("total tokens does not equal input plus output")
    counters["numeric_extensions"] = dict(sorted(extensions.items()))
    return counters


def load_rate_cards(raw=None):
    encoded = raw
    if encoded is None:
        encoded = pkgutil.get_data("agent_workboard", "resources/usage/rate-cards.json")
    if encoded is None:
        raise UsageError("rate card resource is missing")
    if isinstance(encoded, bytes):
        encoded = encoded.decode("utf-8")
    try:
        cards = json.loads(encoded)
    except (TypeError, ValueError):
        raise UsageError("rate card resource is invalid")
    if not isinstance(cards, list) or not cards:
        raise UsageError("rate card resource must be non-empty")
    return cards, _sha(encoded.encode("utf-8"))


def _parse_time(value):
    if not isinstance(value, str):
        raise UsageError("timestamp must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise UsageError("timestamp must be ISO-8601")
    if parsed.tzinfo is None:
        raise UsageError("timestamp must include an offset")
    return parsed.astimezone(datetime.timezone.utc)


def estimate_credits(counters, model, observed_at, rate_cards=None):
    counters = validate_counters(counters)
    cards, resource_sha = load_rate_cards(rate_cards)
    observed = _parse_time(observed_at)
    card = prices = None
    for candidate in cards:
        effective_from = _parse_time(candidate.get("effective_from"))
        effective_to = (_parse_time(candidate.get("effective_to"))
                        if candidate.get("effective_to") is not None else None)
        if (effective_from <= observed and
                (effective_to is None or observed < effective_to) and
                model in candidate.get("models", {})):
            card, prices = candidate, candidate["models"][model]
    extensions = sorted(counters["numeric_extensions"])
    if card is None:
        return {"status": "UNKNOWN", "estimatedCredits": None, "model": model,
                "rateCardVersion": None, "resourceSha256": resource_sha,
                "unpricedFields": extensions}
    required = ("input", "cached_input", "cache_write_input", "output")
    if any(type(prices.get(key)) not in (int, float) for key in required):
        raise UsageError("rate card price fields are invalid")
    uncached = (counters["input_tokens"] - counters["cached_input_tokens"] -
                counters["cache_write_input_tokens"])
    if uncached < 0:
        raise UsageError("uncached input would be negative")
    value = (Decimal(uncached) * Decimal(str(prices["input"])) +
             Decimal(counters["cached_input_tokens"]) * Decimal(str(prices["cached_input"])) +
             Decimal(counters["cache_write_input_tokens"]) * Decimal(str(prices["cache_write_input"])) +
             Decimal(counters["output_tokens"]) * Decimal(str(prices["output"]))) / Decimal(1000000)
    return {"status": "PARTIAL" if extensions else "COMPLETE",
            "estimatedCredits": float(value), "model": model,
            "rateCardVersion": card["version"], "resourceSha256": resource_sha,
            "effectiveFrom": card["effective_from"], "effectiveTo": card.get("effective_to"),
            "unit": card["unit"], "prices": dict(prices), "unpricedFields": extensions}


def normalize_role(value):
    value = (value or "").upper().replace("-", "_")
    if value in ("CONVERGENCE_REVIEWER", "REVIEWER"):
        return "REVIEWER"
    return value if value in ROLES else None


def append_event(connection, event_type, actor_kind, actor_id, payload,
                 work_item_id=None, task_id=None, claim_id=None, provider=None,
                 source_session_id=None, source_snapshot_key=None,
                 observed_at=None, request_id=None):
    require_schema(connection)
    if event_type not in USAGE_EVENT_TYPES:
        raise UsageError("unsupported usage event type")
    request_id = request_id or "usage-" + uuid.uuid4().hex
    encoded = _json(payload)
    existing = connection.execute(
        "SELECT event_type,payload_json FROM usage_events WHERE request_id=?", (request_id,)
    ).fetchone()
    if existing:
        if existing[0] != event_type or existing[1] != encoded:
            raise UsageError("usage request_id was reused with different content")
        return False
    connection.execute(
        "INSERT INTO usage_events(request_id,event_type,work_item_id,task_id,claim_id,"
        "provider,source_session_id,source_snapshot_key,actor_kind,actor_id,payload_json,"
        "observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (request_id, event_type, work_item_id, task_id, claim_id, provider,
         source_session_id, source_snapshot_key, actor_kind, actor_id, encoded,
         observed_at or _now(), _now()),
    )
    return True


def binding_rows(connection, provider=None, session_id=None, work_item_id=None):
    clauses = ["event_type='USAGE_BINDING_RECORDED'"]
    values = []
    for column, value in (("provider", provider), ("source_session_id", session_id),
                          ("work_item_id", work_item_id)):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    return connection.execute(
        "SELECT * FROM usage_events WHERE " + " AND ".join(clauses) + " ORDER BY usage_event_id",
        values,
    ).fetchall()


def record_binding(connection, claim, provider, source_session_id, model,
                   baseline=None, adapter_version="unknown", parser_version="unknown",
                   role_observed=None, request_id=None):
    if provider != "codex-local" or not source_session_id or not model:
        raise UsageError("usage binding requires codex-local, session id, and exact model")
    require_schema(connection)
    duplicate = connection.execute(
        "SELECT 1 FROM usage_events u JOIN claims c ON c.claim_id=u.claim_id "
        "WHERE u.event_type='USAGE_BINDING_RECORDED' AND u.provider=? "
        "AND u.source_session_id=? AND c.status='ACTIVE' AND c.claim_id<>?",
        (provider, source_session_id, claim["claim_id"]),
    ).fetchone()
    counters = validate_counters(baseline["counters"]) if baseline else None
    boundary = ({"adapterVersion": adapter_version, "parserVersion": parser_version,
                 "sourceOrdinal": baseline.get("source_ordinal"),
                 "sourceSnapshotKey": baseline.get("source_snapshot_key")}
                if baseline else None)
    payload = {
        "schemaVersion": USAGE_SCHEMA_VERSION, "adapterVersion": adapter_version,
        "parserVersion": parser_version, "model": model, "role": claim["role"],
        "agentId": claim["agent_id"],
        "attributionStatus": "SHARED" if duplicate else "ATTRIBUTED",
        "baseline": counters,
        "baselineSourceKey": baseline.get("source_snapshot_key") if baseline else None,
        "baselineBoundary": boundary,
        "baselineStatus": "OBSERVED" if baseline else "MISSING",
        "qualityFlags": (["ROLE_MISMATCH"] if role_observed and
                         normalize_role(role_observed) != claim["role"] else []),
    }
    append_event(connection, "USAGE_BINDING_RECORDED", "AGENT", claim["agent_id"], payload,
                 claim["work_item_id"], claim["task_id"], claim["claim_id"], provider,
                 source_session_id, observed_at=claim["acquired_at"], request_id=request_id)
    return payload


def _latest_stream_state(connection, provider, session_id):
    rows = connection.execute(
        "SELECT event_type,payload_json FROM usage_events WHERE provider=? AND source_session_id=? "
        "AND event_type IN ('USAGE_BINDING_RECORDED','USAGE_SPAN_BEGAN','AGENT_USAGE_RECORDED') "
        "ORDER BY usage_event_id", (provider, session_id),
    ).fetchall()
    counters, segment = None, 1
    for row in rows:
        payload = json.loads(row["payload_json"])
        candidate = (payload.get("baseline") if row["event_type"] in
                     ("USAGE_BINDING_RECORDED", "USAGE_SPAN_BEGAN")
                     else payload.get("cumulative"))
        if candidate is not None:
            counters = validate_counters(candidate)
        if row["event_type"] == "AGENT_USAGE_RECORDED":
            segment = payload.get("segment", segment)
    return counters, segment


def _latest_baseline_boundary(connection, provider, session_id):
    row = connection.execute(
        "SELECT payload_json FROM usage_events WHERE provider=? AND source_session_id=? "
        "AND event_type IN ('USAGE_BINDING_RECORDED','USAGE_SPAN_BEGAN') "
        "ORDER BY usage_event_id DESC LIMIT 1", (provider, session_id),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    boundary = payload.get("baselineBoundary")
    if not isinstance(boundary, dict) or type(boundary.get("sourceOrdinal")) is not int:
        return None
    return boundary


def _snapshot_is_at_or_before_boundary(connection, snapshot):
    boundary = _latest_baseline_boundary(
        connection, snapshot["provider"], snapshot["source_session_id"])
    if boundary is None:
        return False
    ordinal = snapshot.get("source_ordinal")
    if type(ordinal) is not int:
        raise UsageError("snapshot source cursor is not comparable to binding boundary")
    if (boundary.get("adapterVersion") != snapshot.get("adapter_version") or
            boundary.get("parserVersion") != snapshot.get("parser_version")):
        raise UsageError("snapshot source cursor version is not comparable to binding boundary")
    return ordinal <= boundary["sourceOrdinal"]


def prepare_interval_boundary(database, provider, session_id, sessions_root=None):
    """Capture an existing stream interval before returning its next boundary.

    The first participant may anchor at the latest source snapshot without
    importing pre-binding history.  Every later participant first consumes all
    snapshots after the preceding boundary under the participants that were
    active at that time.  The returned boundary therefore never advances past
    an unrecorded accepted delta.
    """
    if provider != "codex-local":
        raise UsageError("unsupported usage provider")
    from .lite import open_database
    from .usage_adapters.codex_local import CodexLocalAdapter
    adapter = CodexLocalAdapter(sessions_root=sessions_root)
    parsed = adapter.read(session_id)
    connection = open_database(database)
    try:
        require_schema(connection)
        prior_interval = connection.execute(
            "SELECT 1 FROM usage_events WHERE provider=? AND source_session_id=? "
            "AND event_type IN ('USAGE_BINDING_RECORDED','USAGE_SPAN_BEGAN') LIMIT 1",
            (provider, session_id)).fetchone()
        if prior_interval:
            connection.execute("BEGIN IMMEDIATE")
            for snapshot in parsed["snapshots"]:
                ingest_snapshot(connection, snapshot, actor_id="usage-boundary-handoff")
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    baseline = parsed["snapshots"][-1] if parsed["snapshots"] else None
    if baseline is None and parsed["identity"]:
        baseline = {"source_snapshot_key": "verified-zero-baseline", "source_ordinal": 0,
                    "counters": {"input_tokens": 0, "cached_input_tokens": 0,
                                 "cache_write_input_tokens": 0, "output_tokens": 0,
                                 "reasoning_tokens": 0, "total_tokens": 0,
                                 "numeric_extensions": {}}}
    return baseline, parsed["identity"], adapter


def _stage(connection, work_item_id):
    if not work_item_id:
        return "UNKNOWN"
    row = connection.execute("SELECT state FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone()
    mapping = {"DRAFT": "PLANNING", "PLAN_REVIEW_PENDING": "PLAN_REVIEW",
               "PLAN_REVIEW_APPROVED": "HUMAN_PLAN_WAIT", "IMPLEMENTING": "IMPLEMENTATION",
               "IMPLEMENTATION_COMPLETED": "IMPLEMENTATION_REVIEW",
               "FINAL_ACCEPTANCE_APPROVED": "HUMAN_FINAL_WAIT"}
    return mapping.get(row[0], "UNKNOWN") if row else "UNKNOWN"


def resolve_attribution(connection, provider, session_id):
    participants = []
    for binding in binding_rows(connection, provider, session_id):
        claim = connection.execute("SELECT * FROM claims WHERE claim_id=?", (binding["claim_id"],)).fetchone()
        if claim is not None and claim["status"] == "ACTIVE":
            payload = json.loads(binding["payload_json"])
            review_stage = "PLAN" if _stage(connection, claim["work_item_id"]) == "PLAN_REVIEW" else "FINAL"
            review_round = connection.execute(
                "SELECT count(*)+1 FROM reviews WHERE work_item_id=? AND stage=?",
                (claim["work_item_id"], review_stage)).fetchone()[0]
            revision_stage = "PLAN" if claim["role"] == "PLANNER" else "FINAL"
            revision = bool(claim["role"] in ("PLANNER", "IMPLEMENTER") and connection.execute(
                "SELECT 1 FROM reviews WHERE work_item_id=? AND stage=? AND decision='REJECTED' LIMIT 1",
                (claim["work_item_id"], revision_stage)).fetchone())
            participants.append({"workItemId": claim["work_item_id"], "taskId": claim["task_id"],
                                 "claimId": claim["claim_id"], "agentId": claim["agent_id"],
                                 "role": claim["role"], "agentProfile": (
                                     ("CONVERGENCE_REVIEWER" if review_round == 4 else "ORDINARY_REVIEWER")
                                     if claim["role"] == "REVIEWER" else None),
                                 "model": payload.get("model"), "revision": revision})
    for begin in connection.execute(
            "SELECT * FROM usage_events b WHERE b.event_type='USAGE_SPAN_BEGAN' AND b.provider=? "
            "AND b.source_session_id=? AND NOT EXISTS (SELECT 1 FROM usage_events e "
            "WHERE e.event_type='USAGE_SPAN_ENDED' AND json_extract(e.payload_json,'$.spanId')="
            "json_extract(b.payload_json,'$.spanId')) ORDER BY b.usage_event_id", (provider, session_id)):
        payload = json.loads(begin["payload_json"])
        participants.append({"workItemId": begin["work_item_id"], "taskId": begin["task_id"],
                             "claimId": None, "agentId": begin["actor_id"], "role": "ORCHESTRATOR",
                             "agentProfile": None, "model": payload.get("model"), "revision": False})
    if len(participants) == 1:
        result = participants[0]
        result.update({"status": "ATTRIBUTED", "stage": _stage(connection, result["workItemId"])})
        return result
    if len(participants) > 1:
        return {"workItemId": None, "taskId": None, "claimId": None, "agentId": None,
                "role": None, "agentProfile": None, "model": None, "stage": "UNKNOWN",
                "status": "SHARED", "participantCount": len(participants), "revision": False}
    return {"workItemId": None, "taskId": None, "claimId": None, "agentId": None,
            "role": None, "agentProfile": None, "model": None, "stage": "UNKNOWN",
            "status": "UNATTRIBUTED", "revision": False}


def ingest_snapshot(connection, snapshot, actor_id="usage-sync", dry_run=False):
    require_schema(connection)
    required = ("provider", "source_session_id", "source_snapshot_key", "observed_at",
                "adapter_version", "parser_version", "counters")
    if any(snapshot.get(key) is None for key in required):
        raise UsageError("snapshot identity is incomplete")
    provider, session_id = snapshot["provider"], snapshot["source_session_id"]
    counters = validate_counters(snapshot["counters"])
    existing = connection.execute(
        "SELECT payload_json FROM usage_events WHERE provider=? AND source_session_id=? "
        "AND source_snapshot_key=?", (provider, session_id, snapshot["source_snapshot_key"])).fetchone()
    if existing:
        prior = json.loads(existing[0])
        if prior.get("cumulative") not in (None, counters):
            raise UsageError("source snapshot key was reused with different counters")
        return {"status": "DUPLICATE", "wouldWrite": 0}
    if _snapshot_is_at_or_before_boundary(connection, snapshot):
        return {"status": "BOUNDARY_SKIPPED", "wouldWrite": 0}
    previous, segment = _latest_stream_state(connection, provider, session_id)
    reset = bool(previous and any(counters[field] < previous[field] for field in COUNTER_FIELDS))
    if previous is None or reset:
        delta = dict(counters)
    else:
        delta = {field: counters[field] - previous[field] for field in COUNTER_FIELDS}
        delta["numeric_extensions"] = {}
        for key in set(counters["numeric_extensions"]) | set(previous["numeric_extensions"]):
            current, prior = counters["numeric_extensions"].get(key, 0), previous["numeric_extensions"].get(key, 0)
            if current < prior:
                reset = True
                break
            delta["numeric_extensions"][key] = current - prior
        if reset:
            delta = dict(counters)
    if reset:
        segment += 1
    if (not reset and previous is not None and not any(delta[field] for field in COUNTER_FIELDS)
            and not any(delta["numeric_extensions"].values())):
        return {"status": "UNCHANGED", "wouldWrite": 0}
    attribution = resolve_attribution(connection, provider, session_id)
    credits = estimate_credits(delta, attribution.get("model"), snapshot["observed_at"])
    payload = {"schemaVersion": USAGE_SCHEMA_VERSION, "adapterVersion": snapshot["adapter_version"],
               "parserVersion": snapshot["parser_version"], "accuracy": snapshot.get("accuracy", "OBSERVED"),
               "sourceOrdinal": snapshot.get("source_ordinal"), "segment": segment,
               "cumulative": counters, "delta": delta, "attribution": attribution, "credits": credits}
    writes = 1 + int(reset)
    result = {"status": "ACCEPTED", "wouldWrite": writes, "reset": reset,
              "attribution": attribution["status"], "creditStatus": credits["status"], "delta": delta}
    if dry_run:
        return result
    if reset:
        append_event(connection, "COUNTER_SEGMENT_STARTED", "SYSTEM", actor_id,
                     {"schemaVersion": USAGE_SCHEMA_VERSION, "segment": segment,
                      "reason": "COUNTER_RESET"}, provider=provider,
                     source_session_id=session_id, observed_at=snapshot["observed_at"])
    append_event(connection, "AGENT_USAGE_RECORDED", "SYSTEM", actor_id, payload,
                 attribution.get("workItemId"), attribution.get("taskId"), attribution.get("claimId"),
                 provider, session_id, snapshot["source_snapshot_key"], snapshot["observed_at"])
    return result


def record_quota(connection, quota, provider="codex-local", actor_id="usage-sync", dry_run=False):
    required = ("limit_name", "used_percent", "window_minutes", "resets_at", "observed_at")
    if any(key not in quota for key in required):
        raise UsageError("quota snapshot is incomplete")
    if (type(quota["used_percent"]) not in (int, float) or not 0 <= quota["used_percent"] <= 100 or
            type(quota["window_minutes"]) is not int or quota["window_minutes"] <= 0):
        raise UsageError("quota values are invalid")
    payload = {"schemaVersion": USAGE_SCHEMA_VERSION, "limitName": quota["limit_name"],
               "usedPercent": quota["used_percent"], "windowMinutes": quota["window_minutes"],
               "resetsAt": quota["resets_at"]}
    source_key = "quota-" + _sha(_json([provider, quota["observed_at"], payload]))
    if connection.execute("SELECT 1 FROM usage_events WHERE provider=? AND source_snapshot_key=?",
                          (provider, source_key)).fetchone():
        return {"status": "DUPLICATE", "wouldWrite": 0}
    if not dry_run:
        append_event(connection, "QUOTA_SNAPSHOT_RECORDED", "SYSTEM", actor_id, payload,
                     provider=provider, source_snapshot_key=source_key, observed_at=quota["observed_at"])
    return {"status": "ACCEPTED", "wouldWrite": 1}


def sync(database, work_item_id=None, all_bound=False, dry_run=False, sessions_root=None):
    from .lite import open_database
    from .usage_adapters.codex_local import CodexLocalAdapter
    connection = open_database(database)
    try:
        require_schema(connection)
        if not all_bound and work_item_id is None:
            raise UsageError("sync requires a WorkItem or --all-bound")
        bindings = binding_rows(connection, work_item_id=work_item_id)
        streams = set((row["provider"], row["source_session_id"]) for row in bindings)
        clauses, values = ["event_type='USAGE_SPAN_BEGAN'"], []
        if work_item_id:
            clauses.append("work_item_id=?")
            values.append(work_item_id)
        for row in connection.execute(
                "SELECT provider,source_session_id FROM usage_events WHERE " +
                " AND ".join(clauses), values):
            streams.add((row["provider"], row["source_session_id"]))
        streams = sorted(streams)
        adapter = CodexLocalAdapter(sessions_root=sessions_root)
        summary = {"schemaVersion": USAGE_REPORT_VERSION, "dryRun": bool(dry_run),
                   "streams": len(streams), "accepted": 0, "rejected": 0,
                   "duplicate": 0, "unchanged": 0, "boundary_skipped": 0,
                   "reset": 0, "wouldWriteEvents": 0,
                   "attribution": {key: 0 for key in ATTRIBUTION},
                   "creditStatus": {key: 0 for key in ("COMPLETE", "PARTIAL", "UNKNOWN")},
                   "quotaAccepted": 0, "privacy": "ALLOWLIST_ONLY"}
        if not dry_run:
            connection.execute("BEGIN IMMEDIATE")
        for provider, session_id in streams:
            if provider != "codex-local":
                summary["rejected"] += 1
                continue
            try:
                parsed = adapter.read(session_id)
                for snapshot in parsed["snapshots"]:
                    result = ingest_snapshot(connection, snapshot, dry_run=dry_run)
                    status = result["status"].lower()
                    if status in summary:
                        summary[status] += 1
                    summary["wouldWriteEvents"] += result["wouldWrite"]
                    if result.get("reset"):
                        summary["reset"] += 1
                    if result.get("attribution"):
                        summary["attribution"][result["attribution"]] += 1
                    if result.get("creditStatus"):
                        summary["creditStatus"][result["creditStatus"]] += 1
                for quota in parsed["quota"]:
                    result = record_quota(connection, quota, dry_run=dry_run)
                    if result["status"] == "ACCEPTED":
                        summary["quotaAccepted"] += 1
                    summary["wouldWriteEvents"] += result["wouldWrite"]
            except Exception as exc:
                summary["rejected"] += 1
                if not dry_run:
                    reason_code = getattr(exc, "reason_code", "ADAPTER_REJECTED")
                    wrote = append_event(
                        connection, "USAGE_SYNC_REJECTED", "SYSTEM", "usage-sync",
                        {"schemaVersion": USAGE_SCHEMA_VERSION, "reasonCode": reason_code,
                         "parserVersion": adapter.parser_version},
                        work_item_id=work_item_id, provider=provider,
                        source_session_id=session_id,
                        request_id="usage-rejected-" + _sha(_json(
                            [provider, session_id, work_item_id, reason_code,
                             adapter.parser_version])))
                    summary["wouldWriteEvents"] += int(wrote)
        if not dry_run:
            connection.commit()
        return summary
    except Exception:
        if not dry_run:
            connection.rollback()
        raise
    finally:
        connection.close()


def best_effort_sync(database, work_item_id):
    try:
        return sync(database, work_item_id=work_item_id)
    except Exception as exc:
        return {"status": "coverage-gap", "reasonCode": getattr(exc, "reason_code", "SYNC_FAILED")}


def begin_span(database, work_item_id, task_id, agent_id, session_id, model,
               provider="codex-local", sessions_root=None):
    from .lite import open_database
    baseline = identity = None
    try:
        baseline, identity, adapter = prepare_interval_boundary(
            database, provider, session_id, sessions_root)
    except Exception:
        adapter = None
    connection = open_database(database)
    try:
        require_schema(connection)
        if not connection.execute("SELECT 1 FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone():
            raise UsageError("WorkItem does not exist")
        if task_id and not connection.execute(
                "SELECT 1 FROM tasks WHERE work_item_id=? AND task_id=?", (work_item_id, task_id)).fetchone():
            raise UsageError("span task does not exist")
        span_id = "span-" + uuid.uuid4().hex
        connection.execute("BEGIN IMMEDIATE")
        append_event(connection, "USAGE_SPAN_BEGAN", "AGENT", agent_id,
                     {"schemaVersion": USAGE_SCHEMA_VERSION, "spanId": span_id,
                      "model": model, "role": "ORCHESTRATOR",
                      "baseline": validate_counters(baseline["counters"]) if baseline else None,
                      "baselineSourceKey": baseline.get("source_snapshot_key") if baseline else None,
                      "baselineBoundary": ({
                          "adapterVersion": adapter.adapter_version,
                          "parserVersion": adapter.parser_version,
                          "sourceOrdinal": baseline.get("source_ordinal"),
                          "sourceSnapshotKey": baseline.get("source_snapshot_key")}
                          if baseline and adapter else None),
                      "adapterVersion": adapter.adapter_version if adapter else None,
                      "parserVersion": adapter.parser_version if adapter else None,
                      "anchorStatus": "OBSERVED" if baseline else "MISSING"},
                     work_item_id, task_id, provider=provider, source_session_id=session_id)
        connection.commit()
        return {"spanId": span_id, "status": "OPEN"}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def end_span(database, span_id, agent_id, sessions_root=None):
    from .lite import open_database
    connection = open_database(database)
    try:
        require_schema(connection)
        begin = connection.execute(
            "SELECT * FROM usage_events WHERE event_type='USAGE_SPAN_BEGAN' "
            "AND json_extract(payload_json,'$.spanId')=?", (span_id,)).fetchone()
        if begin is None or begin["actor_id"] != agent_id:
            raise UsageError("open span is not owned by agent")
        if connection.execute(
                "SELECT 1 FROM usage_events WHERE event_type='USAGE_SPAN_ENDED' "
                "AND json_extract(payload_json,'$.spanId')=?", (span_id,)).fetchone():
            raise UsageError("span is already ended")
        work_item_id = begin["work_item_id"]
        begin_data = dict(begin)
    finally:
        connection.close()
    try:
        sync(database, work_item_id=work_item_id, sessions_root=sessions_root)
    except Exception:
        pass
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        append_event(connection, "USAGE_SPAN_ENDED", "AGENT", agent_id,
                     {"schemaVersion": USAGE_SCHEMA_VERSION, "spanId": span_id,
                      "anchorStatus": "OBSERVED"}, work_item_id, begin_data["task_id"],
                     provider=begin_data["provider"], source_session_id=begin_data["source_session_id"])
        connection.commit()
        return {"spanId": span_id, "status": "ENDED"}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def correct(database, usage_event_id, human_id, reason, replacement):
    from .lite import open_database
    connection = open_database(database)
    try:
        require_schema(connection)
        target = connection.execute(
            "SELECT * FROM usage_events WHERE usage_event_id=? AND event_type='AGENT_USAGE_RECORDED'",
            (usage_event_id,)).fetchone()
        if target is None:
            raise UsageError("correction target is not an agent usage event")
        if connection.execute(
                "SELECT 1 FROM usage_events WHERE event_type='USAGE_CORRECTED' "
                "AND json_extract(payload_json,'$.targetEventId')=?", (usage_event_id,)).fetchone():
            raise UsageError("correction branch is not allowed")
        target_payload = json.loads(target["payload_json"])
        original = target_payload["delta"]
        replacement = validate_counters(replacement)
        replacement_credits = estimate_credits(
            replacement, target_payload["attribution"].get("model"), target["observed_at"])
        connection.execute("BEGIN IMMEDIATE")
        append_event(connection, "USAGE_CORRECTED", "HUMAN", human_id,
                     {"schemaVersion": USAGE_SCHEMA_VERSION, "correctionSchemaVersion": 1,
                      "targetEventId": usage_event_id, "original": original,
                      "replacement": replacement, "replacementCredits": replacement_credits,
                      "reason": reason},
                     target["work_item_id"], target["task_id"], target["claim_id"],
                     target["provider"], target["source_session_id"])
        connection.commit()
        return {"status": "ok", "targetEventId": usage_event_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cohort_event(database, event_type, cohort_id, actor_id, actor_kind="AGENT", payload=None):
    from .lite import open_database
    allowed = ("COHORT_STARTED", "COHORT_SNAPSHOT", "COHORT_SEMANTICS_CHANGED", "COHORT_CONCLUDED")
    if event_type not in allowed:
        raise UsageError("invalid cohort event")
    if event_type != "COHORT_SNAPSHOT" and actor_kind != "HUMAN":
        raise UsageError("cohort start, semantics change, and conclusion require HUMAN")
    payload = dict(payload or {})
    payload.update({"schemaVersion": USAGE_SCHEMA_VERSION, "cohortId": cohort_id})
    connection = open_database(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        append_event(connection, event_type, actor_kind, actor_id, payload)
        connection.commit()
        return {"status": "ok", "cohortId": cohort_id, "eventType": event_type}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _effective_events(connection, from_time=None, to_time=None, work_item_id=None):
    clauses, values = ["event_type='AGENT_USAGE_RECORDED'"], []
    for clause, value in (("observed_at>=?", from_time), ("observed_at<?", to_time),
                          ("work_item_id=?", work_item_id)):
        if value:
            clauses.append(clause)
            values.append(value)
    rows = connection.execute(
        "SELECT * FROM usage_events WHERE " + " AND ".join(clauses) + " ORDER BY usage_event_id",
        values).fetchall()
    output = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        correction = connection.execute(
            "SELECT payload_json FROM usage_events WHERE event_type='USAGE_CORRECTED' "
            "AND json_extract(payload_json,'$.targetEventId')=? ORDER BY usage_event_id DESC LIMIT 1",
            (row["usage_event_id"],)).fetchone()
        correction_payload = json.loads(correction[0]) if correction else None
        delta = correction_payload["replacement"] if correction_payload else payload["delta"]
        item = dict(row)
        item.update({"payload": payload, "delta": validate_counters(delta),
                     "correction": correction_payload})
        output.append(item)
    return output


def _nearest(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, int(math.ceil(percentile * len(ordered))) - 1)]


def _participation_quality(connection, work_item_id=None, from_time=None, to_time=None):
    clauses, values = [], []
    if work_item_id:
        clauses.append("work_item_id=?")
        values.append(work_item_id)
    if from_time:
        clauses.append("acquired_at>=?")
        values.append(from_time)
    if to_time:
        clauses.append("acquired_at<?")
        values.append(to_time)
    claims = connection.execute(
        "SELECT * FROM claims" + (" WHERE " + " AND ".join(clauses) if clauses else ""),
        values).fetchall()
    denominator, numerator = len(claims), 0
    for claim in claims:
        binding = connection.execute(
            "SELECT payload_json,provider,source_session_id FROM usage_events "
            "WHERE event_type='USAGE_BINDING_RECORDED' AND claim_id=? "
            "ORDER BY usage_event_id DESC LIMIT 1", (claim["claim_id"],)).fetchone()
        if not binding:
            continue
        payload = json.loads(binding["payload_json"])
        rejected = connection.execute(
            "SELECT 1 FROM usage_events WHERE event_type='USAGE_SYNC_REJECTED' "
            "AND provider=? AND source_session_id=? LIMIT 1",
            (binding["provider"], binding["source_session_id"])).fetchone()
        shared = connection.execute(
            "SELECT 1 FROM usage_events WHERE event_type='AGENT_USAGE_RECORDED' "
            "AND provider=? AND source_session_id=? "
            "AND json_extract(payload_json,'$.attribution.status')='SHARED' LIMIT 1",
            (binding["provider"], binding["source_session_id"])).fetchone()
        if (payload.get("baselineStatus") == "OBSERVED" and
                payload.get("attributionStatus") == "ATTRIBUTED" and not rejected and not shared):
            numerator += 1
    span_clauses, span_values = ["event_type='USAGE_SPAN_BEGAN'"], []
    if work_item_id:
        span_clauses.append("work_item_id=?")
        span_values.append(work_item_id)
    if from_time:
        span_clauses.append("observed_at>=?")
        span_values.append(from_time)
    if to_time:
        span_clauses.append("observed_at<?")
        span_values.append(to_time)
    spans = connection.execute(
        "SELECT * FROM usage_events WHERE " + " AND ".join(span_clauses), span_values).fetchall()
    denominator += len(spans)
    for span in spans:
        payload = json.loads(span["payload_json"])
        ended = connection.execute(
            "SELECT 1 FROM usage_events WHERE event_type='USAGE_SPAN_ENDED' "
            "AND json_extract(payload_json,'$.spanId')=?", (payload["spanId"],)).fetchone()
        rejected = connection.execute(
            "SELECT 1 FROM usage_events WHERE event_type='USAGE_SYNC_REJECTED' "
            "AND provider=? AND source_session_id=? LIMIT 1",
            (span["provider"], span["source_session_id"])).fetchone()
        shared = connection.execute(
            "SELECT 1 FROM usage_events WHERE event_type='AGENT_USAGE_RECORDED' "
            "AND provider=? AND source_session_id=? "
            "AND json_extract(payload_json,'$.attribution.status')='SHARED' LIMIT 1",
            (span["provider"], span["source_session_id"])).fetchone()
        if payload.get("anchorStatus") == "OBSERVED" and ended and not rejected and not shared:
            numerator += 1
    return {"denominator": denominator, "numerator": numerator,
            "coverage": float(numerator) / denominator if denominator else None}


def project(database, group_by="role", from_time=None, to_time=None,
            work_item_id=None, simulate_rate_cards=None):
    from .lite import open_database
    allowed = ("work_item", "role", "agent", "model", "stage", "date")
    if group_by not in allowed:
        raise UsageError("unsupported usage group")
    connection = open_database(database)
    try:
        require_schema(connection)
        groups, item_distribution = {}, {}
        for event in _effective_events(connection, from_time, to_time, work_item_id):
            payload, delta = event["payload"], event["delta"]
            attribution = payload["attribution"]
            key = {"work_item": attribution.get("workItemId") or attribution["status"],
                   "role": attribution.get("role") or attribution["status"],
                   "agent": attribution.get("agentId") or attribution["status"],
                   "model": attribution.get("model") or "UNKNOWN",
                   "stage": attribution.get("stage") or "UNKNOWN",
                   "date": event["observed_at"][:10]}[group_by]
            group = groups.setdefault(key, {
                "group": key, "eventCount": 0, "inputTokens": 0, "cachedInputTokens": 0,
                "cacheWriteInputTokens": 0, "outputTokens": 0, "reasoningTokens": 0,
                "totalTokens": 0, "estimatedCredits": 0.0, "creditStatus": "COMPLETE",
                "revisionTokens": 0, "revisionEstimatedCredits": 0.0,
                "attribution": {name: 0 for name in ATTRIBUTION}, "rateCardVersions": set(),
                "attributionTokenTotals": {name: 0 for name in ATTRIBUTION},
                "acceptanceOutcomes": set()})
            group["eventCount"] += 1
            for source, target in (("input_tokens", "inputTokens"),
                                   ("cached_input_tokens", "cachedInputTokens"),
                                   ("cache_write_input_tokens", "cacheWriteInputTokens"),
                                   ("output_tokens", "outputTokens"),
                                   ("reasoning_tokens", "reasoningTokens"),
                                   ("total_tokens", "totalTokens")):
                group[target] += delta[source]
            credits = (estimate_credits(delta, attribution.get("model"), event["observed_at"],
                                        simulate_rate_cards)
                       if simulate_rate_cards is not None else
                       (event["correction"].get("replacementCredits")
                        if event["correction"] else payload["credits"]))
            if credits["status"] == "UNKNOWN":
                group["creditStatus"] = "UNKNOWN"
            elif credits["status"] == "PARTIAL" and group["creditStatus"] == "COMPLETE":
                group["creditStatus"] = "PARTIAL"
            if credits.get("estimatedCredits") is not None:
                group["estimatedCredits"] += credits["estimatedCredits"]
                if attribution.get("revision"):
                    group["revisionEstimatedCredits"] += credits["estimatedCredits"]
            if attribution.get("revision"):
                group["revisionTokens"] += delta["total_tokens"]
            if credits.get("rateCardVersion"):
                group["rateCardVersions"].add(credits["rateCardVersion"])
            group["attribution"][attribution["status"]] += 1
            group["attributionTokenTotals"][attribution["status"]] += delta["total_tokens"]
            item_id = attribution.get("workItemId")
            if item_id:
                item_state = connection.execute(
                    "SELECT state FROM work_items WHERE work_item_id=?", (item_id,)).fetchone()
                if item_state:
                    group["acceptanceOutcomes"].add(item_state[0])
            if item_id:
                state = connection.execute(
                    "SELECT state FROM work_items WHERE work_item_id=?", (item_id,)).fetchone()
                item = item_distribution.setdefault(item_id, {
                    "credits": 0.0, "allComplete": True, "rateCardVersions": set(),
                    "semanticVersions": set(), "finalAccepted": bool(
                        state and state[0] == "FINAL_ACCEPTANCE_APPROVED")})
                item["allComplete"] = item["allComplete"] and credits["status"] == "COMPLETE"
                item["rateCardVersions"].add(credits.get("rateCardVersion"))
                item["semanticVersions"].add((
                    payload.get("schemaVersion"), payload.get("adapterVersion"),
                    payload.get("parserVersion")))
                if credits["status"] == "COMPLETE":
                    item["credits"] += credits["estimatedCredits"]
        serialized = []
        for key in sorted(groups):
            group = groups[key]
            group["estimatedCredits"] = round(group["estimatedCredits"], 12)
            group["rateCardVersions"] = sorted(group["rateCardVersions"])
            group["acceptanceOutcomes"] = sorted(group["acceptanceOutcomes"])
            group["cacheHitRatio"] = (float(group["cachedInputTokens"]) / group["inputTokens"]
                                        if group["inputTokens"] else None)
            group["revisionTokenRatio"] = (float(group["revisionTokens"]) / group["totalTokens"]
                                            if group["totalTokens"] else None)
            group["revisionCreditRatio"] = (
                group["revisionEstimatedCredits"] / group["estimatedCredits"]
                if group["estimatedCredits"] and group["creditStatus"] == "COMPLETE" else None)
            group["attributionCoverage"] = (
                float(group["attribution"]["ATTRIBUTED"]) / group["eventCount"]
                if group["eventCount"] else None)
            token_total = sum(group["attributionTokenTotals"].values())
            group["attributionTokenRatios"] = {
                name: (float(group["attributionTokenTotals"][name]) / token_total
                       if token_total else None) for name in ATTRIBUTION}
            serialized.append(group)
        quality = {"rejectedSnapshots": connection.execute(
                       "SELECT count(*) FROM usage_events WHERE event_type='USAGE_SYNC_REJECTED'").fetchone()[0],
                   "attributedEvents": sum(g["attribution"]["ATTRIBUTED"] for g in serialized),
                   "sharedEvents": sum(g["attribution"]["SHARED"] for g in serialized),
                   "unattributedEvents": sum(g["attribution"]["UNATTRIBUTED"] for g in serialized)}
        denominator = quality["attributedEvents"] + quality["sharedEvents"] + quality["unattributedEvents"]
        quality["eventAttributionCoverage"] = (float(quality["attributedEvents"]) / denominator
                                                if denominator else None)
        quality["participation"] = _participation_quality(
            connection, work_item_id, from_time, to_time)
        quality["formalCoverageMetric"] = "participation.coverage"
        quality["formalCoverageThreshold"] = 0.9
        token_totals = {name: sum(
            group["attributionTokenTotals"][name] for group in serialized)
                        for name in ATTRIBUTION}
        token_denominator = sum(token_totals.values())
        quality["tokenAttribution"] = {
            "totals": token_totals,
            "ratios": {name: (float(token_totals[name]) / token_denominator
                              if token_denominator else None) for name in ATTRIBUTION}}
        strata = {}
        for item in item_distribution.values():
            if (not item["finalAccepted"] or not item["allComplete"] or
                    len(item["rateCardVersions"]) != 1 or
                    len(item["semanticVersions"]) != 1):
                continue
            stratum_key = (next(iter(item["rateCardVersions"])),
                           next(iter(item["semanticVersions"])))
            strata.setdefault(stratum_key, []).append(item["credits"])
        serialized_strata = []
        for stratum_key in sorted(strata, key=lambda value: repr(value)):
            values = strata[stratum_key]
            serialized_strata.append({
                "rateCardVersion": stratum_key[0],
                "semanticVersion": list(stratum_key[1]), "n": len(values),
                "p50EstimatedCredits": _nearest(values, .5),
                "p90EstimatedCredits": _nearest(values, .9)})
        distribution = next(iter(strata.values())) if len(strata) == 1 else []
        p50, p90 = _nearest(distribution, .5), _nearest(distribution, .9)
        for group in serialized:
            group["sampleN"] = len(distribution)
            group["p50EstimatedCredits"] = p50
            group["p90EstimatedCredits"] = p90
        return {"schemaVersion": USAGE_REPORT_VERSION, "groupBy": group_by, "from": from_time,
                "to": to_time, "workItemId": work_item_id,
                "simulation": simulate_rate_cards is not None, "groups": serialized,
                "distribution": {"n": len(distribution), "p50EstimatedCredits": p50,
                                 "p90EstimatedCredits": p90,
                                 "strata": serialized_strata},
                "quality": quality,
                "quotaWindows": [json.loads(row[0]) for row in connection.execute(
                    "SELECT payload_json FROM usage_events WHERE event_type='QUOTA_SNAPSHOT_RECORDED' "
                    "ORDER BY usage_event_id")]}
    finally:
        connection.close()


def render_table(report):
    columns = ("group", "eventCount", "totalTokens", "cachedInputTokens",
               "estimatedCredits", "creditStatus", "cacheHitRatio", "attributionCoverage",
               "attributedTokens", "sharedTokens", "unattributedTokens",
               "attributedTokenRatio", "sharedTokenRatio", "unattributedTokenRatio",
               "sampleN", "p50EstimatedCredits", "p90EstimatedCredits")
    rows = [columns]
    for group in report["groups"]:
        flattened = dict(group)
        for status, label in (("ATTRIBUTED", "attributed"), ("SHARED", "shared"),
                              ("UNATTRIBUTED", "unattributed")):
            flattened[label + "Tokens"] = group["attributionTokenTotals"][status]
            flattened[label + "TokenRatio"] = group["attributionTokenRatios"][status]
        rows.append(tuple("" if flattened.get(column) is None else str(flattened.get(column))
                          for column in columns))
    widths = [max(len(str(row[index])) for row in rows) for index in range(len(columns))]
    body = "\n".join("  ".join(str(value).ljust(widths[index])
                                for index, value in enumerate(row)) for row in rows)
    distribution = report.get("distribution", {})
    token = report["quality"]["tokenAttribution"]
    participation = report["quality"]["participation"]
    return body + "\n" + "n={0} p50={1} p90={2}".format(
        distribution.get("n", 0), distribution.get("p50EstimatedCredits"),
        distribution.get("p90EstimatedCredits")) + "\n" + (
        "participationCoverage={0} formalThreshold=0.9 "
        "tokenTotals={1} tokenRatios={2}").format(
            participation.get("coverage"), token["totals"], token["ratios"])


def render_sync_table(summary):
    keys = ("dryRun", "streams", "accepted", "rejected", "duplicate", "unchanged",
            "boundary_skipped",
            "reset", "wouldWriteEvents", "quotaAccepted", "privacy")
    width = max(len(key) for key in keys)
    return "\n".join("{0}  {1}".format(key.ljust(width), summary.get(key)) for key in keys)


def export_report(database, destination, file_format="json", group_by="role",
                  from_time=None, to_time=None, work_item_id=None):
    report = project(database, group_by, from_time, to_time, work_item_id)
    nonce = secrets.token_bytes(32)
    from .lite import open_database
    connection = open_database(database)
    try:
        require_schema(connection)
        refs = sorted(set(hashlib.sha256(nonce + row[0].encode("utf-8")).hexdigest()
                          for row in connection.execute(
                              "SELECT source_session_id FROM usage_events "
                              "WHERE source_session_id IS NOT NULL")))
    finally:
        connection.close()
    if file_format == "json":
        envelope = {"schemaVersion": USAGE_EXPORT_VERSION, "report": report,
                    "sessionRefs": refs, "privacy": "EPHEMERAL_NONCE_SHA256_NO_PATHS"}
        with open(destination, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    elif file_format == "csv":
        fields = ("group", "eventCount", "inputTokens", "cachedInputTokens",
                  "cacheWriteInputTokens", "outputTokens", "reasoningTokens", "totalTokens",
                  "estimatedCredits", "creditStatus", "cacheHitRatio", "attributionCoverage",
                  "attributedTokens", "sharedTokens", "unattributedTokens",
                  "attributedTokenRatio", "sharedTokenRatio", "unattributedTokenRatio",
                  "participationCoverage", "formalCoverageThreshold",
                  "sampleN", "p50EstimatedCredits", "p90EstimatedCredits")
        with open(destination, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for group in report["groups"]:
                row = dict(group)
                for status, label in (("ATTRIBUTED", "attributed"), ("SHARED", "shared"),
                                      ("UNATTRIBUTED", "unattributed")):
                    row[label + "Tokens"] = group["attributionTokenTotals"][status]
                    row[label + "TokenRatio"] = group["attributionTokenRatios"][status]
                row["participationCoverage"] = report["quality"]["participation"]["coverage"]
                row["formalCoverageThreshold"] = report["quality"]["formalCoverageThreshold"]
                writer.writerow(row)
    else:
        raise UsageError("export format must be json or csv")
    return {"status": "ok", "schemaVersion": USAGE_EXPORT_VERSION,
            "format": file_format, "groups": len(report["groups"])}


def self_check(database):
    from .lite import open_database
    connection = open_database(database)
    try:
        require_schema(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        triggers = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='trigger' "
            "AND name IN ('usage_events_no_update','usage_events_no_delete')").fetchone()[0]
        return {"schemaVersion": USAGE_SCHEMA_VERSION,
                "status": "PASS" if integrity == "ok" and not foreign and triggers == 2 else "FAIL",
                "integrity": integrity, "foreignKeyErrors": len(foreign),
                "immutableTriggers": triggers,
                "rejectedSnapshots": connection.execute(
                    "SELECT count(*) FROM usage_events WHERE event_type='USAGE_SYNC_REJECTED'").fetchone()[0],
                "formalCohortsStarted": connection.execute(
                    "SELECT count(*) FROM usage_events WHERE event_type='COHORT_STARTED'").fetchone()[0],
                "privacy": "ALLOWLIST_ONLY"}
    finally:
        connection.close()
