from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
import os
import re
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin, urlparse
import hashlib
import time
from generate_cover_letter_semantic import generate_cover_letter
import uuid
from celery.exceptions import MaxRetriesExceededError, Reject
from dotenv import load_dotenv
import datetime
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from typing import Optional, Dict, Any, Union
from celery import chain, signature, states
import traceback

# 전역 로깅 레벨 및 라이브러리 로깅 레벨 조정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("cohere").setLevel(logging.WARNING)
logging.getLogger("playwright").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Celery 작업이 시작될 때 .env 파일 로드
# load_dotenv()를 호출하면 현재 작업 디렉토리 또는 상위 디렉토리에서 .env 파일을 찾아 환경 변수를 로드합니다.
# Docker 환경에서는 컨테이너 내에 .env 파일이 존재하고, Celery 워커가 실행되는 컨텍스트에서 접근 가능해야 합니다.
# 또는 Docker Compose 등을 통해 환경 변수로 직접 주입하는 것이 더 일반적입니다.
# 여기서는 .env 파일이 Celery 워커의 CWD에 있다고 가정합니다.
try:
    if load_dotenv():
        logger.info(".env file loaded successfully by dotenv.")
    else:
        # .env 파일이 없을 수도 있으므로 경고만 로깅하고 진행합니다.
        # API 키는 os.getenv를 통해 직접 환경 변수로 설정되었을 수도 있습니다.
        logger.warning(".env file not found or empty. Trusting environment variables for API keys.")
except Exception as e_dotenv:
    logger.error(f"Error loading .env file: {e_dotenv}", exc_info=True)

# 상수 정의
MAX_IFRAME_DEPTH = 2
IFRAME_LOAD_TIMEOUT = 15000
ELEMENT_HANDLE_TIMEOUT = 10000
PAGE_NAVIGATION_TIMEOUT = 120000
DEFAULT_PAGE_TIMEOUT = 45000
MAX_FILENAME_LENGTH = 100

def sanitize_filename(url_or_name: str, extension: str = "", ensure_unique: bool = False) -> str:
    """URL 또는 임의의 문자열을 기반으로 안전하고 유효한 파일 이름을 생성합니다."""
    try:
        if url_or_name.startswith(('http://', 'https://')):
            parsed_url = urlparse(url_or_name)
            name_part = parsed_url.netloc.replace('www.', '') + "_" + parsed_url.path.replace('/', '_')
        else:
            name_part = url_or_name

        name_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', name_part)
        name_part = re.sub(r'_+', '_', name_part).strip('_')

        # 확장자, 고유 해시 공간을 제외한 순수 이름 파트 길이 계산
        reserved_len = (len(extension) + 1 if extension else 0) + (8 + 1 if ensure_unique else 0) # 해시는 8자, _ 포함
        if len(name_part) > MAX_FILENAME_LENGTH - reserved_len:
            name_part = name_part[:MAX_FILENAME_LENGTH - reserved_len]
        
        if ensure_unique:
            unique_suffix = hashlib.md5(name_part.encode('utf-8')).hexdigest()[:8]
            base_name = f"{name_part}_{unique_suffix}"
        else:
            base_name = name_part

        final_name = f"{base_name}{'.' + extension if extension else ''}".lower()
        logger.debug(f"Sanitized filename for '{url_or_name}': {final_name}")
        return final_name

    except Exception as e:
        logger.error(f"Error sanitizing filename for '{url_or_name}': {e}", exc_info=True)
        timestamp = int(time.time())
        safe_ext = f".{extension}" if extension else ""
        error_name = f"error_filename_{timestamp}_{uuid.uuid4().hex[:4]}{safe_ext}"
        logger.warning(f"Returning error-fallback filename: {error_name}")
        return error_name

def _update_root_task_state(task_id: str, current_step_message: str, status: str = states.STARTED, details: Optional[Dict[str, Any]] = None, error_info: Optional[Dict[str, Any]] = None):
    """루트 작업의 상태를 업데이트하는 헬퍼 함수."""
    try:
        meta_update = {'current_step': current_step_message}
        if details:
            meta_update.update(details)
        if error_info:
            meta_update['error_details'] = error_info
        
        # status가 PENDING (기본값)이 아닌 경우에만 명시적으로 설정, 그 외에는 celery_app.backend.store_result의 기본 동작에 맡김
        # 다만, Celery의 기본 상태 외에 SUCCESS, FAILURE 등 명확한 상태를 전달해야 함.
        # STARTED는 Celery의 표준 상태 중 하나.
        current_status = status
        if status not in [states.SUCCESS, states.FAILURE, states.RETRY, states.REVOKED, states.STARTED]:
            # PROGRESS와 같은 사용자 정의 상태 대신 STARTED를 사용하고, details에 진행 상황 명시
            logger.warning(f"Invalid or custom status '{status}' provided for task {task_id}. Defaulting to STARTED or relying on details. Message: {current_step_message}")
            current_status = states.STARTED # 혹은 기존 상태를 유지하거나 다른 표준 상태로 매핑

        celery_app.backend.store_result(task_id, None, current_status, meta=meta_update)
        logger.info(f"[StateUpdate] Root task {task_id} status: {current_status}, step: '{current_step_message}', details: {meta_update.get('details', 'N/A')}, error: {error_info or 'N/A'}")
    except Exception as e_update:
        logger.error(f"[StateUpdateFailure] Failed to update root task {task_id} state: {e_update}", exc_info=True)

