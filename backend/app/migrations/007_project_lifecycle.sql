CREATE TABLE project_archives (
    project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id),
    archived_at TEXT NOT NULL
);
CREATE TABLE project_create_requests (
    request_id TEXT PRIMARY KEY NOT NULL,
    request_name TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id)
);
