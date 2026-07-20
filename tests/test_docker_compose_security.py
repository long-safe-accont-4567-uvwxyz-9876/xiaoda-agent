"""测试 Docker Compose 三文件安全加固配置。

验证 Task 6 要求：
1. 三个 compose 文件均无 `version:` 字段（Docker Compose v2 已忽略并告警）
2. 三个 compose 文件都有 security_opt: [no-new-privileges:true]
3. docker-compose.yml 与 docker-compose.prod.yml 有 read_only: true 和 tmpfs: [/tmp]
4. docker-compose.dev.yml 不应有 read_only: true（代码挂载 ./:/app 需可写）
5. docker-compose.yml 保留 cap_drop: [ALL]
6. docker-compose.dev.yml 与 docker-compose.prod.yml 有 cap_drop: [ALL]
7. 保留现有 volumes（agent-data:/data、./.env:/app/.env:ro 或 ./:/app）
8. YAML 文件可被 yaml 库正确解析
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml

PROJECT_ROOT = Path(__file__).parent.parent

COMPOSE_FILES = {
    "base": PROJECT_ROOT / "docker-compose.yml",
    "dev": PROJECT_ROOT / "docker-compose.dev.yml",
    "prod": PROJECT_ROOT / "docker-compose.prod.yml",
}

DOCKERFILE = PROJECT_ROOT / "Dockerfile"


def _load_compose(path: Path) -> dict:
    """加载 compose 文件为 dict。"""
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestComposeNoVersionField(unittest.TestCase):
    """所有 compose 文件应移除过时的 version 字段。"""

    def test_base_no_version(self):
        data = _load_compose(COMPOSE_FILES["base"])
        self.assertNotIn("version", data, "docker-compose.yml 不应再包含 version 字段")

    def test_dev_no_version(self):
        data = _load_compose(COMPOSE_FILES["dev"])
        self.assertNotIn("version", data, "docker-compose.dev.yml 不应再包含 version 字段")

    def test_prod_no_version(self):
        data = _load_compose(COMPOSE_FILES["prod"])
        self.assertNotIn("version", data, "docker-compose.prod.yml 不应再包含 version 字段")


class TestComposeSecurityOpt(unittest.TestCase):
    """所有 compose 文件应有 security_opt: [no-new-privileges:true]。"""

    def _agent_service(self, name: str) -> dict:
        data = _load_compose(COMPOSE_FILES[name])
        return data["services"]["agent"]

    def test_base_has_no_new_privileges(self):
        svc = self._agent_service("base")
        self.assertIn("security_opt", svc, "docker-compose.yml 缺 security_opt")
        self.assertIn("no-new-privileges:true", svc["security_opt"])

    def test_dev_has_no_new_privileges(self):
        svc = self._agent_service("dev")
        self.assertIn("security_opt", svc, "docker-compose.dev.yml 缺 security_opt")
        self.assertIn("no-new-privileges:true", svc["security_opt"])

    def test_prod_has_no_new_privileges(self):
        svc = self._agent_service("prod")
        self.assertIn("security_opt", svc, "docker-compose.prod.yml 缺 security_opt")
        self.assertIn("no-new-privileges:true", svc["security_opt"])


class TestComposeCapDrop(unittest.TestCase):
    """所有 compose 文件应有 cap_drop: [ALL]。"""

    def _agent_service(self, name: str) -> dict:
        data = _load_compose(COMPOSE_FILES[name])
        return data["services"]["agent"]

    def test_base_cap_drop_all(self):
        svc = self._agent_service("base")
        self.assertIn("cap_drop", svc, "docker-compose.yml 缺 cap_drop")
        self.assertIn("ALL", svc["cap_drop"])

    def test_dev_cap_drop_all(self):
        svc = self._agent_service("dev")
        self.assertIn("cap_drop", svc, "docker-compose.dev.yml 缺 cap_drop")
        self.assertIn("ALL", svc["cap_drop"])

    def test_prod_cap_drop_all(self):
        svc = self._agent_service("prod")
        self.assertIn("cap_drop", svc, "docker-compose.prod.yml 缺 cap_drop")
        self.assertIn("ALL", svc["cap_drop"])


class TestComposeReadOnlyAndTmpfs(unittest.TestCase):
    """base 与 prod 应有 read_only + tmpfs；dev 不应有 read_only。"""

    def _agent_service(self, name: str) -> dict:
        data = _load_compose(COMPOSE_FILES[name])
        return data["services"]["agent"]

    def test_base_has_read_only(self):
        svc = self._agent_service("base")
        self.assertIn("read_only", svc, "docker-compose.yml 缺 read_only")
        self.assertTrue(svc["read_only"], "docker-compose.yml read_only 应为 true")

    def test_base_has_tmpfs(self):
        svc = self._agent_service("base")
        self.assertIn("tmpfs", svc, "docker-compose.yml 缺 tmpfs")
        # tmpfs 可以是列表或 dict
        tmpfs = svc["tmpfs"]
        if isinstance(tmpfs, dict):
            self.assertIn("/tmp", tmpfs, "tmpfs 应包含 /tmp")
        else:
            self.assertIn("/tmp", tmpfs, "tmpfs 应包含 /tmp")

    def test_prod_has_read_only(self):
        svc = self._agent_service("prod")
        self.assertIn("read_only", svc, "docker-compose.prod.yml 缺 read_only")
        self.assertTrue(svc["read_only"], "docker-compose.prod.yml read_only 应为 true")

    def test_prod_has_tmpfs(self):
        svc = self._agent_service("prod")
        self.assertIn("tmpfs", svc, "docker-compose.prod.yml 缺 tmpfs")
        tmpfs = svc["tmpfs"]
        if isinstance(tmpfs, dict):
            self.assertIn("/tmp", tmpfs, "tmpfs 应包含 /tmp")
        else:
            self.assertIn("/tmp", tmpfs, "tmpfs 应包含 /tmp")

    def test_dev_no_read_only(self):
        """dev 模式因代码挂载 ./:/app 需要可写，不应启用 read_only。"""
        svc = self._agent_service("dev")
        self.assertNotIn(
            "read_only", svc,
            "docker-compose.dev.yml 不应有 read_only（代码挂载 ./:/app 需可写）"
        )


class TestComposeVolumesPreserved(unittest.TestCase):
    """保留现有 volumes：base/prod 保留 agent-data:/data 与 .env 挂载；dev 保留 ./:/app 与 agent-data:/data。"""

    def _agent_service(self, name: str) -> dict:
        data = _load_compose(COMPOSE_FILES[name])
        return data["services"]["agent"]

    def test_base_volumes_preserved(self):
        svc = self._agent_service("base")
        volumes = svc.get("volumes", [])
        joined = " ".join(volumes)
        self.assertIn("agent-data:/data", joined, "base 应保留 agent-data:/data")
        self.assertIn("./.env:/app/.env:ro", joined, "base 应保留 ./.env:/app/.env:ro")

    def test_dev_volumes_preserved(self):
        svc = self._agent_service("dev")
        volumes = svc.get("volumes", [])
        joined = " ".join(volumes)
        self.assertIn("./:/app", joined, "dev 应保留 ./:/app 代码挂载")
        self.assertIn("agent-data:/data", joined, "dev 应保留 agent-data:/data")

    def test_prod_volumes_preserved(self):
        svc = self._agent_service("prod")
        volumes = svc.get("volumes", [])
        joined = " ".join(volumes)
        self.assertIn("agent-data:/data", joined, "prod 应保留 agent-data:/data")
        self.assertIn("./.env:/app/.env:ro", joined, "prod 应保留 ./.env:/app/.env:ro")

    def test_all_have_agent_data_volume_declared(self):
        """三个文件都应声明 agent-data 命名卷。"""
        for name in ("base", "dev", "prod"):
            with self.subTest(file=name):
                data = _load_compose(COMPOSE_FILES[name])
                self.assertIn("volumes", data, f"{name} 缺顶层 volumes 声明")
                self.assertIn("agent-data", data["volumes"], f"{name} 缺 agent-data 卷声明")


class TestComposeYamlValid(unittest.TestCase):
    """YAML 文件可被正确解析，且包含必需的顶层 services 字段。"""

    def test_all_parse_and_have_services(self):
        for name, path in COMPOSE_FILES.items():
            with self.subTest(file=name):
                data = _load_compose(path)
                self.assertIsInstance(data, dict, f"{name} YAML 解析结果非 dict")
                self.assertIn("services", data, f"{name} 缺 services 字段")
                self.assertIn("agent", data["services"], f"{name} 缺 agent 服务")


class TestDockerfileSecurity(unittest.TestCase):
    """Dockerfile 安全相关确认。"""

    def test_dockerfile_exists(self):
        self.assertTrue(DOCKERFILE.exists(), "Dockerfile 不存在")

    def test_appuser_created_and_switched(self):
        """确认 appuser 用户已创建并切换 (USER appuser)。"""
        content = DOCKERFILE.read_text(encoding="utf-8")
        # 创建用户
        self.assertIn("useradd", content, "Dockerfile 应通过 useradd 创建 appuser")
        self.assertIn("appuser", content, "Dockerfile 应包含 appuser")
        # 切换用户（MULTILINE 让 ^ 匹配行首）
        self.assertTrue(
            re.search(r"^\s*USER\s+appuser\s*$", content, re.MULTILINE),
            "Dockerfile 应包含 'USER appuser' 切换指令"
        )

    def test_data_dirs_under_data(self):
        """所有数据写入路径都应在 /data 下。"""
        content = DOCKERFILE.read_text(encoding="utf-8")
        # 关键 ENV
        self.assertIn("KIOXIA_DATA_DIR=/data", content, "应设置 KIOXIA_DATA_DIR=/data")
        self.assertIn("AGENTLY_CLI_HOME=/data/agently-cli", content,
                      "应设置 AGENTLY_CLI_HOME=/data/agently-cli")
        # 数据目录创建
        self.assertIn("/data/db", content, "应在 /data 下创建 db 目录")
        self.assertIn("/data/logs", content, "应在 /data 下创建 logs 目录")
        self.assertIn("/data/credentials", content, "应在 /data 下创建 credentials 目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
