"""Fixtures for donetick-mcp tests.

Two layers of testing:

* **Unit tests** (``test_models.py``, ``test_transforms.py``,
  ``test_client.py``, ``test_mcp_tools.py``) run against an in-process
  ``httpx.MockTransport`` and need no network or server binary.  The MCP tool
  tests drive an in-process FastMCP ``Client`` over that same mock transport.
* **Integration tests** (``test_chores.py``, ``test_users.py``) download the
  Donetick release binary, run it locally on SQLite, and exercise the real API.

Integration tests are skipped unless ``DONETICK_INTEGRATION=1`` is set, so the
default test run stays fast and offline.
"""

from __future__ import annotations

import os
import platform
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from donetick_mcp import Config, DonetickClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Newer releases renamed the due-date field from ``dueDate`` to
#: ``nextDueDate``.  Pinning an old release is what let the original
#: implementation ship a due-date bug unnoticed, so default to current.
DONETICK_VERSION = os.environ.get("DONETICK_VERSION", "0.1.79")
GITHUB_RELEASE_URL = "https://github.com/donetick/donetick/releases/download"

TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpassword123"
TEST_EMAIL = "test@example.com"
TEST_DISPLAY_NAME = "Test User"

INTEGRATION_ENV = "DONETICK_INTEGRATION"

requires_integration = pytest.mark.skipif(
    os.environ.get(INTEGRATION_ENV) != "1",
    reason=f"set {INTEGRATION_ENV}=1 to run tests against a real Donetick server",
)


# ---------------------------------------------------------------------------
# Unit-test helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_config() -> Any:
    """Build a :class:`Config` with test-friendly defaults."""

    def _make(**overrides: Any) -> Config:
        defaults: dict[str, Any] = {
            "url": "https://donetick.test",
            "username": "testuser",
            "password": "testpassword123",
            "timeout": 5.0,
            "timezone": "UTC",
            "rate_limit_per_second": 1000.0,
            "rate_limit_burst": 1000,
            "max_retries": 2,
            "verify_tls": True,
            "log_level": "WARNING",
        }
        defaults.update(overrides)
        return Config(**defaults)

    return _make


class Recorder:
    """A ``httpx.MockTransport`` handler that records requests and replays replies.

    ``replies`` maps ``(METHOD, path)`` to either a response, a list of
    responses consumed in order, or a callable taking the request.
    """

    def __init__(self, replies: dict[tuple[str, str], Any]) -> None:
        self.replies = replies
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            try:
                self.bodies.append(request.read().decode())
            except Exception:  # pragma: no cover - defensive
                self.bodies.append(None)
        else:
            self.bodies.append(None)

        key = (request.method, request.url.path)
        reply = self.replies.get(key)
        if reply is None:
            return httpx.Response(404, json={"error": f"no mock for {key}"})
        if isinstance(reply, list):
            if not reply:
                return httpx.Response(500, json={"error": "mock replies exhausted"})
            return reply.pop(0)
        if callable(reply):
            return reply(request)
        return reply

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]

    def last_json(self) -> Any:
        """Decode the most recent request body."""
        import json

        return json.loads(self.bodies[-1])


@pytest.fixture
def recorder() -> Recorder:
    """A fresh request recorder."""
    return Recorder({})


@pytest.fixture
def mock_client(make_config: Any) -> Iterator[Any]:
    """Yield a ``(client, recorder)`` pair backed by a mock transport.

    Authentication is pre-seeded so tests do not have to model the login
    handshake unless they want to.
    """
    recorders: list[Recorder] = []

    def _build(replies: dict[tuple[str, str], Any], **config_overrides: Any) -> tuple[DonetickClient, Recorder]:
        rec = Recorder(replies)
        recorders.append(rec)
        transport = httpx.MockTransport(rec)
        http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        client = DonetickClient(make_config(**config_overrides), client=http)
        client._token = "test-token"
        client._token_expire = time.time() + 3600
        return client, rec

    yield _build


# ---------------------------------------------------------------------------
# Integration-test helpers
# ---------------------------------------------------------------------------


def _arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine.startswith("armv7"):
        return "armv7"
    if machine.startswith("armv6"):
        return "armv6"
    return machine


def _asset_name() -> str:
    """Return the expected release asset filename for the current platform."""
    system = platform.system()  # e.g. "Linux", "Darwin" (already capitalised)
    return f"donetick_{system}_{_arch()}.tar.gz"


