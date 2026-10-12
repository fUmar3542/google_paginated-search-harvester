# Paginated Search Harvester

A token-protected Flask API that fetches Google search results across a page
range through [SerpApi](https://serpapi.com) and returns them as a CSV file.

## Features

- Keyword + start/end page in, CSV out (`keyword, page, position, title, url, snippet`)
- Secret API token auth (constant-time comparison)
- Input validation, per-IP rate limiting, concurrency cap
- Page cap per request to protect your SerpApi quota (each page = 1 search)
- Randomised delay between pages, timeouts and a total time budget
- Clear JSON errors; partial results are returned if a run stops early
- CSV formula-injection protection; the SerpApi key is never logged

## Setup

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Edit `.env`:

1. `SERPAPI_KEY`: copy it from https://serpapi.com/manage-api-key
2. `API_TOKEN`: your own secret for calling this API. Generate with:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`

## Run

```bash
python app.py
```

Production (keep one worker so the rate limiter and concurrency cap are shared):

```bash
gunicorn -w 1 --threads 4 -t 300 -b 127.0.0.1:5000 app:app
```

## Usage

```bash
curl -X POST http://127.0.0.1:5000/search \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"keyword": "software development", "start_page": 1, "end_page": 2}' \
  -o results.csv -D -
```

The token can also be sent as `X-API-Token: YOUR_API_TOKEN`.

| Field        | Type    | Description                  |
|--------------|---------|------------------------------|
| `keyword`    | string  | Search term (max 200 chars)  |
| `start_page` | integer | First page, >= 1             |
| `end_page`   | integer | Last page, >= `start_page`   |

### Response headers

- `X-Pages-Requested`, `X-Pages-Scraped`
- `X-Stopped-Reason`: `completed`, `no_results`, `quota_or_rate_limited`,
  `timeout`, `request_error`, `invalid_api_key`, `time_budget_exceeded`

### Errors (JSON)

| Status | Meaning                                              |
|--------|------------------------------------------------------|
| 400    | Invalid input                                        |
| 401    | Missing or wrong API token                           |
| 404    | No results for the requested range                   |
| 429    | Rate limit hit, or server busy                       |
| 502    | SerpApi key rejected / SerpApi error                 |
| 503    | SerpApi quota or rate limit reached                  |
| 504    | Timeout                                              |

`GET /health` returns `{"status": "ok"}` with no token needed.

## Notes on quota and depth

- SerpApi's free plan is about 250 searches a month (verify at serpapi.com/pricing).
  Every page you request costs one search, so a 3-page run costs 3.
  Check usage on your SerpApi dashboard.
- Google does not serve very deep pages. Pages 100 to 120 will most likely return
  no results. Start with pages 1 to 3, then increase gradually. A successful but
  empty result still counts against your quota, so avoid repeated deep-page tests.
- Never commit `.env`; it is already in `.gitignore`.