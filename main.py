import sys
sys.path.insert(0, "/app") # 모듈 검색 경로에 /app 추가

import os
import logging
import importlib.util # importlib.util 추가
import json # SSE를 위해 추가
import asyncio # SSE를 위해 추가
from fastapi import FastAPI, HTTPException, status, Query, Path, Request, Form # Request 추가
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse # StreamingResponse 추가
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Any, Optional, Dict, AsyncGenerator # AsyncGenerator 추가
from celery_tasks import process_job_posting_pipeline
from celery.result import AsyncResult
import time
import traceback
import uuid
from celery_app import celery_app
from celery import states
from enum import Enum

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

class LogDisplayedCvRequest(BaseModel):
    displayed_text: str

# Celery 애플리케이션 인스턴스를 가져오는 함수 (celery_worker.app 가정)
# 실제 환경에 맞게 celery_worker.app를 임포트하거나 가져오는 방식을 사용해야 합니다.
def get_celery_app_instance():
    logger.info("--- get_celery_app_instance() 호출됨 ---")
    logger.info(f"현재 작업 디렉토리 (os.getcwd()): {os.getcwd()}")
    logger.info(f"PYTHONPATH 환경 변수: {os.environ.get('PYTHONPATH')}")
    logger.info(f"sys.path: {sys.path}")
    
    celery_app_py_path = os.path.abspath(os.path.join(os.getcwd(), "celery_app.py"))
    logger.info(f"celery_app.py 예상 절대 경로: {celery_app_py_path}")
    logger.info(f"celery_app.py 존재 여부: {os.path.exists(celery_app_py_path)}")
    
    spec = importlib.util.find_spec("celery_app")
    if spec is None:
        logger.error("'celery_app' 모듈을 찾을 수 없습니다 (importlib.util.find_spec 결과 None).")
    else:
        logger.info(f"'celery_app' 모듈 스펙: {spec}")
        logger.info(f"'celery_app' 모듈 위치 (예상): {spec.origin}")

    try:
        from celery_app import app as celery_app_instance # 'celery_worker'를 'celery_app'으로 변경
        logger.info(f"'celery_app' 모듈에서 'app' 임포트 성공. 타입: {type(celery_app_instance)}")
        return celery_app_instance
    except ImportError as ie:
        logger.error(f"'from celery_app import app' 실행 중 ImportError 발생: {ie}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"'celery_app'에서 'app' 임포트 중 예상치 못한 오류 발생: {e}", exc_info=True)
        raise

# 로그 출력을 위한 유틸리티 함수
def try_format_log(data, max_len=200):
    if data is None:
        return "None"
    try:
        s = str(data)
        if len(s) > max_len:
            return s[:max_len] + f"... (len: {len(s)})"
        return s
    except Exception:
        return f"[Unloggable data of type {type(data).__name__}]"

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

@app.post("/log-displayed-cv", status_code=status.HTTP_200_OK)
async def log_displayed_cv_from_frontend(request: LogDisplayedCvRequest):
    """
    프론트엔드의 generated_resume textarea에 표시된 내용을 받아 로깅합니다.
    """
    try:
        logger.info(f"프론트엔드에서 수신된 자기소개서 내용 (검증용):\n--- START ---\n{request.displayed_text}\n--- END ---")
        return {"message": "Displayed CV content logged successfully."}
    except Exception as e:
        logger.error(f"프론트엔드 자기소개서 내용 로깅 중 오류: {e}", exc_info=True)
        # 이 경우는 클라이언트에게 심각한 오류를 알릴 필요는 없을 수 있으므로,
        # 500 대신 로깅 성공 여부와 관계없이 200을 반환하거나, 별도의 상태 코드를 사용할 수 있습니다.
        # 여기서는 간단히 500을 발생시키겠습니다.
        raise HTTPException(status_code=500, detail=f"Error logging displayed CV content: {str(e)}")

@app.post("/log-displayed-cv", status_code=200)
async def log_displayed_cv_endpoint(
    request: LogDisplayedCvRequest,
    background_tasks: BackgroundTasks,
    request_data: Request = None  # Client IP 로깅을 위해 추가
):
    """
    클라이언트에서 실제로 표시된 자기소개서 텍스트를 로깅합니다.
    """
    client_ip = get_client_ip(request_data) if request_data else "Unknown"
    log_id = uuid.uuid4()
    
    # 실제 로깅 로직은 background task로 실행하여 응답 시간을 줄일 수 있습니다.
    # 여기서는 간단히 로그만 남깁니다.
    logger.info(f"[LogDisplayedCV / {log_id} / IP: {client_ip}] Received displayed CV text. Length: {len(request.displayed_text)}")
    
    # 필요하다면 background_tasks.add_task(save_to_db_or_file, request.displayed_text, log_id, client_ip) 등을 사용할 수 있습니다.
    # 지금은 단순히 성공 메시지만 반환합니다.
    return {"message": "Displayed CV text logged successfully.", "log_id": str(log_id)}

@app.post("/", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_task(job_posting_url: str = Form(...), user_prompt: Optional[str] = Form(None)):
    request_id = str(uuid.uuid4())
    logger.info(f"[ReqID: {request_id}] 요청 수신 (비동기 파이프라인 시작): URL='{job_posting_url}', Prompt='{user_prompt[:50] if user_prompt else None}...'")
    
    # 사용자 입력 로깅 (여기서는 URL과 프롬프트 유무만 로깅, 전체 프롬프트는 필요시)
    log_user_input(request_id, job_posting_url, bool(user_prompt))

    try:
        # process_job_posting_pipeline은 이제 일반 함수이며, root_task_id를 반환합니다.
        # 이 root_task_id는 Celery 체인의 논리적 ID로 사용되며, 상태 추적에 사용됩니다.
        logger.info(f"[ReqID: {request_id}] Celery 작업 파이프라인 호출 시도. URL: {job_posting_url}, Prompt: {bool(user_prompt)}")
        
        # process_job_posting_pipeline 함수에 root_task_id로 request_id 전달
        # 함수 내부에서 이 ID를 사용하여 초기 상태를 설정하고 체인을 시작합니다.
        # 반환되는 task_id_for_tracking은 request_id와 동일해야 합니다.
        task_id_for_tracking = process_job_posting_pipeline(
            job_posting_url=job_posting_url, 
            user_prompt=user_prompt,
            root_task_id=request_id # 명시적으로 root_task_id 전달
        )

        if task_id_for_tracking != request_id:
            logger.error(f"[ReqID: {request_id}] Mismatch between generated request_id and task_id_for_tracking from pipeline function: {task_id_for_tracking}. Using request_id.")
            # 안전을 위해 request_id를 계속 사용
            task_id_for_tracking = request_id

        logger.info(f"[ReqID: {request_id}] Celery 작업 파이프라인 제출됨. 추적을 위한 Task ID: {task_id_for_tracking}")
        return {"message": "자기소개서 생성 작업이 시작되었습니다.", "task_id": task_id_for_tracking}
    except Exception as e:
        logger.critical(f"[ReqID: {request_id}] /start-task 엔드포인트에서 심각한 오류 발생: {e}", exc_info=True)
        # 여기서도 _update_root_task_state를 호출하여 해당 root_task_id의 상태를 FAILURE로 명시적으로 설정 가능
        # from celery_tasks import _update_root_task_state, get_detailed_error_info, states # 필요한 경우 임포트
        # failure_meta = {
        #     'status_message': 'FastAPI 작업 제출 중 오류',
        #     'error': str(e),
        #     'type': type(e).__name__,
        #     'details': get_detailed_error_info(e),
        #     'root_task_id': request_id,
        #     'job_posting_url': job_posting_url,
        # }
        # _update_root_task_state(request_id, state=states.FAILURE, meta=failure_meta, exc=e, traceback_str=traceback.format_exc()) 
        # # _update_root_task_state는 celery_tasks 모듈에 있으므로, main.py에서 직접 호출하려면 임포트 필요
        # # 또는 이 로직을 process_job_posting_pipeline 내부의 예외 처리로 옮길 수 있음.
        # # 현재 process_job_posting_pipeline의 최상단 try-except가 이를 처리하므로 여기서는 HTTP 에러만 반환.
        raise HTTPException(status_code=500, detail=f"작업 시작에 실패했습니다: {str(e)}")

async def get_task_status_internal(task_id: str, celery_app_instance):
    log_prefix = f"[InternalStatusCheck / Task {task_id}]"
    logger.info(f"{log_prefix} 내부 상태 확인 시작")
    try:
        # task_id는 이제 process_job_posting_pipeline에서 생성/관리하는 root_task_id입니다.
        task_result = AsyncResult(task_id, app=celery_app_instance) # 전역 celery_app 사용
        
        response_status = task_result.status
        logger.debug(f"{log_prefix} Raw status from AsyncResult: {response_status}")

        result_data = None
        error_details = None

        if response_status == 'SUCCESS':
            # 성공 시, 결과는 task_result.info 또는 task_result.result에 저장됩니다.
            # process_job_posting_pipeline의 콜백에서 meta에 최종 결과를 저장하므로 task_result.info를 사용합니다.
            result_data = task_result.info # 여기가 중요! 콜백이 meta를 업데이트합니다.
            if not result_data:
                # 만약 info가 비어있다면, result도 확인 (하지만 콜백 구조상 info에 있어야 함)
                result_data = task_result.result
                logger.warning(f"{log_prefix} SUCCESS 상태이지만 task_result.info가 비어있어 task_result.result 사용: {result_data}")
            logger.info(f"{log_prefix} 상태: SUCCESS. 결과 데이터 확인 (Info 우선): {result_data}")
        elif response_status == 'FAILURE':
            # 실패 시, 에러 정보는 task_result.info (meta) 또는 task_result.result (예외 객체)에 있을 수 있습니다.
            # _update_root_task_state 와 콜백에서 meta에 에러 정보를 저장하려고 시도합니다.
            error_info_from_meta = task_result.info 
            if error_info_from_meta and isinstance(error_info_from_meta, dict) and error_info_from_meta.get('error'):
                error_details = error_info_from_meta
                logger.error(f"{log_prefix} 상태: FAILURE. 에러 정보 (meta): {error_details}")
            else:
                # meta에 상세 정보가 없다면 result (예외)를 사용
                error_details = str(task_result.result) # 예외 객체를 문자열로
                logger.error(f"{log_prefix} 상태: FAILURE. 에러 정보 (result): {error_details}")
                # 추가로 traceback도 로깅 (존재한다면)
                tb_str = task_result.traceback
                if tb_str:
                    logger.error(f"{log_prefix} 실패 트레이스백:\n{tb_str}")
        elif response_status in ['PENDING', 'STARTED', 'RETRY']:
            logger.info(f"{log_prefix} 상태: {response_status}. 작업 진행 중.")
            # 진행 중일 때는 task_result.info (meta)에 있는 중간 상태 메시지를 확인
            result_data = task_result.info # PENDING/STARTED/PROGRESS 시 meta 정보
        else: # 사용자 정의 상태 또는 기타 상태
            logger.info(f"{log_prefix} 상태: {response_status} (처리되지 않은 상태). 결과: {task_result.info}")
            result_data = task_result.info

        return {
            "task_id": task_id,
            "status": response_status,
            "result": result_data, # 성공 시 결과, 진행 시 meta, 실패 시 None 또는 info
            "error": error_details if response_status == 'FAILURE' else None
        }
    except Exception as e:
        logger.error(f"{log_prefix} get_task_status_internal 호출 중 에러: {type(e).__name__} - {e}", exc_info=True)
        # SSE 스트림에서 이 오류를 처리할 수 있도록 특정 형식으로 반환
        return {
            "task_id": task_id,
            "status": "ERROR_INTERNAL_STATUS_CHECK",
            "result": None,
            "error": f"Error checking task status: {type(e).__name__} - {str(e)}"
        }

MAX_SSE_ERRORS = 5 # 연속 오류 허용 횟수

async def event_generator(task_id_for_stream: str, request: Request):
    # celery_app_instance = get_celery_app_instance() # get_celery_app_instance() 대신 전역 celery_app 사용
    # if not celery_app_instance:
    #     logger.error(f"SSE Task ID: {task_id_for_stream}, Celery app instance를 가져올 수 없습니다.")
    #     yield f"data: {json.dumps({'task_id': task_id_for_stream, 'status': 'ERROR', 'error': 'Celery app not available'})}\n\n"
    #     return
    
    consecutive_errors = 0
    processed_final_status = False # 최종 상태(SUCCESS/FAILURE)가 한 번만 처리되도록 보장

    try:
        while True:
            # 클라이언트 연결 끊김 감지
            if await request.is_disconnected():
                logger.info(f"SSE Task ID: {task_id_for_stream}, 클라이언트 연결 끊김. 스트리밍 중단.")
                break

            # 상태 확인 (get_task_status_internal은 이제 celery_app을 직접 사용)
            status_data = await get_task_status_internal(task_id_for_stream, celery_app) 
            
            current_status = status_data.get("status")
            logger.debug(f"SSE Task ID: {task_id_for_stream}, Polled status: {current_status}, Data: {status_data}")

            if current_status == "ERROR_INTERNAL_STATUS_CHECK" or status_data.get("error") and "Error checking task status" in status_data.get("error", ""):
                consecutive_errors += 1
                logger.error(f"SSE Task ID: {task_id_for_stream}, get_task_status_internal 호출 중 에러 발생. 연속 {consecutive_errors}회.")
                if consecutive_errors >= MAX_SSE_ERRORS:
                    logger.error(f"SSE Task ID: {task_id_for_stream}, get_task_status_internal 연속 오류 {MAX_SSE_ERRORS}회 발생. 스트리밍 중단.")
                    yield f"data: {json.dumps({'task_id': task_id_for_stream, 'status': 'STREAM_ERROR', 'error': 'Server error fetching status repeatedly.', 'final': True })}\n\n"
                    processed_final_status = True # 오류로 인한 최종 상태로 간주
                    break
                # 일시적 오류일 수 있으므로 잠시 후 재시도
                await asyncio.sleep(2) # 오류 시에는 더 긴 간격
                continue 
            else:
                consecutive_errors = 0 # 정상 응답 시 카운터 리셋
            
            # 최종 상태(SUCCESS, FAILURE)이고 아직 처리되지 않았다면 전송하고 종료
            if current_status in [states.SUCCESS, states.FAILURE] and not processed_final_status:
                logger.info(f"SSE Task ID: {task_id_for_stream}, 최종 상태 '{current_status}' 감지. 결과 전송 후 스트리밍 종료.")
                status_data['final'] = True # 프론트엔드에서 종료를 인식할 수 있도록 final 플래그 추가
                yield f"data: {json.dumps(status_data)}\n\n"
                processed_final_status = True
                break
            elif processed_final_status:
                # 이미 최종 상태를 보냈으므로 루프 종료
                logger.debug(f"SSE Task ID: {task_id_for_stream}, 이미 최종 상태 전송됨. 루프 종료.")
                break
            else: # 진행 중인 상태
                status_data['final'] = False
                yield f"data: {json.dumps(status_data)}\n\n"
            
            await asyncio.sleep(2)  # 2초 간격으로 폴링

    except asyncio.CancelledError:
        logger.info(f"SSE Task ID: {task_id_for_stream}, event_generator 작업 취소됨 (클라이언트 연결 종료 가능성).")
    except Exception as e_stream:
        logger.error(f"SSE Task ID: {task_id_for_stream}, event_generator에서 예기치 않은 오류: {e_stream}", exc_info=True)
        try:
            # 스트림 오류 발생 시에도 클라이언트에게 오류 메시지 전송 시도
            if not processed_final_status: # 아직 final 메시지가 전송되지 않았다면
                error_payload = {
                    'task_id': task_id_for_stream, 
                    'status': 'STREAM_ERROR', 
                    'error': f'Unexpected error in stream: {str(e_stream)}', 
                    'final': True
                }
                yield f"data: {json.dumps(error_payload)}\n\n"
        except Exception as e_yield_err:
            logger.error(f"SSE Task ID: {task_id_for_stream}, 스트림 오류 메시지 전송 실패: {e_yield_err}")
    finally:
        logger.info(f"SSE Task ID: {task_id_for_stream}, event_generator 종료.")

@app.get("/stream-task-status/{task_id}")
async def stream_task_status(request: Request, task_id: str = Path(..., description="작업 ID")):
    logger.info(f"SSE 연결 요청 시작. Task ID: {task_id}")
    try:
        celery_app_instance = get_celery_app_instance()
    except Exception as e_celery_app:
        logger.error(f"SSE Task ID: {task_id}, Celery 앱 인스턴스 가져오기 실패: {e_celery_app}", exc_info=True)
        # SSE 스트림 시작 전에 오류가 발생하면 일반적인 HTTP 응답으로 처리도 가능하나,
        # 여기서는 StreamingResponse로 에러를 알리고 바로 종료되도록 시도합니다.
        async def error_stream():
            yield f"data: {json.dumps({'task_id': task_id, 'status': 'ERROR_SETUP', 'result': {'error': str(e_celery_app)}, 'current_step': 'Celery connection error'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    async def event_generator(task_id_for_stream: str):
        known_status: Optional[str] = None
        known_step: Optional[str] = None
        consecutive_error_count = 0
        max_consecutive_errors = 5 # 최대 연속 오류 허용 횟수

        try:
            while True:
                if await request.is_disconnected():
                    logger.info(f"SSE Task ID: {task_id_for_stream}, 클라이언트 연결 끊김 감지. 스트리밍 종료.")
                    break
                
                try:
                    status_data = await get_task_status_internal(task_id_for_stream, celery_app)
                    consecutive_error_count = 0 # 성공 시 리셋
                except Exception as e_internal_status:
                    logger.error(f"SSE Task ID: {task_id_for_stream}, get_task_status_internal 호출 중 에러: {e_internal_status}", exc_info=True)
                    consecutive_error_count += 1
                    if consecutive_error_count >= max_consecutive_errors:
                        logger.error(f"SSE Task ID: {task_id_for_stream}, get_task_status_internal 연속 오류 {max_consecutive_errors}회 발생. 스트리밍 중단.")
                        yield f"data: {json.dumps({'task_id': task_id_for_stream, 'status': 'ERROR_STREAM', 'result': {'error': 'Server error fetching status repeatedly.'}, 'current_step': 'Streaming stopped due to server errors'})}\n\n"
                        break
                    await asyncio.sleep(2) # 오류 시 조금 더 대기
                    continue # 다음 폴링 시도

                current_status = status_data.get("status")
                current_step = status_data.get("current_step")

                # 상태나 단계가 변경되었을 때만 전송, 또는 최종 상태일 때 전송
                if current_status != known_status or current_step != known_step or \
                   current_status in ["SUCCESS", "FAILURE", "REVOKED", "ERROR_INTERNAL", "ERROR_SETUP"]:
                    
                    logger.info(f"SSE Task ID: {task_id_for_stream}, Sending data: Status={current_status}, Step='{current_step}'")
                    try:
                        json_data = json.dumps(status_data)
                        yield f"data: {json_data}\n\n"
                    except TypeError as te:
                        logger.error(f"SSE Task ID: {task_id_for_stream}, JSON 직렬화 실패: {te}. Data: {status_data}", exc_info=True)
                        # 직렬화 실패 시 에러 메시지 전송
                        error_payload = {
                            "task_id": task_id_for_stream,
                            "status": "ERROR_SERIALIZATION",
                            "result": {"error": f"Failed to serialize task status: {str(te)}"},
                            "current_step": current_step
                        }
                        yield f"data: {json.dumps(error_payload)}\n\n"
                        # 직렬화 오류가 계속되면 스트림을 중단할 수도 있음
                        # 여기서는 일단 다음 폴링으로 넘어감

                    known_status = current_status
                    known_step = current_step

                if current_status in ["SUCCESS", "FAILURE", "REVOKED", "ERROR_INTERNAL", "ERROR_SETUP", "ERROR_STREAM"]:
                    logger.info(f"SSE Task ID: {task_id_for_stream}, 최종 상태 ({current_status}) 도달. 스트리밍 종료.")
                    break
                
                await asyncio.sleep(2) # 2초 간격으로 폴링
        except asyncio.CancelledError:
            logger.info(f"SSE Task ID: {task_id_for_stream}, event_generator 태스크 취소됨 (클라이언트 연결 종료 가능성).")
        except Exception as e_gen:
            logger.error(f"SSE Task ID: {task_id_for_stream}, event_generator에서 예기치 않은 오류 발생: {e_gen}", exc_info=True)
            try:
                error_payload = {
                    "task_id": task_id_for_stream,
                    "status": "ERROR_UNEXPECTED_STREAM",
                    "result": {"error": f"Unexpected error in stream: {str(e_gen)}"},
                    "current_step": "Unexpected stream error"
                }
                yield f"data: {json.dumps(error_payload)}\n\n"
            except Exception as e_yield_final_error:
                logger.error(f"SSE Task ID: {task_id_for_stream}, 최종 에러 메시지 yield 중 추가 오류: {e_yield_final_error}")
        finally:
            logger.info(f"SSE Task ID: {task_id_for_stream}, event_generator 종료.")

    return StreamingResponse(event_generator(task_id), media_type="text/event-stream")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status_http(task_id: str = Path(..., alias="task_id", description="조회할 작업의 ID")):
    # HTTP GET 요청을 위한 엔드포인트 (기존 /tasks/{task_id}와 유사)
    # 내부 로직은 get_task_status_internal을 호출하여 재사용
    start_time = time.time()
    logger.info(f"HTTP 작업 상태 조회 요청 시작. Task ID: {task_id}")
    try:
        celery_app_instance = get_celery_app_instance()
        status_dict = await get_task_status_internal(task_id, celery_app_instance)
        
        # get_task_status_internal이 반환한 딕셔너리가 TaskStatusResponse 모델과 호환되는지 확인
        # 예를 들어, status_dict["result"]가 Pydantic 모델이거나 직렬화 불가능한 객체면 오류 발생 가능
        # 여기서는 TaskStatusResponse가 Any를 허용하므로 일단 그대로 반환
        # 필요시 여기서 result를 한 번 더 가공/검증할 수 있음
        
        response_object = TaskStatusResponse(**status_dict)
        duration = time.time() - start_time
        logger.info(f"HTTP Task ID: {task_id}, 상태 조회 완료 ({duration:.4f}초). 반환 상태: {response_object.status}, 현재 단계: {response_object.current_step}")
        return response_object
        
    except HTTPException as he:
        logger.error(f"HTTP Task ID: {task_id}, 처리 중 HTTPException: {he.detail}", exc_info=True)
        raise he # 이미 HTTPException이므로 그대로 raise
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"HTTP Task ID: {task_id}, 상태 조회 중 예상치 못한 오류 ({duration:.4f}초): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error while fetching task status: {str(e)}")

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