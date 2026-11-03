"""End to end: start `python -m wewln_phonecaller serve` as a real process and call the probes."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from wewln_phonecaller.storage import Database


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_health(proc: subprocess.Popen, url: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            return get_json(url)
        except OSError:
            time.sleep(0.2)
    return None


def test_server_process_answers_probes(tmp_path):
    port = free_port()
    data_dir = tmp_path / "data"
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("WEWLN_")}
    env.update(WEWLN_DATA_DIR=str(data_dir), WEWLN_HTTP_PORT=str(port))
    output = tmp_path / "server-output.txt"
    base = f"http://127.0.0.1:{port}"
    with output.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "wewln_phonecaller", "--env-file", "", "serve"],
            cwd=tmp_path,
            env=env,
            stdout=sink,
            stderr=subprocess.STDOUT,
        )
        try:
            health = wait_for_health(proc, f"{base}/health")
            assert health is not None, output.read_text(encoding="utf-8", errors="replace")
            assert health == (200, health[1])
            assert health[1]["status"] == "ok"
            status, ready = get_json(f"{base}/ready")
            assert status == 200
            assert ready["status"] == "ready"
            assert ready["checks"]["real_calls"]["allowed"] is False
            status, info = get_json(f"{base}/api/v1/info")
            assert status == 401
            assert info["error"]["code"] == "auth_not_configured"
        finally:
            proc.terminate()  # SIGTERM on POSIX (graceful), TerminateProcess on Windows
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)

    log_text = (data_dir / "logs" / "wewln.log").read_text(encoding="utf-8")
    assert "server_starting" in log_text
    actions = [entry["action"] for entry in Database(data_dir / "wewln.db").recent_audit()]
    assert "app.start" in actions
    if sys.platform != "win32":
        assert proc.returncode == 0
        assert actions[0] == "app.stop"
