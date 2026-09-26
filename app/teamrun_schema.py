"""手机协同小队的 schema59 表。仅由数据库迁移调用，不在请求中建表。"""


def install_schema(connection) -> None:
    """在调用方持有的迁移事务中安装服务端小队状态表。"""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS team_run(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          actor_id INTEGER NOT NULL,
          request_key TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          query TEXT NOT NULL,
          team_name TEXT NOT NULL,
          team_summary TEXT NOT NULL DEFAULT '',
          mode TEXT NOT NULL,
          depth TEXT NOT NULL,
          leader_emp_idx INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'awaiting_approval',
          summary_task_id INTEGER,
          summary_status TEXT NOT NULL DEFAULT 'pending',
          summary_attempt_no INTEGER NOT NULL DEFAULT 1,
          summary_claim_until REAL,
          summary_error TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          UNIQUE(tenant_id, request_key)
        )
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS team_run_member(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          team_run_id INTEGER NOT NULL,
          tenant_id INTEGER NOT NULL,
          position INTEGER NOT NULL,
          emp_idx INTEGER NOT NULL,
          name TEXT NOT NULL DEFAULT '',
          role TEXT NOT NULL DEFAULT '',
          role_in_team TEXT NOT NULL DEFAULT '',
          task_text TEXT NOT NULL DEFAULT '',
          depends_on_json TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'pending',
          approved INTEGER NOT NULL DEFAULT 0,
          approved_at REAL,
          attempt_no INTEGER NOT NULL DEFAULT 1,
          task_id INTEGER,
          claim_until REAL,
          last_error TEXT,
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          FOREIGN KEY(team_run_id) REFERENCES team_run(id),
          UNIQUE(team_run_id, emp_idx),
          UNIQUE(team_run_id, position)
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_run_tenant_recent "
        "ON team_run(tenant_id, updated_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_run_active "
        "ON team_run(tenant_id,status,updated_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_team_run_member_task "
        "ON team_run_member(tenant_id,task_id)"
    )
