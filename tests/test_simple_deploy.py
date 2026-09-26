"""简易部署通道(deploy/simple)的行为测试。

不需要 root、不需要真的 systemd：在临时目录里模拟 releases/current/数据目录/密钥文件，
用 PATH 里的小脚本替身模拟 systemctl/curl（记录调用顺序、按线上版本返回健康状态），
真实执行 deploy.sh / rollback.sh：建虚拟环境、调用 deploy/backup_db.py 备份、
用新代码迁移临时 SQLite、切换软链接、失败自动回滚并恢复停服快照。
"""
from __future__ import annotations

import getpass
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY = REPO / "deploy" / "simple" / "deploy.sh"
ROLLBACK = REPO / "deploy" / "simple" / "rollback.sh"
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
LATEST = int(
    next(
        line.split("=")[1]
        for line in (REPO / "app" / "db.py").read_text(encoding="utf-8").splitlines()
        if line.startswith("LATEST_SCHEMA_VERSION")
    )
)

SYSTEMCTL_STUB = """#!/bin/sh
S="$STUB_STATE"
echo "$* current=$(readlink "$PAIHUO_BASE/current")" >> "$S/calls.log"
for a in "$@"; do
  [ "$a" = "contentcrew.service" ] && exit 3
done
case "$1" in
  is-active) [ -e "$S/active" ]; exit $? ;;
  stop) rm -f "$S/active" ;;
  start|restart)
    touch "$S/active"
    # 模拟“有问题的新版本”一启动就往库里写了数据
    if [ -e "$PAIHUO_BASE/current/BROKEN" ]; then
      "$STUB_PYTHON" -c "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\\"UPDATE app_setting SET value='after' WHERE key='simple_deploy_marker'\\"); c.commit()" "$PAIHUO_DB_PATH"
    fi ;;
  show) echo 0 ;;
esac
exit 0
"""

