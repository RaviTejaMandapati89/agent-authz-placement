"""Start and stop the domain server as a subprocess."""
import contextlib
import os
import pathlib
import socket
import subprocess
import sys
import time

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open after {timeout}s")


@contextlib.contextmanager
def domain_server(decision_log: str, env_extra: dict | None = None):
    """Start domain server on a free port, yield (port, base_url), then stop."""
    port = free_port()
    env = {
        **os.environ,
        "SERVER_PORT": str(port),
        "DECISION_LOG": decision_log,
        **(env_extra or {}),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "domain.server"],
        env=env,
        cwd=str(_REPO_ROOT),
    )
    try:
        wait_for_port(port)
        base_url = f"http://127.0.0.1:{port}"
        yield port, base_url
    finally:
        proc.terminate()
        proc.wait()
