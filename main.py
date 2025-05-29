import sys
import os
print(f"Current Working Directory: {os.getcwd()}")
print(f"sys.path: {sys.path}")
sys.path.insert(0, "/app") # 모듈 검색 경로에 /app 추가

import logging
import importlib.util # importlib.util 추가
import json # SSE를 위해 추가
import asyncio # SSE를 위해 추가
from fastapi import FastAPI, HTTPException, status, Query, Path, Request, Form, BackgroundTasks # Request, BackgroundTasks 추가
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
    "http://localhost:8000", # 클라이언트 개발 서버 주소
    "http://127.0.0.1:8000", # localhost의 IP 주소 버전도 추가
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
    current_step: Optional[str] = None

class StartTaskRequest(BaseModel):
    job_posting_url: str # HttpUrl 대신 str 사용 (클라이언트에서 일반 문자열로 보낼 것이므로)
    user_prompt: Optional[str] = None

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
        if isinstance(data, bytes): # bytes 타입 처리 추가
            s = data.decode('utf-8', errors='replace')
        else:
            s = str(data)
        if len(s) > max_len:
            return s[:max_len] + f"... (len: {len(s)})"
        return s
    except Exception:
        return f"[Unloggable data of type {type(data).__name__}]"

# log_user_input 함수 정의 추가
def log_user_input(request_id: str, job_url: str, has_prompt: bool):
    logger.info(f"[UserInput / ReqID: {request_id}] Job URL: '{job_url}', Has Prompt: {has_prompt}")

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
        # from celery_tasks import open_url_with_playwright_inspector # 임포트 위치 확인 필요
        # task = open_url_with_playwright_inspector.delay(str(url))
        # 위 라인은 celery_tasks에 해당 함수가 있어야 함. 현재는 정의되어 있지 않으므로 주석 처리 또는 실제 함수 필요.
        # 임시로 에러 발생시키거나, 플레이스홀더 작업 ID 반환
        logger.warning("open_url_with_playwright_inspector 기능이 현재 구현되지 않았습니다.")
        # task_id = str(uuid.uuid4())
        # return TaskStatusResponse(task_id=task_id, status="NOT_IMPLEMENTED", current_step="Inspector Not Implemented")
        raise NotImplementedError("Playwright Inspector 기능이 구현되지 않았습니다.")
        # logger.info(f"Playwright Inspector 작업 시작됨. Task ID: {task.id}")
        # return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="Inspector Requested")
    except NotImplementedError as nie:
        logger.error(f"Playwright Inspector 작업 시작 중 오류: {nie}", exc_info=True)
        raise HTTPException(status_code=501, detail=str(nie))
    except Exception as e:
        logger.error(f"Playwright Inspector 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting Playwright Inspector task: {str(e)}")

@app.post("/extract-body", status_code=status.HTTP_202_ACCEPTED, response_model=TaskStatusResponse)
async def start_extract_body_task(url: HttpUrl = Query(..., description="Body HTML을 추출할 전체 URL")):
    logger.info(f"Body HTML 추출 요청: URL='{url}'")
    try:
        from celery_tasks import extract_body_html_from_url # 실제 함수 임포트
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
        from celery_tasks import extract_text_from_html_file # 실제 함수 임포트
        task = extract_text_from_html_file.delay(html_file_name=file_name, task_id_for_chain_log="extract_text_only")
        logger.info(f"HTML에서 텍스트 추출 작업 시작됨. Task ID: {task.id}")
        return TaskStatusResponse(task_id=task.id, status="PENDING", current_step="Text Extraction Requested")
    except FileNotFoundError as fnf_error:
        logger.error(f"텍스트 추출 작업 시작 중 파일 찾기 오류: {fnf_error}", exc_info=True)
        raise HTTPException(status_code=404, detail=str(fnf_error))
    except Exception as e:
        logger.error(f"HTML에서 텍스트 추출 작업 시작 중 오류 발생: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error starting text extraction from HTML task: {str(e)}")

