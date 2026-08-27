from fastapi import FastAPI

app = FastAPI(title="Hotel Scrape")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