def _get_playwright_page_content_with_iframes_processed(page, original_url: str, chain_log_id: str, step_log_id: str) -> str:
    """Playwright 페이지에서 iframe을 처리하고 전체 HTML 컨텐츠를 반환합니다."""
    log_prefix = f"[Util / Root {chain_log_id} / Step {step_log_id} / GetPageContent]"
    logger.info(f"{log_prefix} Starting page content processing for {original_url}, including iframes.")
    
    _flatten_iframes_in_live_dom_sync(page, 0, MAX_IFRAME_DEPTH, original_url, chain_log_id, step_log_id)

    logger.info(f"{log_prefix} Attempting to get final page content after iframe processing.")
    try:
        content = page.content()
        if not content:
            logger.warning(f"{log_prefix} page.content() returned empty for {original_url}.")
            return "<!-- Page content was empty after processing -->"
        logger.info(f"{log_prefix} Successfully retrieved page content (length: {len(content)}).")
        return content
    except Exception as e_content:
        logger.error(f"{log_prefix} Error getting page content for {original_url}: {e_content}", exc_info=True)
        return f"<!-- Error retrieving page content: {str(e_content)} -->" # 상세 에러 메시지 포함

def _flatten_iframes_in_live_dom_sync(current_playwright_context, 
                                 current_depth: int,
                                 max_depth: int,
                                 original_page_url_for_logging: str,
                                 chain_log_id: str, 
                                 step_log_id: str):
    """(동기 버전) 현재 Playwright 컨텍스트 내 iframe들을 재귀적으로 평탄화합니다."""
    log_prefix = f"[Util / Root {chain_log_id} / Step {step_log_id} / FlattenIframeSync / Depth {current_depth}]"
    if current_depth > max_depth:
        logger.warning(f"{log_prefix} Max iframe depth {max_depth} reached. Stopping recursion.")
        return

    processed_iframe_count = 0
    # iframe을 찾되, 이미 처리했거나 오류가 발생한 iframe은 제외
    iframe_locators = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])')
    
    try:
        num_iframes = iframe_locators.count()
        if num_iframes == 0:
            logger.info(f"{log_prefix} No processable iframes found at this depth.")
            return
        logger.info(f"{log_prefix} Found {num_iframes} processable iframe(s) at this depth.")
    except Exception as e_count:
        logger.warning(f"{log_prefix} Error counting iframes: {e_count}. Aborting at this level.", exc_info=True)
        return

    for i in range(num_iframes):
        iframe_locator = iframe_locators.nth(i)
        iframe_handle = None
        # 각 iframe에 고유 ID 부여 시도 (로깅 및 추적 용이)
        iframe_log_id = f"iframe-gen-{uuid.uuid4().hex[:6]}" 
        
        try:
            # iframe ID 가져오기 또는 설정 (오류 발생 가능성 있음)
            try:
                existing_id = iframe_locator.get_attribute('id')
                if existing_id:
                    iframe_log_id = existing_id
                else:
                    # ID가 없다면 새로 생성하여 설정
                    iframe_locator.evaluate("(el, id) => el.id = id", iframe_log_id)
            except Exception as e_set_id:
                logger.warning(f"{log_prefix} Could not reliably set/get ID for iframe #{i+1}. Using generated: {iframe_log_id}. Error: {e_set_id}")

            logger.info(f"{log_prefix} Processing iframe #{i+1}/{num_iframes} (Effective ID: {iframe_log_id}).")
            # 현재 처리 중인 iframe임을 DOM에 표시
            iframe_locator.evaluate("el => el.setAttribute('data-cvf-processing', 'true')")

            iframe_handle = iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"{log_prefix} Null element_handle for iframe {iframe_log_id}. Marking with error and skipping.")
                iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            iframe_src_attr = iframe_handle.get_attribute('src') or "[src attribute not found]"
            logger.debug(f"{log_prefix} iframe {iframe_log_id} src attribute: {iframe_src_attr[:150]}")

            child_frame = iframe_handle.content_frame()
            if not child_frame: 
                logger.warning(f"{log_prefix} content_frame is None for iframe {iframe_log_id}. Marking with error and skipping.")
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue
            
            child_frame_url_for_log = "[child frame URL not accessible]"
            try:
                child_frame_url_for_log = child_frame.url # 로드 전에 URL 접근 시도
            except Exception:
                pass

            try:
                logger.info(f"{log_prefix} Waiting for child_frame (ID: {iframe_log_id}, URL: {child_frame_url_for_log}) to load (domcontentloaded)...")
                child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT) 
                logger.info(f"{log_prefix} Child_frame (ID: {iframe_log_id}, Final URL: {child_frame.url}) loaded.")
            except Exception as frame_load_err:
                logger.error(f"{log_prefix} Timeout or error loading child_frame {iframe_log_id} (src attr: {iframe_src_attr[:100]}, initial URL: {child_frame_url_for_log}): {frame_load_err}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            # --- 재귀 호출 ---
            _flatten_iframes_in_live_dom_sync(child_frame, current_depth + 1, max_depth, original_page_url_for_logging, chain_log_id, step_log_id)
            # --- 재귀 호출 끝 ---
            
            child_html_content = ""
            try:
                logger.debug(f"{log_prefix} Getting content from child_frame {iframe_log_id} post-recursion.")
                child_html_content = child_frame.content() # 여기서도 타임아웃 또는 오류 발생 가능
                if not child_html_content: 
                    child_html_content = f"<!-- iframe {iframe_log_id} (src: {iframe_src_attr[:100]}) content was empty post-recursion -->"
            except Exception as frame_content_err:
                logger.error(f"{log_prefix} Error getting content from child_frame {iframe_log_id} (src: {iframe_src_attr[:100]}): {frame_content_err}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue # 이 iframe 처리는 실패로 간주하고 다음으로

            # iframe 내용을 div로 감싸서 교체할 HTML 생성
            replacement_div_html = ""
            try:
                soup = BeautifulSoup(child_html_content, 'html.parser')
                # body가 있으면 body 내부, 없으면 전체 HTML을 사용
                content_to_insert = soup.body if soup.body else soup 
                inner_html_str = content_to_insert.decode_contents() if content_to_insert else f"<!-- Parsed content of {iframe_log_id} was empty -->"
                
                # XSS 방지를 위해 src 속성 등을 적절히 이스케이프하거나 제한된 정보만 포함하는 것이 좋으나, 여기서는 원본 추적용으로만 사용
                safe_original_src = (iframe_src_attr[:250] + '...') if len(iframe_src_attr) > 250 else iframe_src_attr
                
                replacement_div_html = (
                    f'<div class="cvf-iframe-content-wrapper" '
                    f'data-cvf-original-src="{safe_original_src}" '
                    f'data-cvf-iframe-depth="{current_depth + 1}" '
                    f'data-cvf-iframe-id="{iframe_log_id}">'
                    f'{inner_html_str}'
                    f'</div>'
                )
            except Exception as bs_err:
                logger.error(f"{log_prefix} Error parsing child frame {iframe_log_id} with BeautifulSoup: {bs_err}", exc_info=True)
                safe_original_src = (iframe_src_attr[:250] + '...') if len(iframe_src_attr) > 250 else iframe_src_attr
                replacement_div_html = (
                    f'<div class="cvf-iframe-content-wrapper cvf-parse-error" '
                    f'data-cvf-original-src="{safe_original_src}" '
                    f'data-cvf-iframe-id="{iframe_log_id}">'
                    f'<!-- Error parsing content of iframe {iframe_log_id}. Original content snippet: {child_html_content[:200]}... -->'
                    f'</div>'
                )
            
            # iframe을 생성된 div HTML로 교체
            try:
                logger.info(f"{log_prefix} Attempting to replace iframe {iframe_log_id} with its content.")
                iframe_handle.evaluate("(el, html) => { el.outerHTML = html; }", replacement_div_html)
                logger.info(f"{log_prefix} Successfully replaced iframe {iframe_log_id} with div wrapper.")
                processed_iframe_count += 1
            except Exception as eval_replace_err: 
                logger.error(f"{log_prefix} Failed to replace iframe {iframe_log_id} using evaluate: {eval_replace_err}", exc_info=True)
                # 교체 실패 시, 원본 iframe에 에러 상태만 마킹 (이미 data-cvf-processing 상태일 것임)
                # 이 시점에서 iframe_handle이 여전히 유효한지 확인 필요. 이미 DOM에서 제거되었을 수도.
                # locator를 사용해 다시 찾아 마킹 시도
                if current_playwright_context.locator(f'iframe[id="{iframe_log_id}"][data-cvf-processing="true"]').count() == 1:
                    current_playwright_context.locator(f'iframe[id="{iframe_log_id}"]').evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                else:
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} not found for error marking after replacement failure.")

        except Exception as e_outer_iframe_loop: 
            # 이 블록은 개별 iframe 처리의 전체 과정을 감싸는 try문의 except
            logger.error(f"{log_prefix} General error processing iframe {iframe_log_id} (loop iteration #{i+1}): {e_outer_iframe_loop}", exc_info=True)
            # iframe_handle이 존재하고, 아직 처리 중(data-cvf-processing)이라면 에러 마킹 시도
            if iframe_handle: 
                try: 
                    # 핸들이 유효하다면 직접 사용
                    if not iframe_handle.is_hidden(): # DOM에 아직 존재하는지 확인 (최선은 아님)
                         iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except Exception as e_final_mark_err:
                    logger.warning(f"{log_prefix} Error during final attempt to mark iframe {iframe_log_id} as error in outer_iframe_loop: {e_final_mark_err}")
            # 다음 iframe 처리를 위해 continue
            continue 
        finally:
            # 각 iframe 처리 후 핸들 정리
            if iframe_handle:
                try: 
                    iframe_handle.dispose()
                except Exception as e_dispose: 
                    logger.warning(f"{log_prefix} Error disposing element_handle for iframe {iframe_log_id}: {e_dispose}", exc_info=True)
            # 처리 중이던 상태(data-cvf-processing)가 남아있다면, 완료 또는 에러 상태로 변경해야 함.
            # locator로 다시 찾아, 만약 아직 processing 상태라면 error로 변경 (최후의 수단)
            # 단, 성공적으로 교체된 경우는 locator로 찾을 수 없음.
            try:
                remaining_iframe_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"][data-cvf-processing="true"]')
                if remaining_iframe_locator.count() == 1:
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} was left in 'processing' state. Marking as error.")
                    remaining_iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
            except Exception as e_final_cleanup_locator:
                 logger.warning(f"{log_prefix} Error during final cleanup check for iframe {iframe_log_id}: {e_final_cleanup_locator}")


    logger.info(f"{log_prefix} Finished all iframe processing at depth {current_depth}. Total iframes found at this depth: {num_iframes}. Successfully processed/replaced: {processed_iframe_count}.")


