# Marriott Hotel Scraper (v1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local FastAPI web app where the user enters a location, date range, and guest count, and gets back a table of Marriott hotels with name / price-per-night / total price, scraped via a headless Playwright browser.

**Architecture:** A vanilla HTML/JS page posts search params to a single FastAPI endpoint. The endpoint calls a Marriott-specific scraper module that drives a real Chromium session through marriott.com's own search UI (destination autosuggest, dates, guests) end-to-end, then extracts hotel cards from the results page. No database; each request is stateless.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, Playwright (Python, Chromium), Pydantic, pytest.

## Global Constraints

- No plain HTTP scraping of marriott.com — confirmed to return `403` from Akamai bot protection (see spec). All Marriott access goes through Playwright's headless Chromium.
- No persistence layer, no auth, no scheduled/repeated runs (spec: out of scope for v1).
- Single site (Marriott) in v1; the scraper module's function signature must not take on FastAPI/HTTP-specific types, so a second site module can be added later without touching the API layer (spec: extensibility).
- No retries against Marriott on block/failure — an observed `Retry-After` was 8 hours, so retrying immediately is pointless (spec: error handling).

---

## File Structure

```
hotel_scrape/
  requirements.txt
  backend/
    app/
      __init__.py
      main.py            # FastAPI app, mounts frontend + API router
      api.py              # POST /api/search route
      models.py           # Pydantic request/response models
      scraper/
        __init__.py
        parsing.py       # pure text -> price parsing helpers
        marriott.py      # Playwright-driven search() for Marriott
    tests/
      __init__.py
      test_parsing.py
      test_models.py
      test_api.py
  frontend/
    index.html            # form + results table, vanilla JS fetch()
  README.md                # run instructions
```

---

### Task 1: Project scaffolding + health check

**Files:**
- Create: `requirements.txt`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/tests/__init__.py`
- Create: `backend/tests/test_main.py`

**Interfaces:**
- Produces: FastAPI `app` object importable as `backend.app.main:app`, with `GET /health` returning `{"status": "ok"}`.

- [ ] **Step 1: Create `requirements.txt`**

```
fastapi==0.141.1
uvicorn[standard]==0.52.4
playwright==1.62.0
pydantic==2.13.4
pytest==9.1.1
httpx==0.28.1
```

(Versions verified installable against the local Python 3.14 interpreter. If
your environment has an older Python, `pip install -r requirements.txt`
will still resolve compatible versions via pip's normal resolution.)

- [ ] **Step 2: Install dependencies and Playwright browser**

Run: `pip install -r requirements.txt && playwright install chromium`
Expected: installs succeed with no errors.

- [ ] **Step 3: Create `backend/app/__init__.py`** (empty file)

- [ ] **Step 4: Create `backend/app/main.py`**

```python
from fastapi import FastAPI

app = FastAPI(title="Hotel Scrape")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 5: Create `backend/tests/__init__.py`** (empty file)

- [ ] **Step 6: Write the failing test — `backend/tests/test_main.py`**

```python
from fastapi.testclient import TestClient

from backend.app.main import app

client = TestClient(app)


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 7: Run test to verify it passes**

Run: `pytest backend/tests/test_main.py -v`
Expected: `test_health_returns_ok PASSED` (main.py already exists, so this confirms wiring rather than TDD-red — that's fine for scaffolding).

- [ ] **Step 8: Commit**

```bash
git add requirements.txt backend/app/__init__.py backend/app/main.py backend/tests/__init__.py backend/tests/test_main.py
git commit -m "Scaffold FastAPI app with health check"
```

---

### Task 2: Price parsing utility

**Files:**
- Create: `backend/app/scraper/__init__.py`
- Create: `backend/app/scraper/parsing.py`
- Create: `backend/tests/test_parsing.py`

**Interfaces:**
- Produces: `parse_price(text: str | None) -> float | None` — used by `backend/app/scraper/marriott.py` (Task 4) to convert raw DOM text like `"$259"` or `"US$1,234.00"` into a float, returning `None` when no numeric price is present.

- [ ] **Step 1: Create `backend/app/scraper/__init__.py`** (empty file)

- [ ] **Step 2: Write the failing tests — `backend/tests/test_parsing.py`**

```python
from backend.app.scraper.parsing import parse_price


