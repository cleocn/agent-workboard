"""Fail-closed selective reader for local Codex JSONL usage records.

Only scalar values on the explicit selection tree are decoded.  Unknown record
payloads and forbidden text values are skipped by the JSON scanner without
being materialized as Python strings or objects.
"""

import hashlib
import json
import os


class AdapterError(Exception):
    def __init__(self, message, reason_code="ADAPTER_REJECTED"):
        Exception.__init__(self, message)
        self.reason_code = reason_code


class _SelectiveJSON(object):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"

    def __init__(self, data, materialization_audit=None):
        if not isinstance(data, bytes):
            raise AdapterError("selective reader requires bytes", "MALFORMED_JSON")
        self.data = data
        self.length = len(data)
        self.pos = 0
        self.materialization_audit = materialization_audit

    def _ws(self):
        while self.pos < self.length and self.data[self.pos] in b" \t\r\n":
            self.pos += 1

    def _string_bounds(self):
        self._ws()
        if self.pos >= self.length or self.data[self.pos] != 34:
            raise AdapterError("expected JSON string", "MALFORMED_JSON")
        start = self.pos
        self.pos += 1
        escaped = False
        while self.pos < self.length:
            byte = self.data[self.pos]
            self.pos += 1
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                return start, self.pos
            elif byte < 32:
                raise AdapterError("control byte in JSON string", "MALFORMED_JSON")
        raise AdapterError("unterminated JSON string", "MALFORMED_JSON")

    def _key(self):
        start, end = self._string_bounds()
        raw = self.data[start + 1:end - 1]
        if b"\\" in raw:
            raise AdapterError("escaped object keys are not supported", "SCHEMA_DRIFT")
        return raw

    def _materialized_string(self):
        start, end = self._string_bounds()
        try:
            value = json.loads(self.data[start:end].decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AdapterError("invalid selected JSON string", "MALFORMED_JSON")
        if not isinstance(value, str):
            raise AdapterError("selected JSON value was not a string", "SCHEMA_DRIFT")
        if self.materialization_audit is not None:
            self.materialization_audit(value)
        return value

    def _skip_string(self):
        self._string_bounds()

    def _skip(self):
        self._ws()
        if self.pos >= self.length:
            raise AdapterError("missing JSON value", "MALFORMED_JSON")
        byte = self.data[self.pos]
        if byte == 34:
            self._skip_string()
            return
        if byte in (91, 123):
            closings = [93 if byte == 91 else 125]
            self.pos += 1
            while self.pos < self.length and closings:
                byte = self.data[self.pos]
                if byte == 34:
                    self._skip_string()
                    continue
                self.pos += 1
                if byte == 91:
                    closings.append(93)
                elif byte == 123:
                    closings.append(125)
                elif byte in (93, 125):
                    if byte != closings.pop():
                        raise AdapterError("mismatched JSON container", "MALFORMED_JSON")
            if closings:
                raise AdapterError("unterminated JSON container", "MALFORMED_JSON")
            return
        while self.pos < self.length and self.data[self.pos] not in b",}] \t\r\n":
            self.pos += 1

    def _lexical_number(self, integer_only):
        self._ws()
        start = self.pos
        while self.pos < self.length and self.data[self.pos] not in b",}] \t\r\n":
            self.pos += 1
        token = self.data[start:self.pos]
        signless = token[1:] if token.startswith(b"-") else token
        valid_integer = bool(signless) and signless.isdigit() and (
            signless == b"0" or not signless.startswith(b"0"))
        if integer_only:
            if not valid_integer:
                raise AdapterError("usage counter was not a lexical integer", "SCHEMA_DRIFT")
            return int(token)
        try:
            decoded = token.decode("ascii")
            value = float(decoded)
        except (UnicodeDecodeError, ValueError):
            raise AdapterError("selected number was invalid", "SCHEMA_DRIFT")
        if decoded in ("NaN", "Infinity", "-Infinity"):
            raise AdapterError("selected number was non-finite", "SCHEMA_DRIFT")
        return value

    def _selected(self, tree):
        self._ws()
        if tree == self.STRING:
            return self._materialized_string()
        if tree == self.INTEGER:
            return self._lexical_number(True)
        if tree == self.NUMBER:
            return self._lexical_number(False)
        if self.pos >= self.length or self.data[self.pos] != 123:
            raise AdapterError("selected usage object was not an object", "SCHEMA_DRIFT")
        self.pos += 1
        output = {}
        while True:
            self._ws()
            if self.pos < self.length and self.data[self.pos] == 125:
                self.pos += 1
                return output
            key = self._key()
            self._ws()
            if self.pos >= self.length or self.data[self.pos] != 58:
                raise AdapterError("invalid JSON object", "MALFORMED_JSON")
            self.pos += 1
            branch = tree.get(key, tree.get(b"*"))
            if branch is None:
                self._skip()
            else:
                try:
                    output_key = key.decode("utf-8")
                except UnicodeDecodeError:
                    raise AdapterError("selected object key was invalid", "SCHEMA_DRIFT")
                output[output_key] = self._selected(branch)
            self._ws()
            if self.pos < self.length and self.data[self.pos] == 44:
                self.pos += 1
                continue
            if self.pos < self.length and self.data[self.pos] == 125:
                self.pos += 1
                return output
            raise AdapterError("invalid JSON object separator", "MALFORMED_JSON")

    def parse(self, tree):
        value = self._selected(tree)
        self._ws()
        if self.pos != self.length:
            raise AdapterError("trailing JSON input", "MALFORMED_JSON")
        return value


_STRING = _SelectiveJSON.STRING
_INTEGER = _SelectiveJSON.INTEGER
_NUMBER = _SelectiveJSON.NUMBER
_DISCRIMINATOR_TREE = {b"type": _STRING, b"payload": {b"type": _STRING}}
_SESSION_TREE = {
    b"timestamp": _STRING, b"type": _STRING,
    b"payload": {
        b"id": _STRING, b"session_id": _STRING, b"parent_thread_id": _STRING,
        b"cli_version": _STRING,
        b"source": {b"subagent": {b"agent_role": _STRING}},
    },
}
_TOKEN_TREE = {
    b"timestamp": _STRING, b"type": _STRING,
    b"payload": {
        b"type": _STRING,
        b"info": {
            b"total_token_usage": {b"*": _INTEGER},
            b"rate_limits": {b"*": {b"used_percent": _NUMBER,
                                      b"window_minutes": _INTEGER,
                                      b"resets_at": _INTEGER}},
        },
    },
}


def _canonical_counters(raw):
    aliases = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
               "cache_write_input_tokens": "cache_write_input_tokens",
               "output_tokens": "output_tokens", "reasoning_output_tokens": "reasoning_tokens",
               "total_tokens": "total_tokens"}
    counters = {"cache_write_input_tokens": 0}
    extensions = {}
    for key, value in raw.items():
        if type(value) is not int or value < 0:
            raise AdapterError("usage counter is not a non-negative integer", "SCHEMA_DRIFT")
        if key in aliases:
            counters[aliases[key]] = value
        else:
            extensions[key] = value
    required = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
    if any(key not in counters for key in required):
        raise AdapterError("required usage counter is missing", "SCHEMA_DRIFT")
    counters["numeric_extensions"] = extensions
    return counters


