import os
import subprocess
from pathlib import Path

import pytest

from vibe_harness.tools.command import (
    DockerSandboxConfig,
    load_docker_sandbox,
    run_command,
)


_IMAGE = os.environ.get("VIBE_HARNESS_DOCKER_E2E_IMAGE")

pytestmark = pytest.mark.skipif(
    not _IMAGE,
    reason="set VIBE_HARNESS_DOCKER_E2E_IMAGE to a preloaded image with python3",
)


def _harness_containers(sandbox: DockerSandboxConfig) -> set[str]:
    result = subprocess.run(
        [
            str(sandbox.docker_path),
            "ps",
            "--all",
            "--filter",
            "name=^/vibe-harness-",
            "--format",
            "{{.Names}}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=True,
        text=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def test_live_docker_sandbox_boundary(tmp_path: Path) -> None:
    assert _IMAGE is not None
    sandbox = load_docker_sandbox(_IMAGE)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SYNTHETIC_SECRET=hidden", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text(
        "synthetic private config",
        encoding="utf-8",
    )
    (tmp_path / "outside-marker.txt").write_text(
        "outside",
        encoding="utf-8",
    )

    result = run_command(
        workspace,
        [
            "python3",
            "-c",
            (
                "from pathlib import Path; import os; "
                "Path('created.txt').write_text('ok'); "
                "print('uid=' + str(os.getuid())); "
                "print('env=' + repr(Path('.env').read_text())); "
                "print('git=' + repr(list(Path('.git').iterdir()))); "
                "print('outside=' + str(Path('../outside-marker.txt').exists()))"
            ),
        ],
        sandbox=sandbox,
    )

    assert "exit_code: 0" in result
    assert "uid=0" not in result
    assert "env=''" in result
    assert "git=[]" in result
    assert "outside=False" in result
    assert (workspace / "created.txt").read_text(encoding="utf-8") == "ok"
    assert "SYNTHETIC_SECRET" not in result
    assert "synthetic private config" not in result

    network = run_command(
        workspace,
        [
            "python3",
            "-c",
            (
                "import socket; "
                "s=socket.socket(); s.settimeout(1); "
                "\ntry: s.connect(('1.1.1.1', 53)); print('connected')"
                "\nexcept OSError: print('blocked')"
            ),
        ],
        sandbox=sandbox,
    )
    assert "blocked" in network
    assert "connected" not in network

    root_write = run_command(
        workspace,
        [
            "python3",
            "-c",
            (
                "from pathlib import Path; "
                "\ntry: Path('/sandbox-root-write').write_text('x'); print('written')"
                "\nexcept OSError: print('blocked')"
            ),
        ],
        sandbox=sandbox,
    )
    assert "blocked" in root_write
    assert "written" not in root_write

    before = _harness_containers(sandbox)
    timed_out = run_command(
        workspace,
        ["python3", "-c", "import time; time.sleep(30)"],
        timeout_seconds=1,
        sandbox=sandbox,
    )
    assert "Error: 命令超时" in timed_out
    assert _harness_containers(sandbox) == before

    too_much_output = run_command(
        workspace,
        ["python3", "-c", "print('x' * 70000)"],
        sandbox=sandbox,
    )
    assert "Error: 命令输出超过限制" in too_much_output
    assert _harness_containers(sandbox) == before
