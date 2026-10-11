import asyncio
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_PORT = 8011
BASE = f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture(scope="session")
def sandbox_url():
    """The real sandbox app, served on a test port for browser-driven tests."""
    from sandbox.app import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield BASE
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def reset_sandbox(sandbox_url):
    def _reset(chaos: str = "off"):
        httpx.post(f"{sandbox_url}/__admin/reset", params={"chaos": chaos}).raise_for_status()
    _reset()
    return _reset


@pytest.fixture
def sandbox_state(sandbox_url):
    return lambda: httpx.get(f"{sandbox_url}/__admin/state").json()


@pytest.fixture
def settings(sandbox_url, tmp_path):
    from worker.config import Settings
    return Settings(provider="scripted", start_url=f"{sandbox_url}/", allowed_origins=(sandbox_url,),
                    runs_dir=tmp_path / "runs", use_lessons=False, max_steps=30)


@pytest.fixture
def run_worker(settings):
    """Run the real Worker loop with a scripted provider and scripted human."""
    from worker.agent import Worker
    from worker.events import RunRecorder
    from worker.runner import new_event_loop

    def _run(provider, human, task="test task", **overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        recorder = RunRecorder("test", settings.runs_dir / "test")
        loop = new_event_loop()
        try:
            result = loop.run_until_complete(Worker(settings, provider).run(task, recorder, human))
        finally:
            loop.close()
        recorder.close()
        return result, recorder
    return _run
