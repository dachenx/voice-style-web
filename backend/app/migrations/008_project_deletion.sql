CREATE TABLE project_deletions (
    project_id TEXT PRIMARY KEY NOT NULL,
    paths_json TEXT NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1))
);
CREATE TABLE deleted_project_requests (
    request_id TEXT PRIMARY KEY NOT NULL
);
