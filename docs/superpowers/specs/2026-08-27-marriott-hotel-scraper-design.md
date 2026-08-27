# Marriott Hotel Scraper (v1) — Design

## Goal

A local web app where the user enters a location, date range, and guest count,
and it scrapes Marriott.com for matching hotels, returning hotel name,
price/night, and total price in a results table.

This is v1 of a tool intended to grow into a multi-site hotel search
aggregator. v1 targets a single site (Marriott) with a minimal but extensible
interface, so adding more sites later doesn't require rearchitecting.

## Non-goals (v1)

- Multi-site aggregation (future work — interface is designed for it)
- Historical price tracking / scheduled runs
- Persistence (database, saved searches)
- Authentication
- Extended hotel data (ratings, room type, cancellation policy, distance)
- Robust anti-bot evasion (proxies, stealth plugins, CAPTCHA solving)

## Context: why Playwright, not plain HTTP

A plain HTTP request to Marriott's search endpoint (tested with curl and a
standard browser User-Agent) returns an immediate `403 Forbidden` from
Akamai's bot management layer (`server: AkamaiGHost`), with an
8-hour `Retry-After`. Marriott fingerprints the TLS/HTTP handshake and
JS execution environment, not just headers.

A real rendered browser session (Playwright + Chromium, headless) is
meaningfully more likely to get through, though not guaranteed — Marriott
may still block, rate-limit, or CAPTCHA-gate automated browser traffic.
Scraping this site is inherently best-effort; the app must degrade
gracefully (clear error state) rather than crash when blocked.

## Architecture

```
Browser (single HTML page, vanilla JS)
        |
        v  POST /api/search { location, checkIn, checkOut, adults, rooms }
FastAPI backend
        |
        +-- 1. Resolve location -> Marriott destinationAddress params
        |      (city, state, country, lat, long, placeId)
        |      via Marriott's own autosuggest endpoint
        |
        +-- 2. Build Marriott search URL from resolved params + dates/guests
        |
        +-- 3. Playwright (headless Chromium) navigates to the URL,
        |      waits for hotel results to render
        |
        +-- 4. Extract hotel name / price-per-night / total price
        |      per result via DOM selectors
        |
        v
JSON response: { hotels: [...] } or { error: "..." }
        |
        v
Frontend renders results table (or error message)
```

## Components

### 1. Location resolver
- Input: free-text location string (e.g. "New York, NY")
- Calls Marriott's autosuggest endpoint to resolve it into the
  `destinationAddress.*` fields the search URL requires (type, city,
  stateProvince, country, latitude, longitude, placeId, etc.)
- If resolution fails (no match, endpoint blocked), return a clear error —
  no fallback geocoding provider in v1 (deferred; noted as a known
  fragility since it depends on an undocumented Marriott endpoint).

### 2. Search URL builder
- Takes resolved location params + check-in/check-out dates + adults +
  rooms, and constructs the Marriott `findHotels.mi` URL matching the
  pattern from the reference URL provided by the user.

### 3. Scraper (Playwright)
- Launches headless Chromium, sets a realistic desktop User-Agent/viewport.
- Navigates to the built URL, waits for the hotel result list container to
  appear (with a reasonable timeout).
- On timeout/block/empty results: returns a structured error, does not
  throw an unhandled exception.
- On success: extracts, per hotel card, name / price-per-night / total
  price (whatever is present — Marriott may show only one of
  price-per-night or total depending on rate type; both are captured when
  available).

### 4. API layer (FastAPI)
- `POST /api/search` — accepts the form payload, orchestrates resolver ->
  URL builder -> scraper, returns JSON results or a JSON error with an
  appropriate HTTP status (e.g. 502 for upstream block/failure).
- Single endpoint for v1; no auth, no rate limiting (single local user).

### 5. Frontend
- One static HTML page with a form (location, check-in, check-out, adults,
  rooms) and a results table area.
- Vanilla JS `fetch()` to `/api/search`, renders table rows or an error
  banner. No build step, no framework.

## Data contract

Request:
```json
{
  "location": "New York, NY",
  "checkIn": "2026-09-11",
  "checkOut": "2026-09-13",
  "adults": 1,
  "rooms": 1
}
```

Success response:
```json
{
  "hotels": [
    { "name": "...", "pricePerNight": 259.0, "totalPrice": 518.0, "currency": "USD" }
  ]
}
```

Error response:
```json
{ "error": "Marriott blocked this search. Try again later." }
```

## Error handling

- Location can't be resolved -> 422 with a clear message.
- Marriott blocks the request (403/CAPTCHA) or times out waiting for
  results -> 502 with a clear message; no retries in v1 (Akamai's
  `Retry-After` was observed at 8 hours, so retrying immediately is
  pointless and risks further flagging).
- No hotels match -> 200 with an empty `hotels` array (not an error).

## Testing approach

- Unit test the search URL builder against known inputs (pure function,
  no network).
- Manual verification of the resolver and scraper end-to-end (network-
  dependent, brittle to automate reliably against a live anti-bot site);
  document manual test steps rather than asserting scraper output in CI.

## Extensibility for future sites

The scraper module exposes a single function signature:
`search(location, check_in, check_out, adults, rooms) -> list[Hotel] | Error`.
Each future site (Hilton, Hyatt, etc.) implements this same signature as
its own module; the API layer can later fan out to multiple modules and
merge results. Not implemented in v1, but the interface is chosen so it
requires no rework of the Marriott module when added.
