"""Fixtures for donetick-mcp integration tests.

Downloads the Donetick server binary from GitHub releases, starts a local
instance backed by SQLite, creates a test user, and provides a
``DonetickClient`` ready for use.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from donetick_mcp import DonetickClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DONETICK_VERSION = os.environ.get("DONETICK_VERSION", "0.1.75")
GITHUB_RELEASE_URL = "https://github.com/donetick/donetick/releases/download"

TEST_USERNAME = "testuser"
TEST_PASSWORD = "testpassword123"
TEST_EMAIL = "test@example.com"
TEST_DISPLAY_NAME = "Test User"


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
    arch = _arch()
    return f"donetick_{system}_{arch}.tar.gz"


def _download_binary(dest: Path) -> Path:
    """Download and extract the Donetick binary, returning the path."""
    binary_path = dest / "donetick"
    if binary_path.exists():
        return binary_path

    asset = _asset_name()
    url = f"{GITHUB_RELEASE_URL}/v{DONETICK_VERSION}/{asset}"
    tar_path = dest / asset

    print(f"Downloading Donetick {DONETICK_VERSION} from {url} ...")
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        resp = client.get(url)
        resp.raise_for_status()
        tar_path.write_bytes(resp.content)

    # Extract tar.gz – look for the 'donetick' binary inside the archive
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if os.path.basename(member.name) == "donetick" and member.isfile():
                member.name = "donetick"  # flatten path
                tf.extract(member, path=dest)
                break
        else:
            msg = f"'donetick' binary not found in archive {asset}"
            raise FileNotFoundError(msg)

    # Make executable
    binary_path.chmod(binary_path.stat().st_mode | stat.S_IEXEC)
    tar_path.unlink()
    return binary_path


def _free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]



def _wait_for_server(
    base_url: str, proc: subprocess.Popen, timeout: float = 30.0
) -> None:
    """Poll the server until it responds or timeout is reached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ret = proc.poll()
        if ret is not None:
            output = proc.stdout.read().decode() if proc.stdout else ""
            msg = f"Donetick server exited immediately with code {ret}:\n{output}"
            raise RuntimeError(msg)
        try:
            resp = httpx.get(f"{base_url}/api/v1/auth/", timeout=2)
            # Any response (even 404) means the server is up
            return
        except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException):
            time.sleep(0.3)
    output = proc.stdout.read().decode() if proc.stdout else ""
    msg = f"Donetick server did not start within {timeout}s. Output:\n{output}"
    raise TimeoutError(msg)


def _create_user(base_url: str) -> None:
    """Register a test user via the Donetick API."""
    resp = httpx.post(
        f"{base_url}/api/v1/auth/",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
            "email": TEST_EMAIL,
            "displayName": TEST_DISPLAY_NAME,
        },
        timeout=10,
    )
    # 409 means user already exists, which is fine
    if resp.status_code not in (200, 201, 409):
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Cache the binary across test runs in a well-known temp location
_CACHE_DIR = Path(tempfile.gettempdir()) / "donetick-mcp-test-cache"


@pytest.fixture(scope="session")
def donetick_binary() -> Path:
    """Download (or reuse cached) Donetick binary."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _download_binary(_CACHE_DIR)


@pytest.fixture(scope="session")
def donetick_server(donetick_binary: Path, tmp_path_factory: pytest.TempPathFactory):
    """Start a local Donetick server for the test session.

    Yields ``(base_url, process)`` and tears down after all tests.
    """
    work_dir = tmp_path_factory.mktemp("donetick")
    port = _free_port()
    db_path = work_dir / "donetick-test.db"

    base_url = f"http://127.0.0.1:{port}"

    env = {
        **os.environ,
        "DT_ENV": "selfhosted",
        "DT_NAME": "donetick-test",
        "DT_IS_DONE_TICK_DOT_COM": "false",
        "DT_IS_USER_CREATION_DISABLED": "false",
        "DT_DATABASE_TYPE": "sqlite",
        "DT_DATABASE_MIGRATION": "true",
        "DT_SQLITE_PATH": str(db_path),
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
        output = proc.stdout.read()
        print(output.decode())
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope="session")
def client(donetick_server: str) -> DonetickClient:
    """Provide an authenticated DonetickClient pointing at the local server."""
    c = DonetickClient(
        url=donetick_server,
        username=TEST_USERNAME,
        password=TEST_PASSWORD,
        timeout=10,
    )
    # Force initial auth
    c._ensure_auth()
    return c
