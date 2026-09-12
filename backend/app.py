from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
# Load configuration before importing auth so DATABASE_PATH and admin settings
# are available while the SQLite schema is initialized.
load_dotenv(ROOT / ".env", override=True)

from .analysis import GPT_PROVIDERS, STRATEGIES, analyze, gpt_provider_info, normalize_gpt_provider, normalize_strategy, propose_selling_points, regenerate_section, strategy_info
from .auth import (
    audit_request,
    create_session,
    db_connect,
    find_user_by_token,
    get_current_user,
    hash_password,
    init_db,
    public_user,
    require_admin,
    revoke_session,
    token_digest,
    verify_password,
)
from .sif_mcp import SifMcpClient, SifMcpError, demo_data

app = FastAPI(title="SIF 综合处理", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Keep the MVP self-contained: the FastAPI process serves the browser assets
# as well as the API, so a fresh checkout has one URL to open.
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def request_audit_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/health"}:
        audit_request(request, response.status_code)
    return response


class ProductInput(BaseModel):
    product_name: str = ""
    brand: str = ""
    # Flexible operator input. The legacy fixed fields below remain optional
    # so historical jobs can still be validated and regenerated.
    operator_notes: str = Field(default="", max_length=20000)
    category: str = ""
    material: str = ""
    specifications: str = ""
    functions: str = ""
    packaging: str = ""
    selling_points: str = ""
    target_customer: str = ""
    claims_allowed: str = ""
    claims_forbidden: str = ""


class AnalyzeRequest(BaseModel):
    product: ProductInput
    asins: list[str] = Field(min_length=3, max_length=10)
    country: str = "US"
    demo: bool = False
    strategy: str = "comprehensive"
    gpt_provider: str = "official"
    gpt_model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=100)

    @field_validator("strategy", mode="before")
    @classmethod
    def validate_strategy(cls, value: str) -> str:
        key = str(value or "comprehensive").strip().lower()
        if key not in STRATEGIES:
            raise ValueError(f"不支持的决策策略: {value}")
        return key

    @field_validator("gpt_provider", mode="before")
    @classmethod
    def validate_gpt_provider(cls, value: str) -> str:
        key = str(value or "official").strip().lower()
        if key not in GPT_PROVIDERS:
            raise ValueError(f"不支持的GPT节点: {value}")
        return key

    @field_validator("gpt_model", mode="before")
    @classmethod
    def validate_gpt_model(cls, value: str) -> str:
        model = str(value or "gpt-5.6-sol").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
            raise ValueError("GPT模型名称格式不正确")
        return model


class SectionRegenerateRequest(BaseModel):
    section: str
    qa_index: int | None = Field(default=None, ge=0, le=100)
    qa_count: int | None = Field(default=None, ge=1, le=50)
    review_count: int | None = Field(default=None, ge=1, le=50)
    current_listing: dict[str, Any] | None = None


class ListingUpdateRequest(BaseModel):
    listing: dict[str, Any]


class SellingPointSelectionRequest(BaseModel):
    selected_ids: list[str] = Field(min_length=1, max_length=8)


class RegenerateRequest(BaseModel):
    """Optional selling-point re-selection for a regeneration run."""

    selected_ids: list[str] | None = Field(default=None, max_length=8)


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=6, max_length=128)


class AdminUserAction(BaseModel):
    action: str
    user_id: int
    password: str | None = Field(default=None, min_length=6, max_length=128)


jobs: dict[str, dict[str, Any]] = {}

