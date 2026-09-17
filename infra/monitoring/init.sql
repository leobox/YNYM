-- Leobox Multi-root Backlog Database Schema

CREATE TABLE IF NOT EXISTS tasks (
    id VARCHAR(20) PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    doc VARCHAR(255),
    status VARCHAR(30) NOT NULL,
    priority VARCHAR(10),
    category VARCHAR(50) NOT NULL,
    phase VARCHAR(100) NOT NULL,
    estimate_min INT DEFAULT 30,
    where_path TEXT,
    deps TEXT[],
    note TEXT,
    done_at DATE,
    updated_at DATE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS task_history (
    id SERIAL PRIMARY KEY,
    task_id VARCHAR(20) REFERENCES tasks(id) ON DELETE CASCADE,
    old_status VARCHAR(30),
    new_status VARCHAR(30) NOT NULL,
    note TEXT,
    changed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 인덱스 생성
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category);
CREATE INDEX IF NOT EXISTS idx_tasks_phase ON tasks(phase);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);

-- 유용한 통계용 뷰
CREATE OR REPLACE VIEW v_backlog_summary AS
SELECT
    count(*) AS total_tasks,
    count(*) FILTER (WHERE status = 'done') AS done_tasks,
    count(*) FILTER (WHERE status = 'in_progress') AS in_progress_tasks,
    count(*) FILTER (WHERE status = 'todo') AS todo_tasks,
    count(*) FILTER (WHERE status = 'blocked') AS blocked_tasks,
    count(*) FILTER (WHERE status = 'needs_decision') AS needs_decision_tasks,
    ROUND((count(*) FILTER (WHERE status = 'done')::numeric / NULLIF(count(*), 0)) * 100, 1) AS progress_pct
FROM tasks;

CREATE OR REPLACE VIEW v_tasks_by_category AS
SELECT
    category,
    count(*) AS total,
    count(*) FILTER (WHERE status = 'done') AS done,
    count(*) FILTER (WHERE status = 'in_progress') AS in_progress,
    count(*) FILTER (WHERE status = 'todo') AS todo
FROM tasks
GROUP BY category;
