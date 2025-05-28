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
from playwright.sync_api import Error as PlaywrightError

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
IFRAME_LOAD_TIMEOUT = 30000
ELEMENT_HANDLE_TIMEOUT = 20000
PAGE_NAVIGATION_TIMEOUT = 120000
DEFAULT_PAGE_TIMEOUT = 45000
MAX_FILENAME_LENGTH = 100
# 새로 추가된 타임아웃 상수
LOCATOR_DEFAULT_TIMEOUT = 5000 
GET_ATTRIBUTE_TIMEOUT = 10000
EVALUATE_TIMEOUT_SHORT = 10000

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
        
        # meta_update 내용을 로깅
        logger.info(f"[StateUpdateMeta] Root task {task_id} - meta_update content before storing: {meta_update}")

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
    
    # 루프 시작 전에 처리해야 할 iframe의 초기 개수를 로깅 (선택적)
    try:
        initial_iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])')
        initial_count = initial_iframe_locator.count()
        logger.info(f"{log_prefix} Initial check: Found {initial_count} processable iframe(s) at this depth.")
        if initial_count == 0:
            logger.info(f"{log_prefix} No processable iframes found at this depth based on initial check.")
            return
    except Exception as e_initial_count:
        logger.warning(f"{log_prefix} Error during initial iframe count: {e_initial_count}. Proceeding with loop.", exc_info=True)
        # 계속 진행하되, 루프 조건이 이를 처리할 것임

    loop_iteration_count = 0
    max_loop_iterations = initial_count + 5 # 무한 루프 방지를 위한 안전장치 (초기 카운트보다 약간 더 많이)

    while loop_iteration_count < max_loop_iterations:
        loop_iteration_count += 1
        iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])').first
        
        try:
            # 처리할 iframe이 더 이상 없는지 확인 (first를 사용하므로 count()로 확인)
            # locator.first 접근 시 요소가 없으면 TimeoutError가 발생할 수 있으므로, count()로 먼저 확인하는 것이 더 안전할 수 있음.
            # 그러나 .first를 직접 사용하고 예외를 잡는 것도 Playwright의 일반적인 패턴임.
            # 여기서는 .count() > 0 조건으로 명시적으로 확인
            if iframe_locator.count() == 0: # 짧은 타임아웃으로 존재 여부 확인
                logger.info(f"{log_prefix} No more processable iframes found. Exiting loop after {loop_iteration_count-1} iterations.")
                break
        except PlaywrightError as e_no_more_iframes:
            logger.info(f"{log_prefix} No more processable iframes found (locator.first likely timed out or element disappeared). Exiting loop. Error: {e_no_more_iframes}")
            break # 처리할 iframe이 없으면 루프 종료
        except Exception as e_count_check_unexpected:
            logger.error(f"{log_prefix} Unexpected error checking for remaining iframes: {e_count_check_unexpected}. Exiting loop.", exc_info=True)
            break


        iframe_handle = None
        iframe_log_id = f"iframe-gen-{uuid.uuid4().hex[:6]}" 
        
        try:
            # iframe ID 가져오기 또는 설정 (오류 발생 가능성 있음)
            # element_handle을 먼저 얻고, 그 다음에 ID를 가져오거나 설정하는 것이 더 안정적일 수 있음.
            # 그러나 locator API를 사용하는 것이 권장됨.
            # 타임아웃을 명시적으로 설정하여 무한 대기 방지
            logger.debug(f"{log_prefix} Attempting to get/set ID for the first found iframe.")
            try:
                # ID 가져오기 시도, 타임아웃 설정
                existing_id = iframe_locator.get_attribute('id', timeout=GET_ATTRIBUTE_TIMEOUT)
                if existing_id:
                    iframe_log_id = existing_id
                else:
                    # ID가 없다면 새로 생성하여 설정, 타임아웃 설정
                    iframe_locator.evaluate("(el, id) => el.id = id", iframe_log_id, timeout=EVALUATE_TIMEOUT_SHORT)
            except PlaywrightError as e_id_timeout: # Playwright의 TimeoutError 또는 기타 에러
                 logger.warning(f"{log_prefix} Timeout or PlaywrightError getting/setting ID for an iframe (iteration {loop_iteration_count}). Using generated: {iframe_log_id}. Error: {e_id_timeout}")
            except Exception as e_set_id: # 기타 예외
                logger.warning(f"{log_prefix} Could not reliably set/get ID for an iframe (iteration {loop_iteration_count}). Using generated: {iframe_log_id}. Error: {e_set_id}")

            logger.info(f"{log_prefix} Processing iframe (loop iteration #{loop_iteration_count}, Effective ID: {iframe_log_id}).")
            
            # 현재 처리 중인 iframe임을 DOM에 표시 (타임아웃 설정)
            iframe_locator.evaluate("el => el.setAttribute('data-cvf-processing', 'true')", timeout=EVALUATE_TIMEOUT_SHORT)

            iframe_handle = iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"{log_prefix} Null element_handle for iframe {iframe_log_id}. Marking with error and skipping.")
                # 핸들을 못 얻었으므로 locator로 에러 마킹 시도
                iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                continue

            iframe_src_attr = "[src attribute not found or error]"
            try:
                # iframe_handle.get_attribute에는 timeout 인수가 없습니다.
                iframe_src_attr = iframe_handle.get_attribute('src') or "[src attribute not found]"
            except Exception as e_get_src:
                logger.warning(f"{log_prefix} Error getting src attribute for iframe {iframe_log_id}: {e_get_src}")
            
            logger.debug(f"{log_prefix} iframe {iframe_log_id} src attribute: {iframe_src_attr[:150]}")

            child_frame = None
            try:
                child_frame = iframe_handle.content_frame() # 이 호출은 타임아웃을 직접 받지 않지만, 핸들이 유효하면 빠르게 반환됨
            except Exception as e_content_frame:
                logger.error(f"{log_prefix} Error getting content_frame for iframe {iframe_log_id}: {e_content_frame}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
                continue

            if not child_frame: 
                logger.warning(f"{log_prefix} content_frame is None for iframe {iframe_log_id}. Marking with error and skipping.")
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
                continue
            
            child_frame_url_for_log = "[child frame URL not accessible]"
            try:
                child_frame_url_for_log = child_frame.url # 로드 전에 URL 접근 시도
            except Exception: # PlaywrightError 등 발생 가능
                pass

            try:
                logger.info(f"{log_prefix} Waiting for child_frame (ID: {iframe_log_id}, URL: {child_frame_url_for_log}) to load (domcontentloaded)...")
                child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT) 
                # 로드 후 URL 다시 로깅 (리다이렉션 등 확인)
                final_child_frame_url = "[child frame final URL not accessible]"
                try:
                    final_child_frame_url = child_frame.url
                except Exception:
                    pass
                logger.info(f"{log_prefix} Child_frame (ID: {iframe_log_id}, Final URL: {final_child_frame_url}) loaded.")
            except PlaywrightError as frame_load_ple: # Playwright TimeoutError 등
                logger.error(f"{log_prefix} PlaywrightError (Timeout or other) loading child_frame {iframe_log_id} (src attr: {iframe_src_attr[:100]}, initial URL: {child_frame_url_for_log}): {frame_load_ple}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
                continue
            except Exception as frame_load_err: # 기타 예외
                logger.error(f"{log_prefix} Generic error loading child_frame {iframe_log_id} (src attr: {iframe_src_attr[:100]}, initial URL: {child_frame_url_for_log}): {frame_load_err}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
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
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
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
                # iframe_handle이 유효하고 evaluate 메소드가 있는지, 그리고 DOM에 연결되어 있는지 확인
                is_connected_js = False
                if iframe_handle and hasattr(iframe_handle, 'evaluate'):
                    try:
                        is_connected_js = iframe_handle.evaluate('el => el.isConnected') # JSHandle.evaluate에는 timeout 없음
                    except Exception as e_eval_isconnected:
                        logger.warning(f"{log_prefix} Error evaluating 'el.isConnected' for iframe {iframe_log_id}: {e_eval_isconnected}")
                        is_connected_js = False # 평가 중 오류 발생 시 연결되지 않은 것으로 간주

                if is_connected_js:
                    iframe_handle.evaluate("(el, html) => { el.outerHTML = html; }", replacement_div_html) # JSHandle.evaluate에는 timeout 없음
                    logger.info(f"{log_prefix} Successfully replaced iframe {iframe_log_id} with div wrapper.")
                    processed_iframe_count += 1
                else:
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} is not connected or evaluate failed. Skipping replacement.")
                    # 이미 연결이 끊겼으므로 에러 마킹도 어려울 수 있음
            except PlaywrightError as ple: # Playwright 관련 에러를 명시적으로 처리
                if "NoModificationAllowedError" in str(ple) or "no parent node" in str(ple):
                    logger.warning(f"{log_prefix} Failed to replace iframe {iframe_log_id} due to NoModificationAllowedError (element likely detached): {ple}")
                else:
                    logger.error(f"{log_prefix} Failed to replace iframe {iframe_log_id} using evaluate (Playwright Error): {ple}", exc_info=True)
                # Playwright 에러 발생 시, 해당 iframe에 에러 마킹 시도 (최선)
                # 단, 이 시점에도 요소가 없을 수 있음
                try:
                    # locator로 다시 찾아, data-cvf-processing 상태가 아니어야 함 (이미 교체 시도 후)
                    # 아직 id로 찾을 수 있고, 에러가 마킹 안되었다면 시도
                    target_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"]:not([data-cvf-error="true"])')
                    if target_locator.count() == 1: # count에는 timeout 불필요
                         target_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                    else:
                        logger.warning(f"{log_prefix} iframe {iframe_log_id} not found or already marked for error after Playwright replacement failure.")
                except Exception as e_mark:
                    logger.warning(f"{log_prefix} Exception while trying to mark iframe {iframe_log_id} as error after replacement failure: {e_mark}")
            except Exception as eval_replace_err: 
                logger.error(f"{log_prefix} Generic failed to replace iframe {iframe_log_id} using evaluate: {eval_replace_err}", exc_info=True)
                # 일반 예외에 대한 에러 마킹 (위와 유사하게)
                try:
                    target_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"]:not([data-cvf-error="true"])')
                    if target_locator.count() == 1: # count에는 timeout 불필요
                         target_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                    else:
                        logger.warning(f"{log_prefix} iframe {iframe_log_id} not found or already marked for error after generic replacement failure.")
                except Exception as e_mark_generic:
                    logger.warning(f"{log_prefix} Exception while trying to mark iframe {iframe_log_id} as error after generic replacement failure: {e_mark_generic}")

        except Exception as e_outer_iframe_loop: 
            # 이 블록은 개별 iframe 처리의 전체 과정을 감싸는 try문의 except
            logger.error(f"{log_prefix} General error processing iframe {iframe_log_id} (loop iteration #{loop_iteration_count}): {e_outer_iframe_loop}", exc_info=True)
            # iframe_handle이 존재하고, 아직 처리 중(data-cvf-processing)이라면 에러 마킹 시도
            if iframe_handle: 
                try: 
                    # 핸들이 유효하다면 직접 사용
                    if not iframe_handle.is_hidden(): # DOM에 아직 존재하는지 확인 (최선은 아님)
                         iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }") # JSHandle.evaluate에는 timeout 없음
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
            # 처리 중이던 상태(data-cvf-processing)가 남아있고, 해당 iframe이 아직 DOM에 있다면,
            # 성공적으로 교체되지 않았음을 의미하므로 에러 상태로 변경해야 함.
            # 이 로직은 iframe_locator가 현재 루프에서 처리하려던 그 iframe을 여전히 가리킨다고 가정.
            try:
                # locator를 사용하여 해당 ID와 processing 상태를 가진 iframe을 다시 찾음.
                # 성공적으로 교체되었다면 이 locator는 아무것도 찾지 않아야 함.
                # 루프 초반에 사용했던 `iframe_locator` 변수는 다음 루프를 위해 `first`로 재할당되므로
                # 여기서 ID로 다시 찾아야 함.
                problematic_iframe_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"][data-cvf-processing="true"]')
                if problematic_iframe_locator.count() == 1: # count에는 timeout 불필요
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} was left in 'processing' state after its loop. Marking as error.")
                    problematic_iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
            except PlaywrightError as e_final_ple: # Playwright 타임아웃 등
                 logger.warning(f"{log_prefix} PlaywrightError during final cleanup check for iframe {iframe_log_id}: {e_final_ple}")
            except Exception as e_final_cleanup_locator:
                 logger.warning(f"{log_prefix} Generic error during final cleanup check for iframe {iframe_log_id}: {e_final_cleanup_locator}")

    if loop_iteration_count >= max_loop_iterations:
        logger.warning(f"{log_prefix} Max loop iterations ({max_loop_iterations}) reached. Exiting iframe processing to prevent infinite loop.")

    logger.info(f"{log_prefix} Finished all iframe processing attempts at depth {current_depth}. Total iterations: {loop_iteration_count-1}. Successfully processed/replaced: {processed_iframe_count}.")


