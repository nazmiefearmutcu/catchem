import asyncio
import os
import sys
from pathlib import Path

import pytest

from scripts.catchem_watcher import (
    main_entrypoint,
    run_all_tasks,
    run_all_tasks_helper,
    should_trigger,
    stream_stream,
    watch_loop,
)


class MockStreamReader:
    def __init__(self, lines: list[bytes]):
        self.lines = lines
        self.idx = 0

    async def readline(self) -> bytes:
        if self.idx < len(self.lines):
            line = self.lines[self.idx]
            self.idx += 1
            return line
        return b""

class MockProcess:
    def __init__(self, stdout: list[bytes], stderr: list[bytes], returncode: int, delay: float = 0.0):
        self.stdout = MockStreamReader(stdout)
        self.stderr = MockStreamReader(stderr)
        self.returncode = returncode
        self.delay = delay

    async def wait(self) -> int:
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.returncode

@pytest.mark.asyncio
async def test_watcher_concurrency(monkeypatch):
    starts = {}
    ends = {}

    async def mock_create_exec(*args, **kwargs):
        task_name = args[0]
        starts[task_name] = asyncio.get_running_loop().time()
        proc = MockProcess(stdout=[], stderr=[], returncode=0, delay=0.05)
        
        original_wait = proc.wait
        async def wrapped_wait():
            rc = await original_wait()
            ends[task_name] = asyncio.get_running_loop().time()
            return rc
        proc.wait = wrapped_wait
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_exec)
    
    await run_all_tasks_helper()
    
    assert len(starts) == 4
    max_start = max(starts.values())
    min_end = min(ends.values())
    assert max_start < min_end

@pytest.mark.asyncio
async def test_watcher_output_prefixing(monkeypatch, capsys):
    async def mock_create_exec(*args, **kwargs):
        cmd = args[0]
        if "ruff" in cmd:
            # Yielding a line with carriage return to exercise carriage return stripping
            return MockProcess(stdout=[b"lint issue found\r\n"], stderr=[b"ruff stderr\n"], returncode=0)
        elif "pytest" in cmd:
            return MockProcess(stdout=[b"test passed\n"], stderr=[], returncode=0)
        else:
            return MockProcess(stdout=[], stderr=[], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_exec)
    await run_all_tasks_helper()
    
    captured = capsys.readouterr().out
    assert "[RUFF] lint issue found" in captured
    assert "[RUFF] ruff stderr" in captured
    assert "[PYTEST] test passed" in captured

@pytest.mark.asyncio
async def test_watcher_exit_codes_success(monkeypatch):
    async def mock_create_exec(*args, **kwargs):
        return MockProcess(stdout=[], stderr=[], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_exec)
    
    with pytest.raises(SystemExit) as exc:
        await main_entrypoint(watch=False)
    assert exc.value.code == 0

@pytest.mark.asyncio
async def test_watcher_exit_codes_failure(monkeypatch):
    async def mock_create_exec(*args, **kwargs):
        cmd = args[0]
        rc = 1 if "pytest" in cmd else 0
        return MockProcess(stdout=[], stderr=[], returncode=rc)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_exec)
    
    with pytest.raises(SystemExit) as exc:
        await main_entrypoint(watch=False)
    assert exc.value.code == 1

