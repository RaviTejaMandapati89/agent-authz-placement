"""Start and stop the domain server as a subprocess."""
import contextlib
import fcntl
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
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
def _port_claim():
    """Hold a machine-wide lock from choosing a free port until the server that
    owns it is listening, so two workers starting at once never pick the same
    port (task 8, item 3)."""
    lock_path = pathlib.Path(tempfile.gettempdir()) / "guides-vs-gates-port.lock"
    with lock_path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def domain_server(decision_log: str, env_extra: dict | None = None):
    """Start domain server on a free port, yield (port, base_url), then stop."""
    claim = contextlib.ExitStack()
    claim.enter_context(_port_claim())
    port = free_port()
    env = {
        **os.environ,
        "SERVER_PORT": str(port),
        "DECISION_LOG": decision_log,
    }
    for key in ("_GATEWAY_TESTING", "GATEWAY_TEST_PLUGIN", "SERVER_TEST_MODE"):
        env.pop(key, None)
    env.update(env_extra or {})
    stderr_file = tempfile.NamedTemporaryFile(
        mode="w+", suffix=".stderr", delete=False,
    )
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "domain.server"],
            env=env,
            cwd=str(_REPO_ROOT),
            stderr=stderr_file,
        )
    except BaseException:
        claim.close()
        raise
    try:
        try:
            wait_for_port(port)
        finally:
            claim.close()
        base_url = f"http://127.0.0.1:{port}"
        yield port, base_url
    except RuntimeError:
        proc.terminate()
        proc.wait()
        stderr_file.seek(0)
        stderr = stderr_file.read()
        stderr_file.close()
        os.unlink(stderr_file.name)
        raise RuntimeError(
            f"port {port} did not open after timeout; server stderr:\n{stderr}"
        ) from None
    finally:
        proc.terminate()
        proc.wait()
        try:
            stderr_file.close()
            os.unlink(stderr_file.name)
        except FileNotFoundError:
            pass