def _download_binary(dest: Path) -> Path:
    """Download and extract the Donetick binary, returning its path."""
    binary_path = dest / "donetick"
    if binary_path.exists():
        return binary_path

    asset = _asset_name()
    url = f"{GITHUB_RELEASE_URL}/v{DONETICK_VERSION}/{asset}"
    tar_path = dest / asset

    print(f"Downloading Donetick {DONETICK_VERSION} from {url} ...")
    with httpx.Client(follow_redirects=True, timeout=120) as http:
        response = http.get(url)
        response.raise_for_status()
        tar_path.write_bytes(response.content)

    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive.getmembers():
            if os.path.basename(member.name) == "donetick" and member.isfile():
                member.name = "donetick"  # flatten path
                archive.extract(member, path=dest)
                break
        else:
            msg = f"'donetick' binary not found in archive {asset}"
            raise FileNotFoundError(msg)

    binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC)
    tar_path.unlink()
    return binary_path


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(
    base_url: str, proc: subprocess.Popen[bytes], timeout: float = 30.0
) -> None:
    """Poll the server until it responds or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ret = proc.poll()
        if ret is not None:
            output = proc.stdout.read().decode() if proc.stdout else ""
            msg = f"Donetick server exited immediately with code {ret}:\n{output}"
            raise RuntimeError(msg)
        try:
            httpx.get(f"{base_url}/api/v1/auth/", timeout=2)
            # Any response (even 404) means the server is listening.
            return
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            time.sleep(0.3)
    output = proc.stdout.read().decode() if proc.stdout else ""
    msg = f"Donetick server did not start within {timeout}s. Output:\n{output}"
    raise TimeoutError(msg)


def _create_user(base_url: str) -> None:
    """Register a test user via the Donetick API."""
    response = httpx.post(
        f"{base_url}/api/v1/auth/",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
            "email": TEST_EMAIL,
            "displayName": TEST_DISPLAY_NAME,
        },
        timeout=10,
    )
    # 409 means the user already exists, which is fine.
    if response.status_code not in (200, 201, 409):
        response.raise_for_status()


_CACHE_DIR = Path(tempfile.gettempdir()) / "donetick-mcp-test-cache"


@pytest.fixture(scope="session")
def donetick_binary() -> Path:
    """Download (or reuse a cached) Donetick binary."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _download_binary(_CACHE_DIR)


@pytest.fixture(scope="session")
def donetick_server(donetick_binary: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run a local Donetick server for the test session, yielding its base URL."""
    work_dir = tmp_path_factory.mktemp("donetick")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "DT_ENV": "selfhosted",
        "DT_NAME": "donetick-test",
        "DT_IS_DONE_TICK_DOT_COM": "false",
        "DT_IS_USER_CREATION_DISABLED": "false",
        "DT_DATABASE_TYPE": "sqlite",
        "DT_DATABASE_MIGRATION": "true",
        "DT_SQLITE_PATH": str(work_dir / "donetick-test.db"),
        "DT_JWT_SECRET": "test-secret-key-for-integration-tests-minimum-32-chars",
        "DT_JWT_SESSION_TIME": "168h",
        "DT_JWT_MAX_REFRESH": "168h",
        "DT_SERVER_PORT": str(port),
        "DT_SERVER_READ_TIMEOUT": "10s",
        "DT_SERVER_WRITE_TIMEOUT": "10s",
        "DT_SERVER_RATE_PERIOD": "60s",
        "DT_SERVER_RATE_LIMIT": "300",
        "DT_SERVER_SERVE_FRONTEND": "false",
        "DT_TELEGRAM_TOKEN": "",
    }

    proc = subprocess.Popen(
        [str(donetick_binary)],
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_server(base_url, proc)
        _create_user(base_url)
        yield base_url
    finally:
        proc.terminate()
        if proc.stdout:
            print(proc.stdout.read().decode())
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture
def config(donetick_server: str) -> Config:
    """A Config pointing at the local test server."""
    return Config(
        url=donetick_server,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        timeout=10.0,
        timezone="UTC",
        rate_limit_per_second=50.0,
        rate_limit_burst=50,
        max_retries=2,
        verify_tls=True,
        log_level="WARNING",
    )


@pytest.fixture
async def client(config: Config) -> AsyncIterator[DonetickClient]:
    """An authenticated client pointed at the local server."""
    async with DonetickClient(config) as authenticated:
        await authenticated.ensure_auth()
        yield authenticated