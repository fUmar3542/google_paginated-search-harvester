"""
Paginated Search Harvester (SerpApi edition) - Flask API

POST /search  {"keyword": "...", "start_page": 1, "end_page": 3}
-> returns a CSV file (auth required via API token).
"""
import csv
import hmac
import io
import logging
import os
import random
import re
import threading
import time
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

# ----------------------------- Config -----------------------------


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise SystemExit(f"Invalid integer for {name} in environment")


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        raise SystemExit(f"Invalid number for {name} in environment")


API_TOKEN = os.getenv("API_TOKEN", "").strip()
if len(API_TOKEN) < 32 or API_TOKEN.lower().startswith("change"):
    raise SystemExit(
        "API_TOKEN is missing or too weak (min 32 chars). Generate one with:\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
if not SERPAPI_KEY or SERPAPI_KEY.lower().startswith("your"):
    raise SystemExit("SERPAPI_KEY is missing. Copy it from https://serpapi.com/manage-api-key")

SERPAPI_URL = "https://serpapi.com/search.json"
HOST = os.getenv("HOST", "127.0.0.1")
PORT = env_int("PORT", 5000)
RATE_LIMIT = os.getenv("RATE_LIMIT", "5 per minute")
MAX_CONCURRENT = env_int("MAX_CONCURRENT", 1)
MAX_PAGES_PER_REQUEST = env_int("MAX_PAGES_PER_REQUEST", 10)  # protects your quota
MAX_PAGE_NUMBER = env_int("MAX_PAGE_NUMBER", 200)
MAX_KEYWORD_LENGTH = env_int("MAX_KEYWORD_LENGTH", 200)
RESULTS_PER_PAGE = 10
GOOGLE_LANG = os.getenv("GOOGLE_LANG", "en")
GOOGLE_COUNTRY = os.getenv("GOOGLE_COUNTRY", "us")
MIN_DELAY = env_float("MIN_DELAY", 1.0)
MAX_DELAY = env_float("MAX_DELAY", 3.0)
REQUEST_TIMEOUT = env_int("REQUEST_TIMEOUT", 30)
MAX_REQUEST_SECONDS = env_int("MAX_REQUEST_SECONDS", 240)

if MIN_DELAY > MAX_DELAY:
    raise SystemExit("MIN_DELAY must be <= MAX_DELAY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("harvester")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["60 per hour"],
    storage_uri="memory://",
)

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

# ----------------------------- Helpers -----------------------------


def require_token(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        else:
            supplied = request.headers.get("X-API-Token", "").strip()

        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), API_TOKEN.encode("utf-8")
        ):
            log.warning("Unauthorized request from %s", get_remote_address())
            return jsonify(error="unauthorized"), 401
        return fn(*args, **kwargs)

    return wrapper


def parse_int(value, field):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"'{field}' must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{field}' must be an integer")


def validate_input(data):
    keyword = data.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("'keyword' is required and must be a non-empty string")
    keyword = " ".join(keyword.split())
    if len(keyword) > MAX_KEYWORD_LENGTH:
        raise ValueError(f"'keyword' must be at most {MAX_KEYWORD_LENGTH} characters")
    if any(ord(c) < 32 for c in keyword):
        raise ValueError("'keyword' contains control characters")

    start = parse_int(data.get("start_page"), "start_page")
    end = parse_int(data.get("end_page"), "end_page")

    if start < 1 or end < 1:
        raise ValueError("Pages must be >= 1")
    if start > end:
        raise ValueError("'start_page' must be <= 'end_page'")
    if end > MAX_PAGE_NUMBER:
        raise ValueError(f"'end_page' must be <= {MAX_PAGE_NUMBER}")
    if end - start + 1 > MAX_PAGES_PER_REQUEST:
        raise ValueError(f"Max {MAX_PAGES_PER_REQUEST} pages per request")
    return keyword, start, end


def csv_safe(value):
    """Prevent CSV/Excel formula injection."""
    value = str(value if value is not None else "")
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def build_csv(rows, keyword):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["keyword", "page", "position", "title", "url", "snippet"])
    for r in rows:
        writer.writerow(
            [
                csv_safe(keyword),
                r["page"],
                r["position"],
                csv_safe(r["title"]),
                r["url"],
                csv_safe(r["snippet"]),
            ]
        )
    return buf.getvalue()


# ----------------------------- SerpApi client -----------------------------