def test_parses_simple_dollar_amount():
    assert parse_price("$259") == 259.0


def test_parses_amount_with_thousands_separator():
    assert parse_price("$1,234.50") == 1234.50


def test_parses_amount_with_currency_prefix():
    assert parse_price("US$259.00") == 259.0


def test_returns_none_for_missing_text():
    assert parse_price(None) is None


def test_returns_none_for_text_without_digits():
    assert parse_price("Sold Out") is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest backend/tests/test_parsing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.scraper.parsing'`

- [ ] **Step 4: Write minimal implementation — `backend/app/scraper/parsing.py`**

```python
import re

_PRICE_RE = re.compile(r"[\d,]+\.?\d*")


def parse_price(text: str | None) -> float | None:
    """Extract a numeric price from raw display text, e.g. '$1,234.50' -> 1234.5."""
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    cleaned = match.group(0).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest backend/tests/test_parsing.py -v`
Expected: all 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/scraper/__init__.py backend/app/scraper/parsing.py backend/tests/test_parsing.py
git commit -m "Add price parsing utility"
```

---

### Task 3: Request/response models

**Files:**
- Create: `backend/app/models.py`
- Create: `backend/tests/test_models.py`

**Interfaces:**
- Produces:
  - `SearchRequest(location: str, check_in: date, check_out: date, adults: int, rooms: int)` — validates `check_out > check_in`, `adults >= 1`, `rooms >= 1`.
  - `Hotel(name: str, price_per_night: float | None, total_price: float | None, currency: str)`
  - `SearchResponse(hotels: list[Hotel])`
  - `ErrorResponse(error: str)`
- Consumed by: `backend/app/api.py` (Task 5), `backend/app/scraper/marriott.py` (Task 4).

- [ ] **Step 1: Write the failing tests — `backend/tests/test_models.py`**

```python
from datetime import date

import pytest
from pydantic import ValidationError

from backend.app.models import ErrorResponse, Hotel, SearchRequest, SearchResponse


def test_valid_search_request():
    req = SearchRequest(
        location="New York, NY",
        check_in=date(2026, 9, 11),
        check_out=date(2026, 9, 13),
        adults=1,
        rooms=1,
    )
    assert req.location == "New York, NY"


def test_rejects_checkout_before_checkin():
    with pytest.raises(ValidationError):
        SearchRequest(
            location="New York, NY",
            check_in=date(2026, 9, 13),
            check_out=date(2026, 9, 11),
            adults=1,
            rooms=1,
        )


def test_rejects_zero_adults():
    with pytest.raises(ValidationError):
        SearchRequest(
            location="New York, NY",
            check_in=date(2026, 9, 11),
            check_out=date(2026, 9, 13),
            adults=0,
            rooms=1,
        )


def test_hotel_and_response_roundtrip():
    hotel = Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD")
    response = SearchResponse(hotels=[hotel])
    assert response.hotels[0].name == "Test Hotel"


def test_error_response():
    err = ErrorResponse(error="blocked")
    assert err.error == "blocked"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.models'`

- [ ] **Step 3: Write minimal implementation — `backend/app/models.py`**

```python
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


class SearchResponse(BaseModel):
    hotels: list[Hotel]


class ErrorResponse(BaseModel):
    error: str
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest backend/tests/test_models.py -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models.py backend/tests/test_models.py
git commit -m "Add request/response models with validation"
```

---

### Task 4: Marriott scraper (Playwright-driven)

This task reverse-engineers a live third-party UI, so — unlike the other tasks — the exact selectors cannot be written from memory; they must be captured from the real site first, then encoded. Follow the two-part procedure below in order.

**Files:**
- Create: `backend/app/scraper/marriott.py`
- Create: `backend/app/scraper/exceptions.py`

