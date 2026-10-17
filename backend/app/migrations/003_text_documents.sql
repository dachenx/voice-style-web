CREATE TABLE text_documents (
    project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    content_json TEXT NOT NULL
);
CREATE TABLE text_confirmations (
    id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    UNIQUE (project_id, revision)
);
