import os
import logging
from fastapi import FastAPI, HTTPException, status, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl
from celery_tasks import perform_processing, open_url_with_playwright_inspector
from celery.result import AsyncResult

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# Gemini API 키 설정은 tasks.py 또는 celery_app.py 내부, 혹은 환경변수를 통해 관리
# 여기서는 직접 설정하지 않습니다.

class ProcessRequest(BaseModel):
    target_url: str
    query: str | None = None

class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    result: dict | None = None

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

@app.post("/process", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_processing_task(request: ProcessRequest):
    log_message_prefix = f"요청 수신 (비동기 작업 시작): URL='{request.target_url}'"
    if request.query:
        log_message_prefix += f", Query='{request.query}'"
    logger.info(log_message_prefix)
    try:
        # Celery 작업 호출 (.delay()는 .apply_async()의 단축형)
        task = perform_processing.delay(request.target_url, request.query)
        logger.info(f"Celery 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING")
    except Exception as e:
        logger.error(f"Celery 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting task: {str(e)}")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    logger.info(f"작업 상태 조회 요청. Task ID: {task_id}")
    try:
        task_result = AsyncResult(task_id)
        response_data = {
            "task_id": task_id,
            "status": task_result.status,
            "result": task_result.result if task_result.successful() else None
        }
        if task_result.failed():
            logger.warning(f"작업 실패 (Task ID: {task_id}). 결과: {task_result.result}")
            # 실패 시 result에 에러 정보를 포함시킬 수 있습니다.
            # 예를 들어, task_result.traceback 등을 포함할 수 있으나, 
            # 프로덕션에서는 민감한 정보 노출에 주의해야 합니다.
            response_data['result'] = {"error": str(task_result.result), "traceback": task_result.traceback}
        elif task_result.status == 'PENDING':
             logger.info(f"작업 대기 중 (Task ID: {task_id})")
        elif task_result.successful():
            logger.info(f"작업 성공 (Task ID: {task_id})")
        else:
            logger.info(f"작업 상태 (Task ID: {task_id}): {task_result.status}")
        
        return TaskStatusResponse(**response_data)
    except Exception as e:
        logger.error(f"작업 상태 조회 중 오류 발생 (Task ID: {task_id}): {e}", exc_info=True)
        # 존재하지 않는 task_id 등의 경우 Celery 내부에서 예외가 발생할 수 있음
        raise HTTPException(status_code=404, detail=f"Task not found or error fetching status: {str(e)}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"로컬에서 FastAPI 서버 시작 (포트: {port})")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port) 