@celery_app.task(bind=True, name='celery_tasks.step_1_extract_html', max_retries=1, default_retry_delay=10)
def step_1_extract_html(self, url: str, chain_log_id: str) -> Dict[str, str]:
    """(1단계) URL에서 HTML을 추출하고 iframe을 평탄화하여 파일에 저장합니다."""
    task_id = self.request.id
    step_log_id = "1_extract_html"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    logger.info(f"{log_prefix} Starting: Extract HTML from URL: {url}")
    _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 본문 추출 시작", details={'url': url, 'current_task_id': task_id})

    saved_file_path = None
    browser = None
            page = None
    context = None

    try:
        with sync_playwright() as p_instance:
            try:
                logger.info(f"{log_prefix} Launching Chromium browser.")
                browser_args = [
                    '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', 
                    '--disable-gpu', '--blink-settings=imagesEnabled=false', '--disable-extensions',
                    '--disable-features=IsolateOrigins,site-per-process,BlockInsecurePrivateNetworkRequests',
                    '--enable-features=NetworkService,NetworkServiceInProcess' 
                ]
                # Docker 환경 변수 IN_DOCKER_CONTAINER는 Dockerfile에서 설정했다고 가정
                # in_docker = os.getenv("IN_DOCKER_CONTAINER", "false").lower() == "true"
                # logger.info(f"{log_prefix} Docker environment detected: {in_docker}") 
                
                browser = p_instance.chromium.launch(headless=True, args=browser_args)
                logger.info(f"{log_prefix} Chromium browser launched (v: {browser.version}).")

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    java_script_enabled=True,
                    bypass_csp=True,
                    ignore_https_errors=True,
                )
                context.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT)
                context.set_default_timeout(DEFAULT_PAGE_TIMEOUT)
                page = context.new_page()
                logger.info(f"{log_prefix} Browser context and page created.")

            except Exception as browser_launch_error:
                logger.error(f"{log_prefix} Failed to launch browser or create context/page: {browser_launch_error}", exc_info=True)
                _update_root_task_state(chain_log_id, f"({step_log_id}) 브라우저 실행 실패", status=states.FAILURE, error_info={'error': str(browser_launch_error), 'details': 'Browser launch/setup failed'})
                raise # 태스크 실패 처리

            try:
                logger.info(f"{log_prefix} Navigating to: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_NAVIGATION_TIMEOUT)
                logger.info(f"{log_prefix} Navigation to {url} successful. Current URL: {page.url}")
                _update_root_task_state(chain_log_id, f"({step_log_id}) 페이지 로드 완료, iframe 처리 시작")
            except Exception as navigation_error:
                logger.error(f"{log_prefix} Navigation failed for {url}: {navigation_error}", exc_info=True)
                _update_root_task_state(chain_log_id, f"({step_log_id}) 페이지 네비게이션 실패", status=states.FAILURE, error_info={'error': str(navigation_error), 'url': url})
                raise

            try:
                logger.info(f"{log_prefix} Waiting for network idle (briefly)...")
                page.wait_for_load_state('networkidle', timeout=15000)
                logger.info(f"{log_prefix} Network idle or timeout reached.")
            except Exception as network_idle_err:
                logger.warning(f"{log_prefix} Networkidle wait error/timeout: {network_idle_err}. Proceeding.")
            
            logger.info(f"{log_prefix} Starting HTML content extraction with iframe processing.")
            html_content = _get_playwright_page_content_with_iframes_processed(page, url, chain_log_id, step_log_id)

            if not html_content or html_content.startswith("<!-- Error") or "Page content was empty" in html_content:
                err_msg = f"Invalid HTML content after iframe processing from {url}. Snippet: {html_content[:200]}"
                logger.error(f"{log_prefix} {err_msg}")
                _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 컨텐츠 추출 실패 (내용부실)", status=states.FAILURE, error_info={'error': err_msg, 'url': url})
                raise ValueError(err_msg)

            logger.info(f"{log_prefix} HTML content extracted (length: {len(html_content)}).")

            logs_dir = "logs"
            os.makedirs(logs_dir, exist_ok=True)
            
            base_fn_prefix = sanitize_filename(url) # URL 기반으로 일단 생성
            # chain_log_id의 일부를 추가하여 고유성 증대 및 추적 용이
            unique_html_fn = sanitize_filename(f"{base_fn_prefix}_raw_html_{chain_log_id[:8]}", "html", ensure_unique=True)
            saved_file_path = os.path.join(logs_dir, unique_html_fn)
            
            try:
                with open(saved_file_path, "w", encoding="utf-8") as f:
                    f.write(html_content)
                logger.info(f"{log_prefix} HTML content saved to: {saved_file_path}")
                _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 파일 저장 완료", details={'html_file_path': saved_file_path})
                # 성공적으로 저장했으므로 결과를 반환합니다.
                return {"html_file_path": saved_file_path, "original_url": url}

            except IOError as e_io_save:
                logger.error(f"{log_prefix} Failed to save HTML to {saved_file_path}: {e_io_save}", exc_info=True)
                _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 파일 저장 실패", status=states.FAILURE, error_info={'error': str(e_io_save), 'file_path': saved_file_path})
                raise # IOError 발생 시 현재 태스크를 실패 처리

    except Exception as e: # Playwright 블록 내의 모든 예외 포함
        logger.error(f"{log_prefix} Unhandled error in HTML extraction: {e}", exc_info=True)
        # 이미 상태 업데이트가 되었을 수 있지만, 최종적으로 실패 상태 보장
        err_details = {'error': str(e), 'type': type(e).__name__, 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 추출 중 심각한 오류", status=states.FAILURE, error_info=err_details)
        raise # Celery가 태스크를 실패로 처리하도록 함

    finally:
        if page: 
            try: page.close()
            except Exception: pass
        if context: 
            try: context.close()
            except Exception: pass
        if browser: 
            try: browser.close()
            except Exception: pass
        logger.info(f"{log_prefix} Playwright resources cleaned up.")

@celery_app.task(bind=True, name='celery_tasks.step_2_extract_text', max_retries=1, default_retry_delay=5)
def step_2_extract_text(self, prev_result: Dict[str, str], chain_log_id: str) -> Dict[str, str]:
    """(2단계) 저장된 HTML 파일에서 텍스트를 추출하여 새 파일에 저장합니다."""
    task_id = self.request.id
    step_log_id = "2_extract_text"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    
    html_file_path = prev_result.get("html_file_path")
    original_url = prev_result.get("original_url", "N/A")

    if not html_file_path or not os.path.exists(html_file_path):
        error_msg = f"HTML file not found or path invalid: {html_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 입력 HTML 파일 없음", status=states.FAILURE, error_info={'error': error_msg, 'path_checked': html_file_path})
        raise ValueError(error_msg)

    logger.info(f"{log_prefix} Starting text extraction from: {html_file_path}")
    _update_root_task_state(chain_log_id, f"({step_log_id}) HTML에서 텍스트 추출 시작", details={'html_file_path': html_file_path, 'current_task_id': task_id})

    extracted_text_file_path = None
    try:
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, "html.parser")
        for el in soup.find_all(string=lambda text: isinstance(text, Comment)):
            el.extract()
        for el in soup(["script", "style", "noscript", "link", "meta", "header", "footer", "nav", "aside"]):
            el.decompose()
        
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r'[\s\xa0]+', ' ', text) # NBSP 포함 모든 공백류를 단일 공백으로
        text = re.sub(r' (\n)+', '\n', text) # 공백 후 개행은 개행만
        text = re.sub(r'(\n)+ ', '\n', text) # 개행 후 공백은 개행만
        text = re.sub(r'(\n){2,}', '\n\n', text) # 2회 이상 연속 개행은 2회로
        text = text.strip()

        if not text:
            logger.warning(f"{log_prefix} No text extracted from {html_file_path}. File will be empty.")
        
        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        base_html_fn = os.path.splitext(os.path.basename(html_file_path))[0]
        # _raw_html_xxxxxxx 부분 제거 시도
        base_html_fn = re.sub(r'_raw_html_[a-f0-9]{8}$', '', base_html_fn)
        unique_text_fn = sanitize_filename(f"{base_html_fn}_extracted_text", "txt", ensure_unique=True)
        extracted_text_file_path = os.path.join(logs_dir, unique_text_fn)

        with open(extracted_text_file_path, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"{log_prefix} Text extracted to: {extracted_text_file_path} (Length: {len(text)})")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 파일 저장 완료", details={'text_file_path': extracted_text_file_path})
        
        # 다음 단계로 필요한 정보만 전달
        return {"text_file_path": extracted_text_file_path, 
                "original_url": original_url, 
                "html_file_path": html_file_path # 로깅/추적용으로 유지
               }

    except Exception as e:
        logger.error(f"{log_prefix} Error extracting text from {html_file_path}: {e}", exc_info=True)
        if extracted_text_file_path and os.path.exists(extracted_text_file_path):
            try: os.remove(extracted_text_file_path) # 실패 시 생성된 파일 삭제
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial text file {extracted_text_file_path}: {e_remove}")
        
        err_details = {'error': str(e), 'type': type(e).__name__, 'html_file': html_file_path, 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 추출 실패", status=states.FAILURE, error_info=err_details)
        raise

@celery_app.task(bind=True, name='celery_tasks.step_3_filter_content', max_retries=1, default_retry_delay=15)
def step_3_filter_content(self, prev_result: Dict[str, str], chain_log_id: str) -> Dict[str, str]:
    """(3단계) 추출된 텍스트를 LLM으로 필터링하고 새 파일에 저장합니다."""
    task_id = self.request.id
    step_log_id = "3_filter_content"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    
    raw_text_file_path = prev_result.get("text_file_path")
    original_url = prev_result.get("original_url", "N/A")
    html_file_path = prev_result.get("html_file_path") # 로깅/추적용

    if not raw_text_file_path or not os.path.exists(raw_text_file_path):
        error_msg = f"Text file not found or path invalid: {raw_text_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 입력 텍스트 파일 없음", status=states.FAILURE, error_info={'error': error_msg, 'path_checked': raw_text_file_path})
        raise ValueError(error_msg)

    logger.info(f"{log_prefix} Starting LLM filtering for: {raw_text_file_path}")
    _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 채용공고 필터링 시작", details={'raw_text_file_path': raw_text_file_path, 'current_task_id': task_id})

    filtered_text_file_path = None
    try:
        with open(raw_text_file_path, "r", encoding="utf-8") as f:
            raw_text = f.read()

        if not raw_text.strip():
            logger.warning(f"{log_prefix} Text file {raw_text_file_path} is empty. Saving as empty filtered file.")
            filtered_content = "<!-- 원본 텍스트 내용 없음 -->"
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error(f"{log_prefix} GROQ_API_KEY not set.")
                _update_root_task_state(chain_log_id, f"({step_log_id}) API 키 없음 (GROQ_API_KEY)", status=states.FAILURE, error_info={'error': 'GROQ_API_KEY not set'})
                raise ValueError("GROQ_API_KEY is not configured.")

            llm_model = os.getenv("GROQ_LLM_MODEL", "llama3-70b-8192") 
            logger.info(f"{log_prefix} Using LLM: {llm_model} via Groq.")
            
            chat = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=llm_model)
            
            sys_prompt = ("You are an expert text processing assistant. Your task is to extract ONLY the core job description from the provided text. "
                          "Remove all extraneous information such as advertisements, company promotions, navigation links, sidebars, headers, footers, legal disclaimers, cookie notices, unrelated articles, and anything not directly related to the job's responsibilities, qualifications, and benefits. "
                          "Present the output as clean, readable plain text. Do NOT use markdown formatting. Focus on the actual job content. "
                          "If the text does not appear to be a job posting, or if it is too corrupted to extract meaningful job information, respond with the exact phrase '추출할 채용공고 내용 없음' and nothing else.")
            human_template = "{text_content}"
            prompt = ChatPromptTemplate.from_messages([("system", sys_prompt), ("human", human_template)])
            parser = StrOutputParser()
            llm_chain = prompt | chat | parser

            logger.info(f"{log_prefix} Invoking LLM. Text length: {len(raw_text)}")
            MAX_LLM_INPUT_LEN = 24000 
            text_for_llm = raw_text
            if len(raw_text) > MAX_LLM_INPUT_LEN:
                logger.warning(f"{log_prefix} Text length ({len(raw_text)}) > limit ({MAX_LLM_INPUT_LEN}). Truncating.")
                text_for_llm = raw_text[:MAX_LLM_INPUT_LEN]
                _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 입력 텍스트 일부 사용 (길이 초과)", 
                                        details={'original_len': len(raw_text), 'truncated_len': len(text_for_llm)})
            
            filtered_content = llm_chain.invoke({"text_content": text_for_llm})
            logger.info(f"{log_prefix} LLM filtering complete. Output length: {len(filtered_content)}")

            if filtered_content.strip() == "추출할 채용공고 내용 없음":
                logger.warning(f"{log_prefix} LLM reported no extractable job content.")
                filtered_content = "<!-- LLM 분석: 추출할 채용공고 내용 없음 -->"

        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        base_text_fn = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")
        unique_filtered_fn = sanitize_filename(f"{base_text_fn}_filtered_text", "txt", ensure_unique=True)
        filtered_text_file_path = os.path.join(logs_dir, unique_filtered_fn)

        with open(filtered_text_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)
        logger.info(f"{log_prefix} Filtered text saved to: {filtered_text_file_path}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 필터링된 텍스트 파일 저장 완료", details={'filtered_text_file_path': filtered_text_file_path})

        return {"filtered_text_file_path": filtered_text_file_path, 
                "original_url": original_url, 
                "html_file_path": html_file_path, # 로깅/추적용
                "raw_text_file_path": raw_text_file_path # 로깅/추적용
               }

    except Exception as e:
        logger.error(f"{log_prefix} Error filtering with LLM: {e}", exc_info=True)
        if filtered_text_file_path and os.path.exists(filtered_text_file_path):
            try: os.remove(filtered_text_file_path)
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial filtered file {filtered_text_file_path}: {e_remove}")

        err_details = {'error': str(e), 'type': type(e).__name__, 'raw_text_file': raw_text_file_path, 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 필터링 실패", status=states.FAILURE, error_info=err_details)
        raise

@celery_app.task(bind=True, name='celery_tasks.step_4_generate_cover_letter', max_retries=1, default_retry_delay=20)
def step_4_generate_cover_letter(self, prev_result: Dict[str, Any], chain_log_id: str, user_prompt_text: Optional[str]) -> Dict[str, Any]:
    """(4단계) 필터링된 텍스트와 사용자 프롬프트를 사용하여 자기소개서를 생성하고 최종 결과를 반환합니다."""
    task_id = self.request.id
    step_log_id = "4_generate_cover_letter"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"

    filtered_text_file_path = prev_result.get("filtered_text_file_path")
    original_url = prev_result.get("original_url", "N/A")
    html_file_path = prev_result.get("html_file_path")
    raw_text_file_path = prev_result.get("raw_text_file_path")

    if not filtered_text_file_path or not os.path.exists(filtered_text_file_path):
        error_msg = f"Filtered text file not found or path invalid: {filtered_text_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 입력 필터링된 텍스트 파일 없음", status=states.FAILURE, error_info={'error': error_msg, 'path_checked': filtered_text_file_path})
        # 이 단계에서 실패하면 파이프라인 실패임
        raise ValueError(error_msg) 

    logger.info(f"{log_prefix} Starting cover letter generation. Filtered file: {filtered_text_file_path}, User prompt: {'Yes' if user_prompt_text else 'No'}")
    _update_root_task_state(chain_log_id, f"({step_log_id}) 자기소개서 생성 시작", 
                            details={'filtered_file': filtered_text_file_path, 'user_prompt': bool(user_prompt_text), 'current_task_id': task_id})
    
    cover_letter_file_path_final = None # 최종 자소서 파일 경로
    try:
        with open(filtered_text_file_path, "r", encoding="utf-8") as f:
            filtered_job_text = f.read()

        if not filtered_job_text.strip() or \
           filtered_job_text.strip().startswith("<!-- LLM 분석:") or \
           filtered_job_text.strip().startswith("<!-- 원본 텍스트 내용 없음 -->"):
            
            warning_msg = f"Filtered text empty or placeholder: '{filtered_job_text[:100]}'. Cover letter cannot be generated."
            logger.warning(f"{log_prefix} {warning_msg}")
            # 이것은 파이프라인 실패가 아니라, 내용이 없어 자소서 생성을 못한 경우이므로 SUCCESS로 처리
            final_pipeline_result = {
                "status": "NO_CONTENT_FOR_COVER_LETTER",
                "message": warning_msg,
                "cover_letter": "",
                "cover_letter_file_path": None,
                "original_url": original_url,
                "intermediate_files": {
                    "html": html_file_path,
                    "raw_text": raw_text_file_path,
                    "filtered_text": filtered_text_file_path
                }
            }
            _update_root_task_state(chain_log_id, "파이프라인 완료 (자소서 생성 불가: 내용 부족)", status=states.SUCCESS, details=final_pipeline_result)
            return final_pipeline_result
        
        logger.info(f"{log_prefix} Calling LLM for cover letter. Text length: {len(filtered_job_text)}, Prompt length: {len(user_prompt_text or '')}")
        
        # generate_cover_letter_semantic.py의 generate_cover_letter 함수가 dict를 반환한다고 가정
        # 예: {"cover_letter": "...", "model_name": "..."}
        llm_cv_data = generate_cover_letter(
            job_posting_text=filtered_job_text, 
            user_prompt=user_prompt_text
        )

        generated_cv_text = llm_cv_data.get("cover_letter")
        if not generated_cv_text or not generated_cv_text.strip():
            error_msg = "LLM generated empty cover letter."
            logger.error(f"{log_prefix} {error_msg} LLM output: {llm_cv_data}")
            _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 자소서 생성 결과 없음", status=states.FAILURE, 
                                    error_info={'error': error_msg, 'llm_output_details': llm_cv_data})
            raise ValueError(error_msg) # 파이프라인 실패 처리
        
        logger.info(f"{log_prefix} Cover letter generated (length: {len(generated_cv_text)}). Used model: {llm_cv_data.get('model_name', 'N/A')}")

        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        base_cv_fn = os.path.splitext(os.path.basename(filtered_text_file_path))[0].replace("_filtered_text","")
        unique_cv_fn = sanitize_filename(f"{base_cv_fn}_cover_letter_{chain_log_id[:8]}", "txt", ensure_unique=True)
        cover_letter_file_path_final = os.path.join(logs_dir, unique_cv_fn)

        with open(cover_letter_file_path_final, "w", encoding="utf-8") as f:
            f.write(generated_cv_text)
        logger.info(f"{log_prefix} Cover letter saved to: {cover_letter_file_path_final}")

        final_pipeline_result = {
            "status": "SUCCESS",
            "message": "Cover letter generated and saved successfully.",
            "cover_letter_file_path": cover_letter_file_path_final,
            "cover_letter_preview": generated_cv_text[:500] + ("..." if len(generated_cv_text) > 500 else ""),
            "original_url": original_url,
            "llm_model_used_for_cv": llm_cv_data.get("model_name", "N/A"),
            "intermediate_files": {
                "html": html_file_path,
                "raw_text": raw_text_file_path,
                "filtered_text": filtered_text_file_path
            }
        }
        _update_root_task_state(chain_log_id, "파이프라인 성공적으로 완료", status=states.SUCCESS, details=final_pipeline_result)
        return final_pipeline_result # 이것이 체인의 최종 결과가 됨
        
    except Exception as e:
        logger.error(f"{log_prefix} Error in cover letter generation: {e}", exc_info=True)
        if cover_letter_file_path_final and os.path.exists(cover_letter_file_path_final):
            try: os.remove(cover_letter_file_path_final)
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial CV file {cover_letter_file_path_final}: {e_remove}")
        
        err_details = {'error': str(e), 'type': type(e).__name__, 'filtered_file': filtered_text_file_path, 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) 자소서 생성 실패", status=states.FAILURE, error_info=err_details)
        raise # 파이프라인 실패

