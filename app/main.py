import asyncio
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from models import ClusterStatus
from monitor import ClusterMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
HISTORY_SIZE = int(os.getenv("HISTORY_SIZE", "48"))

monitor = ClusterMonitor()
latest_status: ClusterStatus | None = None
history: deque[ClusterStatus] = deque(maxlen=HISTORY_SIZE)
_check_in_progress = False


async def monitoring_loop():
    global latest_status, _check_in_progress
    while True:
        try:
            _check_in_progress = True
            logger.info("Running cluster check...")
            status = await monitor.check()
            latest_status = status
            history.append(status)
            issue_count = len(status.issues)
            logger.info("Check complete: %d issue(s) found, LLM=%s", issue_count, status.llm_available)
        except Exception as e:
            logger.error("Monitoring loop error: %s", e)
        finally:
            _check_in_progress = False
        await asyncio.sleep(CHECK_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitoring_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="K8s Cluster Monitor", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path("static/index.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/api/status")
async def get_status() -> dict:
    if latest_status is None:
        return {
            "ready": False,
            "checking": _check_in_progress,
            "message": "Initial check in progress...",
        }
    return {
        "ready": True,
        "checking": _check_in_progress,
        "status": latest_status.model_dump(mode="json"),
    }


@app.get("/api/history")
async def get_history() -> dict:
    return {
        "count": len(history),
        "checks": [s.model_dump(mode="json") for s in history],
    }


@app.post("/api/check")
async def trigger_check() -> dict:
    global latest_status, _check_in_progress
    if _check_in_progress:
        raise HTTPException(status_code=409, detail="Check already in progress")
    _check_in_progress = True
    try:
        status = await monitor.check()
        latest_status = status
        history.append(status)
        return {"ok": True, "status": status.model_dump(mode="json")}
    finally:
        _check_in_progress = False


@app.get("/api/health")
async def health():
    return {"ok": True, "timestamp": datetime.now().isoformat()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=False)