@pytest.mark.asyncio
async def test_watcher_task_start_failure(monkeypatch, capsys):
    async def mock_create_exec(*args, **kwargs):
        raise OSError("executable not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_exec)
    results = await run_all_tasks()
    
    for r in results:
        assert r["status"] == "Failure"
        assert r["exit_code"] == -1
    
    captured = capsys.readouterr().out
    assert "Failed to start command" in captured

def test_should_trigger_filtering(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    
    (tmp_path / "src").mkdir()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "dist").mkdir(parents=True)
    (tmp_path / ".ralph").mkdir()
    
    py_file = tmp_path / "src" / "main.py"
    py_file.touch()
    
    git_file = tmp_path / ".git" / "config"
    git_file.touch()
    
    venv_file = tmp_path / ".venv" / "bin" / "python"
    venv_file.touch()
    
    dist_file = tmp_path / "frontend" / "dist" / "index.html"
    dist_file.touch()
    
    txt_file = tmp_path / "src" / "read.txt"
    txt_file.touch()
    
    assert should_trigger(str(py_file)) is True
    assert should_trigger(str(git_file)) is False
    assert should_trigger(str(venv_file)) is False
    assert should_trigger(str(dist_file)) is False
    assert should_trigger(str(txt_file)) is False
    
    assert should_trigger("/tmp/some_other_file.py") is False

@pytest.mark.asyncio
async def test_watcher_debounce(monkeypatch):
    call_count = 0
    
    async def mock_run_all_tasks(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return []

    async def mock_awatch(*args, **kwargs):
        yield {("modified", "src/catchem/cli.py")}
        await asyncio.sleep(0.005)
        yield {("modified", "src/catchem/api.py")}
        await asyncio.sleep(0.05)

    monkeypatch.setattr("watchfiles.awatch", mock_awatch)
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", mock_run_all_tasks)
    
    watcher_task = asyncio.create_task(watch_loop(debounce_delay=0.01))
    await asyncio.sleep(0.04)
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    
    assert call_count == 1

@pytest.mark.asyncio
async def test_watcher_debounce_negative_delay(monkeypatch):
    call_count = 0
    
    async def mock_run_all_tasks(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return []

    async def mock_awatch(*args, **kwargs):
        yield {("modified", "src/catchem/cli.py")}
        await asyncio.sleep(0.05)

    monkeypatch.setattr("watchfiles.awatch", mock_awatch)
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", mock_run_all_tasks)
    
    # Passing negative delay to trigger immediate time_remaining <= 0 check
    watcher_task = asyncio.create_task(watch_loop(debounce_delay=-1.0))
    await asyncio.sleep(0.02)
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    
    assert call_count == 1

@pytest.mark.asyncio
async def test_main_entrypoint_watch(monkeypatch):
    async def mock_run_all_tasks(*args, **kwargs):
        return [{"label": "[RUFF]", "status": "Success", "exit_code": 0, "duration": 0.0}]
    
    async def mock_watch_loop(*args, **kwargs):
        raise asyncio.CancelledError()
        
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", mock_run_all_tasks)
    monkeypatch.setattr("scripts.catchem_watcher.watch_loop", mock_watch_loop)
    
    with pytest.raises(asyncio.CancelledError):
        await main_entrypoint(watch=True)

def test_main_cli_success(monkeypatch):
    sys_exit_code = None
    def mock_exit(code):
        nonlocal sys_exit_code
        sys_exit_code = code
        
    def mock_run(coro):
        coro.close()
        
    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(asyncio, "run", mock_run)
    monkeypatch.setattr(sys, "argv", ["catchem_watcher.py", "--watch", "--debounce-delay", "0.5"])
    
    from scripts.catchem_watcher import main
    main()

def test_main_cli_keyboard_interrupt(monkeypatch):
    sys_exit_code = None
    def mock_exit(code):
        nonlocal sys_exit_code
        sys_exit_code = code
        
    def mock_run(coro):
        coro.close()
        raise KeyboardInterrupt()
        
    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(asyncio, "run", mock_run)
    monkeypatch.setattr(sys, "argv", ["catchem_watcher.py"])
    
    from scripts.catchem_watcher import main
    main()
    assert sys_exit_code == 0

@pytest.mark.asyncio
async def test_watch_loop_exception(monkeypatch, capsys):
    async def mock_awatch(*args, **kwargs):
        raise RuntimeError("Simulated watch error")
        yield
        
    monkeypatch.setattr("watchfiles.awatch", mock_awatch)
    
    task = asyncio.create_task(watch_loop(debounce_delay=0.01))
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    captured = capsys.readouterr().out
    assert "Error in file watcher: Simulated watch error" in captured

@pytest.mark.asyncio
async def test_stream_stream_none():
    # Verify stream_stream handles None stream gracefully
    await stream_stream(None, "[PREFIX]")

@pytest.mark.asyncio
async def test_stream_stream_various_line_endings(capsys):
    # We will test stream_stream with various line endings
    # to cover lines 22-26 in scripts/catchem_watcher.py
    lines = [
        b"line1\n",     # ends with \n
        b"line2\r\n",   # ends with \r\n
        b"line3\r",     # ends with \r
        b"line4",       # ends with neither
    ]
    stream = MockStreamReader(lines)
    await stream_stream(stream, "[TEST]")
    
    captured = capsys.readouterr().out
    assert "[TEST] line1" in captured
    assert "[TEST] line2" in captured
    assert "[TEST] line3" in captured
    assert "[TEST] line4" in captured

@pytest.mark.asyncio
async def test_watcher_debounce_natural_exit_and_invalid_files(monkeypatch):
    call_count = 0
    
    async def mock_run_all_tasks(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return []

    async def mock_awatch(*args, **kwargs):
        # Yield a valid change
        yield {("modified", "src/catchem/cli.py")}
        await asyncio.sleep(0.005)
        # Yield an invalid change (to trigger should_trigger returning False)
        yield {("modified", ".git/config")}
        await asyncio.sleep(0.005)
        # Yield another invalid change to cover when valid_paths is empty
        yield {("modified", "src/read.txt")}
        # Complete naturally (so the async for loop exits naturally)

    monkeypatch.setattr("watchfiles.awatch", mock_awatch)
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", mock_run_all_tasks)
    
    # Run watcher loop with 0.01s debounce
    watcher_task = asyncio.create_task(watch_loop(debounce_delay=0.01))
    # Wait long enough for mock_awatch to exhaust and watch_task to finish naturally
    await asyncio.sleep(0.06)
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    
    assert call_count == 1

@pytest.mark.asyncio
async def test_watcher_loop_task_cancelled_exception(monkeypatch):
    # Mock asyncio.create_task to raise CancelledError when watch_task is awaited
    original_create_task = asyncio.create_task
    
    def mock_create_task(coro, *args, **kwargs):
        if hasattr(coro, "__name__") and coro.__name__ == "watch_task_func":
            coro.close()
            class MockTask:
                def cancel(self):
                    return True
                def __await__(self):
                    if False:
                        yield
                    raise asyncio.CancelledError()
            return MockTask()
        return original_create_task(coro, *args, **kwargs)
        
    monkeypatch.setattr(asyncio, "create_task", mock_create_task)
    
    # Also mock awatch so that watch_loop doesn't block forever before starting
    async def mock_awatch(*args, **kwargs):
        yield {("modified", "src/catchem/cli.py")}
        await asyncio.sleep(0.05)
        
    monkeypatch.setattr("watchfiles.awatch", mock_awatch)
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", lambda *args, **kwargs: asyncio.sleep(0))
    
    # Run the watch loop, it will create watch_task and then we cancel the watch loop,
    # which enters the finally block, cancels watch_task, and awaits it (raising CancelledError).
    watcher_task = asyncio.create_task(watch_loop(debounce_delay=0.01))
    await asyncio.sleep(0.02)
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass


def test_find_project_root_detection(tmp_path, monkeypatch):
    # Case 1: pyproject.toml exists in parent directory
    root_dir = tmp_path / "project_root"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").touch()
    
    sub_dir = root_dir / "src" / "sub"
    sub_dir.mkdir(parents=True)
    
    monkeypatch.setattr(Path, "cwd", lambda: sub_dir)
    from scripts.catchem_watcher import find_project_root
    assert find_project_root() == root_dir

    # Case 2: fallback to cwd when neither pyproject.toml nor .git exists in parent chain
    class FakePath:
        def __init__(self, name):
            self.name = name
            self.parents = []
        def resolve(self):
            return self
        def __truediv__(self, other):
            class ExistsChecker:
                def exists(self):
                    return False
            return ExistsChecker()
    
    monkeypatch.setattr(Path, "cwd", lambda: FakePath("some_dir"))
    assert find_project_root().name == "some_dir"


def test_should_trigger_with_extra_excludes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "custom_ignored").mkdir()
    (tmp_path / ".agents").mkdir()
    
    py_file = tmp_path / "src" / "main.py"
    py_file.touch()
    
    custom_file = tmp_path / "custom_ignored" / "foo.py"
    custom_file.touch()
    
    agent_file = tmp_path / ".agents" / "report.json"
    agent_file.touch()
    
    # Verify standard should_trigger
    assert should_trigger(str(py_file)) is True
    # .agents is ignored by default
    assert should_trigger(str(agent_file)) is False
    # custom_ignored is not ignored by default
    assert should_trigger(str(custom_file)) is True
    
    # Verify with extra_excludes
    assert should_trigger(str(custom_file), extra_excludes=["custom_ignored"]) is False


def test_main_cli_exclude(monkeypatch):
    args_passed = None
    
    async def mock_main_entrypoint(
        watch, debounce_delay, ruff_cmd, pytest_cmd, build_cmd, scan_cmd, extra_excludes
    ):
        nonlocal args_passed
        args_passed = extra_excludes
        return
        
    def mock_run(coro):
        try:
            coro.send(None)
        except StopIteration:
            pass
            
    monkeypatch.setattr(asyncio, "run", mock_run)
    monkeypatch.setattr(sys, "argv", ["catchem_watcher.py", "--exclude", "foo,bar/baz"])
    monkeypatch.setattr("scripts.catchem_watcher.main_entrypoint", mock_main_entrypoint)
    
    from scripts.catchem_watcher import main
    main()
    
    assert args_passed == ["foo", "bar/baz"]


@pytest.mark.asyncio
async def test_main_entrypoint_subdir_chdir(tmp_path, monkeypatch):
    root_dir = tmp_path / "project_root"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").touch()
    
    sub_dir = root_dir / "frontend"
    sub_dir.mkdir()
    
    monkeypatch.setattr("scripts.catchem_watcher.find_project_root", lambda: root_dir)
    
    cwd_during_run = []
    async def mock_run_all_tasks(*args, **kwargs):
        cwd_during_run.append(Path.cwd())
        return [{"label": "[RUFF]", "status": "Success", "exit_code": 0, "duration": 0.0}]
        
    monkeypatch.setattr("scripts.catchem_watcher.run_all_tasks", mock_run_all_tasks)
    monkeypatch.setattr(sys, "exit", lambda code: None)
    
    current_dir_mock = sub_dir
    def mock_chdir_real(path):
        nonlocal current_dir_mock
        current_dir_mock = Path(path).resolve()
        
    monkeypatch.setattr(os, "chdir", mock_chdir_real)
    monkeypatch.setattr(os, "getcwd", lambda: str(current_dir_mock))
    monkeypatch.setattr(Path, "cwd", lambda: current_dir_mock)
    
    await main_entrypoint(watch=False)
    
    assert cwd_during_run == [root_dir]
    assert current_dir_mock == sub_dir
