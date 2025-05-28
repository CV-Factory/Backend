import sys
sys.path.insert(0, "/app") # 모듈 검색 경로에 /app 추가

import os
import logging
import importlib.util # importlib.util 추가
from fastapi import FastAPI, HTTPException, status, Query, Path
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Any, Optional, Dict
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
    start_time = time.time()
    logger.info(f"작업 상태 조회 요청 시작. Task ID: {task_id}")
    try:
        logger.info(f"AsyncResult 객체 생성 시도. Task ID: {task_id}")
        celery_app_instance = get_celery_app_instance() # celery_app 인스턴스 가져오기
        task_result = AsyncResult(task_id, app=celery_app_instance) # app 인자 전달
        logger.info(f"Task ID: {task_id}, AsyncResult 객체 생성 직후. task_result.id: {task_result.id}, task_result.state: {task_result.state}, task_result.backend: {try_format_log(task_result.backend)}")
    except ModuleNotFoundError as mnfe:
        logger.error(f"Celery 앱 인스턴스 가져오기 중 ModuleNotFoundError 발생 (Task ID: {task_id}): {mnfe}", exc_info=True)
        # get_celery_app_instance 내부에서 이미 상세 로깅을 하므로 여기서는 중복 로깅 최소화
        raise HTTPException(status_code=500, detail=f"Error importing Celery app: {str(mnfe)}")
    except Exception as e:
        logger.error(f"AsyncResult 생성 중 오류 발생 (Task ID: {task_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating AsyncResult: {str(e)}")

    response_status = task_result.status
    logger.info(f"Task ID: {task_id}, task_result.status: {response_status}")

    retrieved_result = None # 이 변수는 이제 직접적인 결과 저장용이 아님
    retrieved_meta = None # task.info에서 가져올 메타데이터

    if task_result.ready():
        logger.info(f"Task ID: {task_id}, 작업 준비됨 (ready). task_result.info 직접 접근 시도...")
        # task_result.get() 호출 제거 또는 주석 처리
        # try:
        #     # get() 호출 전 상태 로깅
        #     logger.info(f"Task ID: {task_id}, BEFORE get(). task_result.result (속성 직접 접근): {try_format_log(task_result.result)}, task_result.info (속성 직접 접근): {try_format_log(task_result.info)}")
        #     retrieved_result = task_result.get(timeout=1.0) 
        #     logger.info(f"Task ID: {task_id}, AFTER get(). task_result.result (get() 호출 후 속성): {try_format_log(task_result.result)}, task_result.info (get() 호출 후 속성): {try_format_log(task_result.info)}")
        #     logger.info(f"Task ID: {task_id}, get()이 반환한 값 (retrieved_result): {try_format_log(retrieved_result)}, type: {type(retrieved_result).__name__}")
        # except TimeoutError:
        #     logger.warning(f"Task ID: {task_id}, 결과 가져오기 시간 초과 (timeout=1.0s). 작업이 아직 완료되지 않았을 수 있습니다.")
        #     # 이 경우에도 response_status는 여전히 이전 상태를 유지할 수 있음 (예: SUCCESS)
        # except Exception as e:
        #     logger.error(f"Task ID: {task_id}, 결과 가져오기 중 오류: {e}", exc_info=True)
        #     retrieved_result = {"error": str(e), "type": type(e).__name__, "message": "결과 가져오기 중 오류 발생"}
        #     response_status = "FAILURE" # get() 실패 시 FAILURE로 간주

        try:
            # .info를 직접 사용
            retrieved_meta = task_result.info 
            logger.info(f"Task ID: {task_id}, task_result.info (retrieved_meta) 직접 접근 결과: {try_format_log(retrieved_meta)}, type: {type(retrieved_meta).__name__}")
            
            # Celery 백엔드에서 결과를 가져온 후 상태가 FAILURE로 변경될 수도 있으므로, 여기서 다시 한번 상태 확인
            if task_result.failed():
                logger.warning(f"Task ID: {task_id}, task_result.info 접근 후 .failed()가 True를 반환. 상태를 FAILURE로 갱신합니다.")
                response_status = "FAILURE"
            elif task_result.successful(): # 성공 상태도 다시 확인
                 response_status = "SUCCESS"
                 if retrieved_meta is None: # 성공했는데 meta가 None이면 문제
                     logger.error(f"Task ID: {task_id}, 작업은 성공(successful())했으나 task_result.info가 None입니다. 백엔드 저장 문제 가능성.")
                     # response_status는 SUCCESS로 두되, final_response_payload에서 오류 메시지 처리
                 
        except Exception as e:
            logger.error(f"Task ID: {task_id}, task_result.info 접근 중 오류: {e}", exc_info=True)
            retrieved_meta = {"error": str(e), "type": type(e).__name__, "message": "메타 정보 접근 중 오류 발생"}
            response_status = "FAILURE" # info 접근 실패 시 FAILURE로 간주

    # 최종 응답 구성
    final_response_payload = None
    current_step_for_response = None

    if response_status == "SUCCESS":
        logger.info(f"Task ID: {task_id} SUCCESS.")
        # 최종 결과는 retrieved_meta에 있을 수도 있고, retrieved_result에 직접 있을 수도 있음.
        # Celery chain의 마지막 태스크가 update_state를 명시적으로 호출하여 info를 채우지 않으면
        # task.result (retrieved_result)에 최종 결과가 담김.
        if isinstance(retrieved_meta, dict) and retrieved_meta.get('status') == 'SUCCESS':
            logger.info(f"Task ID: {task_id} SUCCESS, retrieved_meta가 유효한 결과 객체로 보임: {try_format_log(retrieved_meta)}")
            final_response_payload = retrieved_meta
            current_step_for_response = retrieved_meta.get('current_step', '파이프라인 성공적으로 완료')
        elif retrieved_result is not None and not (isinstance(retrieved_result, dict) and 'error' in retrieved_result):
            # retrieved_meta가 없거나 SUCCESS 상태가 아니지만, retrieved_result (task.result)에 유효한 결과가 있는 경우
            logger.info(f"Task ID: {task_id} SUCCESS, retrieved_meta가 없거나 부적절하지만, retrieved_result에 유효한 결과가 있음: {try_format_log(retrieved_result)}")
            final_response_payload = retrieved_result # task.result를 그대로 사용
            current_step_for_response = "파이프라인 성공적으로 완료 (결과 직접 반환)"
            if isinstance(retrieved_result, dict): # 결과가 dict이면 current_step등을 가져와볼 수 있음
                 current_step_for_response = retrieved_result.get('current_step', current_step_for_response)
                 if not retrieved_result.get('full_cover_letter_text') and retrieved_result.get('message'): # 주요 결과 필드 확인
                    logger.warning(f"Task ID: {task_id}, full_cover_letter_text는 없지만 message는 있음. result: {try_format_log(retrieved_result)}")
            
        elif retrieved_meta is not None: # retrieved_meta가 있지만 SUCCESS 마커가 없는 경우 (예: 문자열)
             logger.warning(f"Task ID: {task_id} SUCCESS, 하지만 retrieved_meta의 형식이 예상과 다름: {try_format_log(retrieved_meta)}. retrieved_result: {try_format_log(retrieved_result)}")
             final_response_payload = retrieved_meta # 일단 meta를 사용
             current_step_for_response = "파이프라인 완료 (메타데이터 형식 확인 필요)"
        else: # meta도 없고 result도 없는 경우 (이론상 SUCCESS면 발생하기 어려움)
            logger.error(f"Task ID: {task_id} SUCCESS, 하지만 retrieved_meta와 retrieved_result가 모두 비어있음. 이는 예상치 못한 상황입니다.")
            final_response_payload = {"message": "작업은 성공했으나 결과를 찾을 수 없습니다."}
            current_step_for_response = "결과 없음 (성공)"
            
    elif response_status == "FAILURE":
        logger.warning(f"Task ID: {task_id} FAILURE.")
        error_details_from_meta = None
        if isinstance(retrieved_meta, dict):
            error_details_from_meta = retrieved_meta.get('error_details', retrieved_meta.get('error'))
            current_step_for_response = retrieved_meta.get('current_step', '작업 실패')
        
        if error_details_from_meta:
            final_response_payload = {"error": error_details_from_meta, "traceback": task_result.traceback}
        elif isinstance(retrieved_result, dict) and 'error' in retrieved_result: # get()에서 발생한 오류
            final_response_payload = retrieved_result
        elif retrieved_result is not None: # task.result가 예외 객체일 수 있음
             final_response_payload = {"error": str(retrieved_result), "type": type(retrieved_result).__name__, "traceback": task_result.traceback}
        else: # 정보가 없는 경우
            final_response_payload = {"error": "알 수 없는 실패", "traceback": task_result.traceback}
        
        if not current_step_for_response:
            current_step_for_response = "작업 실패"

    else: # PENDING, STARTED, RETRY 등
        logger.info(f"Task ID: {task_id} 작업 진행 중. Status: {response_status}")
        current_step_for_response = f"작업 진행 중 ({response_status})"
        if isinstance(retrieved_meta, dict):
            current_step_for_response = retrieved_meta.get('current_step', current_step_for_response)
            final_response_payload = retrieved_meta # 진행 중일 때는 meta가 주 정보원
        elif isinstance(retrieved_meta, str): # 가끔 문자열로 올 때가 있음
            current_step_for_response = retrieved_meta
            final_response_payload = {"message": retrieved_meta}
        else: # meta가 아예 없거나 다른 타입인 경우
            final_response_payload = {"message": f"현재 상태: {response_status}"}


    response_data = {
        "task_id": task_id,
        "status": response_status,
        "result": final_response_payload,
        "current_step": current_step_for_response
    }

    end_time = time.time()
    duration = end_time - start_time
    logger.info(f"작업 상태 조회 완료 (Task ID: {task_id}). 소요 시간: {duration:.4f}초. 응답: {try_format_log(response_data)}")
    return response_data

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