CURL_STUB = """#!/bin/sh
if [ ! -e "$STUB_STATE/active" ]; then printf 000; exit 7; fi
if [ -e "$PAIHUO_BASE/current/BROKEN" ]; then printf 503; exit 0; fi
printf 200
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _tree(root: Path) -> list[str]:
    """目录树快照（不含替身脚本自己的调用记录；只读打开 WAL 库时 SQLite 自己会建
    -wal/-shm 边车文件，不算改动）。"""
    return sorted(
        str(p.relative_to(root)) for p in root.rglob("*")
        if "stub-state" not in p.relative_to(root).parts
        and not p.name.endswith(("-wal", "-shm"))
    )


class SimpleDeployBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="simple-deploy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.base = self.tmp / "srv"
        self.releases = self.base / "releases"
        self.current = self.base / "current"
        self.data = self.tmp / "data"
        self.db = self.data / "contentcrew.db"
        self.backups = self.tmp / "backups"
        self.etc = self.tmp / "etc"
        self.state = self.tmp / "stub-state"
        self.bin = self.tmp / "stub-bin"
        for directory in (self.releases, self.data, self.etc, self.state, self.bin):
            directory.mkdir(parents=True)
        self.env_file = self.etc / "paihuo.env"
        self.write_env(bootstrap=True)
        self.unit_file = self.etc / "paihuo.service"
        self.unit_file.write_text(
            f"Environment=CONTENTCREW_DB_PATH={self.db}\n", encoding="utf-8"
        )
        _write_exec(self.bin / "systemctl", SYSTEMCTL_STUB)
        _write_exec(self.bin / "curl", CURL_STUB)
        _write_exec(self.bin / "journalctl", "#!/bin/sh\nexit 0\n")
        _write_exec(self.bin / "pgrep", "#!/bin/sh\nexit 1\n")
        self.env = dict(os.environ)
        self.env.update(
            PATH=f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            PAIHUO_BASE=str(self.base),
            PAIHUO_DATA_DIR=str(self.data),
            PAIHUO_DB_PATH=str(self.db),
            PAIHUO_BACKUP_DIR=str(self.backups),
            PAIHUO_ENV_FILE=str(self.env_file),
            PAIHUO_UNIT_FILE=str(self.unit_file),
            PAIHUO_PUB_DIR=str(self.tmp / "pub"),
            PAIHUO_APP_USER=getpass.getuser(),
            PAIHUO_PORT=str(_free_port()),
            PAIHUO_PYTHON=BASE_PYTHON,
            PAIHUO_VENV_SYSTEM_SITE="1",
            PAIHUO_SMOKE_TIMEOUT="2",
            PAIHUO_ALLOW_NONROOT="1",
            STUB_STATE=str(self.state),
            STUB_PYTHON=sys.executable,
        )

    def write_env(self, *, bootstrap: bool, config_key: bool = True, mode: int = 0o600):
        import secrets

        lines = [f"CONTENTCREW_SESSION_SECRET={secrets.token_urlsafe(48)}"]
        if config_key:
            lines.append(f"CONTENTCREW_CONFIG_KEY={secrets.token_urlsafe(48)}")
        if bootstrap:
            lines.append("CONTENTCREW_BOOTSTRAP_PASSWORD=Boss-pass-2026")
        self.env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.env_file.chmod(mode)

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *args],
            cwd=self.tmp,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
        )

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def make_source(self, name: str, *, broken: bool = False) -> Path:
        """把仓库里受版本管理的文件复制成一个“解压好的代码目录”；依赖清单置空（不联网）。"""
        src = self.tmp / name
        files = subprocess.run(
            ["git", "-c", f"safe.directory={REPO}", "-C", str(REPO), "ls-files", "-z",
             "app", "deploy", "data", "static", "run.sh"],
            check=True, capture_output=True,
        ).stdout.decode().split("\0")
        for rel in filter(None, files):
            target = src / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, target)
        (src / "requirements.lock.txt").write_text("# 测试：不装第三方包\n", encoding="utf-8")
        (src / "run.sh").chmod(0o755)
        if broken:
            (src / "BROKEN").write_text("smoke should fail\n", encoding="utf-8")
        return src

    def fake_release(self, release_id: str, schema: int) -> Path:
        release = self.releases / release_id
        (release / "app").mkdir(parents=True)
        (release / "app" / "db.py").write_text(
            f"LATEST_SCHEMA_VERSION = {schema}\n", encoding="utf-8"
        )
        (release / "venv" / "bin").mkdir(parents=True)
        _write_exec(release / "venv" / "bin" / "python", "#!/bin/sh\nexit 0\n")
        _write_exec(release / "run.sh", "#!/bin/sh\nexit 0\n")
        return release

    def make_db(self, version: int) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE app_setting(key TEXT PRIMARY KEY, value TEXT)")
            conn.execute(f"PRAGMA user_version={version}")


class DryRunTest(SimpleDeployBase):
    def setUp(self):
        super().setUp()
        self.fake_release("20260101-000000-old", 57)
        self.current.symlink_to("releases/20260101-000000-old")
        self.make_db(57)

    def test_dry_run_passes_preflight_prints_plan_and_changes_nothing(self):
        before = _tree(self.tmp)
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO), "--ref", "HEAD")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        out = result.stdout
        for marker in ("[1/6] 预检", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]",
                       "预检通过", f"v57 → v{LATEST}", "演练结束"):
            self.assertIn(marker, out)
        self.assertIn("python -m deploy.backup_db", result.stderr)
        self.assertEqual(_tree(self.tmp), before, "演练不能创建/删除任何文件")
        self.assertEqual(os.readlink(self.current), "releases/20260101-000000-old")
        # 只查询过服务状态，没有停/起/重启
        self.assertTrue(self.calls())
        self.assertTrue(all(c.startswith(("is-active", "show")) for c in self.calls()), self.calls())

    def test_missing_config_key_fails_preflight_without_changes(self):
        self.write_env(bootstrap=False, config_key=False)
        before = _tree(self.tmp)
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONTENTCREW_CONFIG_KEY", result.stderr)
        self.assertIn("预检有", result.stderr)
        self.assertEqual(_tree(self.tmp), before)

    def test_world_readable_env_file_fails_preflight(self):
        self.write_env(bootstrap=False, mode=0o644)
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("chmod 600", result.stderr)

    def test_database_newer_than_code_is_refused(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute(f"PRAGMA user_version={LATEST + 1}")
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("还新", result.stderr)

    def test_multi_worker_env_is_refused(self):
        with self.env_file.open("a", encoding="utf-8") as handle:
            handle.write("WEB_CONCURRENCY=4\n")
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("单进程", result.stderr)

    def test_first_deploy_requires_bootstrap_password(self):
        self.db.unlink()
        self.write_env(bootstrap=False)
        result = self.run_script(DEPLOY, "--dry-run", "--repo", str(REPO))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONTENTCREW_BOOTSTRAP_PASSWORD", result.stderr)


class DeployFlowTest(SimpleDeployBase):
    def deploy_first(self) -> str:
        result = self.run_script(DEPLOY, "--source-dir", str(self.make_source("src1")))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        release_id = os.readlink(self.current).split("/", 1)[1]
        release = self.releases / release_id
        self.assertEqual(_user_version(self.db), LATEST)
        self.assertTrue((release / "data").is_symlink())
        self.assertEqual((release / "data").resolve(), self.data.resolve())
        self.assertTrue((release / "config" / "departments").is_dir())
        self.assertTrue((release / "venv" / "bin" / "python").exists())
        # 迁移锁/实例锁必须是 600，否则服务起不来
        for suffix in (".migration.lock", ".instance.lock"):
            lock = Path(f"{self.db}{suffix}")
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        meta = (release / ".paihuo-release").read_text(encoding="utf-8")
        self.assertIn(f"SCHEMA_AFTER={LATEST}", meta)
        self.assertIn("PREVIOUS_RELEASE=\n", meta)
        self.assertTrue(any(c.startswith("restart paihuo") for c in self.calls()))
        return release_id

    def test_failed_smoke_restores_snapshot_then_switches_back(self):
        first = self.deploy_first()
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO app_setting(key, value) VALUES('simple_deploy_marker', 'before')"
            )
        (self.data / "assets").mkdir(exist_ok=True)
        (self.data / "assets" / "a.jpg").write_bytes(b"jpg")
        (self.state / "calls.log").unlink()

        result = self.run_script(DEPLOY, "--source-dir", str(self.make_source("src2", broken=True)))
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("冒烟检查不通过", output)
        self.assertIn("先恢复数据库，再切回旧代码", output)
        # 线上回到上一个版本，失败的新版本目录被清掉
        self.assertEqual(os.readlink(self.current), f"releases/{first}")
        self.assertEqual(sorted(p.name for p in self.releases.iterdir()), [first])
        # 新版本启动后写进库的数据被停服快照覆盖回去；被换下的库留在隔离目录
        with sqlite3.connect(self.db) as conn:
            marker = conn.execute(
                "SELECT value FROM app_setting WHERE key='simple_deploy_marker'"
            ).fetchone()[0]
        self.assertEqual(marker, "before")
        quarantines = list(self.data.glob("rollback-quarantine-*"))
        self.assertEqual(len(quarantines), 1)
        with sqlite3.connect(quarantines[0] / "contentcrew.db") as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM app_setting WHERE key='simple_deploy_marker'"
                ).fetchone()[0],
                "after",
            )
        # 在线备份 + 停服快照两份，素材也做了快照
        self.assertEqual(len(list(self.backups.glob("db-*.db"))), 2)
        self.assertTrue(list((self.backups / "assets").glob("assets-*/assets/a.jpg")))
        # 调用顺序：停旧服务 → 起新版本(失败) → 停 → 起旧版本；最后一次启动时 current 已切回
        verbs = [c.split()[0] for c in self.calls() if not c.startswith(("is-active", "show"))]
        self.assertEqual(verbs, ["stop", "restart", "stop", "restart"])
        last_start = [c for c in self.calls() if c.startswith("restart")][-1]
        self.assertIn(f"current=releases/{first}", last_start)
        history = (self.base / "deploy-history.log").read_text(encoding="utf-8")
        self.assertIn(f"rolled-back-to={first}", history)

    def test_manual_rollback_to_older_schema_needs_backup(self):
        first = self.deploy_first()
        old = self.fake_release("20250101-000000-r0", 57)
        os.utime(old, (1, 1))
        backup = self.backups / "db-2025-01-01T000000Z.db"
        self.backups.mkdir(exist_ok=True)
        with sqlite3.connect(backup) as conn:
            conn.execute("CREATE TABLE app_setting(key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("PRAGMA user_version=57")

        listing = self.run_script(ROLLBACK, "--list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(f"* {first}", listing.stdout)
        self.assertIn("20250101-000000-r0", listing.stdout)

        # 没有发布记录的上一个版本按时间找到 r0；库比它新，不带备份就拒绝
        refused = self.run_script(ROLLBACK, "--dry-run")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("回滚到: 20250101-000000-r0", refused.stdout)
        self.assertIn("--restore-deploy-snapshot", refused.stderr)

        before = _tree(self.tmp)
        planned = self.run_script(ROLLBACK, "--dry-run", "--restore-backup", str(backup))
        self.assertEqual(planned.returncode, 0, planned.stdout + planned.stderr)
        self.assertEqual(_tree(self.tmp), before)

        done = self.run_script(ROLLBACK, "--restore-backup", str(backup))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(os.readlink(self.current), "releases/20250101-000000-r0")
        self.assertEqual(_user_version(self.db), 57)
        quarantine = next(self.data.glob("rollback-quarantine-*"))
        self.assertEqual(_user_version(quarantine / "contentcrew.db"), LATEST)
        restart_calls = [c for c in self.calls() if c.startswith(("stop", "start"))]
        self.assertEqual(restart_calls[-2].split()[0], "stop")
        self.assertIn("current=releases/20250101-000000-r0", restart_calls[-1])
        self.assertIn("rollback-ok", (self.base / "deploy-history.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