**Interfaces:**
- Consumes: `SearchRequest` (Task 3), `parse_price` (Task 2).
- Produces: `async def search(req: SearchRequest) -> list[Hotel]`, raising `ScraperBlockedError` (site blocked/CAPTCHA/403) or `ScraperTimeoutError` (results never appeared) on failure. Used by `backend/app/api.py` (Task 5).

- [ ] **Step 1: Create `backend/app/scraper/exceptions.py`**

```python
class ScraperBlockedError(Exception):
    """Raised when the target site blocks or rejects the automated request."""


class ScraperTimeoutError(Exception):
    """Raised when expected page content never appears within the timeout."""
```

- [ ] **Step 2: Record a real search with Playwright codegen**

Run: `playwright codegen https://www.marriott.com/default.mi`

In the opened browser: type a destination (e.g. "New York, NY") into the search box, select the first autosuggest suggestion, set check-in/check-out dates, set adults/rooms, submit the search, and wait for hotel results to appear. Then close the browser.

Expected: Codegen prints a Python script to the terminal showing the exact selectors/actions used (e.g. `page.get_by_placeholder(...)`, `page.get_by_role("option", ...)`). Copy that output into a scratch file, e.g. `/tmp/marriott_codegen.py`, for reference in the next step. If Marriott returns a 403/CAPTCHA during this manual recording, wait out the `Retry-After` window observed earlier (or retry from a different network) before continuing — Step 3 cannot be written without a successful recording.

- [ ] **Step 3: Adapt the recorded flow into `backend/app/scraper/marriott.py`**

Use the selectors/actions captured in Step 2 in place of the illustrative ones below (the structure — navigate, fill destination, pick suggestion, set dates, set guests, submit, wait for results, extract cards — stays the same regardless of the exact selectors found):

```python
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

from backend.app.models import Hotel, SearchRequest
from backend.app.scraper.exceptions import ScraperBlockedError, ScraperTimeoutError
from backend.app.scraper.parsing import parse_price

SEARCH_URL = "https://www.marriott.com/default.mi"
RESULTS_TIMEOUT_MS = 30_000


async def search(req: SearchRequest) -> list[Hotel]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        try:
            response = await page.goto(SEARCH_URL, wait_until="domcontentloaded")
            if response is not None and response.status == 403:
                raise ScraperBlockedError(f"Marriott returned 403 for {SEARCH_URL}")

            # Replace the selectors below with what codegen captured in Step 2.
            await page.get_by_placeholder("Destination").fill(req.location)
            await page.get_by_role("option").first.click()
            await page.get_by_label("Check-in date").fill(req.check_in.strftime("%m/%d/%Y"))
            await page.get_by_label("Check-out date").fill(req.check_out.strftime("%m/%d/%Y"))
            await page.get_by_label("Adults").select_option(str(req.adults))
            await page.get_by_label("Rooms").select_option(str(req.rooms))
            await page.get_by_role("button", name="Search").click()

            await page.wait_for_selector("[data-testid='hotel-card']", timeout=RESULTS_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            raise ScraperTimeoutError(
                f"Timed out waiting for Marriott results for '{req.location}'"
            ) from exc

        cards = await page.query_selector_all("[data-testid='hotel-card']")
        hotels: list[Hotel] = []
        for card in cards:
            name_el = await card.query_selector("[data-testid='hotel-name']")
            price_el = await card.query_selector("[data-testid='price-per-night']")
            total_el = await card.query_selector("[data-testid='total-price']")

            name = (await name_el.inner_text()).strip() if name_el else None
            price_text = await price_el.inner_text() if price_el else None
            total_text = await total_el.inner_text() if total_el else None

            if not name:
                continue

            hotels.append(
                Hotel(
                    name=name,
                    price_per_night=parse_price(price_text),
                    total_price=parse_price(total_text),
                    currency="USD",
                )
            )

        await browser.close()
        return hotels
```

- [ ] **Step 4: Replace the placeholder selectors with the ones from Step 2's codegen output**

