from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.app.models import ErrorResponse, SearchRequest, SearchResponse
from backend.app.scraper.exceptions import (
    ScraperBlockedError,
    ScraperInterruptedError,
    ScraperTimeoutError,
)
from backend.app.scraper.marriott import search
from backend.app.scraper.marriott_prepay import search_prepay

router = APIRouter()


@router.post("/api/search", response_model=SearchResponse)
async def search_hotels(req: SearchRequest):
    try:
        hotels = await search(req)
    except (ScraperBlockedError, ScraperTimeoutError, ScraperInterruptedError) as exc:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )
    return SearchResponse(hotels=hotels)


@router.post("/api/search-prepay", response_model=SearchResponse)
async def search_hotels_prepay(req: SearchRequest, limit: int | None = None):
    try:
        hotels = await search_prepay(req, limit=limit)
    except (ScraperBlockedError, ScraperTimeoutError, ScraperInterruptedError) as exc:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )
    return SearchResponse(hotels=hotels)