def make_session():
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_page(session, keyword, page_number):
    """Returns (rows, stop_reason). stop_reason is None on success."""
    params = {
        "engine": "google",
        "q": keyword,
        "start": (page_number - 1) * RESULTS_PER_PAGE,
        "hl": GOOGLE_LANG,
        "gl": GOOGLE_COUNTRY,
        "api_key": SERPAPI_KEY,
    }
    try:
        resp = session.get(SERPAPI_URL, params=params, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        return [], "timeout"
    except requests.RequestException as exc:
        # log only the exception type: the message can contain the URL with the API key
        log.error("SerpApi request error on page %s: %s", page_number, type(exc).__name__)
        return [], "request_error"

    try:
        data = resp.json()
    except ValueError:
        return [], "bad_response"

    error = str(data.get("error", "")).lower()
    if resp.status_code == 401 or "invalid api key" in error:
        return [], "invalid_api_key"
    if resp.status_code == 429 or "run out of searches" in error or "rate limit" in error:
        return [], "quota_or_rate_limited"
    if "returned any results" in error:
        return [], "no_results"
    if error or resp.status_code != 200:
        log.error("SerpApi error on page %s: %s", page_number, error or resp.status_code)
        return [], "serpapi_error"

    rows = []
    for item in data.get("organic_results", []):
        link, title = item.get("link", ""), (item.get("title") or "").strip()
        if not link.startswith("http") or not title:
            continue
        rows.append(
            {
                "page": page_number,
                "position": len(rows) + 1,
                "title": title,
                "url": link,
                "snippet": (item.get("snippet") or "").strip(),
            }
        )
    return rows, (None if rows else "no_results")


def scrape(keyword, start_page, end_page):
    rows, pages_done, reason = [], 0, "completed"
    deadline = time.monotonic() + MAX_REQUEST_SECONDS
    session = make_session()

    for n in range(start_page, end_page + 1):
        if time.monotonic() > deadline:
            reason = "time_budget_exceeded"
            break

        found, stop = fetch_page(session, keyword, n)
        log.info("page=%s results=%s stop=%s", n, len(found), stop)
        if stop:
            reason = stop
            break

        rows.extend(found)
        pages_done += 1
        if n < end_page:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    return rows, pages_done, reason


# ----------------------------- Routes -----------------------------

FAILURE_STATUS = {
    "no_results": (404, "No results found. The requested page range may not exist."),
    "invalid_api_key": (502, "Server's SerpApi key was rejected. Check SERPAPI_KEY."),
    "quota_or_rate_limited": (503, "SerpApi quota or rate limit reached. Check your SerpApi dashboard."),
    "timeout": (504, "Timed out contacting SerpApi."),
    "request_error": (502, "Could not reach SerpApi."),
    "bad_response": (502, "Unexpected response from SerpApi."),
    "serpapi_error": (502, "SerpApi returned an error."),
    "time_budget_exceeded": (504, "Time budget exceeded before any results were collected."),
}


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.route("/search", methods=["POST", "GET"])
@limiter.limit(RATE_LIMIT)
@require_token
def search():
    data = request.get_json(silent=True) if request.is_json else None
    data = data or request.args.to_dict()

    try:
        keyword, start, end = validate_input(data)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    if not _slots.acquire(blocking=False):
        return jsonify(error="Server is busy with another request. Try again shortly."), 429

    try:
        log.info("Scrape started keyword=%r pages=%s-%s", keyword, start, end)
        rows, pages_done, reason = scrape(keyword, start, end)
    except Exception:
        log.exception("Unexpected failure")
        return jsonify(error="Internal error"), 500
    finally:
        _slots.release()

    if not rows:
        status, message = FAILURE_STATUS.get(reason, (502, "Request failed."))
        return jsonify(error=message, reason=reason, pages_scraped=0), status

    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:40] or "results"
    resp = Response(build_csv(rows, keyword), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{slug}_{start}-{end}.csv"'
    resp.headers["X-Pages-Requested"] = str(end - start + 1)
    resp.headers["X-Pages-Scraped"] = str(pages_done)
    resp.headers["X-Stopped-Reason"] = reason
    return resp


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.errorhandler(404)
def not_found(_):
    return jsonify(error="not found"), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify(error="method not allowed"), 405


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="request body too large"), 413


@app.errorhandler(429)
def rate_limited(_):
    return jsonify(error="rate limit exceeded"), 429


@app.errorhandler(500)
def server_error(_):
    return jsonify(error="internal server error"), 500


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=False)