class CodexLocalAdapter(object):
    provider = "codex-local"
    adapter_version = "codex-local-v1"
    parser_version = "codex-local-selective-v2"

    def __init__(self, sessions_root=None, materialization_audit=None):
        configured = sessions_root or os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")), "sessions")
        if os.path.islink(configured) or not os.path.isdir(configured):
            raise AdapterError("Codex sessions root is unavailable", "UNSAFE_SESSIONS_ROOT")
        self.sessions_root = os.path.realpath(configured)
        self.materialization_audit = materialization_audit

    def _locate(self, session_id):
        if (not isinstance(session_id, str) or not session_id or "/" in session_id or
                "\\" in session_id or session_id in (".", "..")):
            raise AdapterError("unsafe source session id", "UNSAFE_SESSION_ID")
        matches = []
        for base, directories, names in os.walk(self.sessions_root, followlinks=False):
            if any(os.path.islink(os.path.join(base, name)) for name in directories):
                raise AdapterError("symbolic session directory refused", "UNSAFE_SESSION_FILE")
            directories[:] = sorted(directories)
            for name in sorted(names):
                path = os.path.join(base, name)
                if session_id in name and name.endswith(".jsonl"):
                    if os.path.islink(path) or not os.path.isfile(path):
                        raise AdapterError("symbolic session file refused", "UNSAFE_SESSION_FILE")
                    canonical = os.path.realpath(path)
                    if os.path.commonpath((self.sessions_root, canonical)) != self.sessions_root:
                        raise AdapterError("session file escapes root", "UNSAFE_SESSION_FILE")
                    matches.append(canonical)
        if len(matches) != 1:
            raise AdapterError("source session id did not resolve uniquely", "SESSION_NOT_UNIQUE")
        return matches[0]

    def read(self, session_id):
        path = self._locate(session_id)
        identity, snapshots, quota = None, [], []
        with open(path, "rb") as handle:
            for ordinal, line in enumerate(handle, 1):
                raw = line.rstrip(b"\r\n")
                discriminator = _SelectiveJSON(
                    raw, self.materialization_audit).parse(_DISCRIMINATOR_TREE)
                record_type = discriminator.get("type")
                event_subtype = discriminator.get("payload", {}).get("type")
                if record_type == "session_meta":
                    selected = _SelectiveJSON(
                        raw, self.materialization_audit).parse(_SESSION_TREE)
                    payload = selected.get("payload", {})
                    if identity is not None:
                        raise AdapterError("duplicate session metadata", "SCHEMA_DRIFT")
                    identity = {"id": payload.get("id"), "parent_session_id": payload.get("parent_thread_id"),
                                "cli_version": payload.get("cli_version"),
                                "agent_role": payload.get("source", {}).get("subagent", {}).get("agent_role")}
                elif record_type == "event_msg" and event_subtype == "token_count":
                    selected = _SelectiveJSON(
                        raw, self.materialization_audit).parse(_TOKEN_TREE)
                    payload = selected.get("payload", {})
                    info = payload.get("info", {})
                    raw = info.get("total_token_usage")
                    if not isinstance(raw, dict):
                        raise AdapterError("token_count total usage is missing", "SCHEMA_DRIFT")
                    counters = _canonical_counters(raw)
                    observed = selected.get("timestamp")
                    if not isinstance(observed, str) or not observed:
                        raise AdapterError("token_count timestamp is missing", "SCHEMA_DRIFT")
                    key_body = json.dumps([self.provider, session_id, self.adapter_version,
                                           self.parser_version, ordinal, counters],
                                          sort_keys=True, separators=(",", ":"))
                    snapshots.append({"provider": self.provider, "source_session_id": session_id,
                                      "source_snapshot_key": hashlib.sha256(key_body.encode("utf-8")).hexdigest(),
                                      "observed_at": observed, "accuracy": "OBSERVED",
                                      "adapter_version": self.adapter_version,
                                      "parser_version": self.parser_version,
                                      "source_ordinal": ordinal, "counters": counters})
                    for limit_name, window in info.get("rate_limits", {}).items():
                        if not isinstance(window, dict) or set(window) != {"used_percent", "window_minutes", "resets_at"}:
                            raise AdapterError("quota window schema drift", "SCHEMA_DRIFT")
                        quota.append({"limit_name": limit_name, "used_percent": window["used_percent"],
                                      "window_minutes": window["window_minutes"], "resets_at": window["resets_at"],
                                      "observed_at": observed})
        if identity is None or identity.get("id") != session_id:
            raise AdapterError("session metadata id mismatch", "SESSION_ID_MISMATCH")
        return {"identity": identity, "snapshots": snapshots, "quota": quota}

    def latest(self, session_id):
        parsed = self.read(session_id)
        if not parsed["snapshots"]:
            return None, parsed["identity"]
        return parsed["snapshots"][-1], parsed["identity"]
