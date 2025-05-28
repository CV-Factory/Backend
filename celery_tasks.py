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
MAX_IFRAME_DEPTH = 1
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
        meta_for_update = {'current_step': current_step_message}
        if details:
            meta_for_update.update(details)

        result_for_store = None
        traceback_for_store = None
        current_status_for_store = status

        if current_status_for_store == states.FAILURE:
            logger.debug(f"[StateUpdateFailureDetails] Preparing FAILURE state for {task_id}. Original error_info: {error_info}")
            if error_info:
                # error_info에 'error', 'type', 'traceback' 등이 있다고 가정
                err_msg = str(error_info.get('error', 'Unknown error'))
                err_type_name = str(error_info.get('type', 'Exception'))
                
                # result_for_store는 예외 객체여야 합니다.
                # 간단한 Exception 객체를 생성하거나, error_info의 내용을 사용해 더 구체적인 예외 객체를 만들 수 있습니다.
                # 여기서는 간단히 Exception 객체를 사용합니다.
                result_for_store = Exception(f"{err_type_name}: {err_msg}")
                
                traceback_for_store = error_info.get('traceback') # 문자열 형태의 traceback
                
                # meta에는 순수 정보성 데이터만 남기는 것을 고려. 
                # error_details는 이미 result와 traceback으로 분리됨.
                # 필요하다면, error_info의 다른 내용을 meta_for_update['error_details']에 유지할 수 있음.
                # 여기서는 meta_for_update에 error_info의 내용을 'error_details'로 유지합니다.
                meta_for_update['error_details'] = error_info 
            else:
                result_for_store = Exception("Unknown error occurred, no error_info provided.")
            logger.info(f"[StateUpdateFailure] Root task {task_id} being set to FAILURE. Message: {current_step_message}. Result: {result_for_store}, Traceback: {'Provided' if traceback_for_store else 'Not provided'}")
        
        elif current_status_for_store not in [states.SUCCESS, states.RETRY, states.REVOKED, states.STARTED]:
            logger.warning(f"Invalid or custom status '{current_status_for_store}' provided for task {task_id}. Defaulting to STARTED. Message: {current_step_message}")
            current_status_for_store = states.STARTED
            if error_info: # 비표준 상태지만 error_info가 있다면 로깅/meta에 포함
                 meta_for_update['error_details'] = error_info


        logger.info(f"[StateUpdateMeta] Root task {task_id} - meta_for_update before storing: {meta_for_update}")
        
        # store_result 호출
        celery_app.backend.store_result(
            task_id, 
            result_for_store,  # 성공 시 None, 실패 시 Exception 객체
            current_status_for_store, 
            traceback=traceback_for_store, # 실패 시 traceback 문자열
            meta=meta_for_update # 순수 정보성 메타데이터
        )
        
        log_details_str = f"details: {meta_for_update.get('details', 'N/A')}"
        log_error_str = ""
        if current_status_for_store == states.FAILURE:
            log_error_str = f", error_result: {result_for_store}, tb_provided: {'Yes' if traceback_for_store else 'No'}"
        elif error_info: # FAILURE가 아닌데 error_info가 있는 경우 (예: 경고 후 진행)
            log_error_str = f", error_details_in_meta: {meta_for_update.get('error_details', 'N/A')}"

        logger.info(f"[StateUpdate] Root task {task_id} status: {current_status_for_store}, step: '{current_step_message}', {log_details_str}{log_error_str}")

    except Exception as e_update:
        logger.error(f"[StateUpdateFailureCritical] Critically failed to update root task {task_id} state: {e_update}", exc_info=True)

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
    initial_count = 0 # UnboundLocalError 방지를 위해 미리 0으로 초기화

    try:
        initial_iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])')
        # PlaywrightError (Frame detached 등) 발생 가능성 있음
        initial_count = initial_iframe_locator.count() 
        logger.info(f"{log_prefix} Initial check: Found {initial_count} processable iframe(s) at this depth.")
        if initial_count == 0:
            logger.info(f"{log_prefix} No processable iframes found at this depth based on initial check.")
            return
    except PlaywrightError as e_initial_count: # Playwright 관련 오류만 특정하여 처리
        logger.warning(f"{log_prefix} PlaywrightError during initial iframe count: {e_initial_count}. Setting initial_count to 0 and proceeding with loop if possible.", exc_info=True)
        initial_count = 0 # 오류 발생 시 initial_count를 0으로 명시적 설정
        # 루프 조건에서 initial_count가 0이면 아래 로직은 실행되지 않을 수 있음 (max_loop_iterations 계산 때문)
        # 또는, 오류 발생 시 바로 return 할 수도 있음. 여기서는 일단 0으로 설정하고 진행.
    except Exception as e_initial_count_other: # 기타 예외
        logger.error(f"{log_prefix} Unexpected error during initial iframe count: {e_initial_count_other}. Setting initial_count to 0.", exc_info=True)
        initial_count = 0 # 안전하게 0으로 설정


    loop_iteration_count = 0
    # initial_count가 0일 수 있으므로, 0 + 10 = 10이 되어 최소한의 반복은 보장.
    max_loop_iterations = initial_count + 20 # 무한 루프 방지를 위한 안전장치 (초기 카운트보다 충분히 많이)
    logger.debug(f"{log_prefix} Calculated max_loop_iterations: {max_loop_iterations} (based on initial_count: {initial_count})")


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
                logger.debug(f"{log_prefix} Extracted page_content successfully (length verified).")

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

        result_data = {"html_file_path": html_file_path, "original_url": url, "page_content": page_content} # page_content 추가
        
        # 로깅을 위해 result_data의 page_content를 축약된 정보로 대체
        result_data_for_log = result_data.copy()
        if 'page_content' in result_data_for_log:
            page_content_len = len(result_data_for_log['page_content']) if result_data_for_log['page_content'] is not None else 0
            result_data_for_log['page_content'] = f"<page_content_omitted_from_log, length={page_content_len}>"

        _update_root_task_state(chain_log_id, "(1_extract_html) HTML 추출 및 저장 완료", details={'html_file_path': html_file_path})
        logger.info(f"{log_prefix} ---------- Task finished successfully. Result for log: {result_data_for_log} ----------") # 수정된 로깅
        logger.debug(f"{log_prefix} Returning from step_1: keys={list(result_data.keys())}, page_content length: {len(result_data.get('page_content', '')) if result_data.get('page_content') else 0}")
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
    # root_task_id = self.request.root_id # root_id는 chain_log_id로 전달받으므로 중복
    task_id = self.request.id
    step_log_id = "2_extract_text" # step_log_id 정의
    # chain_log_id = get_chain_log_id(self.request) # chain_log_id는 이미 인자로 받으므로 이 줄 삭제
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id}]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result_keys: {list(prev_result.keys()) if isinstance(prev_result, dict) else type(prev_result)} ----------")

    if not isinstance(prev_result, dict) or 'page_content' not in prev_result or 'html_file_path' not in prev_result or 'original_url' not in prev_result:
        error_msg = f"Invalid or incomplete prev_result: {prev_result}. Expected a dict with 'page_content', 'html_file_path', and 'original_url'."
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 오류: 이전 단계 결과 형식 오류", status=states.FAILURE, error_info={'error': error_msg}) # error -> error_info, status 추가
        raise ValueError(error_msg)

    html_content = prev_result.get('page_content')
    html_file_path = prev_result.get('html_file_path')
    original_url = prev_result.get('original_url')

    # 입력 데이터 로깅 (page_content는 길이만 로깅)
    prev_result_for_log = prev_result.copy()
    if 'page_content' in prev_result_for_log:
        page_content_len = len(prev_result_for_log['page_content']) if prev_result_for_log['page_content'] is not None else 0
        prev_result_for_log['page_content'] = f"<page_content_omitted_from_log, length={page_content_len}>"
    logger.info(f"{log_prefix} Received from previous step (for log): {prev_result_for_log}")
    
    if not html_content:
        error_msg = f"Page content is missing from previous step result: {prev_result.keys()}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 이전 단계 HTML 내용 없음", status=states.FAILURE, error_info={'error': error_msg})
        raise ValueError(error_msg)

    # 다음 로직을 위한 extracted_text_file_path 초기화
    extracted_text_file_path = None 

    try:
        # html_file_path 유효성 검사는 파일 저장 시에만 필요할 수 있으나, 로깅을 위해 유지
        if not html_file_path or not isinstance(html_file_path, str):
            logger.warning(f"{log_prefix} html_file_path is invalid ({html_file_path}), will use placeholder for saving text file if needed, but proceeding with page_content.")
            # 파일 이름 생성을 위한 임시 base_html_fn (URL 기반)
            base_html_fn_for_saving = sanitize_filename(original_url if original_url != "N/A" else "unknown_source", ensure_unique=False) + f"_{chain_log_id[:8]}"
        else:
            base_html_fn_for_saving = os.path.splitext(os.path.basename(html_file_path))[0]
            base_html_fn_for_saving = re.sub(r'_raw_html_[a-f0-9]{8}_[a-f0-9]{8}$', '', base_html_fn_for_saving) # 고유 ID 패턴 수정

        logger.info(f"{log_prefix} Starting text extraction from page_content (length: {len(html_content)})")
        _update_root_task_state(chain_log_id, f"({step_log_id}) HTML 내용에서 텍스트 추출 시작", details={'current_task_id': task_id})

        # extracted_text_file_path = None # 초기화 (위로 이동)
        # html_content = page_content # 이미 html_content 변수에 할당되어 있음

        # 이전의 파일 읽기 로직은 제거합니다.
        # logger.debug(f"{log_prefix} Attempting to read HTML file content from: {html_file_path}")
        # with open(html_file_path, "r", encoding="utf-8") as f:
        #     html_content = f.read()
        # logger.info(f"{log_prefix} Successfully read HTML file. Content length: {len(html_content)}")
        logger.debug(f"{log_prefix} HTML content from prev_result successfully received (length verified as {len(html_content)}).")
        
        logger.debug(f"{log_prefix} Initializing BeautifulSoup parser.")
        soup = BeautifulSoup(html_content, "html.parser")
        logger.info(f"{log_prefix} BeautifulSoup initialized.")

        # 원래 로직으로 복원
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
        
        target_soup_object = soup # target_soup_object를 soup로 설정

        logger.debug(f"{log_prefix} Extracting text with target_soup_object.get_text().")
        text = target_soup_object.get_text(separator="\\n", strip=True)
        logger.info(f"{log_prefix} Initial text extracted. Length: {len(text)}.")
        logger.debug(f"{log_prefix} Initial text (first 500 chars): {text[:500]}")

        # Specific cleanup for stray literal 'n' characters acting as separators
        logger.debug(f"{log_prefix} Starting specific 'n' cleanup.")
        original_text_before_n_cleanup = text
        # Replace " n" (space then n) followed by a non-space char, with a space. " X nY" -> " X Y"
        text = re.sub(r'\s+n(?=\S)', ' ', text)
        # Replace a non-space char, followed by "n " (n then space), with a space. "Xn Y" -> "X Y"
        text = re.sub(r'(?<=\S)n\s+', ' ', text)
        # Replace isolated " n " (space-n-space) with a single space
        text = re.sub(r'\s+n\s+', ' ', text)
        if text != original_text_before_n_cleanup:
            logger.info(f"{log_prefix} Text after specific 'n' cleanup. Length: {len(text)}.")
            logger.debug(f"{log_prefix} Text after 'n' cleanup (first 500 chars): {text[:500]}")
        else:
            logger.debug(f"{log_prefix} No changes made by specific 'n' cleanup.")

        # Normalize all whitespaces (including \xa0) to a single space,
        # BUT preserve actual newlines \n for now.
        # First, replace \xa0 and multiple horizontal spaces (space, tab etc.) with a single space.
        text = re.sub(r'[ \t\r\f\v\xa0]+', ' ', text)
        logger.debug(f"{log_prefix} Text after initial horizontal space/nbsp normalization (newlines preserved for now). Length: {len(text)}")
        
        # Now, normalize newlines and spaces around them
        text = re.sub(r' *\n *', '\n', text) # Remove spaces around newlines: " \n " -> "\n"
        text = re.sub(r'\n{2,}', '\n\n', text) # Reduce multiple newlines to max two
        text = text.strip() # Remove leading/trailing whitespace (including newlines if they are at ends)
        logger.info(f"{log_prefix} Text after newline and space normalization. Length: {len(text)}.")
        logger.debug(f"{log_prefix} Normalized text (first 500 chars): {text[:500]}")

        # Now, convert to single line for 50-char formatting
        logger.debug(f"{log_prefix} Converting to single line by splitting by ANY whitespace and rejoining with single spaces.")
        # 1. 모든 종류의 공백을 기준으로 나누고, 빈 문자열은 제거
        words = text.split()
        # 2. 단일 공백으로 다시 합쳐 한 줄로 만듦
        text_single_line = ' '.join(words)
        logger.info(f"{log_prefix} Text converted to single line. Length: {len(text_single_line)}")
        logger.debug(f"{log_prefix} Single line text (first 500 chars): {text_single_line[:500]}")

        # 매 50자마다 실제 개행 문자를 삽입합니다.
        logger.debug(f"{log_prefix} Inserting ACTUAL newline (\n) every 50 characters.")
        chars_per_line = 50
        text_formatted = ""
        if text_single_line: # 빈 문자열이 아닐 경우에만 처리
            # Insert ACTUAL newlines
            text_formatted = '\n'.join(text_single_line[i:i+chars_per_line] for i in range(0, len(text_single_line), chars_per_line))
            logger.info(f"{log_prefix} Text formatted with newlines every {chars_per_line} characters. New length: {len(text_formatted)}")
        else:
            logger.info(f"{log_prefix} Single line text was empty, skipping 50-char formatting.")
            text_formatted = text_single_line # 빈 문자열 그대로 유지

        text = text_formatted # 최종 결과를 text 변수에 할당
        logger.debug(f"{log_prefix} Final extracted text for saving (first 500 chars): {text[:500]}")

        if not text:
            logger.warning(f"{log_prefix} No text extracted after processing from {html_file_path if html_file_path else 'direct content'}. Resulting file will be empty or placeholder.")
            # 빈 텍스트도 파일로 저장하고 다음 단계로 넘길 수 있도록 처리 (필요시)
            # 또는 여기서 특정 오류를 발생시킬 수도 있음. 현재는 경고 후 진행.
        
        logs_dir = "logs"
        logger.debug(f"{log_prefix} Ensuring logs directory exists: {logs_dir}")
        os.makedirs(logs_dir, exist_ok=True)
        
        logger.debug(f"{log_prefix} Sanitizing filename. Original html_file_path info for naming: {html_file_path if html_file_path else base_html_fn_for_saving}")
        # base_html_fn은 위에서 이미 계산됨 (base_html_fn_for_saving 사용)
        # base_html_fn = os.path.splitext(os.path.basename(html_file_path))[0]
        # logger.debug(f"{log_prefix} base_html_fn (splitext): {base_html_fn}")
        # base_html_fn = re.sub(r'_raw_html_[a-f0-9]{8}$', '', base_html_fn) # _raw_html_xxxxxxx 부분 제거
        # logger.debug(f"{log_prefix} base_html_fn (after re.sub): {base_html_fn}")
        
        unique_text_fn_stem = f"{base_html_fn_for_saving}_extracted_text"
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
                             "html_file_path": html_file_path, # 로깅/추적용으로 유지
                             "extracted_text": text # 추출된 텍스트 직접 전달
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_2: {result_to_return.keys()}, extracted_text length: {len(text)}")
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
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result_keys: {list(prev_result.keys()) if isinstance(prev_result, dict) else type(prev_result)} ----------")

    if not isinstance(prev_result, dict) or "extracted_text" not in prev_result:
        error_msg = f"Invalid or incomplete prev_result: {prev_result}. Expected a dict with 'extracted_text'."
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 오류: 이전 단계 결과 형식 오류 ('extracted_text' 누락)", status=states.FAILURE, error_info={'error': error_msg})
        raise ValueError(error_msg)
        
    raw_text_file_path = prev_result.get("text_file_path") # 파일명 생성 및 로깅용
    original_url = prev_result.get("original_url", "N/A")
    html_file_path = prev_result.get("html_file_path") # 로깅/추적용
    extracted_text = prev_result.get("extracted_text") # 실제 내용

    if not extracted_text:
        error_msg = f"Extracted text is missing from previous step result: {prev_result.keys()}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(chain_log_id, f"({step_log_id}) 이전 단계 텍스트 내용 없음", status=states.FAILURE, error_info={'error': error_msg})
        raise ValueError(error_msg)

    # raw_text_file_path는 파일 저장 시 이름 기반으로 사용될 수 있으므로 유효성 검사 또는 생성 로직 필요
    if not raw_text_file_path or not isinstance(raw_text_file_path, str):
        logger.warning(f"{log_prefix} raw_text_file_path is invalid ({raw_text_file_path}). Will use placeholder for saving filtered file name.")
        base_text_fn_for_saving = sanitize_filename(original_url if original_url != "N/A" else "unknown_source_text", ensure_unique=False) + f"_{chain_log_id[:8]}"
    else:
        base_text_fn_for_saving = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")

    logger.info(f"{log_prefix} Starting LLM filtering for text (length: {len(extracted_text)}). Associated raw_text_file_path for logging: {raw_text_file_path}")
    _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 채용공고 필터링 시작", details={'current_task_id': task_id})

    filtered_text_file_path = None
    raw_text = extracted_text # 파일에서 읽는 대신 직접 사용
    try:
        # 이전의 파일 읽기 로직은 제거합니다.
        # logger.debug(f"{log_prefix} Reading raw text from: {raw_text_file_path}")
        # with open(raw_text_file_path, "r", encoding="utf-8") as f:
        #     raw_text = f.read()
        logger.debug(f"{log_prefix} Raw text from prev_result (length: {len(raw_text)}). Raw text (first 500 chars): {raw_text[:500]}")

        if not raw_text.strip():
            logger.warning(f"{log_prefix} Text file {raw_text_file_path} is empty. Saving as empty filtered file.")
            filtered_content = "<!-- 원본 텍스트 내용 없음 -->"
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error(f"{log_prefix} GROQ_API_KEY not set.")
                _update_root_task_state(chain_log_id, f"({step_log_id}) API 키 없음 (GROQ_API_KEY)", status=states.FAILURE, error_info={'error': 'GROQ_API_KEY not set'})
                raise ValueError("GROQ_API_KEY is not configured.")

            # 중요: LLM 모델은 아래 명시된 모델을 사용해야 합니다. 변경하지 마십시오.
            llm_model = os.getenv("GROQ_LLM_MODEL", "meta-llama/llama-4-maverick-17b-128e-instruct") 
            logger.info(f"{log_prefix} Using LLM: {llm_model} via Groq.")
            logger.debug(f"{log_prefix} GROQ_API_KEY: {'*' * (len(groq_api_key) - 4) + groq_api_key[-4:] if groq_api_key else 'Not Set'}") # API 키 일부 마스킹
            
            chat = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=llm_model)
            logger.debug(f"{log_prefix} ChatGroq client initialized: {chat}")

            # 시스템 프롬프트: LLM에게 채용공고 텍스트에서 핵심 내용만 추출하도록 지시 (한국어)
            sys_prompt = ("당신은 전문적인 텍스트 처리 도우미입니다. 당신의 임무는 제공된 텍스트에서 핵심 채용공고 내용만 추출하는 것입니다. "
                          "광고, 회사 홍보, 탐색 링크, 사이드바, 헤더, 푸터, 법적 고지, 쿠키 알림, 관련 없는 기사 등 직무의 책임, 자격, 혜택과 직접적인 관련이 없는 모든 불필요한 정보는 제거하십시오. "
                          "결과는 깨끗하고 읽기 쉬운 일반 텍스트로 제시해야 합니다. 마크다운 형식을 사용하지 마십시오. 실제 채용 내용에 집중하십시오. "
                          "만약 텍스트가 채용공고가 아닌 것 같거나, 의미 있는 채용 정보를 추출하기에 너무 손상된 경우, 정확히 '추출할 채용공고 내용 없음' 이라는 문구로 응답하고 다른 내용은 포함하지 마십시오. " # 한국어 응답 강제 추가
                          "모든 응답은 반드시 한국어로 작성되어야 합니다.") # 한국어 응답 강제 명시
            human_template = "{text_content}"
            prompt = ChatPromptTemplate.from_messages([("system", sys_prompt), ("human", human_template)])
            parser = StrOutputParser()
            llm_chain = prompt | chat | parser
            logger.debug(f"{log_prefix} LLM chain constructed: {llm_chain}")

            logger.info(f"{log_prefix} Preparing to invoke LLM. Original text length: {len(raw_text)}")
            MAX_LLM_INPUT_LEN = 24000 
            text_for_llm = raw_text
            if len(raw_text) > MAX_LLM_INPUT_LEN:
                logger.warning(f"{log_prefix} Text length ({len(raw_text)}) > limit ({MAX_LLM_INPUT_LEN}). Truncating.")
                text_for_llm = raw_text[:MAX_LLM_INPUT_LEN]
                _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 입력 텍스트 일부 사용 (길이 초과)", 
                                        details={'original_len': len(raw_text), 'truncated_len': len(text_for_llm)})
            
            logger.info(f"{log_prefix} Text length for LLM: {len(text_for_llm)}")
            logger.debug(f"{log_prefix} Text for LLM (first 500 chars): {text_for_llm[:500]}")

            try:
                logger.info(f"{log_prefix} >>> Attempting llm_chain.invoke NOW...")
                start_time_llm_invoke = time.time()
                filtered_content = llm_chain.invoke({"text_content": text_for_llm})
                end_time_llm_invoke = time.time()
                duration_llm_invoke = end_time_llm_invoke - start_time_llm_invoke
                logger.info(f"{log_prefix} <<< llm_chain.invoke completed. Duration: {duration_llm_invoke:.2f} seconds.")
                logger.info(f"{log_prefix} LLM filtering complete. Output length: {len(filtered_content)}")
                logger.debug(f"{log_prefix} Filtered content (first 500 chars): {filtered_content[:500]}")
            except Exception as e_llm_invoke:
                logger.error(f"{log_prefix} !!! EXCEPTION during llm_chain.invoke: {type(e_llm_invoke).__name__} - {str(e_llm_invoke)}", exc_info=True)
                # 예외 발생 시, 현재 작업 및 루트 작업 상태를 실패로 업데이트하고 예외를 다시 발생시켜 Celery가 처리하도록 함.
                # 또는 여기서 특정 오류 메시지를 포함한 결과로 바로 반환할 수도 있음.
                # 현재는 전역 예외 처리 로직으로 넘기기 위해 raise.
                err_details_invoke = {'error': str(e_llm_invoke), 'type': type(e_llm_invoke).__name__, 'traceback': traceback.format_exc(), 'context': 'llm_chain.invoke'}
                _update_root_task_state(chain_log_id, f"({step_log_id}) LLM 호출 실패", status=states.FAILURE, error_info=err_details_invoke)
                raise # Celery가 이 태스크를 실패로 처리하고, 설정에 따라 재시도하거나 파이프라인을 중단하도록 함.

            if filtered_content.strip() == "추출할 채용공고 내용 없음":
                logger.warning(f"{log_prefix} LLM reported no extractable job content.")
                filtered_content = "<!-- LLM 분석: 추출할 채용공고 내용 없음 -->"

        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        # base_text_fn은 위에서 base_text_fn_for_saving으로 계산됨
        # base_text_fn = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")
        unique_filtered_fn = sanitize_filename(f"{base_text_fn_for_saving}_filtered_text", "txt", ensure_unique=True)
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
                             "llm_model_used_for_cv": "N/A",
                             "filtered_content": filtered_content # 필터링된 텍스트 직접 전달
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_3: {result_to_return.keys()}, filtered_content length: {len(filtered_content)}")
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
    """Celery 작업: 필터링된 텍스트와 사용자 프롬프트를 기반으로 자기소개서를 생성하고 저장합니다."""
    task_id = self.request.id
    root_task_id = chain_log_id # 체인 ID가 곧 루트 태스크 ID
    log_prefix = f"[Task {task_id} / Root {root_task_id} / Step 4_generate_cover_letter]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result: { {k: (v[:100] + '...' if isinstance(v, str) and len(v) > 100 else v) for k, v in prev_result.items()} }, User Prompt: {'Provided' if user_prompt_text else 'Not provided'} ----------")

    filtered_content = prev_result.get('filtered_content')
    original_url = prev_result.get('original_url', 'N/A')
    html_file_path = prev_result.get('html_file_path', 'N/A')
    raw_text_file_path = prev_result.get('raw_text_file_path', 'N/A')
    filtered_text_file_path = prev_result.get('filtered_text_file_path', 'N/A') # 로깅용

    if not filtered_content:
        error_message = "filtered_content is missing from previous result."
        logger.error(f"{log_prefix} {error_message}")
        # 이 단계에서 실패를 기록하고, 파이프라인의 최종 결과에 반영되도록 예외를 발생시킵니다.
        # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {error_message}", status=states.FAILURE, error_info={'error': error_message, 'details': 'Filtered content was not provided by step 3.'})
        raise ValueError(error_message)

    try:
        logger.info(f"{log_prefix} Starting cover letter generation. Filtered text length: {len(filtered_content)}, User prompt: {'Yes' if user_prompt_text else 'No'}. Associated filtered_text_file_path for logging: {filtered_text_file_path}")
        # _update_root_task_state(root_task_id, current_step_message="(4_generate_cover_letter) 자기소개서 생성 시작", details={'user_prompt': bool(user_prompt_text), 'current_task_id': task_id})

        # generate_cover_letter 함수는 (raw_cv_text, formatted_cv_text) 튜플을 반환합니다.
        # llm_model_used는 generate_cover_letter_semantic 내에서 고정되어 있거나 로깅되므로, 여기서 직접 받지 않습니다.
        start_time = time.monotonic()
        logger.info(f"{log_prefix} Calling LLM for cover letter. Text length: {len(filtered_content)}, Prompt length: {len(user_prompt_text) if user_prompt_text else 0}")
        
        raw_cv_text, formatted_cv_text = generate_cover_letter(
            job_posting_content=filtered_content,
            prompt=user_prompt_text
        )
        duration = time.monotonic() - start_time
        # llm_model_used 변수는 더 이상 여기서 할당되지 않으므로, pipeline_result 구성 시 직접 지정하거나 다른 방식으로 처리합니다.
        # 예시: llm_model_used = "meta-llama/llama-4-maverick-17b-128e-instruct" (실제 사용 모델)
        llm_model_used = "meta-llama/llama-4-maverick-17b-128e-instruct" # 또는 generate_cover_letter_semantic.py에서 가져올 수 있는 방법 모색
        logger.info(f"{log_prefix} LLM call for cover letter generation completed in {duration:.2f} seconds using {llm_model_used}.")

        if not raw_cv_text or not formatted_cv_text:
            error_message = "LLM generated an empty cover letter."
            logger.error(f"{log_prefix} {error_message}")
            # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {error_message}", status=states.FAILURE, error_info={'error': error_message, 'details': 'LLM returned empty content for cover letter.'})
            raise ValueError(error_message)

        logger.info(f"{log_prefix} Successfully unpacked cover letter data. Raw length: {len(raw_cv_text)}, Formatted length: {len(formatted_cv_text)}")
        
        # 파일 저장 시 고유성을 위해 필터링된 텍스트 파일명 일부와 새로운 UUID 일부 사용
        base_name_for_cv = sanitize_filename(original_url, extension="", ensure_unique=False)
        if filtered_text_file_path and os.path.exists(filtered_text_file_path):
            try:
                # logs/jobkorea.co.kr_recruit_gi_read_46819578_d929e756_filtered_text_a7312674.txt
                # -> d929e756_a7312674 부분 추출 시도
                parts = os.path.basename(filtered_text_file_path).split('_')
                if len(parts) > 3: # 충분한 부분이 있는지 확인
                    # 예: jobkorea.co.kr, recruit, gi, read, 46819578, d929e756, filtered, text, a7312674.txt
                    # 휴리스틱: 마지막에서 세 번째, 네 번째 부분 (확장자 제외하고)
                    unique_parts_from_filtered = "_".join(parts[-4:-2]) if parts[-1].endswith('.txt') else "_".join(parts[-3:-1])
                    base_name_for_cv += f"_{unique_parts_from_filtered}"
                    logger.debug(f"{log_prefix} Derived unique parts from filtered_text_file_path: {unique_parts_from_filtered}")
            except Exception as e_parse_name:
                logger.warning(f"{log_prefix} Could not parse unique parts from {filtered_text_file_path}: {e_parse_name}. Will use simpler unique name.")
                base_name_for_cv += f"_{uuid.uuid4().hex[:8]}" # Fallback
        else:
             base_name_for_cv += f"_{uuid.uuid4().hex[:8]}" # Fallback

        cover_letter_filename = sanitize_filename(f"{base_name_for_cv}_coverletter", extension="txt", ensure_unique=True)
        cover_letter_file_path = os.path.join("logs", cover_letter_filename)
        
        # 파일 저장 (formatted_cv_text 우선 사용, 없으면 raw_cv_text)
        text_to_save = formatted_cv_text if formatted_cv_text else raw_cv_text
        logger.info(f"{log_prefix} Using {'formatted_cv_text' if formatted_cv_text else 'raw_cv_text'} for saving. Length: {len(text_to_save)}")

        try:
            os.makedirs(os.path.dirname(cover_letter_file_path), exist_ok=True)
            with open(cover_letter_file_path, "w", encoding="utf-8") as f:
                f.write(text_to_save)
            logger.info(f"{log_prefix} Cover letter saved to: {cover_letter_file_path}")
        except IOError as e_io:
            error_message = f"Failed to save cover letter to file: {e_io}"
            logger.error(f"{log_prefix} {error_message}", exc_info=True)
            # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {error_message}", status=states.FAILURE, error_info={'error': error_message, 'file_path': cover_letter_file_path})
            raise # 예외를 다시 발생시켜 Celery가 처리하도록 함

        # 최종 결과 구성
        pipeline_result = {
            'status': 'SUCCESS', # 이 단계의 성공
            'message': 'Cover letter generated and saved successfully.',
            'cover_letter_file_path': cover_letter_file_path,
            'cover_letter_preview': (text_to_save[:200] + '...') if len(text_to_save) > 200 else text_to_save,
            'full_cover_letter_text': text_to_save, # 전체 자기소개서 텍스트
            'original_url': original_url,
            'llm_model_used_for_cv': llm_model_used,
            'intermediate_files': {
                'html': html_file_path,
                'raw_text': raw_text_file_path,
                'filtered_text': filtered_text_file_path
            }
        }
        
        # 이전 단계의 결과도 모두 포함하여 반환 (체인의 다음 단계나 결과 조회 시 유용)
        # final_output = {**prev_result, **pipeline_result} # prev_result와 pipeline_result 병합
        # 중요: prev_result의 'filtered_content'는 매우 클 수 있으므로, 최종 결과에서는 제외하는 것이 좋을 수 있음.
        #       또는 필요한 필드만 선택적으로 병합. 여기서는 pipeline_result만 반환하도록 단순화.
        #       만약 FastAPI에서 이전 단계의 모든 결과가 필요하다면, 그 때 다시 prev_result와 병합 고려.

        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        # logger.debug(f"{log_prefix} Final result for this step (for log): { {k: (v[:100] + '...' if isinstance(v, str) and len(v) > 100 else v) for k, v in pipeline_result.items()} }")
        return pipeline_result # 이 결과가 체인의 다음 단계로 전달되거나, 체인의 최종 결과가 됨.

    except ValueError as e_val: # 직접 발생시킨 예외
        logger.error(f"{log_prefix} ValueError in step 4: {e_val}", exc_info=True)
        # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {str(e_val)}", status=states.FAILURE, error_info={'error': str(e_val), 'type': 'ValueError'})
        self.update_state(state=states.FAILURE, meta={'error': str(e_val), 'step': '4_generate_cover_letter', 'type': 'ValueError', 'task_id': task_id, 'root_task_id': root_task_id})
        # 중요: 파이프라인의 일부로 실행될 때, 여기서 예외를 발생시키면 체인이 중단됨.
        #       이는 의도된 동작일 수 있음. propagate=True로 호출되면 예외가 전파됨.
        raise Reject(f"Step 4 failed due to ValueError: {e_val}", requeue=False)

    except MaxRetriesExceededError as e_max_retries:
        error_message = f"Max retries exceeded for LLM call: {e_max_retries}"
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {error_message}", status=states.FAILURE, error_info={'error': error_message, 'type': 'MaxRetriesExceededError'})
        self.update_state(state=states.FAILURE, meta={'error': error_message, 'step': '4_generate_cover_letter', 'type': 'MaxRetriesExceededError', 'task_id': task_id, 'root_task_id': root_task_id})
        raise Reject(f"Step 4 failed due to MaxRetriesExceededError: {e_max_retries}", requeue=False)
        
    except Exception as e_gen:
        error_message = f"Unexpected error in cover letter generation: {e_gen}"
        detailed_error_info = get_detailed_error_info(e_gen)
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        # _update_root_task_state(root_task_id, current_step_message=f"(4_generate_cover_letter) 실패: {error_message}", status=states.FAILURE, error_info={'error': error_message, 'type': str(type(e_gen).__name__), 'details': detailed_error_info})
        self.update_state(state=states.FAILURE, meta={'error': error_message, 'step': '4_generate_cover_letter', 'type': str(type(e_gen).__name__), 'details': detailed_error_info, 'task_id': task_id, 'root_task_id': root_task_id})
        # 일반적인 오류 발생 시에도 Reject를 사용하여 Celery가 실패로 처리하도록 함
        raise Reject(f"Step 4 failed due to an unexpected error: {e_gen}", requeue=False)
    finally:
        logger.info(f"{log_prefix} ---------- Task execution attempt ended. ----------")


