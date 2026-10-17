CREATE TABLE paragraph_audio_selections (
    project_id TEXT NOT NULL REFERENCES projects(id),
    paragraph_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, paragraph_id)
);
