"""db_backup.sh 备份脚本行为回归测试（Task 8 Fix 3）。

纯 shell 脚本，测试直接以 bash 调用（临时目录 + sqlite3 CLI 造源库），
断言：
1. 备份写入 XIAODA_BACKUP_DIR，文件 600 / 目录 700；
2. KIOXIA_DATA_DIR 切换后源自动跟随（不再硬编码旧位置）；
3. 键解析顺序镜像 config_paths.py：环境变量优先，项目根 .env 兜底，
   最后回退 ~/.ai-agent/data（备份根默认 ~/.ai-agent/backups）；
4. 7 天清理只删本脚本管理的时间戳目录，不触碰目录内其他文件/外部目录。

隔离：脚本从自身位置推导项目根 .env，测试把脚本复制进临时"项目根"，
避免读写真实仓库 .env 与真实数据目录。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "db_backup.sh"
_MISSING = [t for t in ("sqlite3", "bash") if shutil.which(t) is None]
pytestmark = pytest.mark.skipif(bool(_MISSING), reason=f"缺少依赖命令: {_MISSING}")


def _make_db(path: Path, marker: str) -> None:
    """造一个真实 sqlite 源库（含一行标记数据，供快照内容比对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS t (k TEXT)")
        conn.execute("DELETE FROM t")
        conn.execute("INSERT INTO t VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()


def _make_source(data_root: Path, marker: str) -> None:
    _make_db(data_root / "db" / "agent.db", marker)
    _make_db(data_root / "db" / "agent_vec.db", marker)


def _db_marker(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT k FROM t").fetchone()[0]
    finally:
        conn.close()


def _run_script(script: Path, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    # 剥离宿主环境可能存在的同名变量，保证测试解析路径完全受控
    for k in ("KIOXIA_DATA_DIR", "XIAODA_BACKUP_DIR"):
        env.pop(k, None)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=60,
    )


def _only_ts_dir(backup_root: Path) -> Path:
    ts_dirs = [d for d in backup_root.iterdir() if d.is_dir()]
    assert len(ts_dirs) == 1, f"应只有一个时间戳备份目录，实际: {ts_dirs}"
    return ts_dirs[0]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """把脚本复制进临时"项目根"：隔离真实仓库 .env（可能含 KIOXIA_DATA_DIR）。"""
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, proj / "scripts" / "db_backup.sh")
    return proj


class TestDbBackupScript:
    def test_backup_to_xiaoda_backup_dir_with_tight_permissions(self, project, tmp_path):
        """备份写入 XIAODA_BACKUP_DIR；文件 600、目录 700、内容与源一致。"""
        data_root = tmp_path / "data"
        _make_source(data_root, "marker-a")
        backup_root = tmp_path / "backups"
        backup_root.mkdir(mode=0o755)  # 预存在宽松权限，脚本必须收紧

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(data_root),
            "XIAODA_BACKUP_DIR": str(backup_root),
        })
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

        assert stat.S_IMODE(backup_root.stat().st_mode) == 0o700, "备份根目录必须 700"
        ts_dir = _only_ts_dir(backup_root)
        assert stat.S_IMODE(ts_dir.stat().st_mode) == 0o700, "备份目录必须 700"
        assert _db_marker(ts_dir / "agent.db") == "marker-a"
        assert _db_marker(ts_dir / "agent_vec.db") == "marker-a"
        for name in ("agent.db", "agent_vec.db"):
            f = ts_dir / name
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f"{name} 必须 600"
        assert "不同设备" in r.stdout or "同一设备" in r.stdout, "必须注明备份与源的设备关系"

    def test_source_follows_kioxia_data_dir_switch(self, project, tmp_path):
        """KIOXIA_DATA_DIR 改指新盘后，源跟随新位置（旧位置数据不进备份）。"""
        old_root = tmp_path / "old-usb"
        new_root = tmp_path / "new-usb"
        _make_source(old_root, "old-marker")
        _make_source(new_root, "new-marker")
        backup_root = tmp_path / "backups"

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(new_root),
            "XIAODA_BACKUP_DIR": str(backup_root),
        })
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

        ts_dir = _only_ts_dir(backup_root)
        assert _db_marker(ts_dir / "agent.db") == "new-marker", "备份必须跟随新 KIOXIA_DATA_DIR"
        assert f"{old_root}" not in r.stdout, "不得再备份旧挂载位置"

    def test_env_var_overrides_dotenv(self, project, tmp_path):
        """环境变量优先于 .env（镜像 config_paths.py override=False 语义）。"""
        env_data = tmp_path / "envdata"
        envvar_data = tmp_path / "envvardata"
        _make_source(env_data, "from-dotenv")
        _make_source(envvar_data, "from-envvar")
        (project / ".env").write_text(
            "# 测试 .env\n"
            f"KIOXIA_DATA_DIR={env_data}\n"
            "KIOXIA_DATA_DIR=should-be-ignored\n",
            encoding="utf-8",
        )
        backup_root = tmp_path / "backups"

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(envvar_data),
            "XIAODA_BACKUP_DIR": str(backup_root),
        })
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        assert _db_marker(_only_ts_dir(backup_root) / "agent.db") == "from-envvar"

    def test_dotenv_fallback_when_env_unset(self, project, tmp_path):
        """环境变量未设时回退项目根 .env（含 export 前缀与引号剥壳）。"""
        env_data = tmp_path / "envdata"
        _make_source(env_data, "from-dotenv")
        (project / ".env").write_text(
            f"KIOXIA_DATA_DIR=\"{env_data}\"\n"
            f"export XIAODA_BACKUP_DIR='{tmp_path / 'envbackups'}'\n",
            encoding="utf-8",
        )

        r = _run_script(project / "scripts" / "db_backup.sh")
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        ts_dir = _only_ts_dir(tmp_path / "envbackups")
        assert _db_marker(ts_dir / "agent.db") == "from-dotenv"

    def test_default_backup_dir_under_home(self, project, tmp_path, monkeypatch):
        """XIAODA_BACKUP_DIR 未设且无 .env → 备份根默认 ~/.ai-agent/backups。"""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        data_root = tmp_path / "data"
        _make_source(data_root, "default-home")

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(data_root),
        })
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
        backup_root = fake_home / ".ai-agent" / "backups"
        assert _db_marker(_only_ts_dir(backup_root) / "agent.db") == "default-home"

    def test_retention_cleanup_scoped_to_managed_dirs(self, project, tmp_path):
        """7 天清理只删 DST_ROOT 下超期 20* 时间戳目录，其余一律不动。"""
        data_root = tmp_path / "data"
        _make_source(data_root, "retention")
        backup_root = tmp_path / "backups"
        backup_root.mkdir()

        old_ts = backup_root / "20200101_000000"
        old_ts.mkdir()
        (old_ts / "agent.db").write_text("stale", encoding="utf-8")
        stale_t = time.time() - 9 * 86400  # 超过 KEEP_DAYS=7 的 -mtime +7 窗口
        os.utime(old_ts, (stale_t, stale_t))
        recent_ts = backup_root / "20990101_000000"
        recent_ts.mkdir()
        keepme = backup_root / "keepme"
        keepme.mkdir()
        loose_file = backup_root / "agent_latest.db"
        loose_file.write_text("manual export", encoding="utf-8")
        loose_mode = stat.S_IMODE(loose_file.stat().st_mode)  # 运行前基线（跨 umask 稳定）
        sibling_ts = tmp_path / "elsewhere" / "20200101_000000"
        sibling_ts.mkdir(parents=True)

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(data_root),
            "XIAODA_BACKUP_DIR": str(backup_root),
        })
        assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"

        assert not old_ts.exists(), "超期时间戳目录应被清理"
        assert recent_ts.exists(), "未超期目录不得清理"
        assert keepme.exists(), "非本脚本管理的目录不得清理"
        assert loose_file.exists(), "清理不得触碰目录内其他文件"
        assert stat.S_IMODE(loose_file.stat().st_mode) == loose_mode, "清理不得改动其他文件权限"
        assert sibling_ts.exists(), "清理不得越出备份根（外部目录不受影响）"

    def test_corrupt_source_fails_loudly(self, project, tmp_path):
        """源库损坏 → 脚本非零退出，不静默产出伪成功备份。"""
        data_root = tmp_path / "data"
        _make_source(data_root, "corrupt")
        # 损坏其中一个源库：sqlite3 .backup 直接报错，set -e 使脚本非零退出
        (data_root / "db" / "agent_vec.db").write_text("not a sqlite file", encoding="utf-8")
        backup_root = tmp_path / "backups"

        r = _run_script(project / "scripts" / "db_backup.sh", {
            "KIOXIA_DATA_DIR": str(data_root),
            "XIAODA_BACKUP_DIR": str(backup_root),
        })
        assert r.returncode != 0, "损坏源库不得静默成功"
