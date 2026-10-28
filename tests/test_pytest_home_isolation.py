import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("permanent_env", [False, True])
def test_public_pytest_ignores_permanent_user_env(tmp_path, permanent_env):
    home = tmp_path / "operator-home"
    config_dir = home / ".promptpilot"
    config_dir.mkdir(parents=True)
    if permanent_env:
        (config_dir / ".env").write_text(
            "PP_PYTEST_USER_ENV_CANARY=dummy-user-config\n"
            "PP_PIPELINE_LEASE_KEY_FILE=dummy-permanent-lease.key\n", encoding="utf-8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "pp_home_probe.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def pytest_collection_finish(session):\n"
        "    from promptpilot import config\n"
        "    assert not bool(os.getenv('PP_PYTEST_USER_ENV_CANARY'))\n"
        "    assert Path.home().resolve() != Path(os.environ['TEST_OPERATOR_HOME']).resolve()\n"
        "    assert config.DB_DIR.resolve().is_relative_to(Path.home().resolve())\n",
        encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PP_")}
    env.update(HOME=str(home), USERPROFILE=str(home), TEST_OPERATOR_HOME=str(home),
               PYTHONPATH=str(plugins) + os.pathsep + env.get("PYTHONPATH", ""))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-W", "error", "-p", "pp_home_probe",
         "tests/test_pipeline_replicas.py::test_execution_route_passes_replica_identity_to_preflight"],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
        text=True, encoding="utf-8", timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
