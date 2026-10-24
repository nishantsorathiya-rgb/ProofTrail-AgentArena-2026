"""Exercise local Uvicorn and persistence, saving measured P1 evidence only."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.application.runs import ResearchRunService
from app.infrastructure.database import Database, SQLiteRunRepository

ROOT = Path(__file__).resolve().parents[1]


async def persistence_example(url: str) -> dict:
    database = Database(url)
    try:
        await database.initialize()
        run = await ResearchRunService(SQLiteRunRepository(database)).create(
            "OFFLINE P1 persistence fixture; no research executed.",
        )
    finally:
        await database.aclose()
    reopened = Database(url)
    try:
        await reopened.initialize()
        restored = await SQLiteRunRepository(reopened).get(run.run_id)
        assert restored == run
        return {"survived_reopen": True, "run": asdict(restored)}
    finally:
        await reopened.aclose()


def serialize(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError("Unexpected smoke result type.")


def main() -> int:
    if sys.version_info[:2] not in {(3, 11), (3, 12)}:
        raise SystemExit("Python 3.11 or 3.12 required.")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="prooftrail-p1-") as directory:
        database_url = f"sqlite:///{(Path(directory) / 'smoke.db').as_posix()}"
        environment = os.environ.copy()
        environment.update(DATABASE_URL=database_url, ENABLE_LIVE_REQUESTS="false")
        environment.pop("MESH_API_KEY", None)
        environment.pop("TAVILY_API_KEY", None)
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ]
        clock = time.perf_counter()
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            endpoint = f"http://127.0.0.1:{port}/health"
            # Ignore environment proxy settings even for the local-only request.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.perf_counter() + 20
            while True:
                try:
                    request_started = time.perf_counter()
                    with opener.open(endpoint, timeout=1) as response:
                        status = response.status
                        headers = dict(response.headers)
                        body = json.loads(response.read())
                    request_seconds = time.perf_counter() - request_started
                    break
                except urllib.error.URLError:
                    if process.poll() is not None or time.perf_counter() >= deadline:
                        raise RuntimeError("Local Uvicorn smoke startup failed.") from None
                    time.sleep(0.1)
            assert status == 200
            assert body == {"status": "ok", "phase": "P1", "database": "sqlite"}
            result = {
                "phase": "P1",
                "recorded_at_utc": datetime.now(UTC).isoformat(),
                "mode": "local HTTP and offline persistence fixture",
                "command": command,
                "status_code": status,
                "response": body,
                "request_id": headers["x-request-id"],
                "startup_and_probe_seconds": round(time.perf_counter() - clock, 4),
                "single_health_request_seconds": round(request_seconds, 4),
                "live_mesh_calls": 0,
                "live_tavily_calls": 0,
                "api_cost": 0,
            }
        finally:
            process.terminate()
            try:
                output, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate(timeout=5)
        result["server_stdout"] = output
        result["server_stderr"] = errors
        result["persistence"] = asyncio.run(persistence_example(database_url))
        result["passed"] = True
        destination = ROOT / "results"
        destination.mkdir(exist_ok=True)
        (destination / "p1_http_smoke.json").write_text(
            json.dumps(result, indent=2, default=serialize) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status_code": status,
                    "response": body,
                    "request_id": result["request_id"],
                    "survived_reopen": True,
                    "live_mesh_calls": 0,
                    "live_tavily_calls": 0,
                }
            )
        )
        print("Local smoke PASS; saved results/p1_http_smoke.json; server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
