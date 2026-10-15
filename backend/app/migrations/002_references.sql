CREATE TABLE audio_references (
    id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    filename TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reference_clips (
    id TEXT PRIMARY KEY NOT NULL,
    reference_id TEXT NOT NULL REFERENCES audio_references(id),
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE project_reference_selection (
    project_id TEXT PRIMARY KEY NOT NULL REFERENCES projects(id),
    clip_id TEXT NOT NULL REFERENCES reference_clips(id)
);