# Analysis calls are I/O-bound, so a small configurable worker pool lets
# operators submit several products without making every request wait for the
# previous GPT response. Jobs beyond this limit remain queued in FIFO order.
ANALYSIS_CONCURRENCY = max(1, int(os.getenv("ANALYSIS_CONCURRENCY", "2")))
analysis_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
analysis_workers: set[asyncio.Task[Any]] = set()
active_job_ids: set[str] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_asins(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for token in re.split(r"[\s,;，；]+", value.upper().strip()):
            if token and token not in result:
                result.append(token)
    return result


def persist_job(job: dict[str, Any]) -> None:
    with db_connect() as db:
        db.execute(
            """UPDATE analysis_jobs SET status=?, stage=?, progress=?, message=?, result_json=?, error=?, updated_at=? WHERE job_id=?""",
            (
                job.get("status", "queued"),
                job.get("stage", "queued"),
                int(job.get("progress", 0)),
                job.get("message", ""),
                json.dumps(job.get("result"), ensure_ascii=False) if job.get("result") is not None else None,
                job.get("error"),
                job.get("updated_at", now_iso()),
                job["id"],
            ),
        )


def set_job(job_id: str, **updates: Any) -> None:
    jobs[job_id].update(updates)
    jobs[job_id]["updated_at"] = now_iso()
    persist_job(jobs[job_id])


async def run_job(job_id: str, request: AnalyzeRequest, asins: list[str]) -> None:
    try:
        set_job(job_id, status="running", stage="sif", progress=15, message="正在准备 SIF 数据")
        demo = request.demo or os.getenv("DEMO_MODE", "false").lower() == "true" or not os.getenv("SIF_MCP_SECRET_KEY", "").strip()
        if demo:
            sif_data = demo_data(asins, request.country.upper())
        else:
            async with SifMcpClient() as client:
                sif_data = await client.collect(asins, request.country.upper())
        set_job(job_id, stage="keywords", progress=48, message="SIF 数据已返回，正在整理关键词和候选卖点")
        await asyncio.sleep(0)
        set_job(job_id, stage="selling_points", progress=62, message="正在生成可选商品卖点")
        proposal = await propose_selling_points(
            request.product.model_dump(),
            sif_data,
            strategy=request.strategy,
            gpt_provider=request.gpt_provider,
            gpt_model=request.gpt_model,
        )
        set_job(
            job_id,
            status="waiting_selection",
            stage="selling_points",
            progress=68,
            message="SIF 数据和候选卖点已生成，请选择核心卖点后继续",
            result={"sif_data": sif_data, "analysis": proposal},
        )
    except (SifMcpError, ValueError) as exc:
        set_job(job_id, status="failed", stage="error", progress=100, message=str(exc), error=str(exc))
    except Exception as exc:
        set_job(job_id, status="failed", stage="error", progress=100, message="任务执行失败", error=str(exc))


async def run_content_job(job_id: str, request: AnalyzeRequest) -> None:
    """Generate final content after the operator confirms selling points."""
    try:
        source = load_job(job_id)
        source_result = source.get("result") if source else None
        sif_data = source_result.get("sif_data") if isinstance(source_result, dict) else None
        partial_analysis = source_result.get("analysis") if isinstance(source_result, dict) else None
        selected_points = partial_analysis.get("selected_selling_points") if isinstance(partial_analysis, dict) else None
        if not isinstance(sif_data, dict) or not selected_points:
            raise ValueError("卖点选择或原 SIF 数据不完整，无法继续生成")
        set_job(job_id, status="running", stage="gpt", progress=76, message="正在根据所选卖点生成消费者画像和 Listing 内容")
        result = await analyze(
            request.product.model_dump(),
            sif_data,
            strategy=request.strategy,
            gpt_provider=request.gpt_provider,
            gpt_model=request.gpt_model,
            selected_selling_points=selected_points,
        )
        if isinstance(partial_analysis, dict) and partial_analysis.get("keywords"):
            result["keywords"] = partial_analysis["keywords"]
        result["selling_point_candidates"] = partial_analysis.get("selling_point_candidates", [])
        result["selected_selling_points"] = selected_points
        set_job(job_id, status="completed", stage="complete", progress=100, message="已根据所选卖点完成分析", result={"sif_data": sif_data, "analysis": result})
    except Exception as exc:
        set_job(job_id, status="failed", stage="error", progress=100, message="内容生成失败", error=str(exc))


async def run_regeneration_job(
    job_id: str,
    request: AnalyzeRequest,
    sif_data: dict[str, Any],
    previous_result: dict[str, Any],
    source_job_id: str,
    selected_ids: list[str] | None = None,
) -> None:
    """Regenerate content from stored SIF data without making another SIF call."""
    try:
        set_job(job_id, status="running", stage="gpt", progress=35, message="正在使用已有 SIF 数据重新调用 GPT")
        await asyncio.sleep(0)
        previous_analysis = previous_result.get("analysis") or {}
        # Candidate selling points are generated once per SIF dataset, so keep
        # them on every regeneration and let operators re-use or re-pick them.
        candidates = [item for item in (previous_analysis.get("selling_point_candidates") or []) if isinstance(item, dict)]
        candidate_map = {str(item.get("id")): item for item in candidates if item.get("id")}
        requested_ids = [str(value).strip() for value in (selected_ids or []) if str(value).strip()]
        selected_points = [candidate_map[value] for value in requested_ids if value in candidate_map]
        if not selected_points:
            selected_points = [
                item for item in (previous_analysis.get("selected_selling_points") or []) if isinstance(item, dict)
            ]
        analysis_result = await analyze(
            request.product.model_dump(),
            sif_data,
            regeneration_note=source_job_id[:12],
            strategy=request.strategy,
            gpt_provider=request.gpt_provider,
            gpt_model=request.gpt_model,
            selected_selling_points=selected_points,
        )
        # Keyword extraction is retained from the original result. Only the
        # requested GPT-generated content is replaced by the new pass.
        if previous_analysis.get("keywords"):
            analysis_result["keywords"] = previous_analysis["keywords"]
        if candidates:
            analysis_result["selling_point_candidates"] = candidates
        analysis_result["selected_selling_points"] = selected_points
        analysis_result.setdefault("meta", {})["regenerated_from"] = source_job_id
        set_job(
            job_id,
            stage="complete",
            progress=100,
            message="已基于原 SIF 数据重新生成 GPT 内容",
            result={"sif_data": sif_data, "analysis": analysis_result},
        )
        set_job(job_id, status="completed")
    except Exception as exc:
        set_job(job_id, status="failed", stage="error", progress=100, message="重新生成失败", error=str(exc))


async def analysis_worker() -> None:
    """Process submitted analysis/regeneration jobs with bounded concurrency."""
    while True:
        job_id, job_kind = await analysis_queue.get()
        active_job_ids.add(job_id)
        try:
            job = load_job(job_id)
            if not job or job.get("status") != "queued":
                continue
            request_data = job.get("request") or {}
            request_model = AnalyzeRequest.model_validate(request_data)
            if job_kind == "regeneration":
                source_job_id = job.get("source_job_id") or request_data.get("_regenerated_from")
                source = load_job(str(source_job_id)) if source_job_id else None
                source_result = source.get("result") if source else None
                sif_data = source_result.get("sif_data") if isinstance(source_result, dict) else None
                if not isinstance(source_result, dict) or not isinstance(sif_data, dict):
                    raise ValueError("原任务数据不完整，无法重新生成")
                stored_ids = job.get("selected_selling_point_ids") or (
                    request_data.get("_selected_selling_point_ids") if isinstance(request_data, dict) else None
                )
                await run_regeneration_job(
                    job_id,
                    request_model,
                    sif_data,
                    source_result,
                    str(source_job_id),
                    selected_ids=stored_ids if isinstance(stored_ids, list) else None,
                )
            elif job_kind == "content":
                await run_content_job(job_id, request_model)
            else:
                await run_job(job_id, request_model, job["asins"])
        except Exception as exc:
            # Validation/recovery errors happen before the job runner can
            # update its own state, so persist them here.
            if job_id in jobs:
                set_job(job_id, status="failed", stage="error", progress=100, message="任务执行失败", error=str(exc))
        finally:
            active_job_ids.discard(job_id)
            analysis_queue.task_done()


def queue_position() -> int:
    """Return the approximate FIFO position of a newly queued job."""
    return max(1, len(active_job_ids) + analysis_queue.qsize())


@app.on_event("startup")
async def start_analysis_workers() -> None:
    """Start the bounded worker pool and recover unfinished jobs after restart."""
    with db_connect() as db:
        rows = db.execute(
            "SELECT * FROM analysis_jobs WHERE status IN ('queued', 'running') ORDER BY id ASC"
        ).fetchall()
        for row in rows:
            request_data = json.loads(row["request_json"] or "{}")
            status = "queued"
            message = row["message"] or "任务已重新排队"
            if row["status"] == "running":
                message = "服务重启后已重新排队"
                db.execute(
                    "UPDATE analysis_jobs SET status='queued', stage='queued', progress=0, message=?, error=NULL, updated_at=? WHERE job_id=?",
                    (message, now_iso(), row["job_id"]),
                )
            job = {
                "id": row["job_id"],
                "user_id": row["user_id"],
                "status": status,
                "stage": "queued",
                "progress": 0 if row["status"] == "running" else row["progress"],
                "message": message,
                "asins": json.loads(row["asins_json"]),
                "request": request_data,
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "error": None,
                "created_at": row["created_at"],
                "updated_at": now_iso(),
            }
            source_job_id = request_data.get("_regenerated_from") if isinstance(request_data, dict) else None
            if source_job_id:
                job["source_job_id"] = source_job_id
                job["selected_selling_point_ids"] = request_data.get("_selected_selling_point_ids")
            jobs[job["id"]] = job
            saved_analysis = (job.get("result") or {}).get("analysis") if isinstance(job.get("result"), dict) else None
            has_selected_points = bool(saved_analysis.get("selected_selling_points")) if isinstance(saved_analysis, dict) else False
            job_kind = "regeneration" if source_job_id else "content" if has_selected_points else "analysis"
            await analysis_queue.put((job["id"], job_kind))
    for _ in range(ANALYSIS_CONCURRENCY):
        analysis_workers.add(asyncio.create_task(analysis_worker()))


@app.on_event("shutdown")
async def stop_analysis_workers() -> None:
    """Stop workers cleanly so Uvicorn does not leave unfinished coroutines."""
    workers = list(analysis_workers)
    for worker in workers:
        worker.cancel()
    if workers:
        await asyncio.gather(*workers, return_exceptions=True)
    analysis_workers.clear()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    primary_configured = bool(os.getenv("OPENAI_API_KEY", "").strip())
    official_configured = bool(os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip())
    return {
        "ok": True,
        "sif_configured": bool(os.getenv("SIF_MCP_SECRET_KEY", "").strip()),
        # ``openai_configured`` means at least one usable endpoint exists;
        # the detailed flags let the UI/operator verify failover readiness.
        "openai_configured": primary_configured or official_configured,
        "openai_primary_configured": primary_configured,
        "openai_official_configured": official_configured,
        "openai_failover_enabled": primary_configured and official_configured,
        "demo_mode": os.getenv("DEMO_MODE", "false").lower() == "true",
        "analysis_concurrency": ANALYSIS_CONCURRENCY,
        "analysis_active": len(active_job_ids),
        "analysis_queued": analysis_queue.qsize(),
    }


@app.get("/api/strategies")
async def strategies() -> dict[str, Any]:
    """Return the supported decision strategies for the web client."""
    return {"data": [strategy_info(key) for key in STRATEGIES]}


@app.get("/api/gpt-options")
async def gpt_options() -> dict[str, Any]:
    """Return endpoint availability without exposing API keys."""
    return {
        "default_provider": "official",
        "default_model": "gpt-5.6-sol",
        "providers": [
            {
                **gpt_provider_info(key),
                "configured": (
                    bool(os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip())
                    if key == "official"
                    else bool(os.getenv("OPENAI_API_KEY", "").strip())
                    if key == "middleman"
                    else bool(os.getenv("OPENAI_API_KEY", "").strip()) and bool(os.getenv("OPENAI_OFFICIAL_API_KEY", "").strip())
                ),
            }
            for key in GPT_PROVIDERS
        ],
    }


@app.post("/api/auth/register")
async def register(credentials: Credentials) -> dict[str, Any]:
    username = credentials.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-\u4e00-\u9fff]{3,50}", username):
        raise HTTPException(status_code=422, detail="用户名需为3-50位字母、数字、中文、下划线、短横线或点")
    with db_connect() as db:
        try:
            cursor = db.execute(
                "INSERT INTO users (username, password_hash, role, status, created_at) VALUES (?, ?, 'user', 'active', ?)",
                (username, hash_password(credentials.password), now_iso()),
            )
            user_id = int(cursor.lastrowid)
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                raise HTTPException(status_code=409, detail="用户名已存在") from exc
            raise HTTPException(status_code=500, detail="注册失败") from exc
    token = create_session(user_id)
    with db_connect() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return {"user": public_user(row), "token": token}