Edit the `# Replace the selectors below...` block and the `[data-testid='hotel-card']` / `hotel-name` / `price-per-night` / `total-price` selectors to match what codegen actually recorded and what you observe by inspecting a results page's DOM (Chrome DevTools, or `await page.pause()` inserted temporarily in the script). This step has no fixed expected diff — it's done when Step 5's manual run returns real hotel data.

- [ ] **Step 5: Manually verify end-to-end**

Run:
```bash
python -c "
import asyncio
from datetime import date
from backend.app.models import SearchRequest
from backend.app.scraper.marriott import search

req = SearchRequest(location='New York, NY', check_in=date(2026, 9, 11), check_out=date(2026, 9, 13), adults=1, rooms=1)
print(asyncio.run(search(req)))
"
```
Expected: prints a non-empty list of `Hotel(...)` objects with real names and at least one of `price_per_night`/`total_price` populated. If it raises `ScraperBlockedError`, Marriott is blocking the session — note this in the commit message and proceed (per spec, this is expected best-effort behavior); do not spend further time defeating bot detection in this task.

- [ ] **Step 6: Commit**

```bash
git add backend/app/scraper/marriott.py backend/app/scraper/exceptions.py
git commit -m "Add Playwright-driven Marriott scraper"
```

---

### Task 5: API endpoint

**Files:**
- Create: `backend/app/api.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_api.py`

**Interfaces:**
- Consumes: `SearchRequest`, `SearchResponse`, `ErrorResponse` (Task 3), `search()`, `ScraperBlockedError`, `ScraperTimeoutError` (Task 4).
- Produces: `POST /api/search` mounted on `app` — `200` with `SearchResponse` body on success, `422` with `ErrorResponse` body when the scraper can't resolve/reach the site in a way that's the caller's fault (none currently — reserved), `502` with `ErrorResponse` body on `ScraperBlockedError`/`ScraperTimeoutError`.

- [ ] **Step 1: Write the failing tests — `backend/tests/test_api.py`**

```python
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.app.main import app
from backend.app.models import Hotel
from backend.app.scraper.exceptions import ScraperBlockedError

client = TestClient(app)

VALID_PAYLOAD = {
    "location": "New York, NY",
    "check_in": "2026-09-11",
    "check_out": "2026-09-13",
    "adults": 1,
    "rooms": 1,
}


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_success(mock_search):
    mock_search.return_value = [
        Hotel(name="Test Hotel", price_per_night=100.0, total_price=200.0, currency="USD")
    ]
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {
        "hotels": [
            {
                "name": "Test Hotel",
                "price_per_night": 100.0,
                "total_price": 200.0,
                "currency": "USD",
            }
        ]
    }


@patch("backend.app.api.search", new_callable=AsyncMock)
def test_search_blocked_returns_502(mock_search):
    mock_search.side_effect = ScraperBlockedError("blocked")
    response = client.post("/api/search", json=VALID_PAYLOAD)
    assert response.status_code == 502
    assert "error" in response.json()


def test_search_invalid_dates_returns_422():
    bad_payload = dict(VALID_PAYLOAD, check_in="2026-09-13", check_out="2026-09-11")
    response = client.post("/api/search", json=bad_payload)
    assert response.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest backend/tests/test_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.app.api'` (or 404 on the route)

- [ ] **Step 3: Write minimal implementation — `backend/app/api.py`**

```python
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
```

- [ ] **Step 4: Wire the router — modify `backend/app/main.py`**

