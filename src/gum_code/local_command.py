"""Desktop-only approved host commands and bounded test-execution evidence.

cwd and argv validation are guardrails, not an operating-system sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata

INSTRUCTION = (
    "桌面 run_command 在本机运行，每条命令单独审批；不是 Docker 或安全沙箱。"
    "python/python3 使用 GuMi-Code 的 Python 解释器；可使用 node。不要安装依赖或启动后台服务来绕过审批。"
    "修改程序后必须运行相关测试，读取失败、修复并复测，之后才能完成。"
    "可验证命令为 python3 -m unittest、python3 -m pytest 或 node --test（不要用 shell 或 npm 包装）。"
    "零测试、全部跳过、超时、失败或测试后再次修改都不算通过。"
    "拒绝审批或缺少环境时据实说明未验证，不可声称完成；宿主会有限提醒后结束为未完成。"
)


class LocalCommandConfig:
    def __init__(self, workspace, redactor=lambda text: text):
        self.workspace = Path(workspace).resolve()
        self.redactor = redactor
        self.initial = self.snapshot()
        self.verified = None
        self.test_failed = False
        self.changed = False

    def snapshot(self):
        from gum_code.verification import workspace_snapshot
        try:
            snapshot = workspace_snapshot(self.workspace, ('node_modules', 'dist', 'build', '__pycache__', '.pytest_cache'))
            # Include fixtures and unfamiliar input types; omit documented outputs only.
            return {name: identity for name, identity in snapshot.items()
                    if identity[0] != '__directory__'
                    and not any(part in {'__pycache__', '.pytest_cache'} for part in Path(name).parts)
                    and Path(name).suffix.lower() != '.pyc'}
        except (ValueError, OSError, RuntimeError):
            return None

    def on_file_change(self, change):
        if Path(change.path).suffix.lower() not in {'.md', '.rst'}:
            self.changed = True
            self.verified = None

    def needs_check(self):
        current = self.snapshot()
        def program_inputs(snapshot):
            return None if snapshot is None else {name: identity for name, identity in snapshot.items()
                                                  if Path(name).suffix.lower() not in {'.md', '.rst'}}
        relevant_changes = program_inputs(current) != program_inputs(self.initial)
        return (self.changed or self.test_failed or relevant_changes) and (current is None or current != self.verified)

    def observe(self, argv, result, before):
        after = self.snapshot()
        if result.started is True and (self.initial is None or before is None or after is None):
            # A missing scan cannot establish that an arbitrary host command was read-only.
            self.changed = True
            self.verified = None
        if not is_test_command(argv):
            return
        self.test_failed = True
        if (result.started is True and result.reason == 'exited' and result.exit_code == 0
                and passed_tests(argv, result.output) and before is not None and before == after):
            self.test_failed = False
            self.verified = after
        else:
            self.verified = None


def is_test_command(argv):
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return False
    return (len(argv) >= 3 and argv[0] in {'python', 'python3'} and argv[1] == '-m'
            and argv[2] in {'unittest', 'pytest'}) or (len(argv) >= 2 and argv[0] == 'node' and argv[1] == '--test')


def passed_tests(argv, output):
    if argv[:3] in (['python', '-m', 'unittest'], ['python3', '-m', 'unittest']):
        count = re.search(r'^Ran (\d+) tests? in ', output, re.M)
        footer = re.search(r'^OK(?: \(skipped=(\d+)\))?\s*$', output, re.M)
        return bool(count and footer and int(count[1]) > int(footer[1] or 0))
    if len(argv) >= 3 and argv[2] == 'pytest':
        return bool(re.search(r'\b[1-9]\d* passed\b', output)) and not re.search(r'\b[1-9]\d* (?:failed|error)', output)
    return bool(re.search(r'^[#ℹ] pass [1-9]\d*$', output, re.M) and re.search(r'^[#ℹ] fail 0$', output, re.M))


def validate_local(workspace, argv, cwd, config):
    if Path(workspace).resolve() != config.workspace:
        raise ValueError('本机命令工作区域不匹配')
    if len(argv) > 64 or sum(len(arg) for arg in argv) > 4000 or any(unicodedata.category(char) in {'Cc', 'Cf', 'Zl', 'Zp'} for arg in argv for char in arg):
        raise ValueError('命令参数过长或包含控制字符')
    if len(cwd) > 500 or any(unicodedata.category(char) in {'Cc', 'Cf', 'Zl', 'Zp'} for char in cwd):
        raise ValueError('命令目录过长')
    # Executable names are host-resolved; tool arguments cannot supply a PATH.
    if argv[0] not in {'python', 'python3', 'node', 'npm', 'npx', 'git'}:
        raise ValueError('本机命令支持 python3、node、npm、npx、git；其他程序尚未启用')


def _host_path(workspace):
    parts = [part for part in os.get_exec_path() if Path(part).is_absolute()
             and not Path(part).resolve().is_relative_to(Path(workspace).resolve())]
    for part in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'):
        if part not in parts:
            parts.append(part)
    if not shutil.which('node', path=':'.join(parts)):
        # Finder does not load shell startup files; reuse an installed nvm Node only.
        versions = []
        for directory in (Path.home() / '.nvm/versions/node').glob('v*'):
            match = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', directory.name)
            executable = directory / 'bin/node'
            if (match and not executable.resolve().is_relative_to(Path(workspace).resolve())
                    and executable.is_file() and os.access(executable, os.X_OK)):
                versions.append((tuple(map(int, match.groups())), str(directory / 'bin')))
        if versions:
            parts.append(max(versions)[1])
    return ':'.join(parts)


def run_local(workspace, argv, cwd, timeout_seconds, config, cancel_event=None):
    from gum_code.tools.command import CommandExecutionResult, _CommandInterrupted
    from gum_code.tools.command_logs import command_output_marker
    if cancel_event is not None and cancel_event.is_set():
        raise _CommandInterrupted(CommandExecutionResult(False, None, 'interrupted'))
    before = config.snapshot()
    fixed_path = _host_path(workspace)
    executable = sys.executable if argv[0] in {'python', 'python3'} else shutil.which(argv[0], path=fixed_path)
    if not executable:
        raise ValueError('本机未找到所需程序')
    output = bytearray()
    reason = 'exited'
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix='gum-code-command-') as scratch:
            env = {'PATH': str(Path(sys.executable).parent) + ':' + fixed_path,
                   'HOME': scratch, 'TMPDIR': scratch, 'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8',
                   'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                   'PIP_CONFIG_FILE': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'CI': '1', 'NO_COLOR': '1'}
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            proc = subprocess.Popen([executable, *argv[1:]], cwd=Path(workspace) / cwd,
                                    env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout_seconds
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if cancel_event is not None and cancel_event.is_set():
                        raise KeyboardInterrupt
                    if time.monotonic() >= deadline:
                        reason = 'timeout'
                        break
                    # A daemon child must not retain the pipe after its parent exits.
                    if proc.poll() is not None:
                        _kill_group(proc)
                    for key, _ in selector.select(.05):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > 128 * 1024:
                                reason = 'output_limit'
                                break
                    if reason != 'exited':
                        break
            while proc.poll() is None and reason == 'exited':
                if cancel_event is not None and cancel_event.is_set():
                    raise KeyboardInterrupt
                if time.monotonic() >= deadline:
                    reason = 'timeout'
                    break
                time.sleep(.02)
            _kill_group(proc)
            proc.wait(timeout=5)
    except KeyboardInterrupt:
        if proc is not None:
            _kill_group(proc)
            proc.wait(timeout=5)
        raise _CommandInterrupted(CommandExecutionResult(proc is not None, None, 'interrupted'))
    finally:
        if proc is not None:
            _kill_group(proc)
            proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
    result = CommandExecutionResult(True, proc.returncode, reason,
        config.redactor(output[:128 * 1024].decode('utf-8', errors='replace')))
    config.observe(argv, result, before)
    return CommandExecutionResult(result.started, result.exit_code, result.reason,
        command_output_marker({'backend': 'local', 'started': True, 'exit_code': result.exit_code, 'reason': reason}, result.output))


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
