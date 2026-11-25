-- 这是表结构定义文件, 在这里定义各个app的表结构

CREATE TABLE IF NOT EXISTS vault_configs (
    name TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    symbols TEXT NOT NULL,
    length INTEGER NOT NULL
)