def get_client_ip(request: Request) -> str: 
    """Helper function to get client IP address."""
    if request.client and request.client.host:
        return request.client.host
    # X-Forwarded-For 헤더 (역방향 프록시 환경 고려)
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # 첫 번째 IP 주소를 클라이언트 IP로 간주
        return x_forwarded_for.split(',')[0].strip()
    return "Unknown"

@app.post("/log-displayed-cv", status_code=200) 
async def log_displayed_cv_endpoint(
    request_body: LogDisplayedCvRequest, 
    background_tasks: BackgroundTasks, # 사용되지 않으면 제거 가능
    request_obj: Request 
):
    client_ip = get_client_ip(request_obj) 
    log_id = uuid.uuid4()
    
    logger.info(f"[LogDisplayedCV / {log_id} / IP: {client_ip}] Received displayed CV text. Length: {len(request_body.displayed_text)}")
    # logger.debug(f"[LogDisplayedCV / {log_id}] Text: {request_body.displayed_text[:200]}...") # 필요시 내용 일부 로깅
    
    return {"message": "Displayed CV text logged successfully.", "log_id": str(log_id)}

@app.post("/", status_code=status.HTTP_202_ACCEPTED) # response_model 제거 (실제 반환값과 불일치)
async def start_task(fastapi_request: Request, request_body: StartTaskRequest): # fastapi_request로 명칭 변경
    request_id = str(uuid.uuid4())
    client_ip = get_client_ip(fastapi_request)
    logger.info(f"[ReqID: {request_id} / IP: {client_ip}] Received POST request to / (start_task).")
    
    try:
        # 원시 요청 본문 로깅 (디버깅용, 민감 정보 포함될 수 있으므로 주의)
        raw_body = await fastapi_request.body()
        logger.debug(f"[ReqID: {request_id}] Raw request body: {try_format_log(raw_body)}")
        
        # Pydantic 모델은 FastAPI에 의해 이미 파싱 시도됨. request_body는 파싱된 객체.
        # 만약 파싱 실패 시 FastAPI가 422 Unprocessable Entity를 반환하므로 이 코드까지 오지 않음.
        # 따라서 request_body는 StartTaskRequest 타입임이 보장됨 (FastAPI가 처리).
        job_posting_url = request_body.job_posting_url
        user_prompt = request_body.user_prompt
        logger.info(f"[ReqID: {request_id}] Request body successfully validated by Pydantic. URL='{job_posting_url}', Prompt provided: {bool(user_prompt)}")

    except Exception as e_parse: # 이 블록은 Pydantic 유효성 검사 전에 발생할 수 있는 오류 (예: await fastapi_request.body() 자체의 문제)
        logger.error(f"[ReqID: {request_id}] Error processing request before Pydantic validation: {e_parse}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error processing request: {str(e_parse)}")

    # log_user_input(request_id, job_posting_url, bool(user_prompt)) # 이미 위에서 로깅함

    try:
        logger.info(f"[ReqID: {request_id}] Attempting to call process_job_posting_pipeline. URL: {job_posting_url}, Prompt present: {bool(user_prompt)}")
        
        task_id_for_tracking = process_job_posting_pipeline(
            job_posting_url=job_posting_url, 
            user_prompt=user_prompt,
            root_task_id=request_id 
        )
        logger.info(f"[ReqID: {request_id}] process_job_posting_pipeline called. Returned task_id_for_tracking: {task_id_for_tracking}")

        if task_id_for_tracking != request_id:
            logger.warning(f"[ReqID: {request_id}] Mismatch! request_id: {request_id}, task_id_for_tracking: {task_id_for_tracking}. Using task_id_for_tracking ('{task_id_for_tracking}') for response as it's the actual root Celery task ID.")
            # process_job_posting_pipeline이 반환하는 ID가 실제 Celery 루트 태스크 ID이므로 이를 사용.
            response_task_id = task_id_for_tracking
        else:
            response_task_id = request_id
        
        logger.info(f"[ReqID: {request_id}] Responding with task_id: {response_task_id}")
        return {"message": "자기소개서 생성 작업이 시작되었습니다.", "task_id": response_task_id}

    except HTTPException as he: 
        logger.error(f"[ReqID: {request_id}] HTTPException during task submission: {he.detail}", exc_info=True)
        raise he
    except Exception as e:
        logger.critical(f"[ReqID: {request_id}] Critical error in POST / (start_task) endpoint: {type(e).__name__} - {e}", exc_info=True)
        # from celery_tasks import _update_root_task_state, get_detailed_error_info # 필요시 임포트
        # try:
        #     failure_meta = { ... }
        #     _update_root_task_state(request_id, state=states.FAILURE, meta=failure_meta)
        # except Exception as e_update:
        #     logger.error(f"[ReqID: {request_id}] Failed to update root task state during exception handling: {e_update}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"작업 시작 중 서버 내부 오류 발생: {type(e).__name__}")

