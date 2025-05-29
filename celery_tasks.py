from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
import os
import re
from bs4 import BeautifulSoup, Comment
import uuid
import datetime
import hashlib
from celery.exceptions import MaxRetriesExceededError, Reject
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from typing import Optional, Dict, Any, Union
from celery import chain, signature, states
import traceback
from playwright.sync_api import Error as PlaywrightError
from celery.result import AsyncResult
from generate_cover_letter_semantic import generate_cover_letter

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

# 새로 생성된 유틸리티 모듈 임포트
from .playwright_utils import (_get_playwright_page_content_with_iframes_processed,
                               _flatten_iframes_in_live_dom_sync,
                               MAX_IFRAME_DEPTH,
                               IFRAME_LOAD_TIMEOUT,
                               ELEMENT_HANDLE_TIMEOUT,
                               PAGE_NAVIGATION_TIMEOUT,
                               DEFAULT_PAGE_TIMEOUT,
                               LOCATOR_DEFAULT_TIMEOUT,
                               GET_ATTRIBUTE_TIMEOUT,
                               EVALUATE_TIMEOUT_SHORT)
from .file_utils import sanitize_filename, try_format_log, MAX_FILENAME_LENGTH
from .celery_utils import _update_root_task_state, get_detailed_error_info


# ======================================================================================
# Step 1: HTML 추출
# ======================================================================================
@celery_app.task(bind=True, name='celery_tasks.step_1_extract_html', max_retries=1, default_retry_delay=10)
def step_1_extract_html(self, url: str, chain_log_id: str) -> Dict[str, str]:
    logger.info("GLOBAL_ENTRY_POINT: step_1_extract_html function called.")
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step 1_extract_html]"
    logger.info(f"{log_prefix} ---------- Task started. URL: {url} ----------")
    logger.debug(f"{log_prefix} Input URL: {url}, Chain Log ID: {chain_log_id}")

    _update_root_task_state(
        root_task_id=chain_log_id,
        state=states.STARTED,
        meta={
            'status_message': f"(1_extract_html) HTML 추출 시작: {url}",
            'current_task_id': str(task_id),
            'url_for_step1': url,
            'pipeline_step': 'EXTRACT_HTML_STARTED'
        }
    )

    html_file_path = ""
    try:
        logger.info(f"{log_prefix} Initializing Playwright...")
        with sync_playwright() as p:
            logger.info(f"{log_prefix} Playwright initialized. Launching browser...")
            try:
                browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'])
                logger.info(f"{log_prefix} Browser launched.")
            except Exception as e_browser:
                logger.error(f"{log_prefix} Error launching browser: {e_browser}", exc_info=True)
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE,
                    exc=e_browser,
                    traceback_str=traceback.format_exc(),
                    meta={
                        'status_message': "(1_extract_html) 브라우저 실행 실패",
                        'error_message': str(e_browser),
                        'url': url,
                        'current_task_id': str(task_id),
                        'pipeline_step': 'EXTRACT_HTML_BROWSER_LAUNCH_FAILED'
                    }
                )
                self.update_state(state=states.FAILURE, meta={'error': str(e_browser)})
                raise Reject(f"Browser launch failed: {e_browser}", requeue=False)

            try:
                page = browser.new_page()
                logger.info(f"{log_prefix} New page created. Setting default timeout to {DEFAULT_PAGE_TIMEOUT}ms.")
                page.set_default_timeout(DEFAULT_PAGE_TIMEOUT)
                page.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT)
                
                logger.info(f"{log_prefix} Navigating to URL: {url}")
                page.goto(url, wait_until="domcontentloaded")
                logger.info(f"{log_prefix} Successfully navigated to URL. Current page URL: {page.url}")

                logger.info(f"{log_prefix} iframe 처리 및 페이지 내용 가져오기 시작.")
                page_content = _get_playwright_page_content_with_iframes_processed(page, url, chain_log_id, str(task_id))
                logger.info(f"{log_prefix} 페이지 내용 가져오기 완료 (길이: {len(page_content)}).")

            except PlaywrightError as e_playwright:
                error_message = f"Playwright operation failed: {e_playwright}"
                logger.error(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE,
                    exc=e_playwright,
                    traceback_str=traceback.format_exc(),
                    meta={
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
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.FAILURE,
                    exc=e_general,
                    traceback_str=traceback.format_exc(),
                    meta={
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

        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED, 
            meta={
                'status_message': "(1_extract_html) HTML 추출 및 저장 완료",
                'html_file_path': html_file_path,
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_COMPLETED'
            }
        )
        logger.info(f"{log_prefix} ---------- Task finished successfully. Result for log: {try_format_log(result_data_for_log)} ----------")
        logger.debug(f"{log_prefix} Returning from step_1: keys={list(result_data.keys())}, page_content length: {len(result_data.get('page_content', '')) if result_data.get('page_content') else 0}")
        return result_data

    except Reject as e_reject:
        logger.warning(f"{log_prefix} Task explicitly rejected: {e_reject.reason}. Celery will handle retry/failure.")
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.FAILURE,
            exc=e_reject,
            traceback_str=getattr(e_reject, 'traceback', traceback.format_exc()),
            meta={
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
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.FAILURE,
            exc=e_max_retries,
            traceback_str=traceback.format_exc(),
            meta={
                'status_message': "(1_extract_html) 최대 재시도 초과",
                'error_message': error_message,
                'original_exception': str(e_max_retries),
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_MAX_RETRIES'
            }
        )
        # self.update_state(state=states.FAILURE, meta={'error': error_message, 'original_exception': str(e_max_retries)})
        # MaxRetriesExceededError는 Celery에 의해 자동으로 전파되므로, 여기서 다시 raise할 필요는 없을 수 있으나,
        # 명시적으로 체인을 중단시키고 싶다면 raise하는 것이 안전합니다.
        # 또는 특정 값을 반환하여 파이프라인의 다음 단계에서 이를 인지하도록 할 수 있습니다.
        # 여기서는 더 이상 진행되지 않도록 예외를 다시 발생시킵니다.
        raise

    except Exception as e_outer:
        # 이 블록은 Reject, MaxRetriesExceededError 외의 예외를 잡습니다.
        # 이미 try-except-finally 블록 내에서 대부분의 예외가 처리되어 Reject로 변환되었을 것입니다.
        # 그럼에도 불구하고 여기까지 온 예외는 매우 예기치 않은 상황일 수 있습니다.
        error_message = f"Outer catch-all error in step_1_extract_html: {e_outer}"
        logger.critical(f"{log_prefix} {error_message} (URL: {url})", exc_info=True)
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_outer, 
            traceback_str=traceback.format_exc(), 
            meta={
                'status_message': "(1_extract_html) 처리되지 않은 심각한 오류", 
                'error_message': error_message,
                'current_task_id': str(task_id),
                'pipeline_step': 'EXTRACT_HTML_CRITICAL_ERROR'
            }
        )
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            meta={'status_message': f"({step_log_id}) 오류: 이전 단계 결과 형식 오류", 'error': error_msg, 'current_task_id': task_id}
        )
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            meta={'status_message': f"({step_log_id}) 이전 단계 HTML 내용 없음", 'error': error_msg, 'current_task_id': task_id}
        )
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.STARTED,  # 파이프라인 진행 중
            meta={'status_message': f"({step_log_id}) HTML 내용에서 텍스트 추출 시작", 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_STARTED'}
        )

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
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED, # 파이프라인 진행 중
            meta={'status_message': f"({step_log_id}) 텍스트 파일 저장 완료", 'text_file_path': extracted_text_file_path, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_COMPLETED'}
        )
        
        result_to_return = {"text_file_path": extracted_text_file_path, 
                             "original_url": original_url, 
                             "html_file_path": html_file_path, # 로깅/추적용
                             "extracted_text": text # 추출된 텍스트 직접 전달
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_2: {result_to_return.keys()}, extracted_text length: {len(text)}")
        return result_to_return

    except FileNotFoundError as e_fnf:
        logger.error(f"{log_prefix} FileNotFoundError during text extraction: {e_fnf}. HTML file path: {html_file_path}", exc_info=True)
        err_details_fnf = {'error': str(e_fnf), 'type': type(e_fnf).__name__, 'html_file': str(html_file_path)}
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_fnf, 
            traceback_str=traceback.format_exc(), 
            meta={'status_message': f"({step_log_id}) 텍스트 추출 실패 (파일 없음)", **err_details_fnf, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'}
        )
        raise # Celery가 태스크를 실패로 처리하도록 함
    except IOError as e_io:
        logger.error(f"{log_prefix} IOError during text extraction: {e_io}. HTML file path: {html_file_path}", exc_info=True)
        err_details_io = {'error': str(e_io), 'type': type(e_io).__name__, 'html_file': str(html_file_path), 'traceback': traceback.format_exc()}
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_io, 
            traceback_str=traceback.format_exc(), 
            meta={'status_message': f"({step_log_id}) 텍스트 추출 실패 (IO 오류)", **err_details_io, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'}
        )
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_general, 
            traceback_str=traceback.format_exc(), 
            meta={'status_message': f"({step_log_id}) 텍스트 추출 중 알 수 없는 오류", **err_details_general, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'}
        )
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            meta={'status_message': f"({step_log_id}) 오류: 이전 단계 결과 형식 오류 ('extracted_text' 누락)", 'error': error_msg, 'current_task_id': task_id}
        )
        raise ValueError(error_msg)
        
    raw_text_file_path = prev_result.get("text_file_path")
    original_url = prev_result.get("original_url", "N/A")
    html_file_path = prev_result.get("html_file_path")
    extracted_text = prev_result.get("extracted_text")

    if not extracted_text:
        error_msg = f"Extracted text is missing from previous step result: {prev_result.keys()}"
        logger.error(f"{log_prefix} {error_msg}")
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            meta={'status_message': f"({step_log_id}) 이전 단계 텍스트 내용 없음", 'error': error_msg, 'current_task_id': task_id}
        )
        raise ValueError(error_msg)

    # raw_text_file_path는 파일 저장 시 이름 기반으로 사용될 수 있으므로 유효성 검사 또는 생성 로직 필요
    if not raw_text_file_path or not isinstance(raw_text_file_path, str):
        logger.warning(f"{log_prefix} raw_text_file_path is invalid ({raw_text_file_path}). Will use placeholder for saving filtered file name.")
        base_text_fn_for_saving = sanitize_filename(original_url if original_url != "N/A" else "unknown_source", ensure_unique=False) + f"_{chain_log_id[:8]}"
    else:
        base_text_fn_for_saving = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")

    logger.info(f"{log_prefix} Starting LLM filtering for text (length: {len(extracted_text)}). Associated raw_text_file_path for logging: {raw_text_file_path}")
    _update_root_task_state(
        root_task_id=chain_log_id, 
        state=states.STARTED,
        meta={'status_message': f"({step_log_id}) LLM 채용공고 필터링 시작", 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_STARTED'}
    )

    filtered_text_file_path = None
    raw_text = extracted_text
    try:
        if not raw_text.strip():
            logger.warning(f"{log_prefix} Text file {raw_text_file_path} is empty. Saving as empty filtered file.")
            filtered_content = "<!-- 원본 텍스트 내용 없음 -->"
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error(f"{log_prefix} GROQ_API_KEY not set.")
                _update_root_task_state(
                    root_task_id=chain_log_id, 
                    state=states.FAILURE, 
                    meta={'status_message': f"({step_log_id}) API 키 없음 (GROQ_API_KEY)", 'error': 'GROQ_API_KEY not set', 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_FAILED'}
                )
                raise ValueError("GROQ_API_KEY not configured.")

            llm_model = os.getenv("GROQ_LLM_MODEL", "meta-llama/llama-4-maverick-17b-128e-instruct") 
            logger.info(f"{log_prefix} Using LLM: {llm_model} via Groq.")
            logger.debug(f"{log_prefix} GROQ_API_KEY: {'*' * (len(groq_api_key) - 4) + groq_api_key[-4:] if groq_api_key else 'Not Set'}")
            
            chat = ChatGroq(temperature=0, groq_api_key=groq_api_key, model_name=llm_model)
            logger.debug(f"{log_prefix} ChatGroq client initialized: {chat}")

            sys_prompt = ("당신은 전문적인 텍스트 처리 도우미입니다. 당신의 임무는 제공된 텍스트에서 핵심 채용공고 내용만 추출하는 것입니다. "
                          "광고, 회사 홍보, 탐색 링크, 사이드바, 헤더, 푸터, 법적 고지, 쿠키 알림, 관련 없는 기사 등 직무의 책임, 자격, 혜택과 직접적인 관련이 없는 모든 불필요한 정보는 제거하십시오. "
                          "결과는 깨끗하고 읽기 쉬운 일반 텍스트로 제시해야 합니다. 마크다운 형식을 사용하지 마십시오. 실제 채용 내용에 집중하십시오. "
                          "만약 텍스트가 채용공고가 아닌 것 같거나, 의미 있는 채용 정보를 추출하기에 너무 손상된 경우, 정확히 '추출할 채용공고 내용 없음' 이라는 문구로 응답하고 다른 내용은 포함하지 마십시오. "
                          "모든 응답은 반드시 한국어로 작성되어야 합니다.")
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
                _update_root_task_state(
                    root_task_id=chain_log_id, 
                    state=states.STARTED,
                    meta={
                        'status_message': f"({step_log_id}) LLM 입력 텍스트 일부 사용 (길이 초과)", 
                        'original_len': len(raw_text), 
                        'truncated_len': len(text_for_llm),
                        'current_task_id': task_id,
                        'pipeline_step': 'CONTENT_FILTERING_INPUT_TRUNCATED'
                    }
                )
            
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
                err_details_invoke = {'error': str(e_llm_invoke), 'type': type(e_llm_invoke).__name__, 'traceback': traceback.format_exc(), 'context': 'llm_chain.invoke'}
                _update_root_task_state(
                    root_task_id=chain_log_id, 
                    state=states.FAILURE, 
                    exc=e_llm_invoke, 
                    traceback_str=traceback.format_exc(), 
                    meta={'status_message': f"({step_log_id}) LLM 호출 실패", **err_details_invoke, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_FAILED'}
                )
                raise

            if filtered_content.strip() == "추출할 채용공고 내용 없음":
                logger.warning(f"{log_prefix} LLM reported no extractable job content.")
                filtered_content = "<!-- LLM 분석: 추출할 채용공고 내용 없음 -->"

        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        unique_filtered_fn = sanitize_filename(f"{base_text_fn_for_saving}_filtered_text", "txt", ensure_unique=True)
        filtered_text_file_path = os.path.join(logs_dir, unique_filtered_fn)

        logger.debug(f"{log_prefix} Writing filtered content (length: {len(filtered_content)}) to: {filtered_text_file_path}")
        with open(filtered_text_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_content)
        logger.info(f"{log_prefix} Filtered text saved to: {filtered_text_file_path}")
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED,
            meta={
                'status_message': f"({step_log_id}) 필터링된 텍스트 파일 저장 완료", 
                'filtered_text_file_path': filtered_text_file_path, 
                'current_task_id': task_id, 
                'pipeline_step': 'CONTENT_FILTERING_COMPLETED'
            }
        )
        
        result_to_return = {"filtered_text_file_path": filtered_text_file_path, 
                             "original_url": original_url, 
                             "html_file_path": html_file_path,
                             "raw_text_file_path": raw_text_file_path,
                             "status_history": prev_result.get("status_history", []),
                             "cover_letter_preview": filtered_content[:500] + ("..." if len(filtered_content) > 500 else ""),
                             "llm_model_used_for_cv": "N/A",
                             "filtered_content": filtered_content
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
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e, 
            traceback_str=traceback.format_exc(), 
            meta={'status_message': f"({step_log_id}) LLM 필터링 실패", **err_details, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_FAILED'}
        )
        logger.error(f"{log_prefix} Root task {chain_log_id} updated with pipeline FAILURE status.")
        raise

@celery_app.task(bind=True, name='celery_tasks.step_4_generate_cover_letter', max_retries=1, default_retry_delay=20)
def step_4_generate_cover_letter(self, prev_result: Dict[str, Any], chain_log_id: str, user_prompt_text: Optional[str]) -> Dict[str, Any]:
    """Celery 작업: 필터링된 텍스트와 사용자 프롬프트를 기반으로 자기소개서를 생성하고 저장합니다."""
    task_id = self.request.id
    root_task_id = chain_log_id
    log_prefix = f"[Task {task_id} / Root {root_task_id} / Step 4_generate_cover_letter]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result: { {k: (v[:100] + '...' if isinstance(v, str) and len(v) > 100 else v) for k, v in prev_result.items()} }, User Prompt: {'Provided' if user_prompt_text else 'Not provided'} ----------")

    filtered_content = prev_result.get("filtered_content")
    original_url = prev_result.get("original_url", 'N/A')
    html_file_path = prev_result.get("html_file_path", 'N/A')
    raw_text_file_path = prev_result.get("raw_text_file_path", 'N/A')
    filtered_text_file_path = prev_result.get("filtered_text_file_path", 'N/A')

    if not filtered_content:
        error_message = "filtered_content is missing from previous result."
        logger.error(f"{log_prefix} {error_message}")
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            meta={
                'status_message': f"(4_generate_cover_letter) 실패: {error_message}", 
                'error': error_message, 
                'details': 'Filtered content was not provided by step 3.', 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise ValueError(error_message)

    try:
        logger.info(f"{log_prefix} Starting cover letter generation. Filtered text length: {len(filtered_content)}, User prompt: {'Yes' if user_prompt_text else 'No'}. Associated filtered_text_file_path for logging: {filtered_text_file_path}")
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.STARTED,
            meta={
                'status_message': "(4_generate_cover_letter) 자기소개서 생성 시작", 
                'user_prompt': bool(user_prompt_text), 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_STARTED'
            }
        )

        cover_letter_text = generate_cover_letter(
            job_posting_details=filtered_content,
            specific_requests=user_prompt_text,
            target_company=original_url,
        )
        
        if not cover_letter_text or "생성 실패" in cover_letter_text or len(cover_letter_text) < 50:
            logger.error(f"{log_prefix} 자기소개서 생성 실패 또는 유효하지 않은 결과. 응답: {try_format_log(cover_letter_text)}")
            _update_root_task_state(
                root_task_id=root_task_id, 
                state=states.FAILURE, 
                meta={
                    'status_message': f"(4_generate_cover_letter) 실패: {error_message}", 
                    'error': error_message, 
                    'details': 'LLM returned empty content for cover letter.', 
                    'current_task_id': task_id,
                    'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
                }
            )
            raise ValueError(error_message)

        logger.info(f"{log_prefix} 자기소개서 생성 성공 (길이: {len(cover_letter_text)})")

        cover_letter_file_path = os.path.join("logs", sanitize_filename(f"{original_url}_cover_letter", "txt", ensure_unique=True))
        with open(cover_letter_file_path, "w", encoding="utf-8") as f:
            f.write(cover_letter_text)
        logger.info(f"{log_prefix} 생성된 자기소개서 파일 저장 완료: {cover_letter_file_path}")

        final_result = {
            "page_title": original_url,
            "original_url": original_url,
            "cover_letter_text": cover_letter_text,
            "cover_letter_file_path": cover_letter_file_path,
            "chain_log_id": chain_log_id,
            "status_message": "자기소개서 생성 완료"
        }
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.SUCCESS,
            meta=final_result
        )
        
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        return final_result

    except ValueError as e_val:
        logger.error(f"{log_prefix} ValueError in step 4: {e_val}", exc_info=True)
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_val, 
            traceback_str=traceback.format_exc(), 
            meta={
                'status_message': f"(4_generate_cover_letter) 실패: {str(e_val)}", 
                'error': str(e_val), 
                'type': 'ValueError', 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise Reject(f"Step 4 failed due to ValueError: {e_val}", requeue=False)

    except MaxRetriesExceededError as e_max_retries:
        error_message = f"Max retries exceeded for LLM call: {e_max_retries}"
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_max_retries, 
            traceback_str=traceback.format_exc(), 
            meta={
                'status_message': f"(4_generate_cover_letter) 실패: {error_message}", 
                'error': error_message, 
                'type': 'MaxRetriesExceededError', 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise Reject(f"Step 4 failed due to MaxRetriesExceededError: {e_max_retries}", requeue=False)
        
    except Exception as e_gen:
        error_message = f"Unexpected error in cover letter generation: {e_gen}"
        detailed_error_info = get_detailed_error_info(e_gen)
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_gen, 
            traceback_str=traceback.format_exc(), 
            meta={
                'status_message': f"(4_generate_cover_letter) 실패: {error_message}", 
                'error': error_message, 
                'type': str(type(e_gen).__name__), 
                'details': detailed_error_info, 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise Reject(f"Step 4 failed due to an unexpected error: {e_gen}", requeue=False)
    finally:
        logger.info(f"{log_prefix} ---------- Task execution attempt ended. ----------")

@celery_app.task(bind=True, name="celery_tasks.handle_pipeline_completion")
def handle_pipeline_completion(self, result_or_request_obj, *, root_task_id: str, is_success: bool):
    log_prefix = f"[PipelineCompletion / Root {root_task_id} / Task {self.request.id[:4]}]"
    logger.info(f"{log_prefix} 파이프라인 완료 콜백 시작. Success: {is_success}, Result/Request: {try_format_log(result_or_request_obj, max_len=200)}")

    final_status_meta = {
        "pipeline_overall_status": "SUCCESS" if is_success else "FAILURE",
        "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "root_task_id": root_task_id,
    }

    if is_success:
        logger.info(f"{log_prefix} 파이프라인 성공적으로 완료. 결과: {try_format_log(result_or_request_obj)}")
        final_status_meta["final_output"] = result_or_request_obj
        final_status_meta["status_message"] = result_or_request_obj.get("status_message", "파이프라인 성공적으로 완료")
        
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.SUCCESS,
            meta=final_status_meta
        )
        logger.info(f"{log_prefix} Root task {root_task_id} 최종 상태 SUCCESS로 업데이트됨.")

    else:
        logger.error(f"{log_prefix} 파이프라인 실패로 완료됨. Request object (or error info): {try_format_log(result_or_request_obj)}")
        
        task_result = AsyncResult(root_task_id, app=celery_app)
        existing_meta = task_result.info if isinstance(task_result.info, dict) else {}
        
        error_details = {
            "status_message": "파이프라인 실패.",
            "error_source": "Unknown (check individual task logs or previous root task meta)",
        }

        if 'error' in existing_meta:
            error_details['status_message'] = existing_meta.get('status_message', '파이프라인 실패 (기존 에러 정보 존재)')
            error_details['error'] = existing_meta['error']
            error_details['current_step_at_failure'] = existing_meta.get('current_step')
        
        final_status_meta.update(error_details)
        
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_gen, 
            traceback_str=traceback.format_exc(), 
            meta=final_status_meta
        )
        logger.error(f"{log_prefix} Root task {root_task_id} 최종 상태 FAILURE로 업데이트됨 (콜백에 의해).")

    logger.info(f"{log_prefix} 파이프라인 완료 콜백 종료.")
    return { "callback_processed": True, "root_task_id": root_task_id, "final_status_reported": final_status_meta.get('pipeline_overall_status') }