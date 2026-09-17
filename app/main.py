import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.auth import require_user
from app.config import settings
from app.routers import auth, datagen, monitor, pages, reports, tasks
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
    datagen_recovered = db.recover_datagen_orphans()
    if recovered or datagen_recovered:
        logger.warning(
            "recovered {} orphan run(s) and {} datagen job(s) to failed",
            recovered,
            datagen_recovered,
        )
    runner.set_on_finish(lambda run_id: generate_report(run_id))
    task = asyncio.create_task(collector.run_loop())
    logger.info("SQL Pulse started, data dir: {}", settings.data_dir)
    yield
    task.cancel()
    logger.info("SQL Pulse stopped")


app = FastAPI(title="SQL Pulse", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    session_cookie=settings.auth_session_cookie,
    max_age=settings.auth_session_ttl_seconds,
    same_site="lax",
    https_only=settings.auth_cookie_secure,
)
setup_logging()


@app.get("/healthz")
def healthz():
    return JSONResponse({"status": "ok"})


app.include_router(auth.router)
app.include_router(pages.router, dependencies=[Depends(require_user)])
app.include_router(datagen.router, dependencies=[Depends(require_user)])
app.include_router(tasks.router, dependencies=[Depends(require_user)])
app.include_router(monitor.router, dependencies=[Depends(require_user)])
app.include_router(reports.router, dependencies=[Depends(require_user)])
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
