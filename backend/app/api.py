from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.app.models import ErrorResponse, SearchRequest, SearchResponse
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperTimeoutError
from backend.app.scraper.marriott import search

router = APIRouter()


@router.post("/api/search", response_model=SearchResponse)
async def search_hotels(req: SearchRequest):
    try:
        hotels = await search(req)
    except (ScraperBlockedError, ScraperTimeoutError) as exc:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )
    return SearchResponse(hotels=hotels)
