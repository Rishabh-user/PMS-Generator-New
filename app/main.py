"""PMS Generator (new) — FastAPI entry point.

Step 1 only: serves the form UI plus four /api/options/* endpoints that
hand back the Pressure Rating / Material / Corrosion Allowance / Service
lists from app/data/*.json."""
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.routes.options_routes import router as options_router
from app.routes.resolve_routes import router as resolve_router
from app.routes.ai_routes import router as ai_router
from app.routes.export_routes import router as export_router
from app.routes.pms_agent_routes import router as pms_agent_router
from app.services import session_store


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


app = FastAPI(title=settings.app_name, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
templates = Jinja2Templates(directory=str(settings.templates_dir))

app.include_router(options_router)
app.include_router(resolve_router)
app.include_router(ai_router)
app.include_router(export_router)
app.include_router(pms_agent_router)


@app.on_event("startup")
def _init_session_store() -> None:
    """Create the PMS-Agent SQLite table the first time the server boots."""
    session_store.init()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "data_dir": str(settings.data_dir),
    }
