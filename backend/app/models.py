from datetime import date

from pydantic import BaseModel, model_validator


class SearchRequest(BaseModel):
    location: str
    check_in: date
    check_out: date
    adults: int
    rooms: int

    @model_validator(mode="after")
    def validate_dates_and_counts(self) -> "SearchRequest":
        if self.check_out <= self.check_in:
            raise ValueError("check_out must be after check_in")
        if self.adults < 1:
            raise ValueError("adults must be at least 1")
        if self.rooms < 1:
            raise ValueError("rooms must be at least 1")
        return self


class Hotel(BaseModel):
    name: str
    price_per_night: float | None
    total_price: float | None
    currency: str
    url: str | None = None
    code: str | None = None
    supports_prepay: bool | None = None


class SearchResponse(BaseModel):
    hotels: list[Hotel]


class SearchHistoryEntry(BaseModel):
    key: str
    location: str
    check_in: str
    check_out: str
    adults: int
    rooms: int
    hotel_count: int
    prepay_checked_count: int
    complete: bool


class SearchHistoryResponse(BaseModel):
    searches: list[SearchHistoryEntry]


class ErrorResponse(BaseModel):
    error: str
