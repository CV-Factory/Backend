import logging
import os
import traceback
from urllib.parse import urlparse

from celery import shared_task, states
from playwright.sync_api import sync_playwright

from utils.celery_utils import _update_root_task_state
from utils.common_utils import ensure_dir, sanitize_filename, get_datetime_prefix, try_format_log
from utils.file_utils import save_content_to_file
from utils.html_utils import extract_page_content_with_playwright
from utils.url_utils import get_domain_from_url

logger = logging.getLogger(__name__)

@shared_task(bind=True, name="celery_tasks.step_1_extract_html")
def step_1_extract_html(self, job_url: str, root_task_id: str, task_config: dict = None):
    if not root_task_id:
        # 호출 시 root_task_id가 누락된 경우, 현재 태스크의 ID를 사용 (단독 실행 시)
        # 또는 체인으로 실행된 경우 self.request.root_id를 사용해야 함
        root_task_id = self.request.id
        logger.warning(f"[Task {self.request.id} / Root {root_task_id} / Step 1_extract_html] root_task_id not provided, using current task_id. This might be an issue if part of a chain without explicit root_id passing.")
    
    log_prefix = f"[Task {self.request.id} / Root {root_task_id} / Step 1_extract_html]"
    logger.info(f"{log_prefix} ---------- Task started. URL: {job_url} ----------")
    
    try:
        # 작업 시작 시 상태 업데이트
        _update_root_task_state(
            root_task_id=root_task_id,
            state=states.STARTED, # 또는 PROGRESS
            meta={'current_step': '웹 페이지 접속 및 분석 시작 중...'}
        )

        if not job_url:
            logger.error(f"{log_prefix} Job URL is missing or empty.")
            _update_root_task_state(root_task_id, states.FAILURE, meta={'current_step': '오류: URL 정보 없음', 'error_message': 'Job URL is missing'})
            raise ValueError("Job URL is required")

        parsed_url = urlparse(job_url)
        if not parsed_url.scheme or not parsed_url.netloc:
            logger.error(f"{log_prefix} Invalid URL format: {job_url}")
            _update_root_task_state(root_task_id, states.FAILURE, meta={'current_step': '오류: 잘못된 URL 형식', 'error_message': f'Invalid URL format: {job_url}'})
            raise ValueError(f"Invalid URL format: {job_url}")

        # Playwright를 사용한 웹 페이지 콘텐츠 추출 시작 알림
        _update_root_task_state(
            root_task_id=root_task_id,
            state='PROGRESS', # PROGRESS 상태로 변경
            meta={'current_step': '채용 공고 페이지 접속 및 정보 추출 중... (Playwright 사용)'}
        )
        logger.info(f"{log_prefix} Starting Playwright operations for URL: {job_url}")
        html_content, page_title = extract_page_content_with_playwright(job_url, log_prefix=log_prefix)
        logger.info(f"{log_prefix} Playwright operations complete.")

        if not html_content:
            logger.warning(f"{log_prefix} No HTML content was extracted from {job_url}.")
            _update_root_task_state(root_task_id, states.FAILURE, meta={'current_step': '오류: 페이지 내용 추출 실패', 'error_message': 'No HTML content extracted'})
            # 실패로 처리하거나, 빈 내용으로 계속 진행할지 결정 필요 - 여기서는 실패로 간주
            raise Exception("Failed to extract HTML content.")
            
        datetime_prefix = get_datetime_prefix()
        domain = get_domain_from_url(job_url)
        sanitized_page_title = sanitize_filename(page_title if page_title else domain if domain else "unknown_page")
        
        # 파일명 규칙: logs/{도메인}_{페이지제목}_{루트태스크ID축약}_{시간스탬프}.html -> logs/{도메인}_{페이지제목}_{루트태스크ID축약}_{고유해시}.html
        # 루트 태스크 ID의 일부를 사용하여 파일명의 고유성을 높이고 추적을 용이하게 함
        root_task_id_short = root_task_id.split('-')[0] # 예: 76b7f9d4
        
        # 파일명 생성 시 중복을 피하기 위해 더욱 고유한 값(시간+무작위) 사용
        # datetime_prefix 대신 더 짧은 해시값 사용 고려 (예: uuid.uuid4().hex[:8])
        # 여기서는 기존 datetime_prefix 사용, 파일명 길이 문제 발생 시 수정 필요
        unique_suffix = datetime_prefix # 필요시 self.request.id.split('-')[0] 등을 추가하여 더 높은 고유성 확보
        
        # 파일 저장 경로 설정 및 폴더 생성
        log_dir = os.path.join(os.getcwd(), "logs") # /app/logs
        ensure_dir(log_dir)
        
        # 파일명에 루트 태스크 ID와 고유 식별자(시간 기반) 추가
        filename = f"{sanitized_page_title}_{root_task_id_short}_{unique_suffix}.html" # 원본 HTML 저장 파일명 변경 적용됨
        raw_html_file_path = os.path.join(log_dir, filename)
        
        logger.info(f"{log_prefix} Saving extracted page content to: {raw_html_file_path}")
        _update_root_task_state(
            root_task_id=root_task_id,
            state='PROGRESS',
            meta={'current_step': '추출된 HTML 내용 파일로 저장 중...'}
        )
        save_content_to_file(raw_html_file_path, html_content)
        logger.info(f"{log_prefix} Page content successfully saved to {raw_html_file_path}.")

        result = {
            "html_file_path": raw_html_file_path,
            "original_url": job_url,
            "page_title": page_title, # 페이지 제목 추가
            "page_content": html_content[:500] + "..." if html_content and len(html_content) > 500 else html_content # 로그용으로 일부만
        }
        
        # 성공적으로 완료되었음을 알리는 상태 업데이트 (콜백에서 최종 SUCCESS 처리)
        # 여기서는 PROGRESS 상태로 중간 결과와 메시지 업데이트
        _update_root_task_state(
            root_task_id=root_task_id,
            state='PROGRESS', 
            meta={
                'current_step': '웹 페이지 정보 추출 완료. 다음 단계 진행 중...',
                'html_extraction_result': { # 현재 단계의 주요 결과도 meta에 포함 가능
                    "html_file_path": raw_html_file_path,
                    "original_url": job_url,
                    "page_title": page_title
                }
            }
        )
        logger.info(f"{log_prefix} ---------- Task finished successfully. Result for log: {try_format_log(result, max_len=200)} ----------")
        return result

    except Exception as e:
        formatted_traceback = traceback.format_exc()
        logger.error(f"{log_prefix} Error in step_1_extract_html: {e}
Traceback: {formatted_traceback}")
        # 실패 상태 업데이트
        _update_root_task_state(
            root_task_id=root_task_id,
            state=states.FAILURE,
            meta={
                'current_step': f'웹 페이지 추출 중 심각한 오류 발생: {type(e).__name__}',
                'error_message': str(e),
                'traceback': formatted_traceback
            },
            exc=e, # exc와 traceback_str은 celery_utils에서 자동으로 처리해줄 수 있음
            traceback_str=formatted_traceback
        )
        # 실패 시에는 원래 태스크의 update_state를 호출하여 Celery가 실패를 인지하도록 함
        self.update_state(state=states.FAILURE, meta={'exc_type': type(e).__name__, 'exc_message': str(e), 'traceback': formatted_traceback})
        raise # 예외를 다시 발생시켜 Celery 에러 핸들링 로직이 실행되도록 함 