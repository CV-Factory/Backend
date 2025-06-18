from api.celery_app import celery_app
import logging
import uuid
from dotenv import load_dotenv
from celery import chain, signature, states
from celery.exceptions import Ignore
import time

from api.tasks.html_extraction import step_1_extract_html
from api.tasks.text_extraction import step_2_extract_text
from api.tasks.content_filtering import step_3_filter_content
from api.tasks.cover_letter_generation import step_4_generate_cover_letter
from api.tasks.pipeline_callbacks import handle_pipeline_completion

# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# logging.getLogger("httpcore").setLevel(logging.WARNING)
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("cohere").setLevel(logging.WARNING)
# logging.getLogger("playwright").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

try:
    if load_dotenv():
        logger.info(".env file loaded successfully by dotenv.")
    else:
        logger.warning(".env file not found or empty. Trusting environment variables for API keys.")
except Exception as e_dotenv:
    logger.error(f"Error loading .env file: {e_dotenv}", exc_info=True)

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
        raise

    return root_task_id