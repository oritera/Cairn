from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import auth, db
from cairn.server.routers import export, hints, intents, projects, settings

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    auth.configure()
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

# Default-deny: every route below needs a bearer token or a console session, except the
# paths listed in auth.PUBLIC_PATHS / auth.PUBLIC_PREFIXES (console shell, static assets,
# /health and the /auth endpoints themselves).
app.add_middleware(auth.AuthMiddleware)

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)


@app.get("/health", tags=["health"])
def health():
    """Unauthenticated liveness probe: confirms the process is up and the database opens."""
    with db.get_conn() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
