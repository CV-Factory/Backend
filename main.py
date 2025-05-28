import os
import logging
from fastapi import FastAPI, HTTPException, status, Query, Path
from fastapi.responses import JSONResponse, PlainTextResponse
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
        # Celery 작업 시작
        logger.info(f"Celery 작업 process_job_posting_pipeline 호출 시도. URL: {request.job_url}, Prompt: {request.prompt is not None}")
        task = process_job_posting_pipeline.delay(job_posting_url=request.job_url, user_prompt=request.prompt)
        logger.info(f"Celery 작업 시작됨. Task ID: {task.id}")
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
            final_meta = task_result.backend.get_task_meta(task_id)
            logger.info(f"Task ID: {task_id} SUCCESS. task_result.result (raw): {task_result.result}, type: {type(task_result.result)}")
            logger.info(f"Task ID: {task_id} SUCCESS. final_meta (from backend): {final_meta}, type: {type(final_meta)}")

            if isinstance(final_meta, dict):
                response_data['result'] = final_meta
                response_data['current_step'] = final_meta.get('current_step', '파이프라인 성공적으로 완료')
                if 'full_cover_letter_text' in final_meta or final_meta.get('status') == 'NO_CONTENT_FOR_COVER_LETTER':
                    logger.info(f"Task ID: {task_id} 최종 결과 메타데이터를 response_data.result에 설정합니다.")
                else:
                    logger.warning(f"Task ID: {task_id} SUCCESS, final_meta is dict, but may not contain final result keys. Meta: {final_meta}")
            else:
                logger.warning(f"Task ID: {task_id} SUCCESS but final_meta is not a dict or is None. Value: {final_meta}. Result will be None.")
                response_data['result'] = None
                response_data['current_step'] = response_data.get('current_step') or '파이프라인 완료 (결과 데이터 없음)'
        
        elif status in ['PENDING', 'STARTED', 'RETRY']:
            current_step_from_info = "작업 준비 중이거나 실행 중입니다..."
            if task_result.info and isinstance(task_result.info, dict):
                current_step_from_info = task_result.info.get('current_step', current_step_from_info)
            elif isinstance(task_result.info, str):
                logger.info(f"Task ID: {task_id} status {status}, info is a string: {task_result.info}. Using default step message.")
            
            response_data['current_step'] = current_step_from_info
            if task_result.result and status != 'SUCCESS':
                 response_data['result'] = task_result.result

        elif status == 'FAILURE':
            logger.warning(f"작업 실패 (Task ID: {task_id}). 저장된 결과/예외: {result_payload}")
            error_detail_to_return = None
            if isinstance(task_result.info, dict) and 'error_details' in task_result.info:
                error_detail_to_return = task_result.info['error_details']
            elif result_payload:
                error_detail_to_return = {"error": str(result_payload), "type": type(result_payload).__name__, "traceback": task_result.traceback}
            else:
                error_detail_to_return = {"error": "Unknown error", "traceback": task_result.traceback}
            
            response_data['result'] = error_detail_to_return
            if not response_data['current_step'] and isinstance(task_result.info, dict):
                response_data['current_step'] = task_result.info.get('current_step', 'Failed')
            elif not response_data['current_step']:
                response_data['current_step'] = 'Failed'
        elif status in ['PENDING', 'STARTED', 'PROGRESS']:
            logger.info(f"작업 진행 중 (Task ID: {task_id}, Status: {status}). Meta: {task_result.info}")
            response_data['result'] = task_result.info if isinstance(task_result.info, dict) else None
            if not response_data['current_step'] and isinstance(task_result.info, dict):
                response_data['current_step'] = task_result.info.get('current_step', status)
            elif not response_data['current_step']:
                response_data['current_step'] = status
        
        end_time = time.time()
        logger.info(f"작업 상태 조회 완료 (Task ID: {task_id}). 소요 시간: {end_time - start_time:.4f}초. 응답: {response_data}")
        return TaskStatusResponse(**response_data)

    except Exception as e:
        end_time = time.time()
        logger.error(f"작업 상태 조회 중 심각한 오류 발생 (Task ID: {task_id}): {e}. 소요 시간: {end_time - start_time:.4f}초", exc_info=True)
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=503, detail=f"Error fetching task status for {task_id}. Please try again later.")

@app.get("/logs/{filename}", response_class=PlainTextResponse)
async def get_log_file_content(filename: str = Path(..., description="로그 파일 이름", regex="^[a-zA-Z0-9_\\.\\-@]+$")):
    logger.info(f"로그 파일 내용 요청: {filename}")
    if ".." in filename or "/" in filename or "\\\\" in filename:
        logger.warning(f"잘못된 파일 이름 접근 시도: {filename}")
        raise HTTPException(status_code=400, detail="잘못된 파일 이름입니다.")
    
    log_file_path = os.path.join("logs", filename)
    logger.info(f"요청된 로그 파일 경로: {log_file_path}")
    
    if not os.path.exists(log_file_path):
        logger.warning(f"요청한 로그 파일을 찾을 수 없음: {log_file_path}")
        raise HTTPException(status_code=404, detail=f"로그 파일을 찾을 수 없습니다: {filename}")
    
    if not os.path.isfile(log_file_path):
        logger.warning(f"요청한 경로가 파일이 아님: {log_file_path}")
        raise HTTPException(status_code=400, detail=f"요청한 경로는 파일이 아닙니다: {filename}")

    try:
        with open(log_file_path, "r", encoding="utf-8") as f:
            content = f.read()
        logger.info(f"로그 파일 내용 성공적으로 읽음: {filename} (내용 일부: {content[:100]}...)")
        return PlainTextResponse(content=content)
    except Exception as e:
        logger.error(f"로그 파일 읽기 중 오류 발생 ({filename}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"로그 파일 읽기 중 오류 발생: {filename}")

@app.get("/")
async def health_check():
    logger.info("Health check endpoint called")
    return {"status": "ok", "message": "Welcome to CVFactory Server!"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"로컬에서 FastAPI 서버 시작 (포트: {port})")
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port) 