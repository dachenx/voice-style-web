CREATE TABLE task_deletions (
    task_id TEXT PRIMARY KEY NOT NULL REFERENCES generation_tasks(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1))
);
