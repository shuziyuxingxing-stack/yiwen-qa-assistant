from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app.api.routes import router
from app.services.sysu_anything_chat import sysu_anything_chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    sysu_anything_chat.start_keepalive()
    try:
        yield
    finally:
        await sysu_anything_chat.stop_keepalive()


app = FastAPI(title="中大逸问问答助手", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html?v=20260712-cleanup")

@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse("app/static/favicon.svg", media_type="image/svg+xml")

