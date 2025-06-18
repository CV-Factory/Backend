import logging
import os
import uuid
import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Any, Optional, Dict
from celery import current_app, states
from celery.result import AsyncResult
from sse_starlette.sse import EventSourceResponse

from api.logging_config import setup_logging
from api.celery_tasks import process_job_posting_pipeline

# 로깅 설정
setup_logging()
logger = logging.getLogger(__name__)

# 경로 설정
# 이 파일(main.py)은 /app/api/에 위치하므로, project_root는 /app/이 됩니다.
# Path(__file__) -> /app/api/main.py
# .resolve() -> /app/api/main.py (절대 경로)
# .parent -> /app/api
# .parent.parent -> /app
project_root = Path(__file__).resolve().parent.parent

# FastAPI 앱 생성
app = FastAPI()

# 정적 파일 마운트 ( /static 요청을 프로젝트 루트의 static 폴더로 연결 )
# 예: /static/css/style.css -> /app/static/css/style.css
app.mount("/static", StaticFiles(directory=project_root / "static"), name="static")

# 템플릿 설정 ( templates 폴더를 템플릿 디렉토리로 지정 )
# 예: templates.TemplateResponse("index.html", ...) -> /app/templates/index.html
templates = Jinja2Templates(directory=str(project_root / "templates"))

# CORS 미들웨어 설정
origins = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in origins if origin.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic 모델 정의
class StartTaskRequest(BaseModel):
    job_url: str
    user_story: Optional[str] = None

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Any = None
    current_step: Optional[str] = None
    
# Celery 앱 인스턴스
celery_app = current_app._get_current_object()

@app.on_event("startup")
async def startup_event():
    logger.info("FastAPI application starting up.")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("FastAPI application shutting down.")

# --- 라우트(Routes) ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """메인 페이지를 렌더링합니다."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/create-cover-letter", status_code=202)
async def create_cover_letter(request: StartTaskRequest):
    """자기소개서 생성 파이프라인을 시작합니다."""
    try:
        task_id = process_job_posting_pipeline(
            url=request.job_url,
            user_prompt_text=request.user_story
        )
        logger.info(f"Cover letter generation task started. URL: {request.job_url}, Task ID: {task_id}")
        return {"task_id": task_id}
    except Exception as e:
        logger.error(f"Failed to start cover letter generation task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start the task.")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """작업의 현재 상태를 반환합니다."""
    task_result = AsyncResult(task_id, app=celery_app)
    
    current_step = None
    result_data = None
    
    if isinstance(task_result.info, dict):
        current_step = task_result.info.get("current_step", "상태 정보 없음")
        result_data = task_result.info
    elif task_result.state == states.FAILURE:
        current_step = "작업 실패"
        result_data = str(task_result.info)
    else:
        current_step = task_result.state

    return TaskStatusResponse(
        task_id=task_id,
        status=task_result.state,
        current_step=current_step,
        result=result_data,
    )

@app.get("/stream-task-status/{task_id}")
async def stream_task_status(request: Request, task_id: str):
    """SSE를 사용하여 작업 상태를 실시간으로 스트리밍합니다."""
    async def event_generator():
        while True:
            if await request.is_disconnected():
                logger.warning(f"Client disconnected from task {task_id} stream.")
                break

            task_result = AsyncResult(task_id, app=celery_app)
            status_data = {"status": task_result.state, "info": task_result.info if isinstance(task_result.info, (dict, str)) else None}
            
            yield {
                "event": "update",
                "data": json.dumps(status_data)
            }

            if task_result.ready():
                logger.info(f"Task {task_id} finished. Closing stream.")
                final_data = {"status": task_result.state, "info": task_result.info if isinstance(task_result.info, (dict, str)) else None}
                yield {
                    "event": "end",
                    "data": json.dumps(final_data)
                }
                break

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())

@app.get("/health")
async def health_check():
    """헬스 체크 엔드포인트."""
    return {"status": "ok"}

@app.get("/logs/{filename}", response_class=PlainTextResponse)
async def get_log_file(filename: str):
    """로그 파일을 조회합니다."""
    log_file_path = project_root / "logs" / filename
    if not log_file_path.is_file():
        raise HTTPException(status_code=404, detail="Log file not found.")
    return PlainTextResponse(content=log_file_path.read_text(encoding="utf-8"))