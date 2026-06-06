import asyncio
import os
import shlex
import sys
import time
from pathlib import Path

# Default commands
DEFAULT_RUFF = ".venv/bin/ruff check src tests scripts"
DEFAULT_PYTEST = ".venv/bin/pytest"
DEFAULT_BUILD = "npm run build"
DEFAULT_SCAN = ".venv/bin/python3 -m catchem.cli validate-guards"

def find_project_root() -> Path:
    current = Path.cwd().resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return current

async def stream_stream(stream, prefix):
    if not stream:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        line_str = line.decode('utf-8', errors='replace')
        if line_str.endswith('\n'):
            line_str = line_str[:-1]
        if line_str.endswith('\r'):
            line_str = line_str[:-1]
        print(f"{prefix} {line_str}")

async def run_task(label: str, cmd_str: str, cwd: str | None) -> dict:
    t0 = time.time()
    args = shlex.split(cmd_str)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd
        )
    except Exception as e:
        print(f"{label} Failed to start command '{cmd_str}': {e}")
        duration = time.time() - t0
        return {
            "label": label,
            "status": "Failure",
            "exit_code": -1,
            "duration": duration
        }

    stdout_task = asyncio.create_task(stream_stream(proc.stdout, label))
    stderr_task = asyncio.create_task(stream_stream(proc.stderr, label))

    exit_code = await proc.wait()
    await asyncio.gather(stdout_task, stderr_task)

    duration = time.time() - t0
    status = "Success" if exit_code == 0 else "Failure"
    return {
        "label": label,
        "status": status,
        "exit_code": exit_code,
        "duration": duration
    }

async def run_all_tasks(
    ruff_cmd: str = DEFAULT_RUFF,
    pytest_cmd: str = DEFAULT_PYTEST,
    build_cmd: str = DEFAULT_BUILD,
    scan_cmd: str = DEFAULT_SCAN
) -> list[dict]:
    build_cwd = "frontend" if os.path.exists("frontend") else None
    
    results = await asyncio.gather(
        run_task("[RUFF]", ruff_cmd, None),
        run_task("[PYTEST]", pytest_cmd, None),
        run_task("[BUILD]", build_cmd, build_cwd),
        run_task("[SCAN]", scan_cmd, None)
    )
    
    print_summary(results)
    return results

async def run_all_tasks_helper() -> list[dict]:
    return await run_all_tasks()

def print_summary(results: list[dict]):
    print("\n" + "="*50)
    print(f"{'Task Name':<15} | {'Status':<10} | {'Exit Code':<10} | {'Duration (s)':<12}")
    print("-"*50)
    for r in results:
        name = r["label"].replace("[", "").replace("]", "")
        print(f"{name:<15} | {r['status']:<10} | {r['exit_code']:<10} | {r['duration']:<12.2f}")
    print("="*50 + "\n")

def should_trigger(path_str: str, extra_excludes: list[str] | None = None) -> bool:
    path = Path(path_str).resolve()
    try:
        rel_path = path.relative_to(Path.cwd())
    except ValueError:
        return False
    
    rel_path_str = rel_path.as_posix()
    ignored_dirs = [".git", ".venv", "node_modules", "frontend/dist", ".ralph", ".agents"]
    if extra_excludes:
        ignored_dirs.extend(extra_excludes)
    for ignored in ignored_dirs:
        if rel_path_str == ignored or rel_path_str.startswith(ignored + "/"):
            return False
            
    allowed_extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".css", ".html"}
    if path.suffix not in allowed_extensions:
        return False
        
    return True

async def watch_loop(
    debounce_delay: float = 1.0,
    watch_path: str = ".",
    ruff_cmd: str = DEFAULT_RUFF,
    pytest_cmd: str = DEFAULT_PYTEST,
    build_cmd: str = DEFAULT_BUILD,
    scan_cmd: str = DEFAULT_SCAN,
    extra_excludes: list[str] | None = None
):
    import watchfiles
    queue = asyncio.Queue()
    
    async def watch_task_func():
        try:
            async for changes in watchfiles.awatch(watch_path):
                valid_paths = []
                for _, path in changes:
                    if should_trigger(path, extra_excludes=extra_excludes):
                        valid_paths.append(path)
                if valid_paths:
                    await queue.put(valid_paths)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Error in file watcher: {e}")
            
    watch_task = asyncio.create_task(watch_task_func())
    
    try:
        while True:
            await queue.get()
            last_event_time = asyncio.get_event_loop().time()
            
            while True:
                now = asyncio.get_event_loop().time()
                time_remaining = (last_event_time + debounce_delay) - now
                if time_remaining <= 0:
                    break
                    
                try:
                    _ = await asyncio.wait_for(queue.get(), timeout=time_remaining)
                    last_event_time = asyncio.get_event_loop().time()
                except TimeoutError:
                    break
            
            print("\n[WATCHER] File change detected. Triggering run...")
            await run_all_tasks(
                ruff_cmd=ruff_cmd,
                pytest_cmd=pytest_cmd,
                build_cmd=build_cmd,
                scan_cmd=scan_cmd
            )
    except asyncio.CancelledError:
        pass
    finally:
        watch_task.cancel()
        try:
            await watch_task
        except asyncio.CancelledError:
            pass

async def main_entrypoint(
    watch: bool = False,
    debounce_delay: float = 1.0,
    ruff_cmd: str = DEFAULT_RUFF,
    pytest_cmd: str = DEFAULT_PYTEST,
    build_cmd: str = DEFAULT_BUILD,
    scan_cmd: str = DEFAULT_SCAN,
    extra_excludes: list[str] | None = None
):
    project_root = find_project_root()
    orig_cwd = os.getcwd()
    os.chdir(project_root)
    try:
        results = await run_all_tasks(
            ruff_cmd=ruff_cmd,
            pytest_cmd=pytest_cmd,
            build_cmd=build_cmd,
            scan_cmd=scan_cmd
        )
        
        if not watch:
            all_success = all(r["exit_code"] == 0 for r in results)
            if all_success:
                sys.exit(0)
            else:
                sys.exit(1)
        else:
            await watch_loop(
                debounce_delay=debounce_delay,
                watch_path=".",
                ruff_cmd=ruff_cmd,
                pytest_cmd=pytest_cmd,
                build_cmd=build_cmd,
                scan_cmd=scan_cmd,
                extra_excludes=extra_excludes
            )
    finally:
        os.chdir(orig_cwd)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Parallel verification task coordinator and watcher.")
    parser.add_argument("--ruff-cmd", default=DEFAULT_RUFF, help="Ruff check command")
    parser.add_argument("--pytest-cmd", default=DEFAULT_PYTEST, help="Pytest command")
    parser.add_argument("--build-cmd", default=DEFAULT_BUILD, help="Vite build command")
    parser.add_argument("--scan-cmd", default=DEFAULT_SCAN, help="News poller scan command")
    parser.add_argument("--watch", action="store_true", help="Run in continuous watch mode")
    parser.add_argument("--debounce-delay", type=float, default=1.0, help="Debounce delay in seconds")
    parser.add_argument("--exclude", default="", help="Comma-separated list of directories to ignore")
    
    args = parser.parse_args()
    
    exclude_dirs = [d.strip() for d in args.exclude.split(",") if d.strip()]
    
    try:
        asyncio.run(main_entrypoint(
            watch=args.watch,
            debounce_delay=args.debounce_delay,
            ruff_cmd=args.ruff_cmd,
            pytest_cmd=args.pytest_cmd,
            build_cmd=args.build_cmd,
            scan_cmd=args.scan_cmd,
            extra_excludes=exclude_dirs
        ))
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
