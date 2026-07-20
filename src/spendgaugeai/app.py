"""
app.py — FastAPI app + routes (docs/DESIGN.md §4).

Every route except /health requires a credential — Bearer for machine calls,
Basic for browser access, one shared secret behind both (see auth.py / §5).
"""
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from . import alerts, auth, database

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

MAX_BODY_SIZE = 64 * 1024  # 64KB — a usage-log payload is a handful of fields, never this large
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = 120  # generous — only meant to blunt a runaway client, not throttle normal traffic

_rate_limit_buckets: dict[str, deque] = defaultdict(deque)


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header"})
            if declared_size > MAX_BODY_SIZE:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})

        # Content-Length is absent for chunked-encoded requests, so the check
        # above alone can be bypassed — buffer the body ourselves, bounded by
        # MAX_BODY_SIZE, and reject before it ever reaches the route handler.
        # Setting request._body (rather than raising mid-stream) is the
        # pattern Starlette's own _CachedRequest expects from middleware that
        # consumes the body early — it forwards exactly this buffered value
        # downstream, and avoids an exception raised from inside a custom
        # receive callable getting mangled by BaseHTTPMiddleware's internal
        # anyio task group (which wraps it, losing its HTTPException type).
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BODY_SIZE:
                return JSONResponse(status_code=413, content={"detail": "Request body too large"})
            chunks.append(chunk)
        request._body = b"".join(chunks)

        return await call_next(request)


async def enforce_rate_limit(request: Request) -> None:
    """Simple in-memory sliding-window limiter, keyed by the raw Authorization
    header value. In-memory/resets-on-restart is an accepted trade-off for a
    single self-hosted process (docs/DESIGN.md §4) — not meant to survive a
    multi-process deployment, only to blunt a single runaway client.

    Must run AFTER an auth dependency in a route's `dependencies=[...]` list
    (FastAPI resolves them in order), not before — keying on the raw header
    pre-validation would let unauthenticated requests with a different bogus
    token each time grow this dict forever, an unbounded-memory DoS vector
    from traffic that was never going to be accepted anyway. Since there is
    exactly one valid key server-wide, keying only after auth succeeds keeps
    this dict's real size bounded to 1 entry.
    """
    key = request.headers.get("authorization", "anonymous")
    now = time.monotonic()
    bucket = _rate_limit_buckets[key]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Rate limit exceeded")
    bucket.append(now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    api_key, source = auth.resolve_api_key()
    if source == "generated":
        print(f"[spendgaugeai] Generated API key: {api_key}")
        print("[spendgaugeai] Set SPENDGAUGEAI_API_KEY to pin this across restarts.")
    elif source == "persisted":
        print(f"[spendgaugeai] Using persisted API key: {api_key}")
        print("[spendgaugeai] Set SPENDGAUGEAI_API_KEY to override with your own.")
    else:
        print("[spendgaugeai] Using API key from SPENDGAUGEAI_API_KEY.")
    yield


app = FastAPI(title="SpendGaugeAI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(MaxBodySizeMiddleware)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Request/response models ────────────────────────────────────────────────

class UsageLogRequest(BaseModel):
    project: str = Field(default="default", max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    model: str = Field(max_length=200)
    input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    web_search_requests: int = Field(default=0, ge=0)
    tools_used: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("tools_used")
    @classmethod
    def _cap_tool_name_length(cls, v: list[str]) -> list[str]:
        for name in v:
            if len(name) > 200:
                raise ValueError("tools_used entries must be 200 characters or fewer")
        return v


class CreditRequest(BaseModel):
    starting_balance: float = Field(ge=0)
    alert_threshold: float = Field(default=1.0, ge=0)
    warning_threshold: float | None = Field(default=None, ge=0)
    reset: bool = False


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/usage/log", dependencies=[Depends(auth.require_bearer), Depends(enforce_rate_limit)])
async def post_usage_log(req: UsageLogRequest, background_tasks: BackgroundTasks):
    session_id = req.session_id or str(uuid.uuid4())
    cost = await run_in_threadpool(
        database.usage_log,
        session_id=session_id,
        model=req.model,
        input_tokens=req.input_tokens,
        cache_write=req.cache_write_tokens,
        cache_read=req.cache_read_tokens,
        output_tokens=req.output_tokens,
        tools=req.tools_used,
        project=req.project,
        web_search_requests=req.web_search_requests,
    )
    # Backgrounded, not awaited inline: alerts.run_alert_checks() can chain a
    # real outbound Discord POST (10s timeout) plus several DB reads. Alerts
    # are documented best-effort — they must never add latency to the caller's
    # response, which for wrap()'s callers is meant to be near-instant.
    background_tasks.add_task(alerts.run_alert_checks)
    return {"logged": True, "cost_usd": cost}


@app.post(
    "/usage/credit",
    dependencies=[Depends(auth.require_bearer_or_basic), Depends(enforce_rate_limit)],
)
async def post_usage_credit(req: CreditRequest):
    await run_in_threadpool(
        database.credit_set,
        starting_balance=req.starting_balance,
        alert_threshold=req.alert_threshold,
        reset=req.reset,
        warning_threshold=req.warning_threshold,
    )
    return {"saved": True}


@app.get("/usage/data", dependencies=[Depends(auth.require_basic)])
async def get_usage_data(project: str | None = Query(default=None)):
    data = await run_in_threadpool(database.usage_summary, project=project)
    data["credit"] = await run_in_threadpool(database.credit_status, project=project)
    return data


@app.get("/usage")
async def get_usage_page(request: Request, _: None = Depends(auth.require_basic)):
    return templates.TemplateResponse(request, "usage.html", {})


@app.get("/static/{file_path:path}", dependencies=[Depends(auth.require_basic)])
async def get_static(file_path: str):
    target = (STATIC_DIR / file_path).resolve()
    static_root = STATIC_DIR.resolve()
    if static_root not in target.parents and target != static_root:
        raise HTTPException(status_code=404)
    if not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(target)