```python
from fastapi import FastAPI

from backend.app.api import router

app = FastAPI(title="Hotel Scrape")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest backend/tests/test_api.py -v`
Expected: all 3 tests PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/api.py backend/app/main.py backend/tests/test_api.py
git commit -m "Add POST /api/search endpoint"
```

---

### Task 6: Frontend

**Files:**
- Create: `frontend/index.html`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `POST /api/search` (Task 5) — request body `{location, check_in, check_out, adults, rooms}`, response `{hotels: [...]}` or `{error: "..."}`.

- [ ] **Step 1: Create `frontend/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Hotel Scrape — Marriott</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; }
    form { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; }
    label { display: flex; flex-direction: column; font-size: 0.85rem; }
    input { padding: 0.4rem; font-size: 1rem; }
    button { grid-column: span 2; padding: 0.6rem; font-size: 1rem; cursor: pointer; }
    table { width: 100%; border-collapse: collapse; margin-top: 1.5rem; }
    th, td { text-align: left; padding: 0.5rem; border-bottom: 1px solid #ddd; }
    #error { color: #b00020; margin-top: 1rem; }
  </style>
</head>
<body>
  <h1>Find a Marriott Room</h1>
  <form id="search-form">
    <label>Location
      <input type="text" name="location" placeholder="New York, NY" required />
    </label>
    <label>Adults
      <input type="number" name="adults" value="1" min="1" required />
    </label>
    <label>Check-in
      <input type="date" name="check_in" required />
    </label>
    <label>Check-out
      <input type="date" name="check_out" required />
    </label>
    <label>Rooms
      <input type="number" name="rooms" value="1" min="1" required />
    </label>
    <button type="submit">Search</button>
  </form>

  <div id="error" hidden></div>
  <table id="results" hidden>
    <thead>
      <tr><th>Hotel</th><th>Price / night</th><th>Total</th></tr>
    </thead>
    <tbody></tbody>
  </table>

  <script>
    const form = document.getElementById("search-form");
    const errorEl = document.getElementById("error");
    const resultsEl = document.getElementById("results");
    const resultsBody = resultsEl.querySelector("tbody");

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      errorEl.hidden = true;
      resultsEl.hidden = true;
      resultsBody.innerHTML = "";

      const data = Object.fromEntries(new FormData(form).entries());
      data.adults = Number(data.adults);
      data.rooms = Number(data.rooms);

      const response = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(data),
      });
      const body = await response.json();

      if (!response.ok) {
        errorEl.textContent = body.error || "Search failed.";
        errorEl.hidden = false;
        return;
      }

      for (const hotel of body.hotels) {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${hotel.name}</td>
          <td>${hotel.price_per_night != null ? "$" + hotel.price_per_night.toFixed(2) : "-"}</td>
          <td>${hotel.total_price != null ? "$" + hotel.total_price.toFixed(2) : "-"}</td>
        `;
        resultsBody.appendChild(row);
      }
      resultsEl.hidden = false;
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Serve the frontend — modify `backend/app/main.py`**

```python
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.app.api import router

app = FastAPI(title="Hotel Scrape")
app.include_router(router)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse("frontend/index.html")
```

- [ ] **Step 3: Manually verify in a browser**

Run: `uvicorn backend.app.main:app --reload`

Open `http://localhost:8000/` in a browser, fill in the form (e.g. New York, NY / tomorrow / +2 days / 1 adult / 1 room), submit, and confirm either a results table renders or a clear error message appears (not a blank page or unhandled JS error — check the browser console).

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html backend/app/main.py
git commit -m "Add frontend search form and results table"
```

---

### Task 7: README

**Files:**
- Create: `README.md` (modify if it already has content — check first)

- [ ] **Step 1: Write `README.md`**

```markdown
# hotel_scrape

Local app to search Marriott hotel availability by location, date range, and
guest count. v1 scrapes Marriott only, via a headless Playwright browser
(plain HTTP requests are blocked by Marriott's bot protection).

## Setup

​```bash
pip install -r requirements.txt
playwright install chromium
​```

## Run

​```bash
uvicorn backend.app.main:app --reload
​```

Open http://localhost:8000/ and search.

## Test

​```bash
pytest backend/tests -v
​```

## Known limitations

- Marriott may block or CAPTCHA-gate the automated browser session; the app
  surfaces this as a clear error rather than crashing, per design.
- Location resolution depends on marriott.com's own destination-autosuggest
  UI, which may change without notice.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "Add README with setup and run instructions"
```
