import os
import logging
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Any
from celery_tasks import perform_processing, open_url_with_playwright_inspector, extract_body_html_from_url, extract_text_from_html_file
from celery.result import AsyncResult
import time

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

# Gemini API 키 설정은 tasks.py 또는 celery_app.py 내부, 혹은 환경변수를 통해 관리
# 여기서는 직접 설정하지 않습니다.

class ProcessRequest(BaseModel):
    job_url: str
    prompt: str | None = None

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: Any | None = None

@app.on_event("startup")
async def startup_event():
    logger.info("FastAPI 애플리케이션 시작")
    # Playwright 관련 초기화는 Celery 워커에서 수행되므로 여기서는 제거합니다.

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("FastAPI 애플리케이션 종료")

@app.post("/launch-inspector", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def launch_playwright_inspector_task(url: HttpUrl = Query(..., description="Playwright Inspector를 실행할 URL")):
    logger.info(f"Playwright Inspector 실행 요청: URL='{url}'")
    try:
        task = open_url_with_playwright_inspector.delay(str(url))
        logger.info(f"Playwright Inspector 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING")
    except Exception as e:
        logger.error(f"Playwright Inspector 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting Playwright Inspector task: {str(e)}")

@app.post("/extract-body", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_extract_body_task(url: HttpUrl = Query(..., description="Body HTML을 추출할 전체 URL")):
    logger.info(f"Body HTML 추출 요청: URL='{url}'")
    try:
        task = extract_body_html_from_url.delay(str(url))
        logger.info(f"Body HTML 추출 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING")
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
        task = extract_text_from_html_file.delay(file_name)
        logger.info(f"HTML에서 텍스트 추출 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING")
    except FileNotFoundError as fnf_error: # Celery 작업 내에서 파일 못찾을 경우 대비
        logger.error(f"텍스트 추출 작업 시작 중 파일 찾기 오류: {fnf_error}", exc_info=True)
        raise HTTPException(status_code=404, detail=str(fnf_error))
    except Exception as e:
        logger.error(f"HTML에서 텍스트 추출 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting text extraction from HTML task: {str(e)}")

@app.post("/", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_processing_task(request: ProcessRequest):
    log_message_prefix = f"요청 수신 (비동기 작업 시작): URL='{request.job_url}'"
    if request.prompt:
        log_message_prefix += f", Prompt='{request.prompt}'"
    logger.info(log_message_prefix)
    try:
        # Celery 작업 호출 (.delay()는 .apply_async()의 단축형)
        task = perform_processing.delay(request.job_url, request.prompt)
        logger.info(f"Celery 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING")
    except Exception as e:
        logger.error(f"Celery 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting task: {str(e)}")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    logger.info(f"작업 상태 조회 요청 시작. Task ID: {task_id}")
    start_time = time.time()
    try:
        logger.info(f"AsyncResult 객체 생성 시도. Task ID: {task_id}")
        task_result = AsyncResult(task_id)
        # logger.info(f"Task ID: {task_id}, Backend: {task_result.backend}") # 필요시 백엔드 정보 로깅

        logger.info(f"Task ID: {task_id}, 상태 가져오기 시도...")
        status = task_result.status # 이 호출에서 블로킹이 발생할 수 있음
        logger.info(f"Task ID: {task_id}, 상태: {status}")

        result = None
        # 작업이 준비되었는지 (성공/실패 무관) 확인 후 결과 조회
        if task_result.ready():
             logger.info(f"Task ID: {task_id}, 작업 준비됨 (ready). 결과 가져오기 시도...")
             result = task_result.result # 이 호출도 블로킹 가능성
             logger.info(f"Task ID: {task_id}, 결과 가져오기 완료.")
        else:
             logger.info(f"Task ID: {task_id}, 작업 아직 준비되지 않음 (not ready).")


        response_data = {
            "task_id": task_id,
            "status": status,
            "result": result # 성공/실패 시 실제 결과, 그 외에는 None 또는 이전 상태의 결과일 수 있음
        }

        # 최종적으로 Celery 작업의 성공/실패 상태에 따라 로깅 및 result 재정의
        if status == 'SUCCESS':
            logger.info(f"작업 성공 (Task ID: {task_id})")
            # result는 이미 위에서 할당됨
        elif status == 'FAILURE':
            logger.warning(f"작업 실패 (Task ID: {task_id}). 저장된 결과: {result}")
            # 실패 시 result는 일반적으로 예외 객체임
            response_data['result'] = {"error": str(result), "traceback": task_result.traceback if result else None}
        elif status == 'PENDING':
             logger.info(f"작업 대기 중 (Task ID: {task_id})")
        else:
            logger.info(f"작업 상태 (Task ID: {task_id}): {status}")
        
        end_time = time.time()
        logger.info(f"작업 상태 조회 완료 (Task ID: {task_id}). 소요 시간: {end_time - start_time:.4f}초")
        return TaskStatusResponse(**response_data)
    except Exception as e:
        end_time = time.time()
        logger.error(f"작업 상태 조회 중 심각한 오류 발생 (Task ID: {task_id}): {e}. 소요 시간: {end_time - start_time:.4f}초", exc_info=True)
        # 클라이언트에는 좀 더 일반적인 오류 메시지를 반환하거나, 상황에 따라 다른 status_code 사용 가능
        raise HTTPException(status_code=503, detail=f"Error fetching task status. Please try again later.")

@app.get("/")
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "ok", "message": "Welcome to CVFactory Server!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"로컬에서 FastAPI 서버 시작 (포트: {port})")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port) 