@app.post("/api/auth/login")
async def login(credentials: Credentials) -> dict[str, Any]:
    with db_connect() as db:
        row = db.execute("SELECT * FROM users WHERE username=?", (credentials.username.strip(),)).fetchone()
    if not row or row["status"] != "active" or not verify_password(credentials.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    with db_connect() as db:
        db.execute("UPDATE users SET last_login_at=? WHERE id=?", (now_iso(), row["id"]))
        refreshed = db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
    return {"user": public_user(refreshed), "token": create_session(int(row["id"]))}


@app.post("/api/auth/logout")
async def logout(request: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, bool]:
    authorization = request.headers.get("Authorization", "")
    token = authorization.split(" ", 1)[1].strip() if authorization.lower().startswith("bearer ") else None
    revoke_session(token)
    return {"success": True}


@app.get("/api/auth/me")
async def me(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": user}


@app.post("/api/analyze")
async def create_analysis(request: AnalyzeRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    asins = clean_asins(request.asins)
    if len(asins) < 3 or len(asins) > 10:
        raise HTTPException(status_code=422, detail="请输入 3-10 个不重复的 ASIN")
    invalid = [asin for asin in asins if not re.fullmatch(r"[A-Z0-9]{10}", asin)]
    if invalid:
        raise HTTPException(status_code=422, detail=f"ASIN格式不正确: {', '.join(invalid)}")
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "message": "任务已创建",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "asins": asins,
        "user_id": user["id"],
        "request": request.model_dump(),
    }
    with db_connect() as db:
        db.execute(
            "INSERT INTO analysis_jobs (job_id, user_id, status, stage, progress, message, asins_json, request_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                user["id"],
                "queued",
                "queued",
                0,
                "任务已创建",
                json.dumps(asins, ensure_ascii=False),
                request.model_dump_json(),
                jobs[job_id]["created_at"],
                jobs[job_id]["updated_at"],
            ),
        )
    await analysis_queue.put((job_id, "analysis"))
    return {"job_id": job_id, "status": "queued", "queue_position": queue_position()}


def load_job(job_id: str) -> dict[str, Any] | None:
    job = jobs.get(job_id)
    if job:
        return job
    with db_connect() as db:
        row = db.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row["job_id"],
        "user_id": row["user_id"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "message": row["message"],
        "asins": json.loads(row["asins_json"]),
        "request": json.loads(row["request_json"]) if row["request_json"] else {},
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error"],
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if user["role"] != "admin" and int(job.get("user_id", -1)) != int(user["id"]):
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.post("/api/jobs/{job_id}/select-selling-points")
async def select_selling_points(
    job_id: str,
    payload: SellingPointSelectionRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Resume a paused task using the operator-selected selling points."""
    source = await get_job(job_id, user)
    if source.get("status") != "waiting_selection":
        raise HTTPException(status_code=409, detail="该任务当前不在卖点选择阶段")
    result = source.get("result") or {}
    analysis_result = result.get("analysis") if isinstance(result, dict) else None
    candidates = analysis_result.get("selling_point_candidates") if isinstance(analysis_result, dict) else None
    if not isinstance(candidates, list):
        raise HTTPException(status_code=409, detail="候选卖点数据不完整")
    requested_ids = list(dict.fromkeys(str(value).strip() for value in payload.selected_ids if str(value).strip()))
    candidate_map = {str(item.get("id")): item for item in candidates if isinstance(item, dict) and item.get("id")}
    invalid = [value for value in requested_ids if value not in candidate_map]
    if invalid:
        raise HTTPException(status_code=422, detail="选择中包含无效卖点")
    selected = [candidate_map[value] for value in requested_ids]
    if not selected:
        raise HTTPException(status_code=422, detail="请至少选择一个商品卖点")
    updated_analysis = dict(analysis_result)
    updated_analysis["selected_selling_points"] = selected
    updated_result = {"sif_data": result.get("sif_data") or {}, "analysis": updated_analysis}
    if job_id not in jobs:
        jobs[job_id] = source
    set_job(
        job_id,
        status="queued",
        stage="gpt",
        progress=70,
        message="卖点已确认，内容生成任务已进入队列",
        result=updated_result,
        error=None,
    )
    await analysis_queue.put((job_id, "content"))
    return {"job_id": job_id, "status": "queued", "queue_position": queue_position(), "selected_selling_points": selected}


@app.post("/api/jobs/{job_id}/regenerate")
async def regenerate_job(
    job_id: str,
    payload: RegenerateRequest | None = None,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    source = await get_job(job_id, user)
    if source.get("status") != "completed" or not source.get("result"):
        raise HTTPException(status_code=409, detail="只有已完成的任务可以重新生成")
    source_result = source["result"]
    sif_data = source_result.get("sif_data") if isinstance(source_result, dict) else None
    if not isinstance(sif_data, dict):
        raise HTTPException(status_code=409, detail="原任务没有保存 SIF 数据，无法重新生成")
    source_analysis = source_result.get("analysis") if isinstance(source_result, dict) else None
    candidates = source_analysis.get("selling_point_candidates") if isinstance(source_analysis, dict) else None
    candidate_map = {
        str(item.get("id")): item for item in (candidates or []) if isinstance(item, dict) and item.get("id")
    }
    payload_ids = payload.selected_ids if payload and payload.selected_ids else []
    requested_ids = [str(value).strip() for value in payload_ids if str(value).strip()]
    requested_ids = list(dict.fromkeys(requested_ids))
    if requested_ids:
        invalid = [value for value in requested_ids if value not in candidate_map]
        if invalid:
            raise HTTPException(status_code=422, detail="选择中包含无效卖点")
    try:
        request_data = source.get("request") or {}
        request_model = AnalyzeRequest.model_validate(request_data)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="原任务参数不完整，无法重新生成") from exc

    new_job_id = uuid.uuid4().hex
    created_at = now_iso()
    jobs[new_job_id] = {
        "id": new_job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "message": "重新生成任务已创建，不会重新调用 SIF",
        "created_at": created_at,
        "updated_at": created_at,
        "asins": source["asins"],
        "user_id": user["id"],
        "request": request_model.model_dump(),
        "source_job_id": job_id,
        "selected_selling_point_ids": requested_ids or None,
    }
    request_json = dict(request_model.model_dump())
    request_json["_regenerated_from"] = job_id
    if requested_ids:
        request_json["_selected_selling_point_ids"] = requested_ids
    with db_connect() as db:
        db.execute(
            "INSERT INTO analysis_jobs (job_id, user_id, status, stage, progress, message, asins_json, request_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                new_job_id,
                user["id"],
                "queued",
                "queued",
                0,
                "重新生成任务已创建，不会重新调用 SIF",
                json.dumps(source["asins"], ensure_ascii=False),
                json.dumps(request_json, ensure_ascii=False),
                created_at,
                created_at,
            ),
        )
    await analysis_queue.put((new_job_id, "regeneration"))
    return {"job_id": new_job_id, "source_job_id": job_id, "status": "queued", "queue_position": queue_position()}


@app.post("/api/jobs/{job_id}/regenerate-section")
async def regenerate_job_section(
    job_id: str,
    payload: SectionRegenerateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Regenerate one Listing section from the stored SIF snapshot."""
    source = await get_job(job_id, user)
    if source.get("status") != "completed" or not source.get("result"):
        raise HTTPException(status_code=409, detail="只有已完成的任务可以重新生成内容")
    if payload.section not in {"title", "bullets", "qa", "reviews"}:
        raise HTTPException(status_code=422, detail="不支持的内容类型")
    source_result = source["result"]
    sif_data = source_result.get("sif_data") if isinstance(source_result, dict) else None
    analysis_result = source_result.get("analysis") if isinstance(source_result, dict) else None
    if not isinstance(sif_data, dict) or not isinstance(analysis_result, dict):
        raise HTTPException(status_code=409, detail="原任务结果不完整，无法重新生成")
    if isinstance(payload.current_listing, dict):
        analysis_result = dict(analysis_result)
        analysis_result["listing"] = payload.current_listing
    try:
        request_model = AnalyzeRequest.model_validate(source.get("request") or {})
        value = await regenerate_section(
            request_model.product.model_dump(),
            sif_data,
            analysis_result,
            payload.section,
            payload.qa_index,
            payload.qa_count,
            payload.review_count,
            strategy=request_model.strategy,
            gpt_provider=request_model.gpt_provider,
            gpt_model=request_model.gpt_model,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"内容重新生成失败：{exc}") from exc

    listing = dict(analysis_result.get("listing") or {})
    if payload.section == "title":
        if isinstance(value, dict):
            listing["title"] = value.get("title", listing.get("title", ""))
            listing["title_zh"] = value.get("title_zh", listing.get("title_zh", ""))
        else:
            listing["title"] = value
    elif payload.section == "bullets":
        if isinstance(value, dict):
            listing["bullets"] = value.get("bullets", listing.get("bullets", []))
            listing["bullets_zh"] = value.get("bullets_zh", listing.get("bullets_zh", []))
        else:
            listing["bullets"] = value
    elif payload.section == "reviews":
        listing["internal_review_drafts"] = value
    else:
        if payload.qa_index is None:
            listing["qa"] = value
        else:
            qa = list(listing.get("qa") or [])
            if payload.qa_index >= len(qa):
                raise HTTPException(status_code=422, detail="Q&A索引无效")
            qa[payload.qa_index] = value
            listing["qa"] = qa
    analysis_result["listing"] = listing
    updated_result = {"sif_data": sif_data, "analysis": analysis_result}
    if job_id in jobs:
        set_job(job_id, result=updated_result, message="内容已重新生成")
    else:
        source["result"] = updated_result
        source["message"] = "内容已重新生成"
        source["updated_at"] = now_iso()
        persist_job(source)
    return {"job_id": job_id, "section": payload.section, "qa_index": payload.qa_index, "value": value, "result": updated_result}


@app.patch("/api/jobs/{job_id}/listing")
async def update_job_listing(
    job_id: str,
    payload: ListingUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Persist keep/delete changes made to the current listing result."""
    source = await get_job(job_id, user)
    if source.get("status") != "completed" or not source.get("result"):
        raise HTTPException(status_code=409, detail="只有已完成的任务可以保存内容")
    result = source["result"]
    if not isinstance(result, dict) or not isinstance(result.get("analysis"), dict):
        raise HTTPException(status_code=409, detail="原任务结果不完整，无法保存内容")
    analysis_result = dict(result["analysis"])
    analysis_result["listing"] = payload.listing
    updated_result = {"sif_data": result.get("sif_data") or {}, "analysis": analysis_result}
    if job_id in jobs:
        set_job(job_id, result=updated_result, message="内容已更新")
    else:
        source["result"] = updated_result
        source["message"] = "内容已更新"
        source["updated_at"] = now_iso()
        persist_job(source)
    return {"job_id": job_id, "result": updated_result}


@app.get("/api/jobs/{job_id}/export")
async def export_job(job_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    job = await get_job(job_id, user)
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="任务尚未完成")
    return job["result"]


@app.get("/api/history")
async def history(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    with db_connect() as db:
        if user["role"] == "admin":
            rows = db.execute("SELECT job_id, user_id, status, stage, progress, message, asins_json, request_json, created_at, updated_at FROM analysis_jobs ORDER BY id DESC").fetchall()
        else:
            rows = db.execute("SELECT job_id, user_id, status, stage, progress, message, asins_json, request_json, created_at, updated_at FROM analysis_jobs WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    data = []
    for row in rows:
        item = dict(row)
        item["asins"] = json.loads(item.pop("asins_json"))
        request_data = json.loads(item.pop("request_json") or "{}")
        product_data = request_data.get("product") if isinstance(request_data, dict) else {}
        product_title = product_data.get("product_name") if isinstance(product_data, dict) else ""
        item["product_title"] = str(product_title or "未命名产品").strip() or "未命名产品"
        item["strategy"] = normalize_strategy(request_data.get("strategy")) if isinstance(request_data, dict) else "comprehensive"
        item["strategy_name"] = strategy_info(item["strategy"])["name"]
        item["gpt_provider"] = normalize_gpt_provider(request_data.get("gpt_provider")) if isinstance(request_data, dict) else "official"
        item["gpt_provider_name"] = gpt_provider_info(item["gpt_provider"])["name"]
        item["gpt_model"] = str(request_data.get("gpt_model") or "gpt-5.6-sol").strip() if isinstance(request_data, dict) else "gpt-5.6-sol"
        data.append(item)
    return {"data": data, "total": len(data)}


@app.get("/api/history/{job_id}/export")
async def export_history(job_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    return await export_job(job_id, user)


@app.delete("/api/history/{job_id}")
async def delete_history(job_id: str, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, bool]:
    job = await get_job(job_id, user)
    if job.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务处理中，完成后才能删除")
    with db_connect() as db:
        db.execute("DELETE FROM analysis_jobs WHERE job_id=?", (job_id,))
    jobs.pop(job_id, None)
    return {"success": True}


@app.get("/api/admin/users")
async def admin_users(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with db_connect() as db:
        rows = db.execute("SELECT id, username, role, status, created_at, last_login_at FROM users ORDER BY id DESC").fetchall()
    return {"data": [dict(row) for row in rows]}


@app.patch("/api/admin/users/{user_id}")
async def admin_user_action(user_id: int, action: AdminUserAction, admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if user_id == admin["id"] and action.action in {"disable", "delete"}:
        raise HTTPException(status_code=422, detail="不能禁用或删除当前管理员")
    with db_connect() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        if action.action in {"enable", "disable"}:
            db.execute("UPDATE users SET status=? WHERE id=?", ("active" if action.action == "enable" else "disabled", user_id))
        elif action.action == "reset_password":
            if not action.password:
                raise HTTPException(status_code=422, detail="请输入新密码")
            db.execute("UPDATE users SET password_hash=?, status='active' WHERE id=?", (hash_password(action.password), user_id))
        elif action.action == "delete":
            db.execute("DELETE FROM users WHERE id=? AND role <> 'admin'", (user_id,))
        else:
            raise HTTPException(status_code=422, detail="不支持的操作")
    return {"success": True}


@app.get("/api/admin/requests")
async def admin_requests(_: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with db_connect() as db:
        rows = db.execute("SELECT id, user_id, username, method, path, status_code, ip_address, user_agent, created_at FROM request_logs ORDER BY id DESC LIMIT 500").fetchall()
    return {"data": [dict(row) for row in rows], "total": len(rows)}


class UploadToYmxRequest(BaseModel):
    job_id: str
    reviews: list[dict[str, Any]] = Field(default_factory=list)
    task_date: str | None = None
    operator: str | None = None
    entry_time: str | None = None
    has_image: int | None = None
    task_image_path: str | None = None
    screenshot_path: str | None = None
    remark: str | None = None


@app.post("/api/upload_to_ymx")
async def upload_to_ymx(payload: UploadToYmxRequest, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """上传模拟买家评论到亚马逊养号网站"""
    import httpx

    # 检查功能是否启用
    ymx_enabled = os.getenv("YMX_ENABLED", "false").lower() == "true"
    if not ymx_enabled:
        raise HTTPException(status_code=503, detail="养号网站集成未启用")

    ymx_api_url = os.getenv("YMX_API_URL", "")
    ymx_api_key = os.getenv("YMX_API_KEY", "")

    if not ymx_api_url:
        raise HTTPException(status_code=500, detail="养号网站API地址未配置")

    # 获取任务数据
    job = await get_job(payload.job_id, user)
    if job.get("status") != "completed":
        raise HTTPException(status_code=409, detail="任务尚未完成")

    result = job.get("result", {})
    analysis = result.get("analysis", {})
    listing = analysis.get("listing", {})
    internal_reviews = listing.get("internal_review_drafts", [])

    if not internal_reviews:
        raise HTTPException(status_code=404, detail="未找到模拟买家评论")

    # 获取产品信息
    request_data = json.loads(job.get("request_json") or "{}")
    product_data = request_data.get("product", {})
    product_name = product_data.get("product_name", "")
    asins = json.loads(job.get("asins_json") or "[]")
    main_asin = asins[0] if asins else ""

    # 准备上传的评论数据
    reviews_to_upload = []

    # 如果指定了特定评论，只上传这些；否则上传全部
    if payload.reviews:
        reviews_to_upload = payload.reviews
    else:
        # 转换格式
        for review in internal_reviews:
            reviews_to_upload.append({
                "title_en": review.get("title", ""),
                "title_cn": review.get("title_zh", ""),
                "review_en": review.get("content", ""),
                "review_cn": review.get("content_zh", ""),
                "asin": main_asin,
                "keyword": "",  # 养号网站需要关键词字段
                "product_name": product_name,
                "brand": product_data.get("brand", ""),
                "rating": 5  # 默认5星
            })

    # 准备请求数据
    upload_data = {
        "reviews": reviews_to_upload,
        "task_date": payload.task_date or datetime.now().strftime("%Y-%m-%d"),
        "operator": payload.operator or user.get("username", "SIF系统"),
        "entry_time": payload.entry_time or datetime.now().strftime("%Y-%m-%d"),
        "has_image": payload.has_image if payload.has_image is not None else 0,
        "task_image_path": payload.task_image_path or "",
        "screenshot_path": payload.screenshot_path or "",
        "remark": payload.remark or ""
    }

    # 调用养号网站API
    try:
        headers = {"Content-Type": "application/json"}
        if ymx_api_key:
            headers["Authorization"] = f"Bearer {ymx_api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                ymx_api_url,
                json=upload_data,
                headers=headers
            )

            if response.status_code != 200:
                error_detail = response.text[:500]
                raise HTTPException(
                    status_code=502,
                    detail=f"养号网站返回错误 ({response.status_code}): {error_detail}"
                )

            result_data = response.json()

            if not result_data.get("success"):
                raise HTTPException(
                    status_code=502,
                    detail=f"上传失败: {result_data.get('error', '未知错误')}"
                )

            audit_request(
                db_connect(),
                user["id"],
                user["username"],
                "POST",
                "/api/upload_to_ymx",
                200,
                None,
                None
            )

            return {
                "success": True,
                "message": "上传成功",
                "success_count": result_data.get("success_count", 0),
                "fail_count": result_data.get("fail_count", 0),
                "fail_details": result_data.get("fail_details", []),
                "inserted_ids": result_data.get("inserted_ids", [])
            }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="连接养号网站超时")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"网络请求失败: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"上传失败: {str(e)}")


@app.post("/api/upload_task_image_to_ymx")
async def upload_task_image_to_ymx(request: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """上传任务图片到养号网站"""
    import httpx

    # 检查功能是否启用
    ymx_enabled = os.getenv("YMX_ENABLED", "false").lower() == "true"
    if not ymx_enabled:
        raise HTTPException(status_code=503, detail="养号网站集成未启用")

    ymx_api_url = os.getenv("YMX_API_URL", "")
    ymx_api_key = os.getenv("YMX_API_KEY", "")

    if not ymx_api_url:
        raise HTTPException(status_code=500, detail="养号网站API地址未配置")

    # 获取图片上传API地址（替换最后的文件名）
    base_url = ymx_api_url.rsplit('/', 1)[0]
    upload_url = f"{base_url}/upload_task_image.php"

    try:
        # 读取上传的文件
        form = await request.form()
        task_image = form.get("task_image")

        if not task_image:
            raise HTTPException(status_code=400, detail="未找到上传的图片")

        # 验证文件类型和大小
        content_type = task_image.content_type
        if content_type not in ["image/jpeg", "image/jpg", "image/png", "image/gif"]:
            raise HTTPException(status_code=400, detail="不支持的图片格式，仅支持 JPG/PNG/GIF")

        file_content = await task_image.read()
        if len(file_content) > 5 * 1024 * 1024:  # 5MB
            raise HTTPException(status_code=400, detail="图片大小不能超过 5MB")

        # 准备上传到养号网站
        files = {
            "task_image": (task_image.filename, file_content, content_type)
        }

        headers = {}
        if ymx_api_key:
            headers["Authorization"] = f"Bearer {ymx_api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                upload_url,
                files=files,
                headers=headers
            )

            if response.status_code != 200:
                error_detail = response.text[:500]
                raise HTTPException(
                    status_code=502,
                    detail=f"养号网站返回错误 ({response.status_code}): {error_detail}"
                )

            result_data = response.json()

            if not result_data.get("success"):
                raise HTTPException(
                    status_code=502,
                    detail=f"图片上传失败: {result_data.get('error', '未知错误')}"
                )

            audit_request(
                db_connect(),
                user["id"],
                user["username"],
                "POST",
                "/api/upload_task_image_to_ymx",
                200,
                None,
                None
            )

            return {
                "success": True,
                "task_image_path": result_data.get("task_image_path", "")
            }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="连接养号网站超时")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"网络请求失败: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"图片上传失败: {str(e)}")


@app.post("/api/upload_screenshot_to_ymx")
async def upload_screenshot_to_ymx(request: Request, user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    """上传产品主图到养号网站"""
    import httpx

    # 检查功能是否启用
    ymx_enabled = os.getenv("YMX_ENABLED", "false").lower() == "true"
    if not ymx_enabled:
        raise HTTPException(status_code=503, detail="养号网站集成未启用")

    ymx_api_url = os.getenv("YMX_API_URL", "")
    ymx_api_key = os.getenv("YMX_API_KEY", "")

    if not ymx_api_url:
        raise HTTPException(status_code=500, detail="养号网站API地址未配置")

    # 获取图片上传API地址（替换最后的文件名）
    base_url = ymx_api_url.rsplit('/', 1)[0]
    upload_url = f"{base_url}/upload_screenshot.php"

    try:
        # 读取上传的文件
        form = await request.form()
        screenshot = form.get("screenshot")

        if not screenshot:
            raise HTTPException(status_code=400, detail="未找到上传的图片")

        # 验证文件类型和大小
        content_type = screenshot.content_type
        if content_type not in ["image/jpeg", "image/jpg", "image/png", "image/gif"]:
            raise HTTPException(status_code=400, detail="不支持的图片格式，仅支持 JPG/PNG/GIF")

        file_content = await screenshot.read()
        if len(file_content) > 5 * 1024 * 1024:  # 5MB
            raise HTTPException(status_code=400, detail="图片大小不能超过 5MB")

        # 准备上传到养号网站
        files = {
            "screenshot": (screenshot.filename, file_content, content_type)
        }

        headers = {}
        if ymx_api_key:
            headers["Authorization"] = f"Bearer {ymx_api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                upload_url,
                files=files,
                headers=headers
            )

            if response.status_code != 200:
                error_detail = response.text[:500]
                raise HTTPException(
                    status_code=502,
                    detail=f"养号网站返回错误 ({response.status_code}): {error_detail}"
                )

            result_data = response.json()

            if not result_data.get("success"):
                raise HTTPException(
                    status_code=502,
                    detail=f"产品主图上传失败: {result_data.get('error', '未知错误')}"
                )

            audit_request(
                db_connect(),
                user["id"],
                user["username"],
                "POST",
                "/api/upload_screenshot_to_ymx",
                200,
                None,
                None
            )

            return {
                "success": True,
                "screenshot_path": result_data.get("screenshot_path", "")
            }

    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="连接养号网站超时")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"网络请求失败: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"产品主图上传失败: {str(e)}")
