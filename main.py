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
        raise HTTPException(status_code=500, detail=f"Error importing Celery app: {str(mnfe)}")
    except Exception as e:
        logger.error(f"AsyncResult 생성 중 오류 발생 (Task ID: {task_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error creating AsyncResult: {str(e)}")

    response_status = task_result.status
    logger.info(f"Task ID: {task_id}, task_result.status: {response_status}")

    # 결과 및 현재 단계 초기화
    final_result_for_response: Any = None
    current_step_for_response: Optional[str] = None
    
    # task_result.info (메타데이터) 조회 및 로깅
    task_info = None
    try:
        # task_info = task_result.info # .info는 @property 이므로 직접 접근
        # 또는 task_result.backend.get_task_meta(task_id) 를 사용할 수도 있습니다.
        # Celery 버전에 따라 .info가 dict가 아닐 수도 있으므로 주의. 보통은 dict.
        task_info = task_result.backend.get_task_meta(task_id) # 명시적으로 백엔드 통해 메타 조회
        logger.info(f"Task ID: {task_id}, task_result.info (메타 정보): {try_format_log(task_info)}")
        if isinstance(task_info, dict):
            current_step_for_response = task_info.get('current_step')
            # 'full_cover_letter_text' 또는 다른 주요 결과가 meta에 저장된 경우 여기서 추출
            if 'full_cover_letter_text' in task_info:
                final_result_for_response = task_info # 전체 메타를 결과로 우선 사용
                logger.info(f"Task ID: {task_id}, 'full_cover_letter_text' found in task_info. Using task_info as result.")
            elif response_status == "SUCCESS" and 'result' in task_info: # meta 안에 result 필드가 있을 경우
                final_result_for_response = task_info.get('result')
                logger.info(f"Task ID: {task_id}, 'result' field found in task_info. Using task_info.result as result.")

    except Exception as e_info:
        logger.warning(f"Task ID: {task_id}, task_result.info 조회 중 오류: {e_info}", exc_info=True)

    # 만약 task_info에서 결과를 찾지 못했고, 상태가 SUCCESS인 경우 task_result.result 조회
    if final_result_for_response is None and response_status == "SUCCESS":
        try:
            # task_result.result는 작업의 실제 반환값입니다.
            # get()을 호출하면 작업이 완료될 때까지 블로킹될 수 있으므로,
            # 이미 상태를 확인한 후에는 직접 .result 속성을 사용하는 것이 일반적입니다.
            # (단, .result는 캐시된 값일 수 있으며, get()은 최신 값을 가져옵니다.
            #  하지만 여기서는 상태가 이미 SUCCESS이므로 .result를 사용해도 괜찮을 것입니다.)
            logger.info(f"Task ID: {task_id}, 상태 SUCCESS이고 task_info에 결과 없으므로 task_result.result 조회 시도.")
            task_actual_result = task_result.result 
            logger.info(f"Task ID: {task_id}, task_result.result (실제 반환값): {try_format_log(task_actual_result)}")
            final_result_for_response = task_actual_result
        except Exception as e_result:
            logger.warning(f"Task ID: {task_id}, task_result.result 조회 중 오류: {e_result}", exc_info=True)
            # 이 경우 final_result_for_response는 None으로 유지될 수 있습니다.
    
    # 최종적으로 final_result_for_response가 여전히 None이고 상태가 SUCCESS라면,
    # 결과가 누락되었음을 명확히 하기 위한 메시지를 설정할 수 있습니다.
    if final_result_for_response is None and response_status == "SUCCESS":
        logger.warning(f"Task ID: {task_id}, 상태는 SUCCESS지만 final_result_for_response가 None입니다. 작업은 성공했으나 결과를 찾을 수 없습니다.")
        final_result_for_response = {"message": "작업은 성공했으나 결과를 찾을 수 없습니다. (Result is None but status is SUCCESS)"}
        # current_step_for_response는 task_info에서 이미 설정되었거나 None일 수 있습니다.
        if not current_step_for_response:
             current_step_for_response = "작업 완료 (결과 내용 없음)"

    elif final_result_for_response is None and response_status == "FAILURE":
        logger.warning(f"Task ID: {task_id}, 상태 FAILURE이고 final_result_for_response가 None입니다.")
        # 실패 시에는 task_info (meta)에 에러 관련 정보가 있을 가능성이 높습니다.
        # 이미 위에서 task_info를 final_result_for_response로 할당했을 수 있습니다.
        # 만약 task_info도 비어있다면, 기본적인 에러 메시지를 설정합니다.
        if isinstance(task_info, dict) and task_info: # task_info가 dict이고 내용이 있다면 그걸 사용
             final_result_for_response = task_info
        else:
             final_result_for_response = {"error": "작업 실패 (상세 정보 없음)", "details": try_format_log(task_result.traceback)}
        if not current_step_for_response and isinstance(task_info, dict):
            current_step_for_response = task_info.get('current_step', "작업 실패")
        elif not current_step_for_response:
            current_step_for_response = "작업 실패"


    # 작업 실패 시 task_result.traceback 로깅 (선택적, 이미 result에 포함될 수 있음)
    if response_status == "FAILURE":
        logger.error(f"Task ID: {task_id}, 작업 실패. Traceback: {try_format_log(task_result.traceback, max_len=500)}")

    end_time = time.time()
    logger.info(f"작업 상태 조회 완료. Task ID: {task_id}, Status: {response_status}, Result for log: {try_format_log(final_result_for_response)}, Current Step: {current_step_for_response}. 소요 시간: {(end_time - start_time)*1000:.2f}ms")

    return TaskStatusResponse(
        task_id=task_id,
        status=response_status,
        result=final_result_for_response,
        current_step=current_step_for_response
    )

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