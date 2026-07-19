"""
app.py — FastAPI app + routes (docs/DESIGN.md §4).

Every route except /health requires a credential — Bearer for machine calls,
Basic for browser access, one shared secret behind both (see auth.py / §5).
"""
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
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
        if content_length is not None and int(content_length) > MAX_BODY_SIZE:
            return JSONResponse(status_code=413, content={"detail": "Request body too large"})
        return await call_next(request)


async def enforce_rate_limit(request: Request) -> None:
    """Simple in-memory sliding-window limiter, keyed by the raw Authorization
    header value. In-memory/resets-on-restart is an accepted trade-off for a
    single self-hosted process (docs/DESIGN.md §4) — not meant to survive a
    multi-process deployment, only to blunt a single runaway client."""
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


app = FastAPI(title="SpendGaugeAI", lifespan=lifespan)
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


@app.post("/usage/log", dependencies=[Depends(enforce_rate_limit), Depends(auth.require_bearer)])
async def post_usage_log(req: UsageLogRequest):
    import uuid
    session_id = req.session_id or str(uuid.uuid4())
    cost = database.usage_log(
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
    await alerts.run_alert_checks()
    return {"logged": True, "cost_usd": cost}


@app.post("/usage/credit", dependencies=[Depends(auth.require_bearer)])
async def post_usage_credit(req: CreditRequest):
    database.credit_set(
        starting_balance=req.starting_balance,
        alert_threshold=req.alert_threshold,
        reset=req.reset,
        warning_threshold=req.warning_threshold,
    )
    return {"saved": True}


@app.get("/usage/data", dependencies=[Depends(auth.require_basic)])
async def get_usage_data(project: str | None = Query(default=None)):
    data = database.usage_summary(project=project)
    data["credit"] = database.credit_status(project=project)
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