async def get_task_status_internal(task_id: str, celery_app_instance_param): # celery_app_instance_param으로 이름 변경
    log_prefix = f"[InternalStatusCheck / Task {task_id}]"
    logger.debug(f"{log_prefix} 내부 상태 확인 시작") # INFO -> DEBUG로 변경 (빈번한 호출)
    try:
        task_result = AsyncResult(task_id, app=celery_app_instance_param) 
        
        response_status = task_result.status
        # logger.debug(f"{log_prefix} Raw status from AsyncResult: {response_status}") # 너무 상세할 수 있음

        result_data = None
        error_details = None
        current_step_from_meta = None 

        meta_info = task_result.info
        if isinstance(meta_info, dict):
            current_step_from_meta = meta_info.get('current_step_message') or meta_info.get('status_message') or meta_info.get('pipeline_status')

        if response_status == states.SUCCESS:
            result_data = meta_info 
            if not result_data and task_result.result is not None : # meta가 비어있지만 result는 있을 경우
                result_data = task_result.result 
                logger.info(f"{log_prefix} SUCCESS 상태, info는 비었지만 result 사용: {try_format_log(result_data)}")
            # logger.debug(f"{log_prefix} 상태: SUCCESS. 결과 데이터 (Info 우선): {try_format_log(result_data)}")
        elif response_status == states.FAILURE:
            error_details = meta_info 
            if not (isinstance(error_details, dict) and error_details.get('error_message')): 
                error_info_obj = task_result.result # 예외 객체일 수 있음
                error_details = {
                    'error_message': str(error_info_obj), 
                    'error_type': type(error_info_obj).__name__,
                    'traceback': task_result.traceback
                }
                # logger.info(f"{log_prefix} FAILURE 상태, info에 상세 에러 없어 result/traceback 사용: {try_format_log(error_details)}")
            # else:
                # logger.debug(f"{log_prefix} FAILURE 상태, info에서 에러 정보 사용: {try_format_log(error_details)}")
        elif response_status in [states.PENDING, states.STARTED, states.RETRY, "PROGRESS"]: 
            # logger.debug(f"{log_prefix} 상태: {response_status}. 작업 진행 중.")
            result_data = meta_info 
        else: 
            logger.info(f"{log_prefix} 상태: {response_status} (알 수 없거나 사용자 정의 상태). Info: {try_format_log(meta_info)}, Result: {try_format_log(task_result.result)}")
            result_data = meta_info if meta_info is not None else task_result.result

        return {
            "task_id": task_id,
            "status": response_status,
            "result": result_data, 
            "error": error_details if response_status == states.FAILURE else None,
            "current_step": current_step_from_meta 
        }
    except Exception as e:
        logger.error(f"{log_prefix} get_task_status_internal 호출 중 에러: {type(e).__name__} - {e}", exc_info=True)
        return {
            "task_id": task_id,
            "status": "ERROR_INTERNAL_STATUS_CHECK",
            "result": None,
            "error": f"Error checking task status: {type(e).__name__} - {str(e)}",
            "current_step": "상태 확인 중 서버 오류"
        }

