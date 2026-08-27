from fastapi import FastAPI
from fastapi.responses import FileResponse

from backend.app.api import router

app = FastAPI(title="Hotel Scrape")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse("frontend/index.html")
