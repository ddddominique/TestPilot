"""Small local process launcher, not a production supervisor. No dependencies."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


class Dev:
    def __init__(self):
        self.children = []
        self.stopping = False
        self.compose_started = False

    def interrupted(self, signum, frame):
        self.stopping = True

    def start(self, name, command, *, cwd=ROOT, env=None):
        if self.stopping:
            raise InterruptedError()
        print(f"[dev] Starting {name}", flush=True)
        # Separate groups prevent terminal Ctrl+C from cold-stopping Celery's
        # prefork children. We explicitly request warm shutdown of its parent.
        process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        self.children.append((name, process))
        return process

    def wait(self, process):
        while process.poll() is None:
            if self.stopping:
                raise InterruptedError()
            time.sleep(0.1)
        if process.returncode:
            raise RuntimeError(f"Startup command exited with code {process.returncode}.")

    @staticmethod
    def group_signal(process, signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    def shutdown(self):
        # Ignore repeated Ctrl+C during cleanup; killing a Celery prefork child
        # can bypass the Docker executor's finally block.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print("\n[dev] Shutting down; waiting for active Celery work to finish…", flush=True)
        for name, process in reversed(self.children):
            if name == "Celery":
                if process.poll() is None:
                    try:
                        process.terminate()  # Warm shutdown: signal the parent only.
                    except ProcessLookupError:
                        pass
            else:
                self.group_signal(process, signal.SIGTERM)
        for name, process in reversed(self.children):
            if name == "Celery":
                process.wait()  # Existing task hard limit still applies.
                self.group_signal(process, signal.SIGKILL)
            else:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                # npm/Next.js may leave descendants after the npm parent exits.
                self.group_signal(process, signal.SIGKILL)
                process.wait()
        if self.compose_started:
            try:
                result = subprocess.run(["docker", "compose", "stop", "postgres", "redis"], cwd=ROOT)
                stopped = result.returncode == 0
            except OSError:
                stopped = False
            if not stopped:
                print("[dev] Could not stop Docker services. Run make stop after Docker is available.", file=sys.stderr)

    def run(self):
        for command in ("docker", "node", "npm"):
            if shutil.which(command) is None:
                raise RuntimeError(f"{command} is not on PATH. See README local setup instructions.")
        if not (ROOT / "frontend/node_modules/.bin/next").exists():
            raise RuntimeError("Frontend dependencies are missing. Run cd frontend && npm ci first.")
        if not os.getenv("DATABASE_URL") or not os.getenv("POSTGRES_PASSWORD"):
            raise RuntimeError("Configure DATABASE_URL and POSTGRES_PASSWORD in the root .env.")
        self.compose_started = True  # Also stop partially started services on failure.
        self.wait(self.start("PostgreSQL and Redis", ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"]))
        self.start("database/broker logs", ["docker", "compose", "logs", "--follow", "--tail", "30", "postgres", "redis"])
        python = str(ROOT / ".venv/bin/python")
        self.start("FastAPI", [python, "-u", "-m", "uvicorn", "testpilot.api.app:app", "--host", "127.0.0.1", "--port", "8000"])
        self.start("Celery", [python, "-u", "-m", "celery", "-A", "testpilot.worker.celery_app:app", "worker", "--pool=prefork", "--concurrency=1", "--loglevel=INFO"])
        frontend_env = os.environ.copy()
        for key in ("OPENAI_API_KEY", "GITHUB_TOKEN", "DATABASE_URL", "CELERY_BROKER_URL", "POSTGRES_PASSWORD"):
            frontend_env.pop(key, None)
        self.start("Next.js", ["npm", "run", "dev", "--", "--port", "3000"], cwd=ROOT / "frontend", env=frontend_env)
        print("[dev] Frontend http://localhost:3000 · API http://127.0.0.1:8000/docs · Ctrl+C to stop", flush=True)
        while not self.stopping:
            for name, process in self.children:
                if name != "PostgreSQL and Redis" and process.poll() is not None:
                    raise RuntimeError(f"{name} exited (code {process.returncode}); stopping the other services.")
            time.sleep(0.2)


def main():
    dev = Dev()
    signal.signal(signal.SIGINT, dev.interrupted)
    signal.signal(signal.SIGTERM, dev.interrupted)
    try:
        dev.run()
        return 0
    except InterruptedError:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"[dev] {exc}", file=sys.stderr)
        return 1
    finally:
        dev.shutdown()


if __name__ == "__main__":
    sys.exit(main())