@app.get("/stream-task-status/{task_id}")
async def stream_task_status(request: Request, task_id: str = Path(..., description="작업 ID")):
    client_ip = get_client_ip(request)
    logger.info(f"SSE 연결 요청 시작. Task ID: {task_id}, Client: {client_ip}")
    
    async def event_generator_for_route(task_id_str: str): 
        known_status_payload_str: Optional[str] = None 
        consecutive_error_count = 0
        max_consecutive_errors = 5 
        is_final_sent = False 

        try:
            while not is_final_sent: 
                if await request.is_disconnected():
                    logger.info(f"SSE Task ID: {task_id_str}, Client {client_ip} 연결 끊김 감지. 스트리밍 종료.")
                    break
                
                current_payload_dict = {}
                try:
                    current_payload_dict = await get_task_status_internal(task_id_str, celery_app) # 전역 celery_app 사용
                    consecutive_error_count = 0 
                except Exception as e_internal_status:
                    logger.error(f"SSE Task ID: {task_id_str}, get_task_status_internal 호출 중 에러 from {client_ip}: {e_internal_status}", exc_info=True)
                    consecutive_error_count += 1
                    if consecutive_error_count >= max_consecutive_errors:
                        logger.error(f"SSE Task ID: {task_id_str}, get_task_status_internal 연속 오류 {max_consecutive_errors}회 from {client_ip}. 스트리밍 중단.")
                        current_payload_dict = {
                            'task_id': task_id_str, 
                            'status': 'STREAM_ERROR', 
                            'error': 'Server error fetching status repeatedly.', 
                            'current_step': 'Streaming stopped due to server errors',
                            'final': True 
                        }
                        try: # 최종 오류 메시지 전송 시도
                            yield f"data: {json.dumps(current_payload_dict)}" + "\n\n"
                        except Exception as e_yield_err:
                             logger.error(f"SSE Task ID: {task_id_str}, Error yielding final stream error to {client_ip}: {e_yield_err}")
                        is_final_sent = True
                        break 
                    await asyncio.sleep(2) 
                    continue 

                current_status = current_payload_dict.get("status")
                
                if current_status in [states.SUCCESS, states.FAILURE, "STREAM_ERROR", "ERROR_INTERNAL_STATUS_CHECK", "ERROR_SETUP", "ERROR_UNEXPECTED_STREAM"]:
                    current_payload_dict['final'] = True
                else:
                    current_payload_dict['final'] = False

                try:
                    current_payload_str = json.dumps(current_payload_dict)
                except TypeError as te:
                    logger.error(f"SSE Task ID: {task_id_str}, JSON 직렬화 실패 for {client_ip}: {te}. Data: {current_payload_dict}", exc_info=True)
                    error_payload = {
                        "task_id": task_id_str,
                        "status": "ERROR_SERIALIZATION",
                        "error": f"Failed to serialize task status: {str(te)}",
                        "current_step": current_payload_dict.get("current_step"),
                        "final": True 
                    }
                    try: # 직렬화 오류 메시지 전송 시도
                        yield f"data: {json.dumps(error_payload)}" + "\n\n"
                    except Exception as e_yield_ser_err:
                        logger.error(f"SSE Task ID: {task_id_str}, Error yielding serialization error to {client_ip}: {e_yield_ser_err}")
                    is_final_sent = True
                    break 

                if current_payload_str != known_status_payload_str:
                    logger.info(f"SSE Task ID: {task_id_str}, Sending data to {client_ip}: Status={current_payload_dict.get('status')}, Step='{current_payload_dict.get('current_step')}', Final={current_payload_dict.get('final')}")
                    # logger.debug(f"SSE Task ID: {task_id_str}, Full payload to {client_ip}: {try_format_log(current_payload_str)}")
                    try:
                        yield f"data: {current_payload_str}" + "\n\n"
                    except Exception as e_yield_data:
                        logger.error(f"SSE Task ID: {task_id_str}, Error yielding data to {client_ip}: {e_yield_data}. Breaking stream.")
                        is_final_sent = True # yield 실패 시 스트림 중단
                        break
                    known_status_payload_str = current_payload_str

                if current_payload_dict.get('final'):
                    logger.info(f"SSE Task ID: {task_id_str}, 최종 상태 ({current_status}) 또는 final=True 메시지 전송됨 to {client_ip}. 스트리밍 종료.")
                    is_final_sent = True
                    break 
                
                await asyncio.sleep(1) # 폴링 간격 1초로 줄임 (반응성 향상)
        except asyncio.CancelledError:
            logger.info(f"SSE Task ID: {task_id_str}, event_generator_for_route 태스크 취소됨 (Client: {client_ip} 연결 종료 가능성).")
        except Exception as e_gen:
            logger.error(f"SSE Task ID: {task_id_str}, event_generator_for_route에서 예기치 않은 오류 발생 for {client_ip}: {e_gen}", exc_info=True)
            if not is_final_sent: 
                try:
                    error_payload = {
                        "task_id": task_id_str,
                        "status": "ERROR_UNEXPECTED_STREAM",
                        "error": f"Unexpected error in stream: {str(e_gen)}",
                        "current_step": "Unexpected stream error",
                        "final": True
                    }
                    yield f"data: {json.dumps(error_payload)}" + "\n\n"
                except Exception as e_yield_final_error:
                    logger.error(f"SSE Task ID: {task_id_str}, 최종 에러 메시지 yield 중 추가 오류 to {client_ip}: {e_yield_final_error}")
        finally:
            logger.info(f"SSE Task ID: {task_id_str}, event_generator_for_route 종료 for {client_ip}.")

    return StreamingResponse(event_generator_for_route(task_id), media_type="text/event-stream")

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status_http(task_id: str = Path(..., alias="task_id", description="조회할 작업의 ID")):
    start_time = time.time()
    logger.info(f"HTTP 작업 상태 조회 요청 시작. Task ID: {task_id}")
    try:
        status_dict = await get_task_status_internal(task_id, celery_app) 
        
        # TaskStatusResponse 모델에 맞게 데이터 조정
        # error 정보를 result 필드에 포함 (모델에 error 필드가 없으므로)
        if status_dict.get("error"):
            if status_dict.get("result") is None:
                status_dict["result"] = {"error_details": status_dict["error"]}
            elif isinstance(status_dict.get("result"), dict):
                # 기존 result가 dict면 error_details 키로 추가. 덮어쓰지 않도록 주의.
                status_dict["result"]["error_details"] = status_dict["error"]
            else: # result가 dict가 아니면 error 정보를 추가하기 어려움. 별도 로깅.
                logger.warning(f"HTTP Task ID: {task_id}, 'result' is not a dict, cannot append 'error_details'. Result type: {type(status_dict.get('result'))}")
        
        # TaskStatusResponse 모델에 없는 필드 제거
        final_status_dict_for_response = {
            key: value for key, value in status_dict.items() 
            if key in TaskStatusResponse.model_fields
        }
        
        # current_step이 None일 경우를 위해 기본값 처리 (모델에서 Optional이므로 괜찮을 수 있음)
        if 'current_step' not in final_status_dict_for_response:
            final_status_dict_for_response['current_step'] = None

        response_object = TaskStatusResponse(**final_status_dict_for_response)
        duration = time.time() - start_time
        logger.info(f"HTTP Task ID: {task_id}, 상태 조회 완료 ({duration:.4f}초). 반환 상태: {response_object.status}, 현재 단계: {response_object.current_step}")
        return response_object
        
    except HTTPException as he: 
        logger.error(f"HTTP Task ID: {task_id}, 처리 중 HTTPException: {he.detail}", exc_info=True)
        raise he 
    except Exception as e:
        duration = time.time() - start_time
        logger.error(f"HTTP Task ID: {task_id}, 상태 조회 중 예상치 못한 오류 ({duration:.4f}초): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error while fetching task status: {str(e)}")

