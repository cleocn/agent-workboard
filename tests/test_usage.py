import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from agent_workboard.cli import main as cli_main
from agent_workboard.lite import (LiteError, acquire_claim, create_work_item,
                                  initialize_database, open_database, release_claim)
from agent_workboard.usage import (UsageError, append_event, begin_span, correct,
                                   cohort_event, end_span, estimate_credits, export_report, ingest_snapshot,
                                   project, record_binding, record_quota, self_check,
                                   sync, validate_counters)
from agent_workboard.usage_adapters.codex_local import AdapterError, CodexLocalAdapter


class UsageObservationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, "workboard.db")
        initialize_database(self.database)
        self.sessions = os.path.join(self.temporary.name, "sessions")
        os.makedirs(os.path.join(self.sessions, "2026", "08", "22"))
        self.session_id = "11111111-2222-4333-8444-555555555555"
        self.session_path = os.path.join(
            self.sessions, "2026", "08", "22", "rollout-" + self.session_id + ".jsonl")
        self.sentinel = "FORBIDDEN_HIGH_ENTROPY_7db9a2c0e8f64a33"

    def tearDown(self):
        self.temporary.cleanup()

    def management(self, work_item_id):
        tasks = []
        for seq, role in enumerate(("PLANNER", "IMPLEMENTER", "REVIEWER"), 1):
            tasks.append({"taskId": "%s-T%02d" % (work_item_id, seq), "seq": seq,
                          "title": role, "ownerRole": role, "required": True,
                          "acceptance": ["accepted"], "closureEvidenceRequired": ["evidence"]})
        return {"contractVersion": "AWB-WORKITEM-MGMT-v1",
                "templateContractVersion": "AWB-MANAGEMENT-v1", "scope": ["usage"],
                "outOfScope": ["content"],
                "authorization": {"allowed": ["local"], "forbidden": ["remote"]},
                "safetyConstraints": ["privacy"], "tasks": tasks,
                "acceptance": [{"id": "AC-001", "criterion": "usage"}],
                "closure": [{"id": "CL-001", "criterion": "evidence"}]}

    def create(self, work_item_id="AWB-101"):
        return create_work_item(self.database, work_item_id, "AWB", "usage",
                                management=self.management(work_item_id))

    def expires(self):
        return (datetime.datetime.now(datetime.timezone.utc) +
                datetime.timedelta(hours=1)).replace(microsecond=0).isoformat()

    def line(self, value):
        with open(self.session_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True) + "\n")

    def fixture(self, counters=None, role="planner", quota=True):
        self.line({"timestamp": "2026-08-22T00:00:00+00:00", "type": "session_meta",
                   "payload": {"id": self.session_id, "session_id": "parent",
                               "cli_version": "0.148.0", "base_instructions": self.sentinel,
                               "cwd": "/private/secret/path",
                               "source": {"subagent": {"agent_role": role,
                                                        "prompt": self.sentinel}}}})
        self.line({"timestamp": "2026-08-22T00:00:01+00:00", "type": "response_item",
                   "payload": {"content": self.sentinel, "tool_output": self.sentinel}})
        if counters is not None:
            info = {"total_token_usage": counters, "summary": self.sentinel}
            if quota:
                info["rate_limits"] = {"primary": {"used_percent": 12.5,
                                                       "window_minutes": 300,
                                                       "resets_at": 1800000000}}
            self.line({"timestamp": "2026-08-22T00:00:02+00:00", "type": "event_msg",
                       "payload": {"type": "token_count", "info": info,
                                   "message": self.sentinel}})

    def source_fixture(self, source=None, include_source=True):
        payload = {"id": self.session_id, "session_id": "parent",
                   "cli_version": "0.148.0", "base_instructions": self.sentinel,
                   "cwd": "/private/secret/path"}
        if include_source:
            payload["source"] = source
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "timestamp": "2026-08-22T00:00:00+00:00", "type": "session_meta",
                "payload": payload}, sort_keys=True) + "\n")
        self.line({"timestamp": "2026-08-22T00:00:02+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(),
                       "last_token_usage": self.counters(1, 0, 0, 1, 0),
                       "summary": self.sentinel}, "message": self.sentinel}})

    def session_meta(self, identity, parent=None, include_parent=False,
                     source=None, include_source=False):
        payload = {"id": identity, "session_id": "opaque-session",
                   "cli_version": "0.148.0", "base_instructions": self.sentinel,
                   "cwd": "/private/secret/path"}
        if include_parent:
            payload["parent_thread_id"] = parent
        if include_source:
            payload["source"] = source
        return {"timestamp": "2026-08-22T00:00:00+00:00", "type": "session_meta",
                "payload": payload}

    def token_record(self, counters=None):
        return {"timestamp": "2026-08-22T00:00:02+00:00", "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": counters or self.counters(),
                    "last_token_usage": self.counters(1, 0, 0, 1, 0),
                    "summary": self.sentinel}, "message": self.sentinel}}

    def records_fixture(self, records):
        with open(self.session_path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def counters(self, input_tokens=100, cached=40, cache_write=10,
                 output=20, reasoning=5, extensions=None):
        return {"input_tokens": input_tokens, "cached_input_tokens": cached,
                "cache_write_input_tokens": cache_write, "output_tokens": output,
                "reasoning_output_tokens": reasoning, "total_tokens": input_tokens + output,
                **(extensions or {})}

    def snapshot(self, key, counters, session_id="stream", ordinal=1):
        return {"provider": "codex-local", "source_session_id": session_id,
                "source_snapshot_key": key, "observed_at": "2026-08-22T01:00:00+00:00",
                "accuracy": "OBSERVED", "adapter_version": "fixture-v1",
                "parser_version": "fixture-v1", "source_ordinal": ordinal,
                "counters": counters}

    def test_rate_card_known_fixture_is_exact_and_does_not_double_count_reasoning(self):
        counters = {"input_tokens": 1000000, "cached_input_tokens": 600000,
                    "cache_write_input_tokens": 100000, "output_tokens": 20000,
                    "reasoning_tokens": 5000, "total_tokens": 1020000,
                    "numeric_extensions": {}}
        result = estimate_credits(counters, "gpt-5.6-terra", "2026-08-22T01:00:00+00:00")
        self.assertEqual("COMPLETE", result["status"])
        self.assertEqual(30.25, result["estimatedCredits"])
        self.assertEqual("awb-estimated-credit-2026-08-22-v1", result["rateCardVersion"])

    def test_unknown_model_and_numeric_extension_are_unknown_or_partial(self):
        base = {"input_tokens": 10, "cached_input_tokens": 2, "cache_write_input_tokens": 1,
                "output_tokens": 2, "reasoning_tokens": 1, "total_tokens": 12,
                "numeric_extensions": {}}
        self.assertEqual("UNKNOWN", estimate_credits(
            base, "unknown", "2026-08-22T01:00:00+00:00")["status"])
        base["numeric_extensions"] = {"future_tokens": 3}
        self.assertEqual("PARTIAL", estimate_credits(
            base, "gpt-5.6-terra", "2026-08-22T01:00:00+00:00")["status"])

    def test_counter_invariants_fail_closed(self):
        with self.assertRaises(UsageError):
            validate_counters({"input_tokens": 1, "cached_input_tokens": 2,
                               "output_tokens": 0, "reasoning_tokens": 0,
                               "total_tokens": 1})

    def test_selective_adapter_never_returns_forbidden_content_or_paths(self):
        self.fixture(self.counters())
        materialized = []
        parsed = CodexLocalAdapter(
            self.sessions, materialization_audit=materialized.append).read(self.session_id)
        encoded = json.dumps(parsed, sort_keys=True)
        self.assertNotIn(self.sentinel, encoded)
        self.assertNotIn(self.sentinel, materialized)
        self.assertNotIn("/private/secret/path", encoded)
        self.assertEqual(self.session_id, parsed["identity"]["id"])
        self.assertEqual(1, len(parsed["snapshots"]))
        self.assertEqual(1, len(parsed["quota"]))

    def test_session_source_union_is_private_and_snapshot_stable(self):
        cases = (
            ("object", {"subagent": {"agent_role": "implementer",
                                       "prompt": self.sentinel}}, True, "implementer"),
            ("string", self.sentinel, True, None),
            ("missing", None, False, None),
        )
        snapshot_keys = []
        for label, source, include_source, expected_role in cases:
            with self.subTest(source=label):
                self.source_fixture(source, include_source)
                materialized = []
                parsed = CodexLocalAdapter(
                    self.sessions, materialization_audit=materialized.append).read(
                        self.session_id)
                encoded = json.dumps(parsed, sort_keys=True)
                self.assertEqual(expected_role, parsed["identity"]["agent_role"])
                self.assertEqual("codex-local-v1", parsed["snapshots"][0]["adapter_version"])
                self.assertEqual("codex-local-selective-v2",
                                 parsed["snapshots"][0]["parser_version"])
                self.assertNotIn(self.sentinel, materialized)
                self.assertNotIn(self.sentinel, encoded)
                self.assertNotIn("/private/secret/path", encoded)
                snapshot_keys.append(parsed["snapshots"][0]["source_snapshot_key"])
        self.assertEqual(1, len(set(snapshot_keys)))

        ancestor_one = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        ancestor_two = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        self.records_fixture([
            self.session_meta(self.session_id, ancestor_one, True,
                              {"subagent": {"agent_role": "implementer",
                                             "prompt": self.sentinel}}, True),
            self.session_meta(ancestor_one, ancestor_two, True, self.sentinel, True),
            self.session_meta(ancestor_two, source={"subagent": {
                "agent_role": "reviewer", "prompt": self.sentinel}}, include_source=True),
            self.token_record(),
        ])
        materialized = []
        adapter = CodexLocalAdapter(self.sessions, materialization_audit=materialized.append)
        parsed = adapter.read(self.session_id)
        self.assertEqual(self.session_id, parsed["identity"]["id"])
        self.assertEqual("implementer", parsed["identity"]["agent_role"])
        self.assertEqual("codex-local-selective-v3", adapter.parser_version)
        self.assertEqual("codex-local-selective-v3",
                         parsed["snapshots"][0]["parser_version"])
        self.assertNotIn("reviewer", parsed["identity"].values())
        self.assertNotIn(self.sentinel, materialized)
        self.assertNotIn(self.sentinel, json.dumps(parsed, sort_keys=True))

        self.source_fixture(self.sentinel)
        parsed = adapter.read(self.session_id)
        self.assertEqual("codex-local-selective-v2", adapter.parser_version)
        self.assertEqual("codex-local-selective-v2",
                         parsed["snapshots"][0]["parser_version"])

    def test_invalid_session_source_shapes_fail_closed_without_materialization(self):
        cases = (("null", None), ("array", [self.sentinel]),
                 ("number", 7), ("boolean", True))
        for label, source in cases:
            with self.subTest(source=label):
                self.source_fixture(source)
                materialized = []
                with self.assertRaises(AdapterError) as raised:
                    CodexLocalAdapter(
                        self.sessions, materialization_audit=materialized.append).read(
                            self.session_id)
                self.assertEqual("SCHEMA_DRIFT", raised.exception.reason_code)
                self.assertNotIn(self.sentinel, materialized)
                self.assertNotIn(self.sentinel, str(raised.exception))

                ancestor = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                self.records_fixture([
                    self.session_meta(self.session_id, ancestor, True,
                                      {"subagent": {"agent_role": "implementer"}}, True),
                    self.session_meta(ancestor, source=source, include_source=True),
                    self.token_record(),
                ])
                materialized = []
                with self.assertRaises(AdapterError) as ancestor_error:
                    CodexLocalAdapter(
                        self.sessions, materialization_audit=materialized.append).read(
                            self.session_id)
                self.assertEqual("SCHEMA_DRIFT", ancestor_error.exception.reason_code)
                self.assertNotIn(self.sentinel, materialized)
                self.assertNotIn(self.sentinel, str(ancestor_error.exception))

    def test_duplicate_escaped_and_malformed_session_source_fail_closed(self):
        meta = ('{"timestamp":"2026-08-22T00:00:00+00:00",'
                '"type":"session_meta","payload":{"id":"%s",'
                '"source":"opaque","source":"opaque-two"}}\n' % self.session_id)
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write(meta)
        with self.assertRaises(AdapterError) as duplicate:
            CodexLocalAdapter(self.sessions).read(self.session_id)
        self.assertEqual("SCHEMA_DRIFT", duplicate.exception.reason_code)

        escaped = meta.replace('"source":"opaque","source":"opaque-two"',
                               '"so\\u0075rce":"opaque"')
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write(escaped)
        with self.assertRaises(AdapterError) as escaped_error:
            CodexLocalAdapter(self.sessions).read(self.session_id)
        self.assertEqual("SCHEMA_DRIFT", escaped_error.exception.reason_code)

        malformed = meta.replace('"source":"opaque","source":"opaque-two"',
                                 '"source":[}')
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write(malformed)
        with self.assertRaises(AdapterError) as malformed_error:
            CodexLocalAdapter(self.sessions).read(self.session_id)
        self.assertEqual("MALFORMED_JSON", malformed_error.exception.reason_code)

        ancestor = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        other = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        lineage_cases = (
            ("broken", [self.session_meta(self.session_id, ancestor, True),
                        self.session_meta(other), self.token_record()], "SCHEMA_DRIFT"),
            ("cycle", [self.session_meta(self.session_id, ancestor, True),
                       self.session_meta(ancestor, self.session_id, True),
                       self.session_meta(self.session_id), self.token_record()], "SCHEMA_DRIFT"),
            ("duplicate", [self.session_meta(self.session_id, ancestor, True),
                           self.session_meta(ancestor, ancestor, True),
                           self.session_meta(ancestor), self.token_record()], "SCHEMA_DRIFT"),
            ("requested-not-head", [self.session_meta(ancestor, self.session_id, True),
                                    self.session_meta(self.session_id), self.token_record()],
             "SESSION_ID_MISMATCH"),
            ("metadata-after-token", [self.session_meta(self.session_id, ancestor, True),
                                      self.token_record(), self.session_meta(ancestor)],
             "SCHEMA_DRIFT"),
            ("token-before-lineage", [self.token_record(), self.session_meta(self.session_id)],
             "SCHEMA_DRIFT"),
            ("multiple-requested", [self.session_meta(
                self.session_id, self.session_id, True),
                self.session_meta(self.session_id), self.token_record()], "SCHEMA_DRIFT"),
            ("missing-parent", [self.session_meta(self.session_id),
                                self.session_meta(ancestor), self.token_record()], "SCHEMA_DRIFT"),
            ("null-parent", [self.session_meta(self.session_id, None, True),
                             self.session_meta(ancestor), self.token_record()], "MALFORMED_JSON"),
        )
        for label, records, reason_code in lineage_cases:
            with self.subTest(lineage=label):
                self.records_fixture(records)
                materialized = []
                with self.assertRaises(AdapterError) as lineage_error:
                    CodexLocalAdapter(
                        self.sessions, materialization_audit=materialized.append).read(
                            self.session_id)
                self.assertEqual(reason_code, lineage_error.exception.reason_code)
                self.assertNotIn(self.sentinel, materialized)
                self.assertNotIn(self.sentinel, str(lineage_error.exception))

    def test_root_string_source_claim_binding_is_atomic_and_private(self):
        self.source_fixture(self.sentinel)
        self.create()
        result = acquire_claim(
            self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
            self.expires(), session_id=self.session_id, usage_provider="codex-local",
            model="gpt-5.6-terra", sessions_root=self.sessions)
        self.assertTrue(result["claimId"].startswith("claim-"))
        connection = open_database(self.database)
        try:
            stored = "\n".join(row[0] for row in connection.execute(
                "SELECT payload_json FROM usage_events").fetchall())
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM usage_events "
                "WHERE event_type='USAGE_BINDING_RECORDED'").fetchone()[0])
        finally:
            connection.close()
        self.assertNotIn(self.sentinel, stored)
        self.assertNotIn(self.sessions, stored)

        ancestor = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.records_fixture([
            self.session_meta(self.session_id, ancestor, True,
                              {"subagent": {"agent_role": "planner",
                                             "prompt": self.sentinel}}, True),
            self.session_meta(ancestor, source=self.sentinel, include_source=True),
            self.token_record(),
        ])
        multi_database = os.path.join(self.temporary.name, "multi-workboard.db")
        initialize_database(multi_database)
        create_work_item(multi_database, "AWB-102", "AWB", "usage",
                         management=self.management("AWB-102"))
        acquire_claim(
            multi_database, "AWB-102", "AWB-102-T01", "planner-v3", "PLANNER",
            self.expires(), session_id=self.session_id, usage_provider="codex-local",
            model="gpt-5.6-terra", sessions_root=self.sessions)
        connection = open_database(multi_database)
        try:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM usage_events "
                "WHERE event_type='USAGE_BINDING_RECORDED' AND work_item_id='AWB-102'"
            ).fetchone()[0])
            stored = "\n".join(row[0] for row in connection.execute(
                "SELECT payload_json FROM usage_events").fetchall())
        finally:
            connection.close()
        self.assertEqual("codex-local-selective-v3", payload["parserVersion"])
        self.assertEqual("OBSERVED", payload["baselineStatus"])
        self.assertNotIn(self.sentinel, stored)
        self.assertNotIn(self.sessions, stored)

    def test_invalid_source_rejects_claim_before_any_workflow_write(self):
        self.source_fixture(None)
        self.create()
        with self.assertRaises(LiteError) as raised:
            acquire_claim(
                self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                self.expires(), session_id=self.session_id, usage_provider="codex-local",
                model="gpt-5.6-terra", sessions_root=self.sessions)
        self.assertNotIn(self.sentinel, str(raised.exception))
        connection = open_database(self.database)
        try:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM claims").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM usage_events").fetchone()[0])
        finally:
            connection.close()

        ancestor = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        other = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
        self.records_fixture([
            self.session_meta(self.session_id, ancestor, True,
                              {"subagent": {"agent_role": "planner"}}, True),
            self.session_meta(other, source=self.sentinel, include_source=True),
            self.token_record(),
        ])
        with self.assertRaises(LiteError) as lineage_error:
            acquire_claim(
                self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                self.expires(), session_id=self.session_id, usage_provider="codex-local",
                model="gpt-5.6-terra", sessions_root=self.sessions)
        self.assertNotIn(self.sentinel, str(lineage_error.exception))
        connection = open_database(self.database)
        try:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM claims").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM usage_events").fetchone()[0])
        finally:
            connection.close()

    def test_non_usage_payload_and_invalid_counter_string_are_never_materialized(self):
        self.fixture(self.counters(), quota=False)
        materialized = []
        parsed = CodexLocalAdapter(
            self.sessions, materialization_audit=materialized.append).read(self.session_id)
        self.assertEqual(1, len(parsed["snapshots"]))
        self.assertNotIn(self.sentinel, materialized)
        self.line({"timestamp": "2026-08-22T00:02:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": {"input_tokens": self.sentinel}}}})
        with self.assertRaises(AdapterError) as raised:
            CodexLocalAdapter(
                self.sessions, materialization_audit=materialized.append).read(self.session_id)
        self.assertEqual("SCHEMA_DRIFT", raised.exception.reason_code)
        self.assertNotIn(self.sentinel, materialized)
        self.assertNotIn(self.sentinel, str(raised.exception))

    def test_adapter_rejects_corrupt_unknown_and_symbolic_sources(self):
        with open(self.session_path, "w", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        with self.assertRaises(AdapterError):
            CodexLocalAdapter(self.sessions).read(self.session_id)
        os.unlink(self.session_path)
        external = os.path.join(self.temporary.name, "external.jsonl")
        with open(external, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        os.symlink(external, self.session_path)
        with self.assertRaises(AdapterError):
            CodexLocalAdapter(self.sessions).read(self.session_id)

    def test_claim_binding_is_atomic_and_half_group_is_rejected(self):
        self.fixture(self.counters(), role="implementer")
        self.create()
        with self.assertRaises(LiteError):
            acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                          self.expires(), session_id=self.session_id)
        result = acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                               self.expires(), session_id=self.session_id,
                               usage_provider="codex-local", model="gpt-5.6-terra",
                               sessions_root=self.sessions)
        connection = open_database(self.database)
        try:
            row = connection.execute(
                "SELECT payload_json FROM usage_events WHERE event_type='USAGE_BINDING_RECORDED'").fetchone()
            payload = json.loads(row[0])
            self.assertEqual("OBSERVED", payload["baselineStatus"])
            self.assertEqual(["ROLE_MISMATCH"], payload["qualityFlags"])
            self.assertEqual(result["claimId"], connection.execute(
                "SELECT claim_id FROM usage_events WHERE event_type='USAGE_BINDING_RECORDED'").fetchone()[0])
        finally:
            connection.close()

    def test_sync_rejection_is_content_free_and_idempotent(self):
        self.fixture(self.counters())
        self.create()
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:02:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": {"input_tokens": self.sentinel}}}})
        first = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        second = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(1, first["rejected"])
        self.assertEqual(0, second["wouldWriteEvents"])
        connection = open_database(self.database)
        rows = connection.execute(
            "SELECT payload_json FROM usage_events WHERE event_type='USAGE_SYNC_REJECTED'").fetchall()
        connection.close()
        self.assertEqual(1, len(rows))
        self.assertNotIn(self.sentinel, rows[0][0])
        self.assertNotIn(self.sessions, rows[0][0])

    def test_old_database_without_usage_extension_keeps_workflow_api(self):
        self.create()
        connection = open_database(self.database)
        connection.executescript(
            "DROP TRIGGER usage_events_no_update; DROP TRIGGER usage_events_no_delete; "
            "DROP TABLE usage_events; DELETE FROM schema_meta WHERE key='usage_schema_version';")
        connection.commit()
        connection.close()
        result = acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                               self.expires())
        self.assertTrue(result["claimId"].startswith("claim-"))

    def test_monotonic_duplicate_reset_and_immutable_events(self):
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            first = self.snapshot("one", {"input_tokens": 10, "cached_input_tokens": 2,
                                          "cache_write_input_tokens": 1, "output_tokens": 3,
                                          "reasoning_tokens": 1, "total_tokens": 13,
                                          "numeric_extensions": {}})
            self.assertEqual("ACCEPTED", ingest_snapshot(connection, first)["status"])
            self.assertEqual("DUPLICATE", ingest_snapshot(connection, first)["status"])
            changed = dict(first)
            changed["counters"] = dict(first["counters"], input_tokens=11, total_tokens=14)
            with self.assertRaises(UsageError):
                ingest_snapshot(connection, changed)
            second = self.snapshot("two", {"input_tokens": 2, "cached_input_tokens": 1,
                                            "cache_write_input_tokens": 0, "output_tokens": 1,
                                            "reasoning_tokens": 0, "total_tokens": 3,
                                            "numeric_extensions": {}}, ordinal=2)
            self.assertTrue(ingest_snapshot(connection, second)["reset"])
            connection.commit()
            self.assertEqual(1, connection.execute(
                "SELECT count(*) FROM usage_events WHERE event_type='COUNTER_SEGMENT_STARTED'").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE usage_events SET actor_id='x'")
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM usage_events")
        finally:
            connection.close()

    def test_binding_sync_dry_run_is_zero_write_then_records_exact_delta(self):
        self.fixture(self.counters())
        self.create()
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(150, 60, 10, 30, 6),
                       "rate_limits": {"secondary": {"used_percent": 20,
                                                       "window_minutes": 10080,
                                                       "resets_at": 1800000100}}}}})
        connection = open_database(self.database)
        before = connection.execute("SELECT count(*) FROM usage_events").fetchone()[0]
        connection.close()
        dry = sync(self.database, work_item_id="AWB-101", dry_run=True, sessions_root=self.sessions)
        connection = open_database(self.database)
        self.assertEqual(before, connection.execute("SELECT count(*) FROM usage_events").fetchone()[0])
        connection.close()
        self.assertGreater(dry["wouldWriteEvents"], 0)
        actual = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(1, actual["accepted"])
        report = project(self.database, "role", work_item_id="AWB-101")
        self.assertEqual(60, report["groups"][0]["totalTokens"])
        self.assertEqual(1, report["quality"]["attributedEvents"])
        self.assertEqual(2, len(report["quotaWindows"]))

    def test_claim_boundary_skips_all_pre_binding_snapshots(self):
        ancestor = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
        self.records_fixture([
            self.session_meta(self.session_id, ancestor, True,
                              {"subagent": {"agent_role": "planner"}}, True),
            self.session_meta(ancestor, source=self.sentinel, include_source=True),
            self.token_record(self.counters(10, 2, 0, 2, 1)),
            self.token_record(self.counters(50, 10, 0, 10, 2)),
        ])
        self.create()
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        zero = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(2, zero["boundary_skipped"])
        self.assertEqual(0, zero["accepted"])
        self.assertEqual(0, zero["reset"])
        self.assertEqual([], project(self.database, "role", work_item_id="AWB-101")["groups"])
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(55, 11, 0, 11, 2)}}})
        added = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        repeated = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(1, added["accepted"])
        self.assertEqual(0, added["reset"])
        self.assertEqual(6, project(
            self.database, "role", work_item_id="AWB-101")["groups"][0]["totalTokens"])
        self.assertEqual(1, repeated["duplicate"])
        self.assertEqual(0, repeated["accepted"])
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM usage_events WHERE event_type='COUNTER_SEGMENT_STARTED'").fetchone()[0])
        connection.close()

    def test_orchestrator_span_boundary_skips_pre_span_snapshots(self):
        self.fixture(self.counters(10, 2, 0, 2, 1), quota=False)
        self.line({"timestamp": "2026-08-22T00:00:03+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(50, 10, 0, 10, 2)}}})
        self.create()
        begin_span(self.database, "AWB-101", "AWB-101-T01", "orchestrator",
                   self.session_id, "gpt-5.6-terra", sessions_root=self.sessions)
        zero = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(2, zero["boundary_skipped"])
        self.assertEqual(0, zero["accepted"])
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(60, 12, 0, 12, 3)}}})
        added = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        repeated = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        self.assertEqual(1, added["accepted"])
        self.assertEqual(12, project(
            self.database, "role", work_item_id="AWB-101")["groups"][0]["totalTokens"])
        self.assertEqual(1, repeated["duplicate"])
        connection = open_database(self.database)
        self.assertEqual(0, connection.execute(
            "SELECT count(*) FROM usage_events WHERE event_type='COUNTER_SEGMENT_STARTED'").fetchone()[0])
        connection.close()

    def test_overlapping_span_boundary_handoff_preserves_pre_overlap_delta(self):
        self.fixture(self.counters(100, 0, 0, 0, 0), quota=False)
        self.create()
        begin_span(self.database, "AWB-101", "AWB-101-T01", "orchestrator-1",
                   self.session_id, "gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(110, 0, 0, 0, 0)}}})
        begin_span(self.database, "AWB-101", "AWB-101-T01", "orchestrator-2",
                   self.session_id, "gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:02:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(120, 0, 0, 0, 0)}}})
        overlap = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        repeated = sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        connection = open_database(self.database)
        rows = connection.execute(
            "SELECT payload_json FROM usage_events WHERE event_type='AGENT_USAGE_RECORDED' "
            "ORDER BY usage_event_id").fetchall()
        resets = connection.execute(
            "SELECT count(*) FROM usage_events WHERE event_type='COUNTER_SEGMENT_STARTED'").fetchone()[0]
        connection.close()
        payloads = [json.loads(row[0]) for row in rows]
        self.assertEqual([10, 10], [payload["delta"]["total_tokens"] for payload in payloads])
        self.assertEqual(["ATTRIBUTED", "SHARED"], [
            payload["attribution"]["status"] for payload in payloads])
        self.assertEqual(1, overlap["accepted"])
        self.assertEqual(0, repeated["accepted"])
        self.assertEqual(2, repeated["duplicate"])
        self.assertEqual(0, resets)

    def test_sequential_bindings_keep_each_interval_delta(self):
        self.fixture(self.counters(100, 0, 0, 0, 0), quota=False)
        for item in ("AWB-101", "AWB-102"):
            self.create(item)
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner-1", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(110, 0, 0, 0, 0)}}})
        sync(self.database, work_item_id="AWB-101", sessions_root=self.sessions)
        release_claim(self.database, "AWB-101", "planner-1")
        acquire_claim(self.database, "AWB-102", "AWB-102-T01", "planner-2", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:02:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(120, 0, 0, 0, 0)}}})
        sync(self.database, work_item_id="AWB-102", sessions_root=self.sessions)
        repeated = sync(self.database, work_item_id="AWB-102", sessions_root=self.sessions)
        self.assertEqual(10, project(
            self.database, "role", work_item_id="AWB-101")["groups"][0]["totalTokens"])
        self.assertEqual(10, project(
            self.database, "role", work_item_id="AWB-102")["groups"][0]["totalTokens"])
        self.assertEqual(0, repeated["accepted"])

    def test_overlapping_binding_handoff_preserves_old_then_marks_shared(self):
        self.fixture(self.counters(100, 0, 0, 0, 0), quota=False)
        for item in ("AWB-101", "AWB-102"):
            self.create(item)
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner-1", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:01:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(110, 0, 0, 0, 0)}}})
        acquire_claim(self.database, "AWB-102", "AWB-102-T01", "planner-2", "PLANNER",
                      self.expires(), session_id=self.session_id, usage_provider="codex-local",
                      model="gpt-5.6-terra", sessions_root=self.sessions)
        self.line({"timestamp": "2026-08-22T00:02:00+00:00", "type": "event_msg",
                   "payload": {"type": "token_count", "info": {
                       "total_token_usage": self.counters(120, 0, 0, 0, 0)}}})
        sync(self.database, all_bound=True, sessions_root=self.sessions)
        repeated = sync(self.database, all_bound=True, sessions_root=self.sessions)
        connection = open_database(self.database)
        rows = connection.execute(
            "SELECT payload_json FROM usage_events WHERE event_type='AGENT_USAGE_RECORDED' "
            "ORDER BY usage_event_id").fetchall()
        resets = connection.execute(
            "SELECT count(*) FROM usage_events WHERE event_type='COUNTER_SEGMENT_STARTED'").fetchone()[0]
        connection.close()
        payloads = [json.loads(row[0]) for row in rows]
        self.assertEqual([10, 10], [payload["delta"]["total_tokens"] for payload in payloads])
        self.assertEqual(["ATTRIBUTED", "SHARED"], [
            payload["attribution"]["status"] for payload in payloads])
        self.assertEqual(0, repeated["accepted"])
        self.assertEqual(0, resets)

    def test_overlapping_bindings_are_shared_without_guessing(self):
        for item in ("AWB-101", "AWB-102"):
            self.create(item)
            acquire_claim(self.database, item, item + "-T01", item + "-planner", "PLANNER", self.expires())
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for item in ("AWB-101", "AWB-102"):
                claim = connection.execute("SELECT * FROM claims WHERE work_item_id=?", (item,)).fetchone()
                record_binding(connection, claim, "codex-local", "shared-stream", "gpt-5.6-terra")
            result = ingest_snapshot(connection, self.snapshot(
                "shared", {"input_tokens": 5, "cached_input_tokens": 1,
                           "cache_write_input_tokens": 0, "output_tokens": 2,
                           "reasoning_tokens": 1, "total_tokens": 7,
                           "numeric_extensions": {}}, "shared-stream"))
            connection.commit()
            self.assertEqual("SHARED", result["attribution"])
        finally:
            connection.close()

    def test_correction_projection_quota_export_and_self_check(self):
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            ingest_snapshot(connection, self.snapshot(
                "raw", {"input_tokens": 10, "cached_input_tokens": 2,
                        "cache_write_input_tokens": 1, "output_tokens": 2,
                        "reasoning_tokens": 1, "total_tokens": 12,
                        "numeric_extensions": {}}, self.session_id))
            record_quota(connection, {"limit_name": "primary", "used_percent": 5,
                                      "window_minutes": 300, "resets_at": 1800000000,
                                      "observed_at": "2026-08-22T01:00:00+00:00"})
            connection.commit()
            event_id = connection.execute(
                "SELECT usage_event_id FROM usage_events WHERE event_type='AGENT_USAGE_RECORDED'").fetchone()[0]
        finally:
            connection.close()
        replacement = {"input_tokens": 20, "cached_input_tokens": 4,
                       "cache_write_input_tokens": 2, "output_tokens": 4,
                       "reasoning_tokens": 2, "total_tokens": 24,
                       "numeric_extensions": {}}
        correct(self.database, event_id, "human", "fixture correction", replacement)
        self.assertEqual(24, project(self.database, "role")["groups"][0]["totalTokens"])
        destination = os.path.join(self.temporary.name, "usage.json")
        export_report(self.database, destination, "json")
        with open(destination, encoding="utf-8") as handle:
            exported = handle.read()
        self.assertNotIn(self.session_id, exported)
        self.assertNotIn(self.sessions, exported)
        csv_destination = os.path.join(self.temporary.name, "usage.csv")
        export_report(self.database, csv_destination, "csv")
        with open(csv_destination, encoding="utf-8") as handle:
            csv_exported = handle.read()
        self.assertIn("creditStatus", csv_exported)
        self.assertIn("attributionCoverage", csv_exported)
        self.assertNotIn(self.session_id, csv_exported)
        self.assertEqual("PASS", self_check(self.database)["status"])

    def test_span_lifecycle_and_cohort_events_are_explicit(self):
        self.create()
        first = begin_span(self.database, "AWB-101", "AWB-101-T01", "orchestrator",
                           self.session_id, "gpt-5.6-terra", sessions_root=self.sessions)
        second = begin_span(self.database, "AWB-101", "AWB-101-T01", "orchestrator-2",
                            self.session_id, "gpt-5.6-terra", sessions_root=self.sessions)
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            shared = ingest_snapshot(connection, self.snapshot(
                "span-shared", {"input_tokens": 5, "cached_input_tokens": 1,
                                "cache_write_input_tokens": 0, "output_tokens": 2,
                                "reasoning_tokens": 1, "total_tokens": 7,
                                "numeric_extensions": {}}, self.session_id))
            connection.commit()
        finally:
            connection.close()
        self.assertEqual("SHARED", shared["attribution"])
        self.assertEqual("ENDED", end_span(
            self.database, first["spanId"], "orchestrator", sessions_root=self.sessions)["status"])
        self.assertEqual("ENDED", end_span(
            self.database, second["spanId"], "orchestrator-2", sessions_root=self.sessions)["status"])
        with self.assertRaises(UsageError):
            cohort_event(self.database, "COHORT_STARTED", "cohort-fixture", "agent")
        cohort_event(self.database, "COHORT_STARTED", "cohort-fixture", "human", "HUMAN",
                     {"cohortVersion": 1})
        cohort_event(self.database, "COHORT_SNAPSHOT", "cohort-fixture", "orchestrator",
                     payload={"elapsedDays": 7})
        cohort_event(self.database, "COHORT_SEMANTICS_CHANGED", "cohort-fixture", "human", "HUMAN",
                     {"newVersion": 2})
        cohort_event(self.database, "COHORT_CONCLUDED", "cohort-fixture", "human", "HUMAN",
                     {"outcome": "INCONCLUSIVE", "reason": "fixture"})
        connection = open_database(self.database)
        self.assertEqual(4, connection.execute(
            "SELECT count(*) FROM usage_events WHERE event_type LIKE 'COHORT_%'").fetchone()[0])
        connection.close()

    def test_projection_supports_all_six_dimensions_and_nearest_rank(self):
        self.create()
        acquire_claim(self.database, "AWB-101", "AWB-101-T01", "planner", "PLANNER", self.expires())
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute("SELECT * FROM claims WHERE work_item_id='AWB-101'").fetchone()
            record_binding(connection, claim, "codex-local", "group-stream", "gpt-5.6-terra")
            ingest_snapshot(connection, self.snapshot(
                "group", {"input_tokens": 100, "cached_input_tokens": 40,
                          "cache_write_input_tokens": 10, "output_tokens": 20,
                          "reasoning_tokens": 5, "total_tokens": 120,
                          "numeric_extensions": {}}, "group-stream"))
            connection.execute("UPDATE work_items SET state='FINAL_ACCEPTANCE_APPROVED',"
                               "queue_state='HELD',held_reason='COMPLETED',"
                               "closed_at='2026-08-22T02:00:00+00:00' "
                               "WHERE work_item_id='AWB-101'")
            connection.commit()
        finally:
            connection.close()
        for dimension in ("work_item", "role", "agent", "model", "stage", "date"):
            report = project(self.database, dimension,
                             "2026-08-22T00:00:00+00:00", "2026-08-23T00:00:00+00:00")
            self.assertEqual(1, len(report["groups"]), dimension)
            self.assertEqual(1, report["distribution"]["n"], dimension)
            self.assertEqual(report["distribution"]["p50EstimatedCredits"],
                             report["distribution"]["p90EstimatedCredits"])
        connection = open_database(self.database)
        before = connection.execute("SELECT count(*) FROM usage_events").fetchone()[0]
        connection.close()
        simulated_cards = json.dumps([{"version": "simulation-v1",
                                       "effective_from": "2026-08-22T00:00:00+00:00",
                                       "effective_to": None,
                                       "unit": "estimated_credits_per_1m_tokens",
                                       "models": {"gpt-5.6-terra": {"input": 1,
                                           "cached_input": 1, "cache_write_input": 1,
                                           "output": 1}}}])
        simulated = project(self.database, "model", simulate_rate_cards=simulated_cards)
        self.assertTrue(simulated["simulation"])
        connection = open_database(self.database)
        self.assertEqual(before, connection.execute("SELECT count(*) FROM usage_events").fetchone()[0])
        connection.close()

    def test_distribution_requires_whole_work_item_complete_and_comparable(self):
        for number in range(101, 106):
            self.create("AWB-%d" % number)
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")

            def contribution(item_id, suffix, status="COMPLETE", rate="rate-v1",
                             parser="parser-v1", credits=1.0):
                counters = {"input_tokens": 1, "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0, "output_tokens": 0,
                            "reasoning_tokens": 0, "total_tokens": 1,
                            "numeric_extensions": {}}
                attribution = {"workItemId": item_id, "taskId": item_id + "-T01",
                               "claimId": None, "agentId": "fixture", "role": "PLANNER",
                               "agentProfile": None, "model": "gpt-5.6-terra",
                               "stage": "PLANNING", "status": "ATTRIBUTED",
                               "revision": False}
                append_event(connection, "AGENT_USAGE_RECORDED", "SYSTEM", "fixture", {
                    "schemaVersion": "AWB-USAGE-v1", "adapterVersion": "adapter-v1",
                    "parserVersion": parser, "segment": 1, "cumulative": counters,
                    "delta": counters, "attribution": attribution,
                    "credits": {"status": status,
                                "estimatedCredits": credits if status != "UNKNOWN" else None,
                                "rateCardVersion": rate}}, work_item_id=item_id,
                    task_id=item_id + "-T01", provider="codex-local",
                    source_session_id="stream-" + suffix, source_snapshot_key="key-" + suffix,
                    observed_at="2026-08-22T01:00:00+00:00")

            contribution("AWB-101", "unknown-complete")
            contribution("AWB-101", "unknown", status="UNKNOWN", rate=None)
            contribution("AWB-102", "partial-complete")
            contribution("AWB-102", "partial", status="PARTIAL")
            contribution("AWB-103", "rate-one", rate="rate-v1")
            contribution("AWB-103", "rate-two", rate="rate-v2")
            contribution("AWB-104", "complete-one", credits=2.0)
            contribution("AWB-104", "complete-two", credits=3.0)
            contribution("AWB-105", "not-final", credits=9.0)
            connection.execute(
                "UPDATE work_items SET state='FINAL_ACCEPTANCE_APPROVED',queue_state='HELD',"
                "held_reason='COMPLETED',closed_at='2026-08-22T02:00:00+00:00' "
                "WHERE work_item_id IN ('AWB-101','AWB-102','AWB-103','AWB-104')")
            connection.commit()
        finally:
            connection.close()
        distribution = project(self.database, "work_item")["distribution"]
        self.assertEqual(1, distribution["n"])
        self.assertEqual(5.0, distribution["p50EstimatedCredits"])
        self.assertEqual(1, len(distribution["strata"]))

    def test_token_weighted_attribution_is_distinct_from_event_coverage(self):
        connection = open_database(self.database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for status, tokens in (("ATTRIBUTED", 1), ("SHARED", 1000)):
                counters = {"input_tokens": tokens, "cached_input_tokens": 0,
                            "cache_write_input_tokens": 0, "output_tokens": 0,
                            "reasoning_tokens": 0, "total_tokens": tokens,
                            "numeric_extensions": {}}
                attribution = {"workItemId": None, "taskId": None, "claimId": None,
                               "agentId": None, "role": None, "agentProfile": None,
                               "model": None, "stage": "UNKNOWN", "status": status,
                               "revision": False}
                append_event(connection, "AGENT_USAGE_RECORDED", "SYSTEM", "fixture", {
                    "schemaVersion": "AWB-USAGE-v1", "adapterVersion": "adapter-v1",
                    "parserVersion": "parser-v1", "segment": 1, "cumulative": counters,
                    "delta": counters, "attribution": attribution,
                    "credits": {"status": "UNKNOWN", "estimatedCredits": None,
                                "rateCardVersion": None}}, provider="codex-local",
                    source_session_id="weighted-" + status,
                    source_snapshot_key="weighted-key-" + status,
                    observed_at="2026-08-22T01:00:00+00:00")
            connection.commit()
        finally:
            connection.close()
        report = project(self.database, "stage")
        self.assertEqual(0.5, report["quality"]["eventAttributionCoverage"])
        token = report["quality"]["tokenAttribution"]
        self.assertEqual({"ATTRIBUTED": 1, "SHARED": 1000, "UNATTRIBUTED": 0},
                         token["totals"])
        self.assertAlmostEqual(1.0 / 1001.0, token["ratios"]["ATTRIBUTED"])
        self.assertEqual("participation.coverage", report["quality"]["formalCoverageMetric"])
        self.assertEqual(0.9, report["quality"]["formalCoverageThreshold"])
        table = __import__("agent_workboard.usage", fromlist=["render_table"]).render_table(report)
        self.assertIn("attributedTokens", table)
        self.assertIn("participationCoverage", table)
        destination = os.path.join(self.temporary.name, "weighted.csv")
        export_report(self.database, destination, "csv", group_by="stage")
        with open(destination, encoding="utf-8") as handle:
            exported = handle.read()
        self.assertIn("sharedTokenRatio", exported)
        self.assertIn("formalCoverageThreshold", exported)

    def test_usage_cli_json_table_and_self_check(self):
        output = __import__("io").StringIO()
        with mock.patch("agent_workboard.cli._project_database", return_value=self.database):
            with redirect_stdout(output):
                self.assertEqual(0, cli_main(["usage", "show", "--project", ".",
                                              "--format", "json", "--group-by", "role"]))
            parsed = json.loads(output.getvalue())
            self.assertEqual("AWB-USAGE-REPORT-v1", parsed["schemaVersion"])
            output = __import__("io").StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cli_main(["usage", "self-check", "--project", "."]))
            self.assertIn('"status": "PASS"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
