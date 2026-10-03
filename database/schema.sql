-- PrepMate database schema

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER  PRIMARY KEY AUTOINCREMENT,
    name       TEXT     NOT NULL,
    email      TEXT     NOT NULL UNIQUE,
    password   TEXT     NOT NULL,          -- werkzeug pbkdf2 hash
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title       TEXT     NOT NULL,
    category    TEXT     NOT NULL,
    description TEXT,
    due_date    TEXT,                      -- ISO-8601 date string YYYY-MM-DD
    is_complete INTEGER  NOT NULL DEFAULT 0,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_id  ON tasks (user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks (user_id, category);
CREATE INDEX IF NOT EXISTS idx_tasks_complete ON tasks (user_id, is_complete);

-- ── Planner (Milestone 3) ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS roadmaps (
    id             INTEGER  PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Inputs captured at generation time
    target_role    TEXT     NOT NULL,
    skills         TEXT     NOT NULL,
    hours_per_day  REAL     NOT NULL,
    duration_weeks INTEGER  NOT NULL,
    -- Metadata
    title          TEXT     NOT NULL,      -- short display title
    source         TEXT     NOT NULL DEFAULT 'fallback',  -- 'ai' | 'fallback'
    is_draft       INTEGER  NOT NULL DEFAULT 1,  -- 1=draft (pending approval), 0=saved
    -- Why Bedrock failed (NULL when source='ai' or not attempted)
    -- Values: no_credentials | auth | config | transient | parse
    failure_reason TEXT,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roadmap_milestones (
    id           INTEGER  PRIMARY KEY AUTOINCREMENT,
    roadmap_id   INTEGER  NOT NULL REFERENCES roadmaps(id) ON DELETE CASCADE,
    week_number  INTEGER  NOT NULL,
    title        TEXT     NOT NULL,
    priority     TEXT     NOT NULL DEFAULT 'medium',  -- 'high' | 'medium' | 'low'
    category     TEXT     NOT NULL,   -- must match a CATEGORIES value
    description  TEXT,
    daily_tasks  TEXT     NOT NULL DEFAULT '[]',      -- JSON array of strings
    position     INTEGER  NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_roadmaps_user      ON roadmaps (user_id, is_draft);
CREATE INDEX IF NOT EXISTS idx_milestones_roadmap ON roadmap_milestones (roadmap_id, position);

-- Track which milestones have already been imported as tasks (prevent duplicates)
CREATE TABLE IF NOT EXISTS milestone_imports (
    milestone_id INTEGER NOT NULL REFERENCES roadmap_milestones(id) ON DELETE CASCADE,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (milestone_id, task_id)
);
