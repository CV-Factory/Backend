import os
import logging
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Any
from celery_tasks import process_job_posting_pipeline
from celery.result import AsyncResult
import time
import traceback

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# CORS 미들웨어 추가 시작
origins = [
    "*", # 모든 출처 허용 (테스트 목적)
    # "http://cvfactory.dev",
    # "https://cvfactory.dev", # HTTPS도 고려
    # "http://localhost",
    # "http://localhost:80", # CVFactory가 실행될 수 있는 기본 포트
    # "http://127.0.0.1",
    # "http://127.0.0.1:80",
    # 필요하다면 CVFactory 개발서버가 사용하는 다른 포트도 추가 (예: 3000, 5000 등)
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, # 특정 출처 허용, 개발 중에는 ["*"] 사용도 가능
    allow_credentials=True,
    allow_methods=["*"], # 모든 HTTP 메소드 허용
    allow_headers=["*"], # 모든 헤더 허용
)
# CORS 미들웨어 추가 끝

# Groq API 키 설정은 Celery 작업 내 또는 환경변수를 통해 관리됩니다.

class ProcessRequest(BaseModel):
    job_url: str
    prompt: str | None = None

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Any | None = None
    current_step: str | None = None

@app.on_event("startup")
async def startup_event():
    logger.info("FastAPI 애플리케이션 시작")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("FastAPI 애플리케이션 종료")

@app.post("/launch-inspector", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def launch_playwright_inspector_task(url: HttpUrl = Query(..., description="Playwright Inspector를 실행할 URL")):
    logger.info(f"Playwright Inspector 실행 요청: URL='{url}'")
    try:
        task = open_url_with_playwright_inspector.delay(str(url))
        logger.info(f"Playwright Inspector 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="Inspector Requested")
    except Exception as e:
        logger.error(f"Playwright Inspector 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting Playwright Inspector task: {str(e)}")

@app.post("/extract-body", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_extract_body_task(url: HttpUrl = Query(..., description="Body HTML을 추출할 전체 URL")):
    logger.info(f"Body HTML 추출 요청: URL='{url}'")
    try:
        task = extract_body_html_from_url.delay(str(url), task_id_for_chain_log="extract_body_only")
        logger.info(f"Body HTML 추출 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="HTML Extraction Requested")
    except Exception as e:
        logger.error(f"Body HTML 추출 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting body HTML extraction task: {str(e)}")

@app.post("/extract-text-from-html", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_extract_text_task(file_name: str = Query(..., description=".html 파일에서 텍스트를 추출할 파일 이름 (logs 디렉토리 내 위치)")):
    logger.info(f"HTML에서 텍스트 추출 요청: file_name='{file_name}'")
    if not file_name.endswith(".html"):
        logger.warning(f"잘못된 파일 확장자 요청: {file_name}. .html 파일이어야 합니다.")
        raise HTTPException(status_code=400, detail="Invalid file extension. Please provide an .html file name.")
    try:
        task = extract_text_from_html_file.delay(html_file_name=file_name, task_id_for_chain_log="extract_text_only")
        logger.info(f"HTML에서 텍스트 추출 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="Text Extraction Requested")
    except FileNotFoundError as fnf_error:
        logger.error(f"텍스트 추출 작업 시작 중 파일 찾기 오류: {fnf_error}", exc_info=True)
        raise HTTPException(status_code=404, detail=str(fnf_error))
    except Exception as e:
        logger.error(f"HTML에서 텍스트 추출 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting text extraction from HTML task: {str(e)}")

@app.post("/", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_processing_task(request: ProcessRequest):
    log_message_prefix = f"요청 수신 (비동기 파이프라인 시작): URL='{request.job_url}'"
    if request.prompt:
        log_message_prefix += f", Prompt='{request.prompt}'"
    logger.info(log_message_prefix)
    try:
        task = process_job_posting_pipeline.delay(url=request.job_url, user_prompt=request.prompt)
        logger.info(f"Celery 작업 파이프라인 시작됨. Root Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="Pipeline Initiated")
    except Exception as e:
        logger.error(f"Celery 작업 파이프라인 시작 중 오류 발생: {e}", exc_info=True)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Error starting task pipeline: {str(e)}")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    logger.info(f"작업 상태 조회 요청 시작. Task ID: {task_id}")
    start_time = time.time()
    try:
        logger.info(f"AsyncResult 객체 생성 시도. Task ID: {task_id}")
        task_result = AsyncResult(task_id)
        
        logger.info(f"Task ID: {task_id}, 상태 가져오기 시도...")
        status = task_result.status
        logger.info(f"Task ID: {task_id}, 상태: {status}")

        result_payload = None
        current_step_from_meta = None

        if task_result.ready():
            logger.info(f"Task ID: {task_id}, 작업 준비됨 (ready). 결과 가져오기 시도...")
            result_payload = task_result.result
            logger.info(f"Task ID: {task_id}, 결과 가져오기 완료.")
            if status == 'SUCCESS' and isinstance(result_payload, dict):
                current_step_from_meta = result_payload.get('current_step', 'Completed')
            elif status == 'FAILURE':
                if isinstance(task_result.info, dict):
                    current_step_from_meta = task_result.info.get('current_step', 'Failed')
                else:
                    current_step_from_meta = 'Failed'
        else:
            logger.info(f"Task ID: {task_id}, 작업 아직 준비되지 않음 (not ready).")
            if isinstance(task_result.info, dict):
                current_step_from_meta = task_result.info.get('current_step')
            elif status == 'PENDING':
                current_step_from_meta = 'Pending'
            elif status == 'STARTED':
                current_step_from_meta = 'Started'

        response_data = {
            "task_id": task_id,
            "status": status,
            "result": None,
            "current_step": current_step_from_meta
        }

        if status == 'SUCCESS':
            logger.info(f"작업 성공 (Task ID: {task_id})")
            response_data['result'] = result_payload
            if not response_data['current_step']:
                response_data['current_step'] = 'Completed'
        elif status == 'FAILURE':
            logger.warning(f"작업 실패 (Task ID: {task_id}). 저장된 결과/예외: {result_payload}")
            error_info = {"error": str(result_payload if result_payload else 'Unknown error'), 
                          "traceback": task_result.traceback if result_payload else None}
            response_data['result'] = error_info
            if not response_data['current_step']:
                response_data['current_step'] = 'Failed'
        elif status in ['PENDING', 'STARTED', 'PROGRESS']:
            logger.info(f"작업 진행 중 (Task ID: {task_id}, Status: {status}). Meta: {task_result.info}")
            response_data['result'] = task_result.info if isinstance(task_result.info, dict) else None
            if not response_data['current_step']:
                response_data['current_step'] = status
        
        end_time = time.time()
        logger.info(f"작업 상태 조회 완료 (Task ID: {task_id}). 소요 시간: {end_time - start_time:.4f}초. 응답: {response_data}")
        return TaskStatusResponse(**response_data)

    except Exception as e:
        end_time = time.time()
        logger.error(f"작업 상태 조회 중 심각한 오류 발생 (Task ID: {task_id}): {e}. 소요 시간: {end_time - start_time:.4f}초", exc_info=True)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=503, detail=f"Error fetching task status for {task_id}. Please try again later.")

@app.get("/")
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "ok", "message": "Welcome to CVFactory Server!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"로컬에서 FastAPI 서버 시작 (포트: {port})")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port) 