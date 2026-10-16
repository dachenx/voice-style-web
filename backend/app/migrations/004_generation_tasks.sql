CREATE TABLE generation_tasks (
    id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'canceled')),
    phase TEXT NOT NULL,
    completed_paragraphs INTEGER NOT NULL DEFAULT 0,
    total_paragraphs INTEGER NOT NULL CHECK (total_paragraphs > 0),
    state_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    error_message TEXT,
    result_json TEXT,
    snapshot_json TEXT NOT NULL,
    retry_of TEXT UNIQUE REFERENCES generation_tasks(id)
);
CREATE INDEX generation_queue_order ON generation_tasks(state, created_at, id);
CREATE UNIQUE INDEX generation_single_running ON generation_tasks((1)) WHERE state = 'running';
CREATE TABLE generation_requests (
    project_id TEXT NOT NULL REFERENCES projects(id),
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES generation_tasks(id),
    PRIMARY KEY (project_id, request_id)
);
CREATE TABLE generation_worker_health (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
