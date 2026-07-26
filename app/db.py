"""ContentCrew 数据层:SQLite,单文件,线程安全."""
import json
import os
import sqlite3
import threading
import time
import atexit

DB_PATH = os.environ.get(
    "CONTENTCREW_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "data", "contentcrew.db"),
)
class StaleWriteError(RuntimeError):
    """异步写池发现目标库已被切换;该快照写应被丢弃而非执行代际切换。"""


_init_lock = threading.RLock()
_thread = threading.local()
# ``_conn`` remains the process anchor for backward-compatible lifecycle checks
# (tests and maintenance commands explicitly close/reset it).  Request workers use
# their own thread-local connection so one long query/transaction no longer holds
# a Python lock around every other tenant.
_conn = None
_conn_path = None
_connection_generation = 0
_all_connections: set[sqlite3.Connection] = set()
LATEST_SCHEMA_VERSION = 47

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_profile(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  persona_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brief_json TEXT NOT NULL,
  profile_id INTEGER,
  mode TEXT NOT NULL DEFAULT 'copilot',      -- autopilot / copilot / manual
  status TEXT NOT NULL DEFAULT 'running',    -- running/awaiting_review/gate_blocked/failed/done/cancelled
  current_idx INTEGER NOT NULL DEFAULT 0,
  source_schedule_id INTEGER,
  source_schedule_occurrence TEXT,
  gate_json TEXT,
  cost_usd REAL NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  billing_status TEXT NOT NULL DEFAULT 'charged', -- pending/charged/refunded
  billing_points REAL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS station_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  station_idx INTEGER NOT NULL,
  skill_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  output_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/awaiting_review/done/failed/stale/skipped/rejected
  review_comment TEXT,
  latency_ms INTEGER, tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_run_job ON station_run(job_id, station_idx, version);
CREATE TABLE IF NOT EXISTS asset(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,           -- topic / final / benchmark
  payload_json TEXT NOT NULL,
  job_id INTEGER,
  created_at REAL, updated_at REAL
);
"""


def _column_exists(c, table: str, column: str) -> bool:
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid table")
    return any(row["name"] == column for row in c.execute(f"PRAGMA table_info({table})"))


def _add_column(c, table: str, column: str, declaration: str):
    """只在列确实缺失时迁移；不再把锁库、磁盘满等真实错误静默吞掉。"""
    if not _column_exists(c, table, column):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _existing_schema_version(c) -> tuple[int, int, int]:
    """在任何 WAL/DDL 之前只读识别版本，防止旧程序先改未来库再拒绝。"""
    user_version = int(c.execute("PRAGMA user_version").fetchone()[0] or 0)
    has_ledger = c.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='table' AND name='schema_version'"
    ).fetchone()
    ledger_version = 0
    if has_ledger:
        row = c.execute(
            "SELECT COALESCE(MAX(version),0) FROM schema_version"
        ).fetchone()
        ledger_version = int((row or [0])[0] or 0)
    return max(user_version, ledger_version), ledger_version, user_version


def _columns(c, table: str) -> set[str]:
    if not table.replace("_", "").isalnum():
        raise ValueError("invalid table")
    return {str(row["name"]) for row in c.execute(f"PRAGMA table_info({table})")}


def _require_columns(c, table: str, required: set[str], stage: str) -> None:
    actual = _columns(c, table)
    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            f"数据库{stage}结构不完整：{table} 缺少列 {','.join(missing)}"
        )


def _validate_existing_database(c, ledger_version: int) -> None:
    """拒绝把无关/残缺 SQLite 文件叠加迁移成“最新库”标记。"""
    tables = {
        str(row["name"])
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        return
    if "job" not in tables:
        raise RuntimeError("数据库旧版结构不完整：缺少核心表 job")
    _require_columns(
        c,
        "job",
        {
            "id", "brief_json", "mode", "status", "current_idx",
            "created_at", "updated_at",
        },
        "旧版",
    )
    # v8 起已经是多租户；无版本账本的生产旧库也必须有这组可识别签名。
    if ledger_version >= 8 or "schema_version" not in tables:
        for table, required in {
            "tenants": {"id", "name"},
            "users": {
                "id", "tenant_id", "username", "password_hash", "role"
            },
        }.items():
            if table not in tables:
                raise RuntimeError(f"数据库旧版结构不完整：缺少核心表 {table}")
            _require_columns(c, table, required, "旧版")


def _validate_migrated_database(c) -> None:
    contracts = {
        "tenants": {"id", "name", "enabled", "balance"},
        "users": {
            "id", "tenant_id", "username", "password_hash", "role",
            "modules_json", "enabled", "must_change_password",
        },
        "job": {
            "id", "brief_json", "mode", "status", "current_idx", "tenant_id",
            "billing_status", "billing_points", "retry_count", "deleted_at",
            "created_at", "updated_at",
        },
        "task": {
            "id", "emp_idx", "brief_json", "status", "tenant_id",
            "billing_status", "billing_points", "retry_count", "deleted_at",
        },
        "knowledge": {"id", "tenant_id", "title", "content", "deleted_at"},
        "avatar_job": {
            "id", "tenant_id", "params_json", "status", "billing_status",
            "retry_count", "deleted_at",
        },
        "meeting": {
            "id", "tenant_id", "question", "status", "phase",
            "billing_status", "retry_count",
        },
        "tv_job": {
            "id", "tenant_id", "params_json", "status", "billing_status",
            "retry_count",
        },
        "tool_job": {
            "id", "tenant_id", "kind", "status", "billing_status",
            "retry_count", "retry_started_at",
        },
        "pub_task": {
            "id", "tenant_id", "platform", "status", "retry_count",
            "submission_state", "submit_started_at",
        },
        "notification": {
            "id", "tenant_id", "kind", "title", "body", "link",
            "read_at", "created_at",
        },
        "funnel_event": {
            "day", "event", "dimension", "tenant_id", "actor_hash", "hits",
            "first_at", "last_at",
        },
        "billing_operation": {
            "op_key", "tenant_id", "action", "points", "status",
        },
        "wechat_draft_delivery": {
            "id", "tenant_id", "job_id", "request_hash", "status",
            "billing_status", "op_key",
        },
    }
    tables = {
        str(row["name"])
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, required in contracts.items():
        if table not in tables:
            raise RuntimeError(f"数据库迁移后结构不完整：缺少核心表 {table}")
        _require_columns(c, table, required, "迁移后")


def _initialize_anchor():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _all_connections.add(_conn)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=30000")
        try:
            found_version, ledger_version, _ = _existing_schema_version(_conn)
        except Exception:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise
        if found_version > LATEST_SCHEMA_VERSION:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise RuntimeError(
                f"数据库 schema v{found_version} 高于当前程序支持的 "
                f"v{LATEST_SCHEMA_VERSION}，已拒绝降级启动")
        try:
            _validate_existing_database(_conn, ledger_version)
        except Exception:
            _conn.close()
            _all_connections.discard(_conn)
            _conn = None
            raise
        _conn.execute("PRAGMA journal_mode=WAL")       # 读写不互斥,断电也只丢最后一笔
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(SCHEMA)
        _add_column(_conn, "job", "billing_status", "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "job", "billing_points", "REAL")
        _add_column(_conn, "station_run", "steps_json", "TEXT")
        # v2 迁移:数字员工配置(可编辑提示词 + 全网进修技能库)
        _conn.execute("""CREATE TABLE IF NOT EXISTS employee_config(
          idx INTEGER PRIMARY KEY,
          prompt_template TEXT,
          skills_json TEXT NOT NULL DEFAULT '[]',
          learned_at REAL,
          created_at REAL, updated_at REAL
        )""")
        _add_column(_conn, "employee_config", "settings_json", "TEXT")
        _add_column(_conn, "employee_config", "caps_off_json", "TEXT")
        # v4 迁移:全局设置 / 知识沉淀库 / 定时任务
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_setting(
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS notification(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL DEFAULT '',
          link TEXT NOT NULL DEFAULT '#/',
          read_at REAL,
          created_at REAL,
          updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS knowledge(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          content TEXT NOT NULL DEFAULT '',
          tags_json TEXT NOT NULL DEFAULT '[]',
          source TEXT,                -- auto:job交付自动沉淀 / manual:老板手记
          job_id INTEGER,
          pinned INTEGER NOT NULL DEFAULT 0,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS task(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          emp_idx INTEGER NOT NULL,          -- 专家员工 idx(100+)
          brief_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/failed
          output_md TEXT,
          summary_md TEXT,
          steps_json TEXT,
          source_meeting_id INTEGER,
          source_action_key TEXT,
          source_task_id INTEGER,
          cost_usd REAL NOT NULL DEFAULT 0,
          tokens INTEGER NOT NULL DEFAULT 0,
          billing_status TEXT NOT NULL DEFAULT 'included',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS avatar_job(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          params_json TEXT NOT NULL,           -- photo_name/voice_id/script/prompt
          status TEXT NOT NULL DEFAULT 'queued',  -- queued/running/done/failed
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          steps_json TEXT,
          audio_file TEXT, video_file TEXT, error TEXT,
          deleted_at REAL, deleted_by INTEGER, delete_reason TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS schedule(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          brief_json TEXT NOT NULL,
          mode TEXT NOT NULL DEFAULT 'copilot',
          profile_id INTEGER,
          kind TEXT NOT NULL DEFAULT 'daily',   -- daily / weekly / interval
          at_time TEXT,                          -- 'HH:MM' 北京时间(daily/weekly)
          weekday INTEGER,                       -- 0=周一 … 6=周日(weekly)
          every_hours INTEGER,                   -- interval:每 N 小时
          enabled INTEGER NOT NULL DEFAULT 1,
          last_run_at REAL, next_run_at REAL, last_note TEXT,
          claim_until REAL, claim_token TEXT,
          created_at REAL, updated_at REAL
        );
        """)
        # 旧版“访问口令”从未参与真实登录鉴权，却以明文保存。升级时删除该
        # 失效界面遗留值，统一只保留账号密码 + 会话认证。
        _conn.execute("DELETE FROM app_setting WHERE key='boss_pin'")
        _add_column(_conn, "task", "billing_status", "TEXT NOT NULL DEFAULT 'included'")
        _add_column(_conn, "task", "billing_points", "REAL")
        _add_column(_conn, "avatar_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "avatar_job", "billing_points", "REAL")
        _add_column(_conn, "employee_config", "enabled", "INTEGER NOT NULL DEFAULT 1")
        _add_column(_conn, "schedule", "claim_until", "REAL")
        # 连续非计费失败要累计,静默 10 分钟重试一天 144 次老板却毫不知情
        _add_column(_conn, "schedule", "fail_streak",
                    "INTEGER NOT NULL DEFAULT 0")
        _add_column(_conn, "schedule", "claim_token", "TEXT")
        for col in ("model_text", "model_image"):  # v6 迁移:员工级模型路由
            _add_column(_conn, "employee_config", col, "TEXT")
        for tbl in ("knowledge", "asset"):  # v5 迁移:多维评估打标
            _add_column(_conn, tbl, "meta_json", "TEXT")
        # v8 迁移:多租户(企业/账号/数据隔离)
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS tenants(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'member',   -- root/owner/member
          modules_json TEXT NOT NULL DEFAULT '[]',
          enabled INTEGER NOT NULL DEFAULT 1,
          must_change_password INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        """)
        had_password_policy = _column_exists(
            _conn, "users", "must_change_password")
        _add_column(
            _conn,
            "users",
            "must_change_password",
            "INTEGER NOT NULL DEFAULT 0",
        )
        if not had_password_policy:
            # 哈希无法证明旧明文是否达到新策略。升级时对全部存量账号强制一次
            # 自助改密；新库/升级后新建账号按写入口显式决定是否需要首登改密。
            _conn.execute(
                "UPDATE users SET must_change_password=1 WHERE enabled=1"
            )
        for tbl in ("job", "task", "knowledge", "asset", "avatar_job", "schedule",
                    "account_profile"):
            _add_column(_conn, tbl, "tenant_id", "INTEGER NOT NULL DEFAULT 1")
        # v8.1 迁移:计费(点数钱包/套餐/流水)
        for col, typ in (("balance", "REAL NOT NULL DEFAULT 0"), ("plan", "TEXT"),
                         ("plan_expires", "REAL"), ("industries_json", "TEXT")):
            _add_column(_conn, "tenants", col, typ)
        _conn.executescript("""
        CREATE TABLE IF NOT EXISTS meeting(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL DEFAULT 1,
          question TEXT NOT NULL,
          constraints TEXT,
          acceptance_criteria TEXT,
          emp_idxs_json TEXT NOT NULL,
          messages_json TEXT NOT NULL DEFAULT '[]',
          actions_json TEXT,
          summary_md TEXT,
          status TEXT NOT NULL DEFAULT 'queued',
          phase TEXT NOT NULL DEFAULT 'queued',
          round_no INTEGER NOT NULL DEFAULT 0,
          decision TEXT,
          consensus_md TEXT,
          next_action TEXT,
          proposals_json TEXT NOT NULL DEFAULT '[]',
          validations_json TEXT NOT NULL DEFAULT '[]',
          execution_task_ids_json TEXT NOT NULL DEFAULT '[]',
          auto_execute INTEGER NOT NULL DEFAULT 1,
          intervention_count INTEGER NOT NULL DEFAULT 0,
          intervention_state TEXT,
          intervention_op_key TEXT,
          intervention_snapshot_json TEXT,
          intervention_question TEXT,
          intervention_started_at REAL,
          billing_status TEXT NOT NULL DEFAULT 'included',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS guests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone TEXT NOT NULL, company TEXT, name TEXT,
          used INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS account_apply(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          phone TEXT NOT NULL, name TEXT, company TEXT, note TEXT,
          status INTEGER NOT NULL DEFAULT 0,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS billing_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          delta REAL NOT NULL, balance REAL NOT NULL,
          reason TEXT, created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS billing_operation(
          op_key TEXT PRIMARY KEY,
          tenant_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          units INTEGER NOT NULL DEFAULT 1,
          points REAL NOT NULL DEFAULT 0,
          note TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_billing_operation_status
          ON billing_operation(status, created_at);
        CREATE TABLE IF NOT EXISTS wechat_draft_delivery(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER NOT NULL,
          request_hash TEXT NOT NULL,
          request_key TEXT NOT NULL,
          title TEXT,
          status TEXT NOT NULL DEFAULT 'pending_charge',
          billing_status TEXT NOT NULL DEFAULT 'pending',
          billing_points REAL,
          op_key TEXT,
          media_id TEXT,
          publish_log_id INTEGER,
          report_json TEXT,
          error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_wechat_delivery_request
          ON wechat_draft_delivery(tenant_id,job_id,request_hash,id DESC);
        CREATE INDEX IF NOT EXISTS idx_wechat_delivery_active_lookup
          ON wechat_draft_delivery(tenant_id,job_id,status);
        CREATE TABLE IF NOT EXISTS client_error(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          user_id INTEGER,
          route TEXT,
          kind TEXT,
          message TEXT,
          user_agent TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_client_error_tenant_created
          ON client_error(tenant_id, id DESC);
        CREATE TABLE IF NOT EXISTS funnel_event(
          day TEXT NOT NULL,
          event TEXT NOT NULL,
          dimension TEXT NOT NULL,
          tenant_id INTEGER NOT NULL DEFAULT 0,
          actor_hash TEXT NOT NULL,
          hits INTEGER NOT NULL DEFAULT 1,
          first_at REAL NOT NULL,
          last_at REAL NOT NULL,
          PRIMARY KEY(day,event,dimension,tenant_id,actor_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_funnel_event_day_event
          ON funnel_event(day,event);
        CREATE INDEX IF NOT EXISTS idx_funnel_event_tenant_day
          ON funnel_event(tenant_id,day,event);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_funnel_first_work
          ON funnel_event(event,tenant_id,actor_hash)
          WHERE event='first_job_submitted';
        CREATE TABLE IF NOT EXISTS censor_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          kind TEXT NOT NULL DEFAULT 'pre',      -- pre:发前审查 / retro:发后复盘
          platform TEXT, title TEXT,
          verdict TEXT, score REAL,
          issues_json TEXT, report TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS tv_job(       -- V25:图文转视频
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          job_id INTEGER,                        -- 关联内容工单(可空=独立成片)
          params_json TEXT NOT NULL,             -- title/body/script/images/voice_id/image_query
          script TEXT,
          status TEXT NOT NULL DEFAULT 'queued', -- pending_charge/queued/running/done/failed/cancelled
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          steps_json TEXT, video_file TEXT, error TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS publish_log(  -- V25:发布台账(自动复盘的钟表)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          platform TEXT, title TEXT,
          job_id INTEGER, url TEXT,
          source TEXT DEFAULT 'manual',          -- draft:草稿箱推送 / manual:手动登记
          published_at REAL,
          retro_json TEXT,                       -- {"1":{"due":ts,"state":"pending/notified/done"},"3":...,"7":...}
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS tool_job(     -- V25.4:工具箱后台作业(挂起可回看)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          kind TEXT NOT NULL,                    -- hot/pcal/warm/leads/bench
          params_json TEXT,
          status TEXT NOT NULL DEFAULT 'running',-- pending_charge/running/done/failed
          billing_status TEXT NOT NULL DEFAULT 'charged',
          billing_points REAL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          retry_started_at REAL,
          result_json TEXT, error TEXT, progress TEXT,
          created_at REAL, updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS pub_task(     -- V25:矩阵真发布队列(beta)
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id INTEGER NOT NULL,
          platform TEXT NOT NULL, account TEXT,
          payload_json TEXT NOT NULL,            -- title/body/images/video
          status TEXT NOT NULL DEFAULT 'queued', -- queued/running/done/failed
          retry_count INTEGER NOT NULL DEFAULT 0,
          submission_state TEXT NOT NULL DEFAULT 'not_submitted',
          submit_started_at REAL,
          log TEXT, created_at REAL, updated_at REAL
        );
        """)
        _add_column(_conn, "meeting", "actions_json", "TEXT")
        # 旧预览版索引按 request_hash 隔离，会允许同一工单换主题后并发双投。
        _conn.execute("DROP INDEX IF EXISTS idx_wechat_delivery_active")
        delivery_conflicts = list(_conn.execute(
            "SELECT tenant_id,job_id,COUNT(*) AS n "
            "FROM wechat_draft_delivery "
            "WHERE status IN "
            "('pending_charge','processing','submitting','submitted') "
            "GROUP BY tenant_id,job_id HAVING COUNT(*)>1"
        ))
        if not delivery_conflicts:
            _conn.execute(
                "CREATE UNIQUE INDEX idx_wechat_delivery_active "
                "ON wechat_draft_delivery(tenant_id,job_id) "
                "WHERE status IN "
                "('pending_charge','processing','submitting','submitted')"
            )
            _conn.execute(
                "DELETE FROM app_setting "
                "WHERE key='wechat_delivery_migration_conflicts'"
            )
        else:
            # 旧版本可能已把同一工单投递多次；submitting/submitted 不可盲退。
            # 保留全部锚点让服务可启动，并记录待人工对账清单；新 claim 仍会阻断再扣。
            payload = [
                {
                    "tenant_id": int(row["tenant_id"]),
                    "job_id": int(row["job_id"]),
                    "count": int(row["n"]),
                }
                for row in delivery_conflicts
            ]
            _conn.execute(
                "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                (
                    "wechat_delivery_migration_conflicts",
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )
        _add_column(_conn, "meeting", "billing_status", "TEXT NOT NULL DEFAULT 'included'")
        _add_column(_conn, "meeting", "billing_points", "REAL")
        for col in ("tenant_id INTEGER", "username TEXT"):  # v25.5:申请一键开通,记录开的账号
            name, declaration = col.split(" ", 1)
            _add_column(_conn, "account_apply", name, declaration)
        _add_column(_conn, "tool_job", "progress", "TEXT")
        _add_column(_conn, "tool_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "tool_job", "billing_points", "REAL")
        # 旧版图文转视频工单在升级前已经按旧逻辑结算。只在首次增加列时
        # 回填历史终态，避免历史失败工单因服务重启再次退款。
        had_tv_billing = _column_exists(_conn, "tv_job", "billing_status")
        _add_column(_conn, "tv_job", "billing_status",
                    "TEXT NOT NULL DEFAULT 'charged'")
        _add_column(_conn, "tv_job", "billing_points", "REAL")
        if not had_tv_billing:
            _conn.execute(
                "UPDATE tv_job SET billing_status='succeeded' "
                "WHERE status='done'"
            )
            _conn.execute(
                "UPDATE tv_job SET billing_status='refunded' "
                "WHERE status IN ('failed','cancelled')"
            )
        _add_column(_conn, "task", "summary_md", "TEXT")
        _add_column(_conn, "meeting", "summary_md", "TEXT")
        _add_column(_conn, "pub_task", "fail_json", "TEXT")
        had_pub_submission_state = _column_exists(
            _conn, "pub_task", "submission_state")
        _add_column(
            _conn, "pub_task", "submission_state",
            "TEXT NOT NULL DEFAULT 'legacy_unknown'",
        )
        _add_column(_conn, "pub_task", "submit_started_at", "REAL")
        if not had_pub_submission_state:
            # 老版本没有“点击发布前”的持久锚点，历史运行/失败记录无法证明
            # 平台是否已经收到提交。全部按不确定处理，禁止升级后盲目重发。
            _conn.execute(
                "UPDATE pub_task SET submission_state='submitted' "
                "WHERE status='done'"
            )
            _conn.execute(
                "UPDATE pub_task SET submission_state='not_submitted' "
                "WHERE status='queued'"
            )
        # v28:结果型 AI 会议。把开放式群聊收紧为「提案→验证→执行」状态机，
        # 共识与每轮结构化产出直接留在 SQLite，服务重启后也能继续交接。
        for col, typ in (
                ("phase", "TEXT NOT NULL DEFAULT 'queued'"),
                ("constraints", "TEXT"),
                ("acceptance_criteria", "TEXT"),
                ("round_no", "INTEGER NOT NULL DEFAULT 0"),
                ("decision", "TEXT"),
                ("consensus_md", "TEXT"),
                ("next_action", "TEXT"),
                ("proposals_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("validations_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("execution_task_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("auto_execute", "INTEGER NOT NULL DEFAULT 1"),
                ("intervention_count", "INTEGER NOT NULL DEFAULT 0"),
                ("intervention_state", "TEXT"),
                ("intervention_op_key", "TEXT"),
                ("intervention_snapshot_json", "TEXT"),
                ("intervention_question", "TEXT"),
                ("intervention_started_at", "REAL"),
        ):
            _add_column(_conn, "meeting", col, typ)
        # 自动执行任务必须可幂等恢复：同一会议同一个行动键只允许生成一次任务。
        for col, typ in (("source_meeting_id", "INTEGER"),
                         ("source_action_key", "TEXT"),
                         ("source_task_id", "INTEGER")):
            _add_column(_conn, "task", col, typ)
        # v42:核心业务对象进入可恢复回收站。删除只隐藏/终止，不再摧毁计费锚点
        # 或交付文件；重试次数也持久化，避免失败重试被并发提交多次。
        # (人设档案与资产库后续也纳入同一回收站,老板误删可自行找回。)
        for table in ("job", "task", "knowledge", "avatar_job",
                      "account_profile", "asset"):
            for col, typ in (
                ("deleted_at", "REAL"),
                ("deleted_by", "INTEGER"),
                ("delete_reason", "TEXT"),
            ):
                _add_column(_conn, table, col, typ)
        for table in (
                "job", "task", "meeting", "avatar_job", "tv_job",
                "tool_job", "pub_task"):
            _add_column(_conn, table, "retry_count",
                        "INTEGER NOT NULL DEFAULT 0")
        _add_column(_conn, "tool_job", "retry_started_at", "REAL")
        _conn.execute("DROP INDEX IF EXISTS idx_task_meeting_action")
        _conn.execute("CREATE UNIQUE INDEX idx_task_meeting_action "
                      "ON task(source_meeting_id, source_action_key) "
                      "WHERE source_meeting_id IS NOT NULL "
                      "AND source_action_key IS NOT NULL AND deleted_at IS NULL")
        _add_column(_conn, "job", "source_schedule_id", "INTEGER")
        _add_column(_conn, "job", "source_schedule_occurrence", "TEXT")
        _conn.execute("DROP INDEX IF EXISTS idx_job_schedule_occurrence")
        _conn.execute(
            "CREATE UNIQUE INDEX idx_job_schedule_occurrence "
            "ON job(source_schedule_id, source_schedule_occurrence) "
            "WHERE source_schedule_id IS NOT NULL "
            "AND source_schedule_occurrence IS NOT NULL AND deleted_at IS NULL"
        )
        # 任务中心的高频读取索引：按租户看最新、按状态筛选、按员工追踪。
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_created "
                      "ON task(tenant_id, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_status_created "
                      "ON task(tenant_id, status, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_task_tenant_emp_created "
                      "ON task(tenant_id, emp_idx, id DESC)")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_job_tenant_created "
                      "ON job(tenant_id, id DESC)")
        for table in ("job", "task", "knowledge", "avatar_job",
                      "account_profile", "asset"):
            _conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant_deleted "
                f"ON {table}(tenant_id, deleted_at, id DESC)"
            )
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notification_tenant_unread "
            "ON notification(tenant_id, read_at, id DESC)"
        )
        # 副账号协作可见:工单/任务记录发起人,工位记录拍板人。老板事后可追溯
        # "这单是哪个副账号开的、这一站是谁批的";历史数据没有操作人,留空即可。
        _add_column(_conn, "job", "created_by", "INTEGER")
        _add_column(_conn, "task", "created_by", "INTEGER")
        _add_column(_conn, "station_run", "reviewed_by", "INTEGER")
        # v47:会话签名密钥只能由 root-owned systemd EnvironmentFile 注入。
        # 清除旧版写入业务库的全局密钥，避免只读数据库泄漏演变为会话伪造。
        _conn.execute(
            "DELETE FROM app_setting WHERE key='session_secret'"
        )
        # 旧会议没有 V28 决策字段；只做展示态回填，绝不把历史会议重新跑一遍。
        _conn.execute("UPDATE meeting SET phase='completed' "
                      "WHERE status='done' AND phase='queued' AND decision IS NULL")
        _conn.execute("UPDATE meeting SET phase='failed' "
                      "WHERE status='failed' AND phase='queued'")
        _validate_migrated_database(_conn)
        _conn.execute(
            "INSERT OR IGNORE INTO schema_version(version,name,applied_at) VALUES(?,?,?)",
            (
                LATEST_SCHEMA_VERSION,
                "environment-only-session-secret-migration",
                time.time(),
            ),
        )
        _conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        _conn.commit()
    return _conn


def _close_thread_connection():
    connection = getattr(_thread, "connection", None)
    if connection is not None and connection is not _conn:
        try:
            connection.close()
        except sqlite3.Error:
            pass
        _all_connections.discard(connection)
    _thread.connection = None
    _thread.generation = -1
    _thread.db_path = None
    _thread.atomic_depth = 0


def _close_all_connections():
    """Close registered connections during test DB swaps and interpreter exit."""
    for connection in list(_all_connections):
        try:
            connection.close()
        except sqlite3.Error:
            pass
    _all_connections.clear()


def conn():
    """Return a connection owned by the current worker thread.

    Schema initialization still happens once on the process anchor.  Afterwards
    each request-worker thread gets an independent SQLite connection configured
    for WAL and busy waiting.  This removes the former process-wide ``RLock``
    bottleneck while keeping ``db.atomic()`` bound to exactly one connection.
    """
    global _conn, _conn_path, _connection_generation

    path = os.path.abspath(DB_PATH)
    local = getattr(_thread, "connection", None)
    if (
        _conn is not None
        and _conn_path == path
        and local is not None
        and getattr(_thread, "generation", -1) == _connection_generation
        and getattr(_thread, "db_path", None) == path
    ):
        return local

    if (
        (_conn is None or _conn_path not in (None, path))
        and threading.current_thread().name.startswith("dbio")
    ):
        # 异步写池线程绝不执行代际切换:走到这里说明库已被换走(测试/维护),
        # 这笔快照写属于已死的库,丢弃即正确语义。若允许池线程做切换,它会
        # 与主线程互相关闭对方正在使用的连接——那是段错误,不是异常。
        raise StaleWriteError(path)

    # 数据库代际切换(测试/维护工具换 DB_PATH)前,先在锁外排空异步写池:
    # 在途任务可能正拿着旧库连接执行,任何跨线程 close 都会段错误。
    # 必须在 _init_lock 外等——池任务走慢路径时要拿这把锁,锁内等待会死锁。
    if _conn is not None and _conn_path not in (None, path):
        _shutdown_async_pool(wait=True)

    with _init_lock:
        # Tests/maintenance tools intentionally switch DB_PATH after closing the
        # anchor.  Treat either signal as a new database generation.
        if _conn is None or _conn_path not in (None, path):
            _close_thread_connection()
            # 不做跨线程关闭:其它线程的旧代连接由各自线程在下次 conn() 的
            # 代际检查里自行关闭(_close_thread_connection),这里只关自己的
            # 与锚点。跨线程 close 一个可能正在执行的连接是段错误之源。
            if _conn is not None:
                try:
                    _conn.close()
                except sqlite3.Error:
                    pass
                _conn = None
            try:
                anchor = _initialize_anchor()
            except Exception:
                if _conn is not None:
                    try:
                        _conn.close()
                    except sqlite3.Error:
                        pass
                    _all_connections.discard(_conn)
                    _conn = None
                _conn_path = None
                raise
            _conn_path = path
            _connection_generation += 1
            if threading.current_thread().name.startswith("dbio"):
                # 异步写池线程绝不领养锚点:锚点(_conn)会被测试/维护工具从主线程
                # 直接 close,若此刻池线程正用它执行就是段错误。池线程一律用
                # 自己的独立连接,让"谁的连接谁关"始终成立。
                connection = sqlite3.connect(
                    path, timeout=30, check_same_thread=False
                )
                _all_connections.add(connection)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=30000")
                connection.execute("PRAGMA synchronous=NORMAL")
                _thread.connection = connection
                _thread.generation = _connection_generation
                _thread.db_path = path
                _thread.atomic_depth = 0
                return connection
            _thread.connection = anchor
            _thread.generation = _connection_generation
            _thread.db_path = path
            _thread.atomic_depth = 0
            return anchor

        if _conn_path is None:
            # Compatibility with an anchor created before this wrapper first ran.
            _conn_path = path
            _connection_generation += 1

        local = getattr(_thread, "connection", None)
        if (
            local is not None
            and getattr(_thread, "generation", -1) != _connection_generation
        ):
            _close_thread_connection()

        connection = sqlite3.connect(
            path, timeout=30, check_same_thread=False
        )
        _all_connections.add(connection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=NORMAL")
        _thread.connection = connection
        _thread.generation = _connection_generation
        _thread.db_path = path
        _thread.atomic_depth = 0
        return connection


# ---------------- 全局设置(V4) ----------------
def get_setting(key, default=None):
    r = one("SELECT value FROM app_setting WHERE key=?", (key,))
    return r["value"] if r and r["value"] is not None else default


def set_setting(key, value):
    connection = conn()
    connection.execute(
        "INSERT INTO app_setting(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        "updated_at=excluded.updated_at",
        (key, value, time.time()),
    )
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()


def q(sql, args=()):
    connection = conn()
    cur = connection.execute(sql, args)
    rows = [dict(r) for r in cur.fetchall()]
    # SELECT/PRAGMA 等只读查询绝不能替外层 db.atomic() 提前提交。
    # 写语句没有结果列，仍保留历史 q("DELETE ...") 的自动提交语义.
    if cur.description is None and not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return rows


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def insert(table, data):
    data = dict(data)
    data.setdefault("created_at", time.time())
    data.setdefault("updated_at", time.time())
    cols = ",".join(data)
    ph = ",".join("?" * len(data))
    connection = conn()
    cur = connection.execute(
        f"INSERT INTO {table}({cols}) VALUES({ph})", list(data.values()))
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return cur.lastrowid


def update(table, id_, data):
    data = dict(data)
    data["updated_at"] = time.time()
    sets = ",".join(f"{k}=?" for k in data)
    connection = conn()
    connection.execute(
        f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [id_])
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()


from contextlib import contextmanager


@contextmanager
def atomic():
    """Run a transaction on the current worker's private connection.

    Nested callers use a savepoint.  Helper calls such as ``db.update`` detect
    the active depth and never commit the outer transaction early.
    """
    c = conn()
    depth = int(getattr(_thread, "atomic_depth", 0) or 0)
    savepoint = f"paihuo_sp_{depth}"
    if depth == 0:
        c.execute("BEGIN IMMEDIATE")
    else:
        c.execute(f"SAVEPOINT {savepoint}")
    _thread.atomic_depth = depth + 1
    try:
        yield c
        if depth == 0:
            c.commit()
        else:
            c.execute(f"RELEASE SAVEPOINT {savepoint}")
    except BaseException:
        if depth == 0:
            c.rollback()
        else:
            c.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            c.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    finally:
        _thread.atomic_depth = depth


def execute(sql, args=()) -> int:
    """写语句,返回受影响行数(条件扣费等原子操作用)."""
    connection = conn()
    cur = connection.execute(sql, args)
    if not getattr(_thread, "atomic_depth", 0):
        connection.commit()
    return cur.rowcount


def jloads(s, default=None):
    if not s:
        return default if default is not None else {}
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default if default is not None else {}


# ---------------- 异步门面 ----------------
# SQLite 是同步库,busy_timeout=30s。事件循环上的协程(引擎流水线、SSE 周边、
# async 路由)直接调 db 时,任何一次写锁等待都会把整个循环冻住——所有租户的
# SSE、所有请求一起停。异步侧必须经由这里的门面把 db 调用卸载到专用线程池:
# 连接本就是线程本地的(见 conn()),每个池线程各持一条连接,WAL 下并发安全。
#
# 池子刻意有界:SQLite 同时只有一个写者,更多线程只会排队占内存;4 条足够
# 覆盖「读 + 写 + 看门狗 + 后台任务」的并发形态。
_async_pool = None
_async_pool_lock = threading.Lock()


def _pool():
    global _async_pool
    if _async_pool is None:
        with _async_pool_lock:
            if _async_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                _async_pool = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="dbio")
    return _async_pool


async def arun(fn, *args, **kwargs):
    """在 db 线程池里执行任意同步函数(含整段 with atomic() 的事务体)。

    这是异步侧访问数据库的唯一正道:事务必须整体进池(不能在持有 BEGIN 的
    情况下 await),所以传入的是完整的同步函数而非单条语句。
    """
    import asyncio
    import functools
    loop = asyncio.get_running_loop()
    call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    return await loop.run_in_executor(_pool(), call)


_pending_writes: list = []
_pending_lock = threading.Lock()


def submit_write(fn, *args, **kwargs):
    """不等待结果地把一次写操作投给 db 线程池(节流型进度落库用)。

    只适用于「丢了也无碍、下次会整体重写」的快照类写入;需要结果或需要
    顺序保证的写仍然要走 arun。异常在池线程里记日志,不向上冒泡。
    需要「先前的异步写都已落地」时,用 adrain() 冲刷。
    """
    import functools
    import logging
    call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
    submitted_path = os.path.abspath(DB_PATH)
    submitted_generation = _connection_generation

    def _guarded():
        try:
            # 数据库在提交后被切换(测试/维护)时,这笔写属于已死的库,直接丢弃;
            # 快照类写入的语义本就允许丢帧,写进错误的库反而是事故。
            # 同时比对代际:路径字符串在切换瞬间可能仍相同(TOCTOU),代际不会。
            if (os.path.abspath(DB_PATH) != submitted_path
                    or _connection_generation != submitted_generation):
                return
            call()
        except StaleWriteError:
            pass       # conn() 在执行中发现库被切换,静默丢弃这笔快照写
        except sqlite3.ProgrammingError:
            pass       # 连接在执行间隙被回收(库已切换),同样按过期快照丢弃
        except Exception as exc:
            logging.getLogger("db").warning(
                "submit_write failed error_type=%s", type(exc).__name__)

    future = _pool().submit(_guarded)
    with _pending_lock:
        _pending_writes.append(future)
        if len(_pending_writes) > 64:      # 顺手清掉已完成的,防列表无限增长
            _pending_writes[:] = [f for f in _pending_writes if not f.done()]


async def adrain():
    """等待此刻之前提交的全部 submit_write 落地(收尾处保证「返回即持久」)。"""
    import asyncio
    with _pending_lock:
        snapshot = [f for f in _pending_writes if not f.done()]
        _pending_writes[:] = snapshot
    for future in snapshot:
        await asyncio.wrap_future(future)


async def aq(sql, args=()):
    return await arun(q, sql, args)


async def aone(sql, args=()):
    return await arun(one, sql, args)


async def ainsert(table, data):
    return await arun(insert, table, data)


async def aupdate(table, id_, data):
    return await arun(update, table, id_, data)


async def aexecute(sql, args=()):
    return await arun(execute, sql, args)


async def aget_setting(key, default=None):
    return await arun(get_setting, key, default)


async def aset_setting(key, value):
    return await arun(set_setting, key, value)


def _shutdown_async_pool(wait: bool = True):
    """停掉异步写池。默认等在途任务收尾——不等就关连接会段错误。"""
    global _async_pool
    pool = None
    with _async_pool_lock:
        pool = _async_pool
        _async_pool = None
    if pool is not None:
        pool.shutdown(wait=wait)


def _shutdown_all():
    # 顺序是安全性的一部分:必须先等池里的在途任务结束,再关闭连接。
    _shutdown_async_pool(wait=True)
    _close_all_connections()


atexit.register(_shutdown_all)
