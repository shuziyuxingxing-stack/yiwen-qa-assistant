from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.services.sysu_anything_chat import sysu_anything_chat


@asynccontextmanager
async def lifespan(app: FastAPI):
    sysu_anything_chat.start_keepalive()
    try:
        yield
    finally:
        await sysu_anything_chat.stop_keepalive()


app = FastAPI(title="Yiwen Gateway POC", version="0.1.0", lifespan=lifespan)
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
    return RedirectResponse(url="/static/index.html?v=20260704-freshman-channel")

