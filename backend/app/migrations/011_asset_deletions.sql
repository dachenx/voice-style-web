CREATE TABLE asset_deletions (
    project_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('reference', 'clip', 'export')),
    asset_id TEXT NOT NULL,
    reference_id TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id, kind, asset_id)
);