@app.get("/logs/{filename}", response_class=PlainTextResponse)
async def get_log_file_content(filename: str = Path(..., description="로그 파일 이름", regex="^[a-zA-Z0-9_\.\-@]+$")): # 정규식 강화 가능
    logger.info(f"로그 파일 내용 요청: {filename}")
    
    # 디렉토리 트래버설 방지 강화
    if ".." in filename or filename.startswith(("/", "\\")): # 상대경로 및 절대경로 시작 방지
        logger.warning(f"잘못된 파일 이름 패턴 시도: {filename}")
        raise HTTPException(status_code=400, detail="잘못된 파일 이름입니다.")
    
    base_logs_dir = os.path.abspath("logs")
    log_file_path = os.path.normpath(os.path.join(base_logs_dir, filename)) # 경로 정규화
    
    if not log_file_path.startswith(base_logs_dir):
        logger.warning(f"디렉토리 트래버설 시도 의심: {filename} -> {log_file_path}")
        raise HTTPException(status_code=400, detail="잘못된 파일 접근입니다.")

    logger.info(f"요청된 로그 파일 경로: {log_file_path}")
    
    if not os.path.exists(log_file_path):
        logger.warning(f"요청한 로그 파일을 찾을 수 없음: {log_file_path}")
        raise HTTPException(status_code=404, detail=f"로그 파일을 찾을 수 없습니다: {filename}")
    
    if not os.path.isfile(log_file_path):
        logger.warning(f"요청한 경로가 파일이 아님: {log_file_path}")
        raise HTTPException(status_code=400, detail=f"요청한 경로는 파일이 아닙니다: {filename}")

    try:
        with open(log_file_path, "r", encoding="utf-8", errors='replace') as f: # errors='replace' 추가
            content = f.read()
        logger.info(f"로그 파일 내용 성공적으로 읽음: {filename} (내용 크기: {len(content)} bytes)")
        # logger.debug(f"로그 파일 내용 일부: {content[:200]}...") # 필요시 로깅
        return PlainTextResponse(content=content)
    except Exception as e:
        logger.error(f"로그 파일 읽기 중 오류 발생 ({filename}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"로그 파일 읽기 중 오류 발생: {filename}")

@app.get("/health") # GET / -> /health 로 변경
async def health_check_get(): 
    logger.info("Health check GET endpoint (/health) called")
    return {"status": "ok", "message": "CVFactory Server is healthy. Use POST to / to generate CV."} 

if __name__ == "__main__":
    # PORT 환경 변수가 설정되어 있지 않으면 기본값으로 8001을 사용
    # Docker 환경에서는 이 포트가 컨테이너 외부로 노출되어야 함
    port = int(os.environ.get("PORT", 8001)) 
    host = os.environ.get("HOST", "0.0.0.0") # 모든 인터페이스에서 수신 대기
    
    # Uvicorn 실행 시 reload 옵션은 개발 환경에서 유용
    # 프로덕션 환경에서는 reload=False 또는 Gunicorn 등 다른 ASGI 서버 사용 고려
    reload_enabled = os.environ.get("UVICORN_RELOAD", "true").lower() == "true"
    
    logger.info(f"FastAPI 서버 (main:app) 시작 준비. Host: {host}, Port: {port}, Reload: {reload_enabled}")
    import uvicorn
    uvicorn.run("main:app", host=host, port=port, reload=reload_enabled) 