# hotel_scrape

Local app to search Marriott hotel availability by location, date range, and
guest count. v1 scrapes Marriott only, via a headless Playwright browser
(plain HTTP requests are blocked by Marriott's bot protection).

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
uvicorn backend.app.main:app --reload
```

Open http://localhost:8000/ and search.

## Test

```bash
pytest backend/tests -v
```

## Known limitations

- Marriott may block or CAPTCHA-gate the automated browser session; the app
  surfaces this as a clear error rather than crashing, per design.
- Location resolution depends on marriott.com's own destination-autosuggest
  UI, which may change without notice.