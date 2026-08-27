# hotel_scrape

Local app to search Marriott hotel availability by location, date range, and
guest count. v1 scrapes Marriott only, via a patchright-driven Chrome browser
(plain HTTP requests and stock Playwright are blocked by Marriott's Akamai
bot protection; patchright patches the CDP leaks that detection keys on).

## Setup

```bash
pip install -r requirements.txt
patchright install chrome
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

## 安装与运行（中文）

### 安装

```bash
pip install -r requirements.txt
patchright install chrome
```

### 运行

```bash
uvicorn backend.app.main:app --reload
```

打开 http://localhost:8000/ 即可搜索。

### 测试

```bash
pytest backend/tests -v
```

## Known limitations

- A visible Chrome window opens for each search (patchright works headed,
  not headless, against Marriott's bot protection); it closes automatically
  when the search completes.
- Location parsing only supports "City, ST" (US) input for now.
- Marriott may still block or CAPTCHA-gate the session; the app surfaces
  this as a clear error rather than crashing, per design.
- Only price-per-night is available from the results page; total price is
  computed as price-per-night × nights.