@celery_app.task(bind=True, name='celery_tasks.step_1_extract_html', max_retries=1, default_retry_delay=10)
def step_1_extract_html(self, url: str, chain_log_id: str) -> Dict[str, str]:
    logger.info("GLOBAL_ENTRY_POINT: step_1_extract_html function called.") # 최상단 진입 로그 추가
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step 1_extract_html]"
    logger.info(f"{log_prefix} ---------- Task started. URL: {url} ----------")
    logger.debug(f"{log_prefix} Input URL: {url}, Chain Log ID: {chain_log_id}")

    # 루트 작업 상태 업데이트 (시작)
    _update_root_task_state(chain_log_id, f"(1_extract_html) HTML 추출 시작: {url}", details={'current_task_id': str(task_id), 'url_for_step1': url})

    html_file_path = ""
    try:
        logger.info(f"{log_prefix} Initializing Playwright...")
        logger.debug(f"{log_prefix} Playwright sync_playwright context starting...")
        with sync_playwright() as p:
            logger.debug(f"{log_prefix} Playwright sync_playwright context active.")
            logger.info(f"{log_prefix} Playwright initialized. Launching browser...")
            try:
                # browser = p.chromium.launch(headless=True) # 로컬 테스트 시
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']) # Docker 환경
                logger.info(f"{log_prefix} Browser launched.")
                logger.debug(f"{log_prefix} Browser object: {browser}")
            except Exception as e_browser:
                logger.error(f"{log_prefix} Error launching browser: {e_browser}", exc_info=True)
                _update_root_task_state(chain_log_id, "(1_extract_html) 브라우저 실행 실패", status=states.FAILURE, error_info={'error': str(e_browser), 'traceback': traceback.format_exc()})
                self.update_state(state=states.FAILURE, meta={'error': str(e_browser)})
                raise Reject(f"Browser launch failed: {e_browser}", requeue=False)

            try:
                page = browser.new_page()
                logger.info(f"{log_prefix} New page created. Setting default timeout to {DEFAULT_PAGE_TIMEOUT}ms.")
                logger.debug(f"{log_prefix} Page object: {page}")
                page.set_default_timeout(DEFAULT_PAGE_TIMEOUT) # 모든 Playwright 작업에 대한 기본 타임아웃 설정
                page.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT)
                
                logger.info(f"{log_prefix} Navigating to URL: {url}")
                logger.debug(f"{log_prefix} Calling page.goto(\"{url}\", wait_until=\"domcontentloaded\")")
                page.goto(url, wait_until="domcontentloaded") # 'load' 또는 'networkidle' 고려
                logger.info(f"{log_prefix} Successfully navigated to URL. Current page URL: {page.url}")
                logger.debug(f"{log_prefix} Navigation complete. Page URL after goto: {page.url}")

                # 페이지 로드 후 추가적인 안정화 시간 (선택적)
                # logger.info(f"{log_prefix} Waiting for 3 seconds for dynamic content to potentially load...")
                # time.sleep(3)

                logger.info(f"{log_prefix} Starting iframe processing and content extraction.")
                logger.debug(f"{log_prefix} Calling _get_playwright_page_content_with_iframes_processed for URL: {url}")
                page_content = _get_playwright_page_content_with_iframes_processed(page, url, chain_log_id, str(task_id))
                logger.info(f"{log_prefix} Page content extracted. Length: {len(page_content)}")
                logger.debug(f"{log_prefix} Extracted page_content (first 500 chars): {page_content[:500]}")

                # 파일 저장 로직
                # ... (이하 동일)
            except PlaywrightError as e_playwright: # Playwright 관련 주요 예외
                error_message = f"Playwright operation failed: {e_playwright}"
                logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
                _update_root_task_state(chain_log_id, "(1_extract_html) Playwright 작업 실패", status=states.FAILURE, error_info={'error': str(e_playwright), 'traceback': traceback.format_exc(), 'url': url})
                # self.update_state(state=states.FAILURE, meta={'error': str(e_playwright), 'url': url}) # 개별 작업 상태도 업데이트
                # 실패 시 재시도 로직은 Celery의 max_retries에 의해 이미 처리됨. 여기서는 Reject로 명시적 실패 처리.
                raise Reject(error_message, requeue=False) # 재시도하지 않고 실패 처리
            except Exception as e_general:
                error_message = f"An unexpected error occurred during HTML extraction: {e_general}"
                logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
                _update_root_task_state(chain_log_id, "(1_extract_html) HTML 추출 중 예기치 않은 오류", status=states.FAILURE, error_info={'error': str(e_general), 'traceback': traceback.format_exc(), 'url': url})
                # self.update_state(state=states.FAILURE, meta={'error': str(e_general), 'url': url})
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

        # 파일 이름 생성 및 저장
        os.makedirs("logs", exist_ok=True)
        filename_base = sanitize_filename(url, ensure_unique=False) # 고유 ID는 아래에서 추가
        # 파일 이름에 chain_log_id의 일부와 고유 해시를 추가하여 추적 용이성 및 충돌 방지
        unique_file_id = hashlib.md5((chain_log_id + str(uuid.uuid4())).encode('utf-8')).hexdigest()[:8]
        html_file_name = f"{filename_base}_raw_html_{chain_log_id[:8]}_{unique_file_id}.html"
        html_file_path = os.path.join("logs", html_file_name)
            
        logger.info(f"{log_prefix} Saving extracted HTML to: {html_file_path}")
        logger.debug(f"{log_prefix} Opening file {html_file_path} for writing page_content (length: {len(page_content)}).")
        with open(html_file_path, "w", encoding="utf-8") as f:
            f.write(page_content)
        logger.info(f"{log_prefix} HTML content successfully saved to {html_file_path}.")

        result_data = {"html_file_path": html_file_path, "original_url": url}
        _update_root_task_state(chain_log_id, "(1_extract_html) HTML 추출 및 저장 완료", details={'html_file_path': html_file_path})
        logger.info(f"{log_prefix} ---------- Task finished successfully. Result: {result_data} ----------")
        logger.debug(f"{log_prefix} Returning from step_1: {result_data}")
        return result_data

    except Reject as e_reject: # 명시적으로 Reject된 경우, Celery가 재시도 또는 실패 처리
        logger.warning(f"{log_prefix} Task explicitly rejected: {e_reject.reason}. Celery will handle retry/failure.")
        _update_root_task_state(chain_log_id, f"(1_extract_html) 작업 명시적 거부: {e_reject.reason}", status=states.FAILURE, error_info={'error': str(e_reject.reason), 'reason_for_reject': getattr(e_reject, 'message', str(e_reject))}) # 상세 정보 추가
        raise # Celery가 처리하도록 re-raise

    except MaxRetriesExceededError as e_max_retries:
        error_message = "Max retries exceeded for HTML extraction."
        logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True) # exc_info=True 추가
        _update_root_task_state(chain_log_id, "(1_extract_html) 최대 재시도 초과", status=states.FAILURE, error_info={'error': error_message, 'original_exception': str(e_max_retries), 'traceback': traceback.format_exc()})
        # self.update_state(state=states.FAILURE, meta={'error': error_message, 'original_exception': str(e_max_retries)})
        # MaxRetriesExceededError는 Celery에 의해 자동으로 전파되므로, 여기서 다시 raise할 필요는 없을 수 있으나,
        # 명시적으로 체인을 중단시키고 싶다면 raise하는 것이 안전합니다.
        # 또는 특정 값을 반환하여 파이프라인의 다음 단계에서 이를 인지하도록 할 수 있습니다.
        # 여기서는 더 이상 진행되지 않도록 예외를 다시 발생시킵니다.
        raise

    except Exception as e_outer:
        # 이 블록은 Reject, MaxRetriesExceededError 외의 예외를 잡습니다.
        # 이미 try-except-finally 블록 내에서 대부분의 예외가 처리되어 Reject로 변환되거나 로깅되었을 것입니다.
        # 그럼에도 불구하고 여기까지 온 예외는 매우 예기치 않은 상황일 수 있습니다.
        error_message = f"Outer catch-all error in step_1_extract_html: {e_outer}"
        logger.critical(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
        _update_root_task_state(chain_log_id, "(1_extract_html) 처리되지 않은 심각한 오류", status=states.FAILURE, error_info={'error': str(e_outer), 'traceback': traceback.format_exc()})
        # self.update_state(state=states.FAILURE, meta={'error': error_message})
        # 심각한 오류이므로, 재시도하지 않고 즉시 실패 처리하기 위해 Reject 사용 가능
        raise Reject(f"Critical unhandled error: {e_outer}", requeue=False)

@celery_app.task(bind=True, name='celery_tasks.step_2_extract_text', max_retries=1, default_retry_delay=5)
def step_2_extract_text(self, prev_result: Dict[str, str], chain_log_id: str) -> Dict[str, str]:
    """(2단계) 저장된 HTML 파일에서 텍스트를 추출하여 새 파일에 저장합니다."""
    task_id = self.request.id
    step_log_id = "2_extract_text"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result: {prev_result} ----------")
    logger.debug(f"{log_prefix} Input prev_result: {prev_result}, Chain Log ID: {chain_log_id}")
    
    html_file_path = None
    original_url = "N/A"
    try:
        html_file_path = prev_result.get("html_file_path")
        original_url = prev_result.get("original_url", "N/A")
        logger.info(f"{log_prefix} Extracted html_file_path: {html_file_path}, original_url: {original_url}")

        if not html_file_path or not isinstance(html_file_path, str) or not os.path.exists(html_file_path):
            error_msg = f"HTML file not found, path invalid, or not a string: {html_file_path} (type: {type(html_file_path).__name__})"
            logger.error(f"{log_prefix} {error_msg}")
            _update_root_task_state(chain_log_id, f"({step_log_id}) 입력 HTML 파일 오류", status=states.FAILURE, error_info={'error': error_msg, 'path_checked': str(html_file_path)})
            # 중요: 여기서 ValueError를 발생시켜 Celery가 재시도하거나 실패 처리하도록 함
            raise ValueError(error_msg)

        logger.info(f"{log_prefix} Starting text extraction from valid HTML file: {html_file_path}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) HTML에서 텍스트 추출 시작", details={'html_file_path': html_file_path, 'current_task_id': task_id})

        extracted_text_file_path = None # 초기화

        logger.debug(f"{log_prefix} Attempting to read HTML file content from: {html_file_path}")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        logger.info(f"{log_prefix} Successfully read HTML file. Content length: {len(html_content)}")
        logger.debug(f"{log_prefix} HTML content (first 500 chars): {html_content[:500]}")
        
        logger.debug(f"{log_prefix} Initializing BeautifulSoup parser.")
        soup = BeautifulSoup(html_content, "html.parser")
        logger.info(f"{log_prefix} BeautifulSoup initialized.")

        logger.debug(f"{log_prefix} Removing comments.")
        comments_removed_count = 0
        for el in soup.find_all(string=lambda text_node: isinstance(text_node, Comment)):
            el.extract()
            comments_removed_count += 1
        logger.info(f"{log_prefix} Removed {comments_removed_count} comments.")

        logger.debug(f"{log_prefix} Removing script, style, and other unwanted tags.")
        decomposed_tags_count = 0
        tags_to_decompose = ["script", "style", "noscript", "link", "meta", "header", "footer", "nav", "aside"]
        for tag_name in tags_to_decompose:
            for el in soup.find_all(tag_name):
                el.decompose()
                decomposed_tags_count +=1
        logger.info(f"{log_prefix} Decomposed {decomposed_tags_count} unwanted tags ({tags_to_decompose}).")
        
        logger.debug(f"{log_prefix} Extracting text with soup.get_text().")
        text = soup.get_text(separator="\\n", strip=True)
        logger.info(f"{log_prefix} Initial text extracted. Length: {len(text)}. Applying regex.")

        logger.debug(f"{log_prefix} Normalizing whitespaces and newlines.")
        text_before_re = text
        text = re.sub(r'[\\s\\xa0]+', ' ', text) # NBSP 포함 모든 공백류를 단일 공백으로
        text = re.sub(r' (\\n)+', '\\n', text) # 공백 후 개행은 개행만
        text = re.sub(r'(\\n)+ ', '\\n', text) # 개행 후 공백은 개행만
        text = re.sub(r'(\\n){2,}', '\\n\\n', text) # 2회 이상 연속 개행은 2회로
        text = text.strip()
        logger.info(f"{log_prefix} Text after regex. Length: {len(text)}. (Before regex: {len(text_before_re)})")
        logger.debug(f"{log_prefix} Final extracted text (first 500 chars): {text[:500]}")

        if not text:
            logger.warning(f"{log_prefix} No text extracted after processing from {html_file_path}. Resulting file will be empty or placeholder.")
            # 빈 텍스트도 파일로 저장하고 다음 단계로 넘길 수 있도록 처리 (필요시)
            # 또는 여기서 특정 오류를 발생시킬 수도 있음. 현재는 경고 후 진행.
        
        logs_dir = "logs"
        logger.debug(f"{log_prefix} Ensuring logs directory exists: {logs_dir}")
        os.makedirs(logs_dir, exist_ok=True)
        
        logger.debug(f"{log_prefix} Sanitizing filename. Original html_file_path: {html_file_path}")
        base_html_fn = os.path.splitext(os.path.basename(html_file_path))[0]
        logger.debug(f"{log_prefix} base_html_fn (splitext): {base_html_fn}")
        base_html_fn = re.sub(r'_raw_html_[a-f0-9]{8}$', '', base_html_fn) # _raw_html_xxxxxxx 부분 제거
        logger.debug(f"{log_prefix} base_html_fn (after re.sub): {base_html_fn}")
        
        unique_text_fn_stem = f"{base_html_fn}_extracted_text"
        unique_text_fn = sanitize_filename(unique_text_fn_stem, "txt", ensure_unique=True)
        extracted_text_file_path = os.path.join(logs_dir, unique_text_fn)
        logger.info(f"{log_prefix} Determined extracted text file path: {extracted_text_file_path}")

        logger.debug(f"{log_prefix} Attempting to write extracted text (length: {len(text)}) to file: {extracted_text_file_path}")
        with open(extracted_text_file_path, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"{log_prefix} Text extracted and saved to: {extracted_text_file_path} (Final Length: {len(text)})")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 파일 저장 완료", details={'text_file_path': extracted_text_file_path})
        
        result_to_return = {"text_file_path": extracted_text_file_path, 
                             "original_url": original_url, 
                             "html_file_path": html_file_path # 로깅/추적용으로 유지
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_2: {result_to_return}")
        return result_to_return

    except FileNotFoundError as e_fnf:
        logger.error(f"{log_prefix} FileNotFoundError during text extraction: {e_fnf}. HTML file path: {html_file_path}", exc_info=True)
        err_details_fnf = {'error': str(e_fnf), 'type': type(e_fnf).__name__, 'html_file': str(html_file_path), 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 추출 실패 (파일 없음)", status=states.FAILURE, error_info=err_details_fnf)
        raise # Celery가 태스크를 실패로 처리하도록 함
    except IOError as e_io:
        logger.error(f"{log_prefix} IOError during text extraction: {e_io}. HTML file path: {html_file_path}", exc_info=True)
        err_details_io = {'error': str(e_io), 'type': type(e_io).__name__, 'html_file': str(html_file_path), 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 추출 실패 (IO 오류)", status=states.FAILURE, error_info=err_details_io)
        raise
    except Exception as e_general:
        logger.error(f"{log_prefix} General error during text extraction from {html_file_path}: {e_general}", exc_info=True)
        if extracted_text_file_path and os.path.exists(extracted_text_file_path):
            try:
                logger.warning(f"{log_prefix} Attempting to remove partially created file: {extracted_text_file_path} due to error.")
                os.remove(extracted_text_file_path) 
            except Exception as e_remove: 
                logger.warning(f"{log_prefix} Failed to remove partial text file {extracted_text_file_path}: {e_remove}", exc_info=True)
        
        err_details_general = {'error': str(e_general), 'type': type(e_general).__name__, 'html_file': str(html_file_path), 'traceback': traceback.format_exc()}
        _update_root_task_state(chain_log_id, f"({step_log_id}) 텍스트 추출 중 알 수 없는 오류", status=states.FAILURE, error_info=err_details_general)
        raise
    finally:
        logger.info(f"{log_prefix} ---------- Task execution attempt ended. ----------")

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
        logger.debug(f"{log_prefix} Reading raw text from: {raw_text_file_path}")
        with open(raw_text_file_path, "r", encoding="utf-8") as f:
            raw_text = f.read()
        logger.debug(f"{log_prefix} Raw text length: {len(raw_text)}. Raw text (first 500 chars): {raw_text[:500]}")

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
            logger.debug(f"{log_prefix} GROQ_API_KEY: {'*' * (len(groq_api_key) - 4) + groq_api_key[-4:] if groq_api_key else 'Not Set'}") # API 키 일부 마스킹
            
            chat = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=llm_model)
            logger.debug(f"{log_prefix} ChatGroq client initialized: {chat}")
            
            sys_prompt = ("You are an expert text processing assistant. Your task is to extract ONLY the core job description from the provided text. "
                          "Remove all extraneous information such as advertisements, company promotions, navigation links, sidebars, headers, footers, legal disclaimers, cookie notices, unrelated articles, and anything not directly related to the job's responsibilities, qualifications, and benefits. "
                          "Present the output as clean, readable plain text. Do NOT use markdown formatting. Focus on the actual job content. "
                          "If the text does not appear to be a job posting, or if it is too corrupted to extract meaningful job information, respond with the exact phrase '추출할 채용공고 내용 없음' and nothing else.")
            human_template = "{text_content}"
            prompt = ChatPromptTemplate.from_messages([("system", sys_prompt), ("human", human_template)])
            parser = StrOutputParser()
            llm_chain = prompt | chat | parser
            logger.debug(f"{log_prefix} LLM chain constructed: {llm_chain}")

            logger.info(f"{log_prefix} Invoking LLM. Text length: {len(raw_text)}")
            MAX_LLM_INPUT_LEN = 24000 
            text_for_llm = raw_text
            if len(raw_text) > MAX_LLM_INPUT_LEN:
                logger.warning(f"{log_prefix} Text length ({len(raw_text)}) > limit ({MAX_LLM_INPUT_LEN}). Truncating.")
                text_for_llm = raw_text[:MAX_LLM_INPUT_LEN]
                _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 입력 텍스트 일부 사용 (길이 초과)", 
                                        details={'original_len': len(raw_text), 'truncated_len': len(text_for_llm)})
            
            logger.debug(f"{log_prefix} Text for LLM (first 500 chars): {text_for_llm[:500]}")
            filtered_content = llm_chain.invoke({"text_content": text_for_llm})
            logger.info(f"{log_prefix} LLM filtering complete. Output length: {len(filtered_content)}")
            logger.debug(f"{log_prefix} Filtered content (first 500 chars): {filtered_content[:500]}")

            if filtered_content.strip() == "추출할 채용공고 내용 없음":
                logger.warning(f"{log_prefix} LLM reported no extractable job content.")
                filtered_content = "<!-- LLM 분석: 추출할 채용공고 내용 없음 -->"

        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        base_text_fn = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")
        unique_filtered_fn = sanitize_filename(f"{base_text_fn}_filtered_text", "txt", ensure_unique=True)
        filtered_text_file_path = os.path.join(logs_dir, unique_filtered_fn)

        logger.debug(f"{log_prefix} Writing filtered content (length: {len(filtered_content)}) to: {filtered_text_file_path}")
        with open(filtered_text_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)
        logger.info(f"{log_prefix} Filtered text saved to: {filtered_text_file_path}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 필터링된 텍스트 파일 저장 완료", details={'filtered_text_file_path': filtered_text_file_path})

        result_to_return = {"filtered_text_file_path": filtered_text_file_path, 
                             "original_url": original_url, 
                             "html_file_path": html_file_path, # 로깅/추적용
                             "raw_text_file_path": raw_text_file_path, # 로깅/추적용
                             "status_history": prev_result.get("status_history", []),
                             "cover_letter_preview": filtered_content[:500] + ("..." if len(filtered_content) > 500 else ""),
                             "llm_model_used_for_cv": "N/A"
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_3: {result_to_return}")
        return result_to_return

    except Exception as e:
        logger.error(f"{log_prefix} Error filtering with LLM: {e}", exc_info=True)
        if filtered_text_file_path and os.path.exists(filtered_text_file_path):
            try: os.remove(filtered_text_file_path)
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial filtered file {filtered_text_file_path}: {e_remove}")

        err_details = {'error': str(e), 'type': type(e).__name__, 'filtered_file': raw_text_file_path, 'traceback': traceback.format_exc()}
        logger.error(f"{log_prefix} Attempting to update root task {chain_log_id} with pipeline FAILURE status due to exception. Error details: {err_details}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 필터링 실패", status=states.FAILURE, error_info=err_details)
        logger.error(f"{log_prefix} Root task {chain_log_id} updated with pipeline FAILURE status.")
        raise # 파이프라인 실패

@celery_app.task(bind=True, name='celery_tasks.step_4_generate_cover_letter', max_retries=1, default_retry_delay=20)
def step_4_generate_cover_letter(self, prev_result: Dict[str, Any], chain_log_id: str, user_prompt_text: Optional[str]) -> Dict[str, Any]:
    """(4단계) 필터링된 텍스트와 사용자 프롬프트를 사용하여 자기소개서를 생성하고 최종 결과를 반환합니다."""
    task_id = self.request.id
    step_log_id = "4_generate_cover_letter"
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result: {prev_result}, User Prompt: {'Provided' if user_prompt_text else 'N/A'} ----------")
    logger.debug(f"{log_prefix} Input prev_result: {prev_result}, Chain Log ID: {chain_log_id}, User Prompt Text: {user_prompt_text[:100] if user_prompt_text else 'None'}")

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
        logger.debug(f"{log_prefix} Reading filtered job text from: {filtered_text_file_path}")
        with open(filtered_text_file_path, "r", encoding="utf-8") as f:
            filtered_job_text = f.read()
        logger.debug(f"{log_prefix} Filtered job text length: {len(filtered_job_text)}. Filtered job text (first 500 chars): {filtered_job_text[:500]}")

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
            logger.info(f"{log_prefix} Attempting to update root task {chain_log_id} with NO_CONTENT_FOR_COVER_LETTER status. Details: {final_pipeline_result}")
            _update_root_task_state(chain_log_id, "파이프라인 완료 (자소서 생성 불가: 내용 부족)", status=states.SUCCESS, details=final_pipeline_result)
            logger.info(f"{log_prefix} Root task {chain_log_id} updated with NO_CONTENT_FOR_COVER_LETTER status.")
            return final_pipeline_result
        
        logger.info(f"{log_prefix} Calling LLM for cover letter. Text length: {len(filtered_job_text)}, Prompt length: {len(user_prompt_text if user_prompt_text else '')}")
        logger.debug(f"{log_prefix} Job posting content for LLM (first 500 chars): {filtered_job_text[:500]}")
        logger.debug(f"{log_prefix} User prompt for LLM: {user_prompt_text[:200] if user_prompt_text else 'None'}")
        
        # LLM을 호출하여 자기소개서 생성
        llm_cv_data_tuple = generate_cover_letter(
            job_posting_content=filtered_job_text,
            prompt=user_prompt_text
        )
        logger.debug(f"{log_prefix} LLM (generate_cover_letter) returned: Type={type(llm_cv_data_tuple)}, Value (first 100 chars if str): {str(llm_cv_data_tuple)[:100] if isinstance(llm_cv_data_tuple, (str, tuple, dict)) else type(llm_cv_data_tuple)}")


        # 반환 값 처리 수정: 튜플의 각 요소를 직접 할당
        generated_cv_text = None
        formatted_cv_text = None # 초기화 추가

        if isinstance(llm_cv_data_tuple, tuple) and len(llm_cv_data_tuple) == 2:
            generated_cv_text, formatted_cv_text = llm_cv_data_tuple
            logger.info(f"{log_prefix} Successfully unpacked cover letter data from tuple.")
        elif isinstance(llm_cv_data_tuple, dict): # 혹시 모를 이전 방식 호환성 (하지만 현재 generate_cover_letter_semantic.py는 튜플 반환)
            generated_cv_text = llm_cv_data_tuple.get("cover_letter")
            formatted_cv_text = llm_cv_data_tuple.get("formatted_cover_letter")
            if generated_cv_text is not None: # .get()은 키가 없으면 None을 반환하므로
                 logger.info(f"{log_prefix} Successfully retrieved cover letter data using .get() from dict (fallback).")
            else:
                logger.error(f"{log_prefix} Failed to get 'cover_letter' from dict result: {llm_cv_data_tuple}")
                # 이 경우 generated_cv_text는 None으로 유지됨
        else:
            logger.error(f"{log_prefix} Unexpected format for llm_cv_data: {type(llm_cv_data_tuple)}. Expected tuple of two strings or dict.")
            # 여기서 에러 처리를 하거나, generated_cv_text와 formatted_cv_text를 None으로 유지


        if not generated_cv_text: # None이거나 빈 문자열인 경우
            logger.error(f"{log_prefix} Cover letter generation failed or returned empty. llm_cv_data: {llm_cv_data_tuple}") # 원본 데이터 로깅
            # 실패 시, formatted_cv_text에 담긴 오류 메시지(있다면) 또는 일반 메시지를 사용
            error_message_from_llm = formatted_cv_text if formatted_cv_text else "LLM으로부터 유효한 자기소개서를 받지 못했습니다."
            raise ValueError(f"자기소개서 생성 실패: {error_message_from_llm}")
        
        logger.info(f"{log_prefix} Cover letter generated successfully. Raw length: {len(generated_cv_text)}, Formatted length: {len(formatted_cv_text if formatted_cv_text else '')}")
        logger.debug(f"{log_prefix} Generated CV text (first 500 chars): {generated_cv_text[:500]}")
        logger.debug(f"{log_prefix} Formatted CV text (first 500 chars): {(formatted_cv_text[:500] if formatted_cv_text else 'None')}")


        # 생성된 자기소개서 파일명 결정 (기존 파일명 활용하여 일관성 유지)
        logs_dir = "logs"  # logs_dir 변수 정의
        os.makedirs(logs_dir, exist_ok=True) # 디렉토리 생성 보장
        base_filename = os.path.splitext(os.path.basename(filtered_text_file_path))[0]
        unique_cv_fn = sanitize_filename(f"{base_filename}_cover_letter_{chain_log_id[:8]}", "txt", ensure_unique=True)
        cover_letter_file_path_final = os.path.join(logs_dir, unique_cv_fn)

        logger.debug(f"{log_prefix} Writing final cover letter (length: {len(generated_cv_text)}) to: {cover_letter_file_path_final}")
        with open(cover_letter_file_path_final, "w", encoding="utf-8") as f:
            f.write(generated_cv_text)
        logger.info(f"{log_prefix} Cover letter saved to: {cover_letter_file_path_final}")

        final_pipeline_result = {
            "status": "SUCCESS",
            "message": "Cover letter generated and saved successfully.",
            "cover_letter_file_path": cover_letter_file_path_final,
            "cover_letter_preview": generated_cv_text[:500] + ("..." if len(generated_cv_text) > 500 else ""),
            "original_url": original_url,
            "llm_model_used_for_cv": "N/A",
            "intermediate_files": {
                "html": html_file_path,
                "raw_text": raw_text_file_path,
                "filtered_text": filtered_text_file_path
            }
        }
        logger.info(f"{log_prefix} Attempting to update root task {chain_log_id} with pipeline SUCCESS status. Details: {final_pipeline_result}")
        _update_root_task_state(chain_log_id, "파이프라인 성공적으로 완료", status=states.SUCCESS, details=final_pipeline_result)
        logger.info(f"{log_prefix} Root task {chain_log_id} updated with pipeline SUCCESS status.")
        logger.debug(f"{log_prefix} Returning from step_4 (final pipeline result): {final_pipeline_result}")
        return final_pipeline_result # 이것이 체인의 최종 결과가 됨
        
    except Exception as e:
        logger.error(f"{log_prefix} Error in cover letter generation: {e}", exc_info=True)
        if cover_letter_file_path_final and os.path.exists(cover_letter_file_path_final):
            try: os.remove(cover_letter_file_path_final)
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial CV file {cover_letter_file_path_final}: {e_remove}")
        
        err_details = {'error': str(e), 'type': type(e).__name__, 'filtered_file': raw_text_file_path, 'traceback': traceback.format_exc()}
        logger.error(f"{log_prefix} Attempting to update root task {chain_log_id} with pipeline FAILURE status due to exception. Error details: {err_details}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 자소서 생성 실패", status=states.FAILURE, error_info=err_details)
        logger.error(f"{log_prefix} Root task {chain_log_id} updated with pipeline FAILURE status.")
        raise # 파이프라인 실패

@celery_app.task(bind=True, name='celery_tasks.process_job_posting_pipeline')
def process_job_posting_pipeline(self, url: str, user_prompt: Optional[str] = None) -> str:
    """전체 채용공고 처리 파이프라인: HTML 추출 -> 텍스트 추출 -> 내용 필터링 -> 자기소개서 생성"""
    root_task_id = self.request.id # 이 ID가 chain_log_id로 사용됨
    log_prefix = f"[Pipeline / Root {root_task_id}]"
    logger.info(f"{log_prefix} Initiating pipeline for URL: {url}, User Prompt: {'Provided' if user_prompt else 'N/A'}")
    logger.debug(f"{log_prefix} Root Task ID (self.request.id): {root_task_id}")
    logger.debug(f"{log_prefix} URL: {url}, User Prompt (first 100 chars): {user_prompt[:100] if user_prompt else 'N/A'}")


    _update_root_task_state(root_task_id, "파이프라인 시작됨", status=states.STARTED, 
                            details={'url': url, 'user_prompt_provided': bool(user_prompt)})
    logger.debug(f"{log_prefix} Root task state updated to STARTED.")

    # Celery 체인 정의 (가장 일반적이고 이해하기 쉬운 형태):
    s1 = step_1_extract_html.s(url=url, chain_log_id=root_task_id)
    s2 = step_2_extract_text.s(chain_log_id=root_task_id)
    s3 = step_3_filter_content.s(chain_log_id=root_task_id)
    s4 = step_4_generate_cover_letter.s(chain_log_id=root_task_id, user_prompt_text=user_prompt) # user_prompt_text는 명시적 kwargs로 전달
    logger.debug(f"{log_prefix} Sub-task signatures defined: s1={s1}, s2={s2}, s3={s3}, s4={s4}")
    
    processing_chain_final = chain(s1, s2, s3, s4)
    logger.debug(f"{log_prefix} Chain object created: {processing_chain_final}")

    # 체인 구조 로깅 개선
    chain_structure_log = f"{s1.name}(...) | {s2.name}(...) | {s3.name}(...) | {s4.name}(...)"
    logger.info(f"{log_prefix} Celery chain structure defined: {chain_structure_log}")
    logger.info(f"{log_prefix} Full chain object: {processing_chain_final}") # 전체 체인 객체도 로깅

    # 임시 테스트 코드 제거하고 원래 체인 실행 로직으로 복원
    try:
        chain_async_result = processing_chain_final.apply_async()
        logger.info(f"{log_prefix} Dispatched chain. Last task ID in chain: {chain_async_result.id}. Polling root ID: {root_task_id}")
        logger.debug(f"{log_prefix} Chain dispatched. AsyncResult: id={chain_async_result.id}, parent_id={chain_async_result.parent.id if chain_async_result.parent else 'None'}, root_id={chain_async_result.root_id}")
        logger.debug(f"{log_prefix} Returning root_task_id: {root_task_id} from process_job_posting_pipeline.")
        return root_task_id # 파이프라인의 루트 ID 반환
        
    except Exception as e_chain_dispatch:
        logger.error(f"{log_prefix} Failed to dispatch chain: {e_chain_dispatch}", exc_info=True)
        err_details = {'error': str(e_chain_dispatch), 'type': type(e_chain_dispatch).__name__, 'traceback': traceback.format_exc()}
        _update_root_task_state(root_task_id, "파이프라인 실행 실패 (요청단계)", status=states.FAILURE, error_info=err_details)
        raise # FastAPI가 500 에러 반환하도록 함