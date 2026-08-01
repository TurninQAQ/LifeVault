from __future__ import annotations

import ipaddress
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Sequence

from lifevault.config import PACKAGE_ROOT, Settings


@dataclass(frozen=True)
class LaunchPlan:
    url: str
    streamlit_command: tuple[str, ...]
    worker_command: tuple[str, ...] | None
    environment: tuple[tuple[str, str], ...]


def build_launch_plan(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8501,
    worker_interval: int = 60,
    no_worker: bool = False,
) -> LaunchPlan:
    _require_loopback(host)
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if worker_interval <= 0:
        raise ValueError("worker interval must be positive")

    selected_port = find_available_port(host, port)
    display_host = "127.0.0.1" if host == "localhost" else host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{selected_port}"
    app_path = PACKAGE_ROOT / "app" / "main.py"
    streamlit_command = (
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        host,
        "--server.port",
        str(selected_port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    )
    worker_command = None
    if not no_worker:
        worker_command = (
            sys.executable,
            "-m",
            "lifevault.cli",
            "worker",
            "--interval",
            str(worker_interval),
        )
    environment = (
        ("LIFEVAULT_DB", str(settings.database_path)),
        ("LIFEVAULT_LANGGRAPH_DB", str(settings.langgraph_checkpoint_path)),
        ("LIFEVAULT_BACKUP_DIR", str(settings.backup_dir)),
        ("LIFEVAULT_QWEN_BASE_URL", settings.qwen_base_url),
        ("LIFEVAULT_QWEN_MODEL", settings.qwen_model),
        ("LIFEVAULT_QWEN_TIMEOUT", str(settings.qwen_timeout_seconds)),
        ("LIFEVAULT_USER_ID", settings.default_user_id),
        ("LIFEVAULT_TIMEZONE", settings.default_timezone),
        ("LIFEVAULT_DEFAULT_REMINDER_TIME", settings.default_reminder_time),
        ("LIFEVAULT_DEFAULT_ADVANCE_DAYS", str(settings.default_advance_days)),
        ("LIFEVAULT_INPUT_MAX_CHARS", str(settings.input_max_chars)),
        ("LIFEVAULT_USE_QWEN", "1" if settings.use_qwen else "0"),
    )
    return LaunchPlan(
        url=url,
        streamlit_command=streamlit_command,
        worker_command=worker_command,
        environment=environment,
    )


def serve(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8501,
    worker_interval: int = 60,
    no_worker: bool = False,
) -> int:
    plan = build_launch_plan(
        settings,
        host=host,
        port=port,
        worker_interval=worker_interval,
        no_worker=no_worker,
    )
    print(f"LifeVault UI: {plan.url}", flush=True)
    print(
        "Reminder Worker: disabled" if no_worker else "Reminder Worker: running",
        flush=True,
    )
    try:
        return run_launch_plan(plan)
    except OSError as exc:
        raise RuntimeError(f"Failed to start a LifeVault process: {exc}") from exc


def run_launch_plan(plan: LaunchPlan) -> int:
    processes: list[subprocess.Popen[bytes]] = []
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    previous_handlers: dict[signal.Signals, object] = {}
    handled_signals = [signal.SIGINT, signal.SIGTERM]
    for handled_signal in handled_signals:
        previous_handlers[handled_signal] = signal.getsignal(handled_signal)
        signal.signal(handled_signal, request_stop)

    try:
        if plan.worker_command:
            processes.append(_start_process(plan.worker_command, plan.environment))
        streamlit = _start_process(plan.streamlit_command, plan.environment)
        processes.append(streamlit)

        while not stopping:
            for process in processes:
                return_code = process.poll()
                if return_code is None:
                    continue
                if process is streamlit:
                    return return_code
                print("Reminder Worker stopped unexpectedly.", file=sys.stderr, flush=True)
                return return_code or 1
            time.sleep(0.25)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_processes(processes)
        for handled_signal, previous in previous_handlers.items():
            signal.signal(handled_signal, previous)


def find_available_port(host: str, preferred_port: int, attempts: int = 20) -> int:
    candidates: Sequence[int]
    last_candidate = preferred_port
    if preferred_port == 0:
        candidates = (0,)
    else:
        last_candidate = min(preferred_port + attempts - 1, 65535)
        candidates = range(preferred_port, last_candidate + 1)
    for candidate in candidates:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, candidate))
            except OSError:
                continue
            return int(probe.getsockname()[1])
    raise RuntimeError(
        f"No available local port found from {preferred_port} "
        f"through {last_candidate}."
    )


def _require_loopback(host: str) -> None:
    if host == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("LifeVault serve only accepts a loopback address") from exc
    if not address.is_loopback:
        raise ValueError("LifeVault serve only accepts a loopback address")


def _stop_processes(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    for process in reversed(processes):
        if process.poll() is not None:
            continue
        timeout = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _start_process(
    command: tuple[str, ...],
    environment: tuple[tuple[str, str], ...],
) -> subprocess.Popen[bytes]:
    child_environment = os.environ.copy()
    child_environment.update(environment)
    return subprocess.Popen(
        command,
        env=child_environment,
        start_new_session=os.name == "posix",
    )
