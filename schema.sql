-- 这是表结构定义文件, 在这里定义各个app的表结构

CREATE TABLE IF NOT EXISTS vault_configs (
    name TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    symbols TEXT NOT NULL,
    length INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_records (
    token TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL,
    subject_id TEXT NOT NULL CHECK (subject_id <> '')
);

CREATE TABLE IF NOT EXISTS auth_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    authenticated BOOL NOT NULL CHECK (authenticated IN (0, 1)),
    identity TEXT,
    request_time TIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    interface TEXT NOT NULL -- HTTP 方法和路由，例如 GET /vault/configs
);
