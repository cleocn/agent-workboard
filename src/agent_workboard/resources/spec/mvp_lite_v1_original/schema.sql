PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta(key, value) VALUES
  ('schema_version', 'MVP-LITE-v1'),
  ('plan_revision', 'PLAN-MVP-LITE-v1');

CREATE TABLE work_items (
  work_item_id TEXT PRIMARY KEY,
  work_item_type TEXT NOT NULL CHECK (work_item_type IN ('TI','FE','R','WA','AWB')),
  title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
  mode TEXT NOT NULL CHECK (mode IN ('STANDARD','READ_ONLY_DIAGNOSIS')),
  state TEXT NOT NULL DEFAULT 'DRAFT' CHECK (state IN (
    'DRAFT','PLAN_REVIEW_PENDING','PLAN_REVIEW_APPROVED',
    'IMPLEMENTING','IMPLEMENTATION_COMPLETED','FINAL_ACCEPTANCE_APPROVED'
  )),
  queue_state TEXT NOT NULL DEFAULT 'CLAIMABLE' CHECK (queue_state IN (
    'CLAIMABLE','CLAIMED','WAITING_HUMAN','HELD','BLOCKED'
  )),
  priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P0','P1','P2','P3')),
  current_role TEXT CHECK (current_role IN ('PLANNER','IMPLEMENTER','REVIEWER','ORCHESTRATOR')),
  held_reason TEXT,
  blocked_reason TEXT,
  row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT,
  CHECK (queue_state <> 'HELD' OR held_reason IS NOT NULL),
  CHECK (queue_state <> 'BLOCKED' OR blocked_reason IS NOT NULL),
  CHECK (state <> 'FINAL_ACCEPTANCE_APPROVED' OR (queue_state = 'HELD' AND closed_at IS NOT NULL))
);

CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  seq INTEGER NOT NULL CHECK (seq > 0),
  title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
  owner_role TEXT NOT NULL CHECK (owner_role IN ('PLANNER','IMPLEMENTER','REVIEWER')),
  status TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (status IN (
    'NOT_STARTED','IN_PROGRESS','BLOCKED','WAITING_ACCEPTANCE','COMPLETED','CANCELLED'
  )),
  required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0,1)),
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(work_item_id, seq)
);

CREATE TABLE claims (
  claim_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  task_id TEXT REFERENCES tasks(task_id),
  agent_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('PLANNER','IMPLEMENTER','REVIEWER','ORCHESTRATOR')),
  generation INTEGER NOT NULL CHECK (generation > 0),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED')),
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE UNIQUE INDEX one_active_claim_per_work_item
  ON claims(work_item_id) WHERE status = 'ACTIVE';

CREATE TABLE repository_locks (
  lock_id TEXT PRIMARY KEY,
  repository_key TEXT NOT NULL,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  agent_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation > 0),
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','RELEASED','EXPIRED')),
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  released_at TEXT
);

CREATE UNIQUE INDEX one_active_writer_per_repository
  ON repository_locks(repository_key) WHERE status = 'ACTIVE';

CREATE TABLE reviews (
  review_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  stage TEXT NOT NULL CHECK (stage IN ('PLAN','FINAL')),
  reviewer_agent_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE human_gates (
  gate_id TEXT PRIMARY KEY,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  stage TEXT NOT NULL CHECK (stage IN ('PLAN','FINAL')),
  human_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK (decision IN ('APPROVED','REJECTED')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_item_id TEXT NOT NULL REFERENCES work_items(work_item_id),
  request_id TEXT NOT NULL UNIQUE,
  event_type TEXT NOT NULL,
  actor_kind TEXT NOT NULL CHECK (actor_kind IN ('AGENT','HUMAN','SYSTEM')),
  actor_id TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  created_at TEXT NOT NULL
);

CREATE INDEX tasks_by_work_item ON tasks(work_item_id, seq);
CREATE INDEX work_items_inbox ON work_items(priority, updated_at, work_item_id);
CREATE INDEX events_timeline ON events(work_item_id, event_id);
