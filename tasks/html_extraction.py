from api.celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright, Error as PlaywrightError
import os
import hashlib
import uuid
import traceback
from celery.exceptions import MaxRetriesExceededError, Reject
from celery import states
from typing import Dict
from api.utils.playwright_utils import (_get_playwright_page_content_with_iframes_processed,
                               DEFAULT_PAGE_TIMEOUT, PAGE_NAVIGATION_TIMEOUT)
from api.utils.file_utils import sanitize_filename, try_format_log
from api.utils.celery_utils import _update_root_task_state

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, name='celery_tasks.step_1_extract_html', max_retries=1, default_retry_delay=10)
def step_1_extract_html(self, url: str, chain_log_id: str) -> Dict[str, str]:
    logger.info("GLOBAL_ENTRY_POINT: step_1_extract_html function called.")
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step 1_extract_html]"
    logger.info(f"{log_prefix} ---------- Task started. URL: {url} ----------")
    logger.debug(f"{log_prefix} Input URL: {url}, Chain Log ID: {chain_log_id}")

    # 작업 시작 시 상태 업데이트 (진행률 0%)
    self.update_state(state='PROGRESS', meta={'current_step': '채용공고 페이지 분석을 시작합니다...', 'percentage': 0, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_INITIATED'})
    _update_root_task_state(
        root_task_id=chain_log_id,
        state=states.STARTED,
        meta={
            'current_step': '채용공고 분석 준비 중... (HTML 추출 단계 시작)',
            'status_message': f"(1_extract_html) HTML 추출 시작: {url}", 
            'current_task_id': str(task_id), 
            'url_for_step1': url,
            'pipeline_step': 'EXTRACT_HTML_INITIATED',
            'percentage': 2 # 예시 진행률
        }
    )

    html_file_path = ""
    try:
        logger.info(f"{log_prefix} Initializing Playwright...")
        # Playwright 초기화 중 상태 업데이트 (진행률 5%)
        self.update_state(state='PROGRESS', meta={'current_step': '페이지 분석 도구를 준비하고 있습니다...', 'percentage': 5, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_PLAYWRIGHT_INIT'})
        _update_root_task_state(
            root_task_id=chain_log_id, state=states.STARTED,
            meta={'current_step': '채용공고 페이지 분석 도구를 준비하고 있습니다...', 'pipeline_step': 'EXTRACT_HTML_PLAYWRIGHT_INIT', 'percentage': 7}
        )
        with sync_playwright() as p:
            logger.info(f"{log_prefix} Playwright initialized. Launching browser...")
            # 브라우저 실행 중 상태 업데이트 (진행률 10%)
            self.update_state(state='PROGRESS', meta={'current_step': '가상 브라우저를 실행하여 페이지에 접속 준비 중입니다...', 'percentage': 10, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_BROWSER_LAUNCHING'})
            _update_root_task_state(
                root_task_id=chain_log_id, state=states.STARTED,
                meta={'current_step': '채용공고 페이지를 열기 위해 가상 브라우저를 실행 중입니다...', 'pipeline_step': 'EXTRACT_HTML_BROWSER_LAUNCHING', 'percentage': 12}
            )
            try:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
                logger.info(f"{log_prefix} Browser launched.")
            except Exception as e_browser:
                logger.error(f"{log_prefix} Error launching browser: {e_browser}", exc_info=True)
                # 실패 상태 업데이트
                self.update_state(state=states.FAILURE, meta={'current_step': "오류: 가상 브라우저 실행에 실패했습니다. 잠시 후 다시 시도해주세요.", 'error': str(e_browser), 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_BROWSER_LAUNCH_FAILED'})
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE, 
                    exc=e_browser, 
                    traceback_str=traceback.format_exc(), 
                    meta={
                        'current_step': "오류: 가상 브라우저 실행에 실패했습니다. 잠시 후 다시 시도해주세요.",
                        'status_message': "(1_extract_html) 브라우저 실행 실패", 
                        'error_message': str(e_browser), 
                        'url': url,
                        'current_task_id': str(task_id),
                        'pipeline_step': 'EXTRACT_HTML_BROWSER_LAUNCH_FAILED'
                    }
                )
                raise Reject(f"Browser launch failed: {e_browser}", requeue=False)

            try:
                page = browser.new_page()
                logger.info(f"{log_prefix} New page created. Setting default timeout to {DEFAULT_PAGE_TIMEOUT}ms.")
                page.set_default_timeout(DEFAULT_PAGE_TIMEOUT)
                page.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT)
                
                logger.info(f"{log_prefix} Navigating to URL: {url}")
                # 페이지 이동 중 상태 업데이트 (진행률 20%)
                self.update_state(state='PROGRESS', meta={'current_step': '채용공고 페이지에 접속하고 있습니다...', 'percentage': 20, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_PAGE_NAVIGATING'})
                _update_root_task_state(
                    root_task_id=chain_log_id, state=states.STARTED,
                    meta={'current_step': '채용공고 페이지에 접속하고 있습니다...', 'pipeline_step': 'EXTRACT_HTML_PAGE_NAVIGATING', 'percentage': 22}
                )
                page.goto(url, wait_until="domcontentloaded")
                logger.info(f"{log_prefix} Successfully navigated to URL. Current page URL: {page.url}")

                logger.info(f"{log_prefix} iframe 처리 및 페이지 내용 가져오기 시작.")
                # 페이지 내용 가져오는 중 상태 업데이트 (진행률 40%)
                self.update_state(state='PROGRESS', meta={'current_step': '페이지의 전체 내용을 로드하고 있습니다. (iframe 포함)', 'percentage': 40, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_GETTING_CONTENT'})
                _update_root_task_state(
                    root_task_id=chain_log_id, state=states.STARTED,
                    meta={'current_step': '채용공고 페이지의 전체 내용을 불러오는 중입니다...', 'pipeline_step': 'EXTRACT_HTML_GETTING_CONTENT', 'percentage': 42}
                )
                page_content = _get_playwright_page_content_with_iframes_processed(page, url, chain_log_id, str(task_id))
                logger.info(f"{log_prefix} 페이지 내용 가져오기 완료 (길이: {len(page_content)}).")
                # 내용 가져오기 완료 후 상태 업데이트 (진행률 70%)
                self.update_state(state='PROGRESS', meta={'current_step': '페이지 내용 로드 완료. 분석을 위해 저장합니다.', 'percentage': 70, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_CONTENT_LOADED'})
                _update_root_task_state(
                    root_task_id=chain_log_id, state=states.STARTED,
                    meta={'current_step': '페이지 내용 로드가 완료되었습니다. 추출된 내용을 저장합니다.', 'pipeline_step': 'EXTRACT_HTML_CONTENT_LOADED', 'percentage': 72}
                )

            except PlaywrightError as e_playwright:
                error_message = f"Playwright operation failed: {e_playwright}"
                logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
                # 실패 상태 업데이트
                self.update_state(state=states.FAILURE, meta={'current_step': '오류: 페이지 분석 중 문제가 발생했습니다.', 'error': error_message, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_PLAYWRIGHT_FAILED'})
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE, 
                    exc=e_playwright, 
                    traceback_str=traceback.format_exc(), 
                    meta={
                        'current_step': "오류: 채용공고 페이지 분석 중 문제가 발생했습니다. (Playwright 오류)",
                        'status_message': "(1_extract_html) Playwright 작업 실패", 
                        'error_message': error_message, 
                        'url': url,
                        'current_task_id': str(task_id),
                        'pipeline_step': 'EXTRACT_HTML_PLAYWRIGHT_FAILED'
                    }
                )
                raise Reject(error_message, requeue=False)
            except Exception as e_general:
                error_message = f"An unexpected error occurred during HTML extraction: {e_general}"
                logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
                # 실패 상태 업데이트
                self.update_state(state=states.FAILURE, meta={'current_step': '오류: HTML 추출 중 예기치 않은 문제가 발생했습니다.', 'error': error_message, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_UNEXPECTED_ERROR'})
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE, 
                    exc=e_general, 
                    traceback_str=traceback.format_exc(), 
                    meta={
                        'current_step': "오류: 채용공고 HTML 추출 중 예기치 않은 오류", 
                        'status_message': "(1_extract_html) HTML 추출 중 예기치 않은 오류", 
                        'error_message': error_message, 
                        'url': url,
                        'current_task_id': str(task_id),
                        'pipeline_step': 'EXTRACT_HTML_UNEXPECTED_ERROR'
                    }
                )
                raise Reject(error_message, requeue=False)
            finally:
                logger.info(f"{log_prefix} Closing browser.")
                if 'browser' in locals() and browser:
                    try:
                        browser.close()
                        logger.info(f"{log_prefix} Browser closed successfully.")
                    except Exception as e_close:
                        logger.warning(f"{log_prefix} Error closing browser: {e_close}", exc_info=True)
                logger.info(f"{log_prefix} Playwright context cleanup finished.")
        
        logger.info(f"{log_prefix} Playwright operations complete.")
        # 파일 저장 중 상태 업데이트 (진행률 80%)
        self.update_state(state='PROGRESS', meta={'current_step': '추출된 페이지 내용을 파일로 저장하고 있습니다...', 'percentage': 80, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_SAVING_CONTENT'})
        _update_root_task_state(
            root_task_id=chain_log_id, state=states.STARTED,
            meta={'current_step': '추출된 채용공고 내용을 저장하고 있습니다...', 'pipeline_step': 'EXTRACT_HTML_SAVING_CONTENT', 'percentage': 82}
        )

        os.makedirs("logs", exist_ok=True)
        filename_base = sanitize_filename(url, ensure_unique=False)
        unique_file_id = hashlib.md5((chain_log_id + str(uuid.uuid4())).encode('utf-8')).hexdigest()[:8]
        html_file_name = f"{filename_base}_raw_html_{chain_log_id[:8]}_{unique_file_id}.html"
        html_file_path = os.path.join("logs", html_file_name)
            
        logger.info(f"{log_prefix} Saving extracted page content to: {html_file_path}")
        with open(html_file_path, "w", encoding="utf-8") as f:
            f.write(page_content)
        logger.info(f"{log_prefix} Page content successfully saved to {html_file_path}.")

        result_data = {"html_file_path": html_file_path, "original_url": url, "page_content": page_content}
        
        result_data_for_log = result_data.copy()
        if 'page_content' in result_data_for_log:
            page_content_len = len(result_data_for_log['page_content']) if result_data_for_log['page_content'] is not None else 0
            result_data_for_log['page_content'] = f"<page_content_omitted_from_log, length={page_content_len}>"

        # 파일 저장 완료 및 다음 단계 준비 상태 업데이트 (진행률 90%)
        self.update_state(state='PROGRESS', meta={'current_step': '페이지 내용 저장 완료. 다음 분석 단계를 준비합니다.', 'percentage': 90, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_COMPLETED'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED, # SUCCESS 전 마지막 PROGRESS
            meta={
                'current_step': "채용공고 HTML 추출 완료. 다음 단계로 이동합니다.",
                'status_message': "(1_extract_html) HTML 추출 및 저장 완료", 
                'html_file_path': html_file_path,
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_COMPLETED',
                'percentage': 95 # 예시 진행률
            }
        )
        logger.info(f"{log_prefix} ---------- Task finished successfully. Result for log: {try_format_log(result_data_for_log)} ----------")
        logger.debug(f"{log_prefix} Returning from step_1: keys={list(result_data.keys())}, page_content length: {len(result_data.get('page_content', '')) if result_data.get('page_content') else 0}")
        # 최종 성공 상태 업데이트 (진행률 100%)
        self.update_state(state=states.SUCCESS, meta={**result_data, 'current_step': '채용공고 페이지 분석 및 HTML 추출이 성공적으로 완료되었습니다.', 'percentage': 100, 'pipeline_step': 'EXTRACT_HTML_SUCCESS'})
        return result_data

    except Reject as e_reject:
        logger.warning(f"{log_prefix} Task explicitly rejected: {e_reject.reason}. Celery will handle retry/failure.")
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': f'오류: HTML 추출 작업이 중단되었습니다. (사유: {e_reject.reason})', 'error': e_reject.reason, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_REJECTED'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.FAILURE, 
            exc=e_reject, 
            traceback_str=getattr(e_reject, 'traceback', traceback.format_exc()),
            meta={
                'current_step': f"오류: HTML 추출 작업 중 문제가 발생하여 중단되었습니다. (사유: {e_reject.reason})",
                'status_message': f"(1_extract_html) 작업 명시적 거부: {e_reject.reason}", 
                'error_message': str(e_reject.reason), 
                'reason_for_reject': getattr(e_reject, 'message', str(e_reject)),
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_REJECTED'
            }
        ) 
        raise

    except MaxRetriesExceededError as e_max_retries:
        error_message = "Max retries exceeded for HTML extraction."
        logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: HTML 추출 작업 재시도 한도를 초과했습니다.', 'error': error_message, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_MAX_RETRIES'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.FAILURE, 
            exc=e_max_retries, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': "오류: HTML 추출 작업 재시도 한도 초과. 관리자에게 문의하세요.",
                'status_message': "(1_extract_html) 최대 재시도 초과", 
                'error_message': error_message, 
                'original_exception': str(e_max_retries),
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_MAX_RETRIES'
            }
        )
        raise

    except Exception as e_outer:
        error_message = f"Outer catch-all error in step_1_extract_html: {e_outer}"
        logger.critical(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: HTML 추출 중 예기치 않은 심각한 오류가 발생했습니다.', 'error': error_message, 'current_task_id': str(task_id), 'pipeline_step': 'EXTRACT_HTML_CRITICAL_ERROR'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_outer, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': "오류: HTML 추출 중 예기치 않은 심각한 오류", 
                'status_message': "(1_extract_html) 처리되지 않은 심각한 오류", 
                'error_message': error_message,
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_CRITICAL_ERROR'
            }
        )
        raise Reject(f"Critical unhandled error: {e_outer}", requeue=False) 