@celery_app.task(bind=True, name='celery_tasks.process_job_posting_pipeline')
def process_job_posting_pipeline(self, url: str, user_prompt: Optional[str] = None) -> str:
    """전체 채용공고 처리 파이프라인: HTML 추출 -> 텍스트 추출 -> 내용 필터링 -> 자기소개서 생성"""
    root_task_id = self.request.id # 이 ID가 chain_log_id로 사용됨
    log_prefix = f"[Pipeline / Root {root_task_id}]"
    logger.info(f"{log_prefix} Initiating pipeline for URL: {url}, User Prompt: {'Provided' if user_prompt else 'N/A'}")

    _update_root_task_state(root_task_id, "파이프라인 시작됨", status=states.STARTED, 
                            details={'url': url, 'user_prompt_provided': bool(user_prompt)})

    # Celery 체인 정의 (가장 일반적이고 이해하기 쉬운 형태):
    processing_chain_final = chain(
        step_1_extract_html.s(url=url, chain_log_id=root_task_id),
        step_2_extract_text.s(chain_log_id=root_task_id),
        step_3_filter_content.s(chain_log_id=root_task_id),
        step_4_generate_cover_letter.s(chain_log_id=root_task_id, user_prompt_text=user_prompt) # user_prompt_text는 명시적 kwargs로 전달
    )

    logger.info(f"{log_prefix} Celery chain created: {processing_chain_final}")
    
    try:
        # 체인 실행. 체인 자체에 대한 ID는 Celery가 자동 생성 또는 apply_async에서 지정 가능.
        # 여기서 중요한 것은 root_task_id로 전체 파이프라인 상태를 추적하는 것.
        chain_async_result = processing_chain_final.apply_async()
        
        logger.info(f"{log_prefix} Dispatched chain. Last task ID in chain: {chain_async_result.id}. Polling root ID: {root_task_id}")
        _update_root_task_state(root_task_id, "파이프라인 작업들 실행 중", 
                                details={'chain_last_task_id': chain_async_result.id, 'status_polling_id': root_task_id})
        
        # 이 태스크는 체인을 시작시키고, 클라이언트가 폴링할 루트 작업 ID를 반환.
        return root_task_id
        
    except Exception as e_chain_dispatch:
        logger.error(f"{log_prefix} Failed to dispatch chain: {e_chain_dispatch}", exc_info=True)
        err_details = {'error': str(e_chain_dispatch), 'type': type(e_chain_dispatch).__name__, 'traceback': traceback.format_exc()}
        _update_root_task_state(root_task_id, "파이프라인 실행 실패 (요청단계)", status=states.FAILURE, error_info=err_details)
        raise # FastAPI가 500 에러 반환하도록 함