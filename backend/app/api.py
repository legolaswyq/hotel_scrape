from fastapi import APIRouter
from fastapi.responses import JSONResponse

from backend.app.models import (
    ErrorResponse,
    SearchHistoryEntry,
    SearchHistoryResponse,
    SearchRequest,
    SearchResponse,
)
from backend.app.scraper import search_result_store
from backend.app.scraper.exceptions import (
    ScraperBlockedError,
    ScraperInterruptedError,
    ScraperTimeoutError,
)
from backend.app.scraper.marriott import search
from backend.app.scraper.marriott_prepay import check_prepay

router = APIRouter()


@router.get("/api/search-history", response_model=SearchHistoryResponse)
async def search_history():
    """Every search cached locally, most recently searched first, so the
    frontend can offer them as one-click re-searches instead of the user
    retyping location/dates."""
    summaries = search_result_store.list_all()
    return SearchHistoryResponse(
        searches=[
            SearchHistoryEntry(
                location=s.location,
                check_in=s.check_in,
                check_out=s.check_out,
                adults=s.adults,
                rooms=s.rooms,
                hotel_count=s.hotel_count,
                prepay_checked_count=s.prepay_checked_count,
                complete=s.complete,
            )
            for s in summaries
        ]
    )


@router.post("/api/search", response_model=SearchResponse)
async def search_hotels(req: SearchRequest, prepay_limit: int | None = None):
    """List every hotel for this search, then check each for a Prepay
    Non-refundable rate (see check_prepay) -- `hotel.supports_prepay` is
    True/False once checked, None if not yet checked this call.

    If listing itself fails, the whole request fails (502) -- there's
    nothing to show. If prepay checking fails/blocks partway through, the
    listing is still returned as-is (some hotels may have supports_prepay
    still None) rather than discarding it.
    """
    try:
        hotels = await search(req)
    except (ScraperBlockedError, ScraperTimeoutError, ScraperInterruptedError) as exc:
        return JSONResponse(
            status_code=502,
            content=ErrorResponse(error=str(exc)).model_dump(),
        )

    try:
        hotels = await check_prepay(req, hotels, limit=prepay_limit)
    except (ScraperBlockedError, ScraperTimeoutError, ScraperInterruptedError):
        pass

    return SearchResponse(hotels=hotels)
