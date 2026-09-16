import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from loguru import logger

from app import db
from app.config import settings
from app.routers import monitor, pages, reports, tasks
from app.services.collector import collector
from app.services.report import generate_report
from app.services.runner import runner


def setup_logging() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    logger.add(
        settings.logs_dir / "app.log",
        rotation="10 MB", retention=10, level="DEBUG", encoding="utf-8",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_schema()
    recovered = db.recover_orphans()
    if recovered:
        logger.warning("recovered {} orphan run(s) to failed", recovered)
    runner.set_on_finish(lambda run_id: generate_report(run_id))
    task = asyncio.create_task(collector.run_loop())
    logger.info("SQL Pulse started, data dir: {}", settings.data_dir)
    yield
    task.cancel()
    logger.info("SQL Pulse stopped")


app = FastAPI(title="SQL Pulse", version="0.1.0", lifespan=lifespan)
setup_logging()


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


app.include_router(pages.router)
app.include_router(tasks.router)
app.include_router(monitor.router)
app.include_router(reports.router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