@celery_app.task(bind=True, name='celery_tasks.process_job_posting_pipeline', max_retries=0)
def process_job_posting_pipeline(self, job_posting_url: str, user_prompt: Optional[str] = None) -> Dict[str, Any]: # 반환 타입을 명시적으로 Dict로 변경
    """전체 채용 공고 처리 파이프라인을 정의하고 실행합니다."""
    # 이 태스크 자체가 루트 태스크가 되므로, self.request.id가 루트 태스크 ID.
    root_task_id = self.request.id
    log_prefix = f"[PipelineTask {root_task_id}]"
    logger.info(f"{log_prefix} Pipeline initiated for URL: {job_posting_url}. User prompt: {'Provided' if user_prompt else 'Not provided'}")

    # 초기 상태 업데이트: 파이프라인 시작
    self.update_state(state='STARTED', meta={'current_step': '파이프라인 시작됨', 'job_posting_url': job_posting_url, 'root_task_id': root_task_id})

    try:
        # 파이프라인 정의
        pipeline = chain(
            step_1_extract_html.s(url=job_posting_url, chain_log_id=root_task_id),
            step_2_extract_text.s(chain_log_id=root_task_id),
            step_3_filter_content.s(chain_log_id=root_task_id),
            step_4_generate_cover_letter.s(chain_log_id=root_task_id, user_prompt_text=user_prompt)
        )
        
        logger.info(f"{log_prefix} Celery chain created. Executing pipeline and waiting for result.")
        # 체인을 비동기적으로 시작하고, 그 결과를 동기적으로 기다립니다.
        # .get()은 체인이 완료될 때까지 현재 태스크(process_job_posting_pipeline)를 블로킹합니다.
        # propagate=True는 체인 내에서 예외 발생 시 .get()이 해당 예외를 다시 발생시키도록 합니다.
        async_result_of_chain = pipeline.apply_async()
        
        # 여기서 get() 호출 시 타임아웃을 설정할 수도 있습니다. e.g., async_result_of_chain.get(timeout=300) # 5분
        # 타임아웃 없이 기다리면 체인이 완료될 때까지 블록됩니다.
        logger.info(f"{log_prefix} Waiting for chain (first task ID: {async_result_of_chain.id}) to complete...")
        final_result_from_chain = async_result_of_chain.get(propagate=True) # 예외 발생 시 전파
        
        logger.info(f"{log_prefix} Pipeline execution finished. Final result from chain received.")
        # logger.debug(f"{log_prefix} Final result details (for log): { {k: (v[:100] + '...' if isinstance(v, str) and len(v) > 100 else v) for k, v in final_result_from_chain.items()} }")

        if isinstance(final_result_from_chain, dict) and final_result_from_chain.get('status') == 'SUCCESS':
            logger.info(f"{log_prefix} Chain completed successfully. Updating pipeline task state to SUCCESS.")
            # 파이프라인 태스크의 최종 상태와 결과를 meta에 저장
            self.update_state(state=states.SUCCESS, meta=final_result_from_chain)
            return final_result_from_chain # 이 결과가 process_job_posting_pipeline 태스크의 result가 됨
        else:
            # 체인이 성공적으로 완료되었으나, 반환된 결과가 예상한 형식이 아닐 경우
            # 또는 step_4에서 'status': 'SUCCESS'를 포함하지 않은 경우
            error_message = "Pipeline chain completed, but the final result format was unexpected or indicated an issue."
            logger.error(f"{log_prefix} {error_message} Result: {final_result_from_chain}")
            # 최종 결과가 있더라도, 문제가 있다고 판단되면 FAILURE로 처리
            failure_meta = {
                'current_step': '파이프라인 완료 (결과 형식 오류)',
                'error': error_message,
                'details': str(final_result_from_chain), # 전체 결과 문자열로 저장
                'root_task_id': root_task_id,
                'job_posting_url': job_posting_url,
            }
            if isinstance(final_result_from_chain, dict): # 원래 결과의 일부를 유지할 수 있다면
                 failure_meta.update(final_result_from_chain) # 기존 정보에 에러 메시지 추가

            self.update_state(state=states.FAILURE, meta=failure_meta)
            return failure_meta # 실패 시에도 이 메타 정보를 반환 (result 필드에 저장됨)

    except Reject as e_reject: # 하위 태스크에서 Reject로 명시적으로 실패 처리한 경우
        error_message = f"Pipeline execution rejected by a sub-task: {e_reject.reason}"
        detailed_error_info = {'reason': e_reject.reason, 'requeue': e_reject.requeue}
        logger.error(f"{log_prefix} {error_message}", exc_info=False) # exc_info=False로 스택 트레이스 중복 방지
        
        failure_meta = {
            'current_step': '파이프라인 중단 (하위 작업 거부)',
            'error': error_message,
            'details': detailed_error_info,
            'root_task_id': root_task_id,
            'job_posting_url': job_posting_url,
        }
        self.update_state(state=states.FAILURE, meta=failure_meta)
        return failure_meta

    except Exception as e_pipeline:
        error_message = f"An unexpected error occurred in the pipeline execution: {e_pipeline}"
        detailed_error_info = get_detailed_error_info(e_pipeline)
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        
        failure_meta = {
            'current_step': '파이프라인 실행 중 예외 발생',
            'error': error_message,
            'type': str(type(e_pipeline).__name__),
            'details': detailed_error_info, # traceback 포함될 수 있음
            'root_task_id': root_task_id,
            'job_posting_url': job_posting_url,
        }
        self.update_state(state=states.FAILURE, meta=failure_meta)
        return failure_meta # 실패 시에도 이 메타 정보를 반환 (result 필드에 저장됨)
    finally:
        logger.info(f"{log_prefix} Pipeline task execution attempt concluded.")

# 예외 정보 추출 헬퍼 함수 (기존 정의 유지)
def get_detailed_error_info(exception_obj: Exception) -> Dict[str, str]:
    """예외 객체로부터 상세 정보를 추출합니다."""
    return {
        "error_type": type(exception_obj).__name__,
        "error_message": str(exception_obj),
        "traceback": traceback.format_exc()
    }