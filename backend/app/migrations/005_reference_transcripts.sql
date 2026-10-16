CREATE TABLE reference_transcripts (
    clip_id TEXT NOT NULL REFERENCES reference_clips(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    clip_sha256 TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (clip_id, revision)
);
