from celery_app import celery_app
import logging
import uuid
from dotenv import load_dotenv
from celery import chain, signature, states

# tasks 패키지에서 실제 작업 함수들 임포트
from .tasks.html_extraction import step_1_extract_html
from .tasks.text_extraction import step_2_extract_text
from .tasks.content_filtering import step_3_filter_content
from .tasks.cover_letter_generation import step_4_generate_cover_letter
from .tasks.pipeline_callbacks import handle_pipeline_completion

# 전역 로깅 레벨 및 라이브러리 로깅 레벨 조정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("cohere").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Celery 작업이 시작될 때 .env 파일 로드
try:
    if load_dotenv():
        logger.info(".env file loaded successfully by dotenv.")
    else:
        logger.warning(".env file not found or empty. Trusting environment variables for API keys.")
except Exception as e_dotenv:
    logger.error(f"Error loading .env file: {e_dotenv}", exc_info=True)

# ======================================================================================
# 파이프라인 실행 함수
# ======================================================================================
def process_job_posting_pipeline(url: str, user_prompt_text: str = None, root_task_id: str = None) -> str:
    """주어진 URL에 대해 전체 채용공고 처리 파이프라인을 시작합니다."""
    if not root_task_id:
        root_task_id = str(uuid.uuid4())
    
    log_prefix = f"[PipelineTrigger / Root {root_task_id}]"
    logger.info(f"{log_prefix} 파이프라인 시작 요청. URL: {url}, User Prompt: {'Yes' if user_prompt_text else 'No'}")

    pipeline = chain(
        step_1_extract_html.s(url=url, chain_log_id=root_task_id),
        step_2_extract_text.s(chain_log_id=root_task_id),
        step_3_filter_content.s(chain_log_id=root_task_id),
        step_4_generate_cover_letter.s(chain_log_id=root_task_id, user_prompt_text=user_prompt_text)
    )

    on_success_sig = handle_pipeline_completion.s(root_task_id=root_task_id, is_success=True)
    on_failure_sig = handle_pipeline_completion.s(root_task_id=root_task_id, is_success=False)

    logger.info(f"{log_prefix} Celery 파이프라인 비동기 실행 시작.")
    try:
        pipeline.apply_async(
            task_id=root_task_id, 
            link=on_success_sig, 
            link_error=on_failure_sig
        )
        logger.info(f"{log_prefix} 파이프라인 비동기 작업 시작됨. Root Task ID: {root_task_id}")
    except Exception as e_apply_async:
        logger.error(f"{log_prefix} 파이프라인 apply_async 호출 중 오류 발생: {e_apply_async}", exc_info=True)
        # API 서버 레벨에서 실패 알림 로직 (예: handle_pipeline_completion 직접 호출 또는 상태 업데이트)
        # AsyncResult(root_task_id, app=celery_app).backend.store_result(...) 등을 사용하여 상태 업데이트 가능
        raise

    return root_task_id

# --------------------------------------------------------------------------------------
# 이전의 모든 step 함수 정의 (step_1_extract_html, step_2_extract_text, 
# step_3_filter_content, step_4_generate_cover_letter) 및 
# handle_pipeline_completion 함수 정의는 각 tasks/*.py 파일로 이동되었으므로 삭제합니다.
# 또한, 관련 전역 상수들도 해당 파일들 또는 utils로 이동되었으므로 삭제합니다.
# --------------------------------------------------------------------------------------

# 전역 상수 정의 (대부분 tasks 또는 utils 하위 모듈로 이동되었거나 이동 예정)
# MAX_IFRAME_DEPTH = 5
# IFRAME_LOAD_TIMEOUT = 10000  # 10초
# ELEMENT_HANDLE_TIMEOUT = 5000 # 5초
# PAGE_NAVIGATION_TIMEOUT = 30000 # 30초
# DEFAULT_PAGE_TIMEOUT = 15000 # 15초
# LOCATOR_DEFAULT_TIMEOUT = 10000 # 10초
# GET_ATTRIBUTE_TIMEOUT = 3000  # 3초
# EVALUATE_TIMEOUT_SHORT = 2000 # 2초
# MAX_FILENAME_LENGTH = 200 # file_utils로 이동됨


# 예시: FastAPI 엔드포인트에서 호출하는 방법 (main.py 등에 위치할 내용)
# from fastapi import FastAPI, BackgroundTasks, HTTPException
# from celery.result import AsyncResult
# import uuid

# app = FastAPI()

# @app.post("/process-job-posting/")
# async def create_job_processing_task(url: str, user_prompt: Optional[str] = None):
#     if not url.startswith("http://") and not url.startswith("https://"):
#         raise HTTPException(status_code=400, detail="Invalid URL format.")
    
#     # 고유한 root_task_id 생성 (선택적, celery_tasks.py에서 생성하도록 할 수도 있음)
#     # custom_root_task_id = str(uuid.uuid4())
    
#     try:
#         # process_job_posting_pipeline 함수는 root_task_id를 반환
#         # task_id = process_job_posting_pipeline(url, user_prompt, custom_root_task_id)
#         task_id = process_job_posting_pipeline(url, user_prompt)
#         return {"message": "Job processing started.", "task_id": task_id, "status_check_url": f"/task-status/{task_id}"}
#     except Exception as e:
#         logger.error(f"Error starting job processing pipeline: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail=f"Failed to start job processing: {str(e)}")

# @app.get("/task-status/{task_id}")
# async def get_task_status(task_id: str):
#     # AsyncResult를 사용하여 Celery 백엔드에서 직접 태스크 상태를 가져옴
#     # 이 task_id는 process_job_posting_pipeline에서 반환된 root_task_id임
#     task_result = AsyncResult(task_id, app=celery_app)
    
#     response = {
#         "task_id": task_id,
#         "status": task_result.status,
#         "result": task_result.result, # 성공 시 최종 결과, 실패 시 예외 정보 포함 가능
#         "info": task_result.info # update_state로 저장된 메타데이터 (딕셔너리 형태)
#     }
    
#     # 실패 시 추가 정보 로깅 또는 반환 (선택적)
#     if task_result.failed():
#         logger.error(f"Task {task_id} failed. Result: {task_result.result}, Info: {task_result.info}")
#         # response['traceback'] = task_result.traceback # 보안상 이유로 실제 traceback은 반환하지 않는 것이 좋음
    
#     return response

# 이 파일의 나머지 부분은 비워두거나, 
# Celery 앱 설정 및 태스크 자동 검색 관련 로직만 남길 수 있습니다.
# 예를 들어, celery_app.autodiscover_tasks() 호출은 여기에 남겨둘 수 있습니다.
# (현재는 celery_app.py에서 autodiscover_tasks를 호출하고 있을 것으로 가정)

# 만약 celery_app.py에 autodiscover_tasks()가 없다면 여기에 추가:
# app.autodiscover_tasks(['CVFactory_Server.tasks']) # tasks 패키지를 명시적으로 지정
# 또는
# app.autodiscover_tasks() # settings.INSTALLED_APPS 등을 통해 자동 검색 (Django 사용 시)