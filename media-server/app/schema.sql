CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS libraries (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    character_notes TEXT NOT NULL DEFAULT '',
    model_ids TEXT NOT NULL DEFAULT '[]',          -- JSON list of Open WebUI model/preset ids
    rating_cap TEXT NOT NULL DEFAULT 'explicit',   -- sfw | suggestive | explicit
    start_heat INTEGER NOT NULL DEFAULT 1,         -- heat a fresh chat starts at (1..5)
    cooldown_turns INTEGER,                        -- NULL = server default
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- image | video
    source TEXT NOT NULL,                          -- upload | import
    rel_path TEXT NOT NULL,                        -- relative to uploads_dir or import_root
    play_path TEXT,                                -- transcoded mp4 relative to derived_dir (videos only)
    sha256 TEXT NOT NULL,
    mime TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    duration REAL,
    thumb_path TEXT,                               -- relative to derived_dir
    poster_path TEXT,                              -- relative to derived_dir (videos)
    frames TEXT NOT NULL DEFAULT '[]',             -- JSON list of keyframe paths (videos)
    caption TEXT NOT NULL DEFAULT '',
    rating TEXT,
    heat INTEGER NOT NULL DEFAULT 3,               -- 1 innocent .. 5 sexual; see ollama.DEFAULT_PROMPT
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | tagging | ready | error
    error TEXT,
    tags_locked INTEGER NOT NULL DEFAULT 0,        -- 1 = manually edited, don't overwrite on bulk re-tag
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    tagged_at TEXT,
    UNIQUE (library_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_media_lib_status ON media(library_id, status);

CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (media_id, category, value)
);
CREATE INDEX IF NOT EXISTS idx_tags_cat_val ON media_tags(category, value);

-- rowid = media.id
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(caption, tags);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                            -- tag | embed
    status TEXT NOT NULL DEFAULT 'queued',         -- queued | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    message_id TEXT,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    library_id INTEGER NOT NULL,
    model_id TEXT,
    user_turn INTEGER,
    sent_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sends_chat ON sends(chat_id, id);
