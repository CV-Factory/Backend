from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
import os
import re
from bs4 import BeautifulSoup, Comment
from urllib.parse import urljoin, urlparse
import hashlib
import time
from generate_cover_letter_semantic import generate_cover_letter
import uuid
from celery.exceptions import MaxRetriesExceededError
from dotenv import load_dotenv
import datetime
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from typing import Optional, Tuple, Dict, Any
from celery import chain, states
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
MAX_IFRAME_DEPTH = 3  # iframe 최대 재귀 깊이
IFRAME_LOAD_TIMEOUT = 20000  # iframe 로드 타임아웃 (밀리초)
ELEMENT_HANDLE_TIMEOUT = 15000 # element handle 가져오기 타임아웃 (밀리초)
PAGE_NAVIGATION_TIMEOUT = 180000 # 3분
DEFAULT_PAGE_TIMEOUT = 60000 # 1분

def sanitize_filename(url: str) -> str:
    """URL을 기반으로 짧고 안전한 파일 이름을 생성합니다."""
    try:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.replace('www.', '') # 'www.' 제거
        # 경로와 쿼리에서 일부 유의미한 부분을 추출 (필요에 따라 로직 수정)
        # 여기서는 간단히 경로와 쿼리 문자열을 합쳐 사용
        path_and_query = parsed_url.path + parsed_url.query
        
        # 고유성을 위한 짧은 해시값 생성 (URL 전체 사용)
        url_hash = hashlib.md5(url.encode('utf-8')).hexdigest()[:8] # URL 전체의 짧은 MD5 해시값
        
        # 도메인 + 유의미한 부분 (간단화) + 해시값 조합
        # 유의미한 부분은 경로/쿼리에서 비알파벳/숫자를 _로 바꾸고 짧게 자름
        sanitized_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', path_and_query)
        sanitized_part = '_'.join(part for part in sanitized_part.split('_') if part)[:50] # 각 부분 합쳐서 짧게

        # 최종 파일 이름 형식: domain_sanitizedpart_hash
        # 예: jobkorea_Recruit_GI_Read_hash.html
        if sanitized_part:
            base_name = f"{domain}_{sanitized_part}_{url_hash}"
        else:
            base_name = f"{domain}_{url_hash}"
            
        # 파일 이름 길이 제한은 유지 (더 짧게 생성되겠지만 안전을 위해)
        if len(base_name) > 150: # 기존 200에서 좀 더 줄임
            base_name = base_name[:150]
            
        logger.debug(f"Generated base filename for '{url}': {base_name}")
        return base_name.lower() # 소문자로 변환

    except Exception as e:
        logger.error(f"Error generating filename for URL '{url}': {e}", exc_info=True)
        # 오류 발생 시 대체 파일명 사용 (타임스탬프 추가)
        timestamp = int(time.time())
        return f"error_filename_{timestamp}_{uuid.uuid4().hex[:4]}" # 고유성 강화

def _flatten_iframes_in_live_dom(current_playwright_context, 
                                 current_depth: int,
                                 max_depth: int,
                                 original_page_url_for_logging: str,
                                 chain_log_id: str, 
                                 step_log_id: str):
    """
    현재 Playwright 컨텍스트(페이지 또는 프레임) 내의 iframe들을 재귀적으로 평탄화합니다.
    iframe의 내용을 가져와 원래 iframe 태그를 DOM에서 교체합니다.
    """
    log_prefix = f"[Util / Root {chain_log_id} / Step {step_log_id} / FlattenIframe / Depth {current_depth}]"
    if current_depth > max_depth:
        logger.warning(f"{log_prefix} Max iframe depth {max_depth} reached for {original_page_url_for_logging}. Stopping recursion.")
        return

    processed_iframe_count_at_this_level = 0
    while True:
        iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])').first
        
        try:
            if iframe_locator.count() == 0:
                logger.info(f"{log_prefix} No more processable iframes found.")
                break
        except Exception as e_count:
            logger.warning(f"{log_prefix} Error checking iframe count: {e_count}. Assuming no iframes.", exc_info=True)
            break

        iframe_handle = None
        iframe_unique_id_for_log = f"iframe-{uuid.uuid4().hex[:6]}"
        try:
            iframe_handle = iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"{log_prefix} Could not get element_handle for an iframe. Marking locator as processed and breaking.")
                try: iframe_locator.evaluate("el => el.setAttribute('data-cvf-processed', 'true')")
                except: pass
                break
            
            iframe_src_for_log = iframe_handle.get_attribute('src') or "[src not found]"
            iframe_unique_id_from_attr = iframe_handle.get_attribute('id')
            if iframe_unique_id_from_attr: iframe_unique_id_for_log = iframe_unique_id_from_attr
            else: 
                try: iframe_handle.evaluate("(el, id) => el.id = id", iframe_unique_id_for_log)
                except: pass
            processed_iframe_count_at_this_level += 1
            logger.info(f"{log_prefix} Processing iframe #{processed_iframe_count_at_this_level} (id: {iframe_unique_id_for_log}, src: {iframe_src_for_log[:100]}).")
            try: iframe_handle.evaluate("el => el.setAttribute('data-cvf-processing', 'true')")
            except Exception as e_mark_processing: logger.warning(f"{log_prefix} Failed to mark iframe {iframe_unique_id_for_log} as 'processing': {e_mark_processing}", exc_info=True)

            child_frame = None
            try: child_frame = iframe_handle.content_frame()
            except Exception as e_content_frame: 
                logger.warning(f"{log_prefix} Could not get content_frame for {iframe_unique_id_for_log}: {e_content_frame}. Marking error.", exc_info=True)
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except: pass
                continue
            if not child_frame:
                logger.warning(f"{log_prefix} content_frame is None for {iframe_unique_id_for_log}. Marking error.")
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except: pass
                continue
            try:
                logger.info(f"{log_prefix} Waiting for child_frame (src: {iframe_src_for_log[:100]}, frame_url: {child_frame.url}) to load...")
                child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT)
                logger.info(f"{log_prefix} Child_frame loaded.")
            except Exception as frame_load_err:
                logger.error(f"{log_prefix} Timeout/error for child_frame (src: {iframe_src_for_log[:100]}): {frame_load_err}", exc_info=True)
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except: pass
                continue

            _flatten_iframes_in_live_dom(child_frame, current_depth + 1, max_depth, original_page_url_for_logging, chain_log_id, step_log_id)
            
            child_frame_html_content = ""
            try:
                logger.info(f"{log_prefix} Getting content from child_frame (src: {iframe_src_for_log[:100]}) after recursive flattening.")
                child_frame_html_content = child_frame.content()
                if not child_frame_html_content: 
                     logger.warning(f"{log_prefix} child_frame.content() empty for {iframe_src_for_log[:100]}.")
                     child_frame_html_content = "<!-- iframe content was empty or inaccessible -->"
            except Exception as frame_content_err:
                logger.error(f"{log_prefix} Error getting content from child_frame (src: {iframe_src_for_log[:100]}): {frame_content_err}", exc_info=True)
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except: pass
                continue 
            replacement_html_string = ""
            try:
                child_soup = BeautifulSoup(child_frame_html_content, 'html.parser')
                content_to_insert_bs = child_soup.body if child_soup.body else child_soup
                wrapper_div_start = f"""<div class="cvf-iframe-content-wrapper" data-original-src="{iframe_src_for_log[:250]}" data-iframe-depth="{current_depth + 1}" data-iframe-id="{iframe_unique_id_for_log}">"""
                wrapper_div_end = "</div>"
                inner_html = content_to_insert_bs.decode_contents() if content_to_insert_bs else f"<!-- Parsed iframe (id: {iframe_unique_id_for_log}, src: {iframe_src_for_log[:200]}) content empty -->"
                replacement_html_string = wrapper_div_start + inner_html + wrapper_div_end
            except Exception as bs_parse_err:
                logger.error(f"{log_prefix} Error parsing child frame (src: {iframe_src_for_log[:100]}): {bs_parse_err}", exc_info=True)
                replacement_html_string = f"""<div class="cvf-iframe-content-wrapper cvf-error" data-original-src="{iframe_src_for_log[:250]}" data-iframe-id="{iframe_unique_id_for_log}">
<!-- Error processing (id: {iframe_unique_id_for_log}, src: {iframe_src_for_log[:200]}): {str(bs_parse_err)}. Raw: {child_frame_html_content[:200]} -->
</div>"""
            try:
                logger.info(f"{log_prefix} Replacing iframe {iframe_unique_id_for_log} (src: {iframe_src_for_log[:100]}).")
                iframe_handle.evaluate("function(el, html) { el.outerHTML = html; }", replacement_html_string)
                logger.info(f"{log_prefix} Replaced iframe {iframe_unique_id_for_log}.")
            except Exception as eval_error:
                logger.error(f"{log_prefix} Failed to replace iframe {iframe_unique_id_for_log}: {eval_error}", exc_info=True)
                try: 
                    error_marking_locator = current_playwright_context.locator(f'iframe[id="{iframe_unique_id_for_log}"][data-cvf-processing="true"]')
                    if error_marking_locator.count() == 1:
                         error_marking_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                         logger.info(f"{log_prefix} Marked iframe {iframe_unique_id_for_log} as error after replacement failure.")
                    else: logger.warning(f"{log_prefix} Could not find iframe {iframe_unique_id_for_log} to mark as error.")
                except Exception as mark_err: logger.error(f"{log_prefix} Further error marking iframe {iframe_unique_id_for_log} as error: {mark_err}", exc_info=True)
        except Exception as e_outer_loop:
            logger.error(f"{log_prefix} Outer error for iframe (src: {iframe_src_for_log if 'iframe_src_for_log' in locals() else 'N/A'}): {e_outer_loop}", exc_info=True)
            if iframe_handle:
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except Exception as e_mark_final: logger.warning(f"{log_prefix} Failed to mark iframe error in outer exception: {e_mark_final}")
            logger.warning(f"{log_prefix} Continuing to next iframe after error.")
            continue
        finally:
            if iframe_handle:
                try: iframe_handle.dispose()
                except Exception as e_dispose: logger.warning(f"{log_prefix} Error disposing iframe handle (src: {iframe_src_for_log if 'iframe_src_for_log' in locals() else 'N/A'}): {e_dispose}", exc_info=True)
    logger.info(f"{log_prefix} Finished iframe processing attempts at this depth. Processed {processed_iframe_count_at_this_level} iframe(s).")

@celery_app.task(bind=True, name='celery_tasks.extract_body_html_from_url')
def extract_body_html_from_url(self, url: str, chain_log_id: str, step_log_id: str) -> str:
    """
    Playwright를 사용하여 지정된 URL의 <body> 내부 전체 HTML을 가져와 파일에 저장합니다.
    iframe 내부 컨텐츠를 재귀적으로 파싱하여 부모 HTML에 통합합니다.
    파일 저장 후, 저장된 파일의 절대 경로를 반환합니다.
    """
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id} / ExtractBodyHTML]"
    logger.info(f"{log_prefix} Attempting to extract body HTML from URL: {url} with iframe processing.")
    
    try:
        initial_state = states.STARTED if step_log_id == "1_extract_html" else states.PROGRESS
        celery_app.backend.store_result(chain_log_id, None, initial_state, 
                                        meta={'current_step': f'({step_log_id}) HTML 본문 추출 중', 'url': url, 'details': f'Task {task_id} running'})
    except Exception as e_update:
        logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state: {e_update}")

    browser = None
    saved_file_path = None 
    page = None
    try:
        with sync_playwright() as p_instance:
            try:
                logger.info(f"{log_prefix} Launching Chromium browser.")
                browser_args = [
                    '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', 
                    '--disable-gpu', '--blink-settings=imagesEnabled=false'
                ]
                if os.getenv("IN_DOCKER_CONTAINER"):
                    browser = p_instance.chromium.launch(headless=True, args=browser_args)
                else:
                    browser = p_instance.chromium.launch(headless=True, args=browser_args)
                logger.info(f"{log_prefix} Playwright Chromium browser launched: {browser.version}")
            except Exception as browser_launch_error:
                logger.error(f"{log_prefix} Failed to launch Playwright browser: {browser_launch_error}", exc_info=True)
                raise ConnectionError(f"Failed to launch Playwright browser: {browser_launch_error}") from browser_launch_error

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                java_script_enabled=True,
            )
            logger.info(f"{log_prefix} Browser context created.")
            page = context.new_page()
            page.set_default_navigation_timeout(PAGE_NAVIGATION_TIMEOUT) 
            page.set_default_timeout(DEFAULT_PAGE_TIMEOUT)
            logger.info(f"{log_prefix} New page created.")
            
            logger.info(f"{log_prefix} Navigating to URL: {url}")
            page.goto(url, wait_until='domcontentloaded')
            logger.info(f"{log_prefix} Page loaded. Current URL: {page.url}")

            logger.info(f"{log_prefix} Flattening iframes (max_depth={MAX_IFRAME_DEPTH})...")
            _flatten_iframes_in_live_dom(page, 0, MAX_IFRAME_DEPTH, url, chain_log_id, step_log_id)
            logger.info(f"{log_prefix} Finished iframe flattening.")

            logger.info(f"{log_prefix} Extracting full page HTML content...")
            full_html_content = page.content()
            logger.info(f"{log_prefix} Full HTML content extracted. Length: {len(full_html_content)} characters.")

            base_filename = sanitize_filename(url)
            unique_id_for_file = uuid.uuid4().hex[:8]
            output_filename = f"{base_filename}_full_page_{unique_id_for_file}.html"
            
            current_dir = os.path.dirname(os.path.abspath(__file__))
            logs_dir = os.path.join(current_dir, "logs") 
            os.makedirs(logs_dir, exist_ok=True)
            saved_file_path = os.path.join(logs_dir, output_filename)

            logger.info(f"{log_prefix} Saving extracted HTML to: {saved_file_path}")
            with open(saved_file_path, "w", encoding="utf-8") as f:
                f.write(full_html_content)
            logger.info(f"{log_prefix} Successfully saved HTML to {saved_file_path}")
            
            try:
                celery_app.backend.store_result(chain_log_id, None, states.PROGRESS,
                                                meta={'current_step': f'({step_log_id}) HTML 본문 추출 완료', 'output_file': saved_file_path, 'details': f'Task {task_id} success'})
            except Exception as e_update:
                logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on success: {e_update}")

            return saved_file_path

    except Exception as e:
        detailed_error = traceback.format_exc()
        logger.error(f"{log_prefix} Error during HTML extraction for URL {url}: {e}\n{detailed_error}", exc_info=True)
        try:
            celery_app.backend.store_result(chain_log_id, None, states.FAILURE,
                                            meta={'current_step': f'({step_log_id}) HTML 본문 추출 실패', 'error': str(e), 'input_url': url, 'details': f'Task {task_id} failed', 'traceback': detailed_error})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on error: {e_update}")
        raise 
    finally:
        if page:
            try: page.close()
            except Exception as e_page_close: logger.warning(f"{log_prefix} Error closing page: {e_page_close}", exc_info=True)
        if browser:
            try:
                logger.info(f"{log_prefix} Closing browser.")
                browser.close()
                logger.info(f"{log_prefix} Browser closed.")
            except Exception as e_close:
                logger.error(f"{log_prefix} Error closing browser: {e_close}", exc_info=True)

@celery_app.task(name='celery_tasks.open_url_with_playwright_inspector')
def open_url_with_playwright_inspector(url: str):
    """
    Playwright를 사용하여 지정된 URL을 열고 Playwright Inspector를 실행합니다.
    """
    log_prefix = "[PlaywrightInspector]"
    logger.info(f"{log_prefix} Attempting to open URL with Playwright Inspector: {url}")
    browser = None
    page = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False)
                logger.info(f"{log_prefix} Playwright Chromium browser launched: {browser.version}")
            except Exception as browser_launch_error:
                logger.error(f"{log_prefix} Failed to launch Playwright Chromium browser: {browser_launch_error}", exc_info=True)
                try:
                    logger.info(f"{log_prefix} Attempting to launch Firefox as a fallback.")
                    browser = p.firefox.launch(headless=False)
                    logger.info(f"{log_prefix} Playwright Firefox browser launched: {browser.version}")
                except Exception as firefox_launch_error:
                    logger.error(f"{log_prefix} Failed to launch Playwright Firefox browser: {firefox_launch_error}", exc_info=True)
                    raise ConnectionError(f"Failed to launch any Playwright browser. Last error (Firefox): {firefox_launch_error}") from firefox_launch_error
            
            context = browser.new_context()
            page = context.new_page()
            logger.info(f"{log_prefix} New page created. Navigating to URL: {url}")
            page.goto(url, timeout=180000) 
            logger.info(f"{log_prefix} Successfully navigated to URL: {url}")
            
            logger.info(f"{log_prefix} Opening Playwright Inspector. Execution will pause here until the Inspector is closed.")
            page.pause() 
            
            logger.info(f"{log_prefix} Playwright Inspector closed by user.")
            return f"Successfully opened {url} and closed Playwright Inspector."

    except ConnectionError as conn_err: 
        logger.error(f"{log_prefix} Playwright browser connection error: {conn_err}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"{log_prefix} An unexpected error occurred: {e}", exc_info=True)
        raise
    finally:
        if page:
            try: page.close() 
            except Exception as e_page_close: logger.warning(f"{log_prefix} Error closing page: {e_page_close}", exc_info=True)
        if browser:
            try: browser.close()
            except Exception as e_browser_close: logger.warning(f"{log_prefix} Error closing browser: {e_browser_close}", exc_info=True)
        logger.info(f"{log_prefix} Playwright Inspector task finished for URL: {url}")

@celery_app.task(bind=True, name='celery_tasks.extract_text_from_html_file', max_retries=3, default_retry_delay=10)
def extract_text_from_html_file(self, html_file_path: str, chain_log_id: str, step_log_id: str) -> str:
    """
    지정된 HTML 파일(*_full_page_*.html)을 읽어 <body> 태그 내부의 텍스트 컨텐츠만 추출하고,
    결과를 새 파일(*_extracted_*.txt)에 저장한 후, 그 파일의 경로를 반환합니다.
    """
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id} / ExtractText]"
    logger.info(f"{log_prefix} Starting text extraction from HTML file: {html_file_path}")

    try:
        celery_app.backend.store_result(chain_log_id, None, states.PROGRESS, 
                                        meta={'current_step': f'({step_log_id}) HTML 텍스트 추출 중', 'input_file': html_file_path, 'details': f'Task {task_id} running'})
    except Exception as e_update:
        logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state: {e_update}")

    if not html_file_path or not os.path.exists(html_file_path):
        error_msg = f"HTML file not found at path: {html_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        try:
            celery_app.backend.store_result(chain_log_id, None, states.FAILURE,
                                            meta={'current_step': f'({step_log_id}) HTML 텍스트 추출 오류', 'error': error_msg, 'input_file': html_file_path, 'details': f'Task {task_id} failed', 'traceback': traceback.format_exc()})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on file not found: {e_update}")
        raise FileNotFoundError(error_msg)
    
    output_text_file_path = None
    try:
        base_name_with_ext = os.path.basename(html_file_path)
        base_name_no_ext = os.path.splitext(base_name_with_ext)[0]
        
        unique_part = ""
        core_name = base_name_no_ext
        
        full_page_marker = "_full_page_"
        if full_page_marker in base_name_no_ext:
            parts = base_name_no_ext.split(full_page_marker)
            core_name = parts[0]
            if len(parts) > 1 and parts[1]: 
                unique_part = "_" + parts[1] 
        
        text_file_name = f"{core_name}_extracted{unique_part}.txt"

        logs_dir = os.path.dirname(html_file_path) 
        output_text_file_path = os.path.join(logs_dir, text_file_name)
        
        logger.info(f"{log_prefix} Reading HTML file: {html_file_path}")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        
        logger.info(f"{log_prefix} Parsing HTML content with BeautifulSoup.")
        soup = BeautifulSoup(html_content, 'html.parser')

        for element in soup(["script", "style", "header", "footer", "nav", "aside", "form"]):
            element.decompose()
        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()
        
        target_element = soup.body if soup.body else soup
        if target_element:
            text_content = target_element.get_text(separator='\n', strip=True)
            text_content = re.sub(r'\n\s*\n+', '\n\n', text_content)
        else:
            logger.warning(f"{log_prefix} No body tag found in {html_file_path}. Using entire document for text extraction.")
            text_content = soup.get_text(separator='\n', strip=True)
            text_content = re.sub(r'\n\s*\n+', '\n\n', text_content)

        if not text_content.strip():
            logger.warning(f"{log_prefix} No significant text content extracted from {html_file_path}.")
            text_content = "<!-- No extractable text content -->"

        logger.info(f"{log_prefix} Saving extracted text to: {output_text_file_path} (Length: {len(text_content)})")
        with open(output_text_file_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        logger.info(f"{log_prefix} Successfully saved extracted text to {output_text_file_path}")

        try:
            celery_app.backend.store_result(chain_log_id, None, states.PROGRESS,
                                            meta={'current_step': f'({step_log_id}) HTML 텍스트 추출 완료', 'output_file': output_text_file_path, 'details': f'Task {task_id} success'})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on success: {e_update}")
        
        return output_text_file_path

    except Exception as e:
        detailed_error = traceback.format_exc()
        logger.error(f"{log_prefix} Error during text extraction from {html_file_path}: {e}\n{detailed_error}", exc_info=True)
        if output_text_file_path and os.path.exists(output_text_file_path):
            try: os.remove(output_text_file_path)
            except: pass
        try:
            celery_app.backend.store_result(chain_log_id, None, states.FAILURE,
                                            meta={'current_step': f'({step_log_id}) HTML 텍스트 추출 실패', 'error': str(e), 'input_file': html_file_path, 'details': f'Task {task_id} failed', 'traceback': detailed_error})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on error: {e_update}")
        raise

@celery_app.task(bind=True, name='celery_tasks.filter_job_posting_with_llm', max_retries=2, default_retry_delay=30)
def filter_job_posting_with_llm(self, raw_text_file_path: str, chain_log_id: str, step_log_id: str) -> str:
    """
    추출된 텍스트 파일(*_extracted_*.txt)을 읽어 LLM을 사용하여 채용 공고 내용을 필터링/요약하고,
    결과를 새 파일(*_filtered_*.txt)에 저장한 후, 그 파일의 경로를 반환합니다.
    """
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id} / LLMFilter]"
    logger.info(f"{log_prefix} Starting LLM filtering for text file: {raw_text_file_path}")

    try:
        celery_app.backend.store_result(chain_log_id, None, states.PROGRESS,
                                        meta={'current_step': f'({step_log_id}) LLM 채용공고 필터링 중', 'input_file': raw_text_file_path, 'details': f'Task {task_id} running'})
    except Exception as e_update:
        logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state: {e_update}")

    if not raw_text_file_path or not os.path.exists(raw_text_file_path):
        error_msg = f"Raw text file not found at path: {raw_text_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        try:
            celery_app.backend.store_result(chain_log_id, None, states.FAILURE,
                                            meta={'current_step': f'({step_log_id}) LLM 채용공고 필터링 오류', 'error': error_msg, 'input_file': raw_text_file_path, 'details': f'Task {task_id} failed', 'traceback': traceback.format_exc()})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on file not found: {e_update}")
        raise FileNotFoundError(error_msg)
    
    llm_filtered_file_path = None
    try:
        base_name_with_ext = os.path.basename(raw_text_file_path)
        base_name_no_ext = os.path.splitext(base_name_with_ext)[0]

        unique_part = ""
        core_name = base_name_no_ext

        extracted_marker = "_extracted"
        if base_name_no_ext.endswith(extracted_marker):
            core_name = base_name_no_ext[:-len(extracted_marker)]
        elif extracted_marker + "_" in base_name_no_ext:
            parts = base_name_no_ext.split(extracted_marker + "_")
            core_name = parts[0]
            if len(parts) > 1 and parts[1]:
                unique_part = "_" + parts[1]
        
        llm_filtered_file_name = f"{core_name}_filtered{unique_part}.txt"
        
        logs_dir = os.path.dirname(raw_text_file_path)
        llm_filtered_file_path = os.path.join(logs_dir, llm_filtered_file_name)
        
        with open(raw_text_file_path, "r", encoding="utf-8") as f:
            raw_text_content = f.read()
        
        if not raw_text_content.strip() or raw_text_content.strip() == "<!-- No extractable text content -->":
            logger.warning(f"{log_prefix} Raw text content for {raw_text_file_path} is empty or placeholder. Skipping LLM filtering.")
            filtered_text_content = "<!-- 원본 내용 없음 (LLM 필터링 건너뜀) -->"
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error(f"{log_prefix} CRITICAL: GROQ_API_KEY is NOT SET!")
                filtered_text_content = "GROQ_API_KEY not configured. LLM filtering skipped."
            else:
                logger.info(f"{log_prefix} Initializing Groq Chat LLM (llama-3.1-70b-versatile).")
                model = ChatGroq(
                    temperature=0, model_name="llama-3.1-70b-versatile", groq_api_key=groq_api_key
                )
                prompt_template_str = """당신은 텍스트에서 채용 공고 내용만 정확하게 추출하는 전문가입니다. 
주어진 텍스트에서 다른 모든 내용을 제거하고 오직 채용 공고와 직접적으로 관련된 내용만 남겨주세요.
만약 텍스트에 채용 공고 내용이 포함되어 있지 않거나, 너무 손상되어 채용 공고라고 보기 어렵다면, 다른 어떤 설명도 없이 정확히 '추출할 내용 없음' 이라고만 응답해주세요.
"채용 공고 내용은 다음과 같습니다:" 와 같은 도입부나 설명은 추가하지 마세요. 추출된 텍스트 또는 '추출할 내용 없음'만 제공하세요.

분석할 텍스트:
{text_input}
                            """
                prompt = ChatPromptTemplate.from_template(prompt_template_str)
                output_parser = StrOutputParser()
                llm_chain = prompt | model | output_parser
                
                logger.info(f"{log_prefix} Invoking Groq LLM chain for file: {raw_text_file_path}...")
                filtered_text_content = llm_chain.invoke({"text_input": raw_text_content})
                logger.info(f"{log_prefix} Groq LLM chain invocation complete. Output length: {len(filtered_text_content)}")

                if not filtered_text_content.strip() or filtered_text_content.strip() == "추출할 내용 없음":
                    logger.warning(f"{log_prefix} LLM returned empty or '추출할 내용 없음' response.")
                    filtered_text_content = "<!-- LLM 필터링 결과 내용 없음 -->"
                else:
                    logger.info(f"{log_prefix} Successfully filtered text using Groq LLM.")

        with open(llm_filtered_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_text_content)
        logger.info(f"{log_prefix} Successfully saved filtered text to {llm_filtered_file_path}")

        try:
            celery_app.backend.store_result(chain_log_id, None, states.PROGRESS,
                                            meta={'current_step': f'({step_log_id}) LLM 채용공고 필터링 완료', 'output_file': llm_filtered_file_path, 'details': f'Task {task_id} success'})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on success: {e_update}")

        return llm_filtered_file_path

    except Exception as e:
        detailed_error = traceback.format_exc()
        logger.error(f"{log_prefix} Error during LLM filtering for {raw_text_file_path}: {e}\n{detailed_error}", exc_info=True)
        if llm_filtered_file_path and os.path.exists(llm_filtered_file_path):
            try: os.remove(llm_filtered_file_path)
            except: pass
        try:
            celery_app.backend.store_result(chain_log_id, None, states.FAILURE,
                                            meta={'current_step': f'({step_log_id}) LLM 채용공고 필터링 실패', 'error': str(e), 'input_file': raw_text_file_path, 'details': f'Task {task_id} failed', 'traceback': detailed_error})
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on error: {e_update}")
        raise

@celery_app.task(bind=True, name='celery_tasks.generate_cover_letter_from_text', max_retries=2, default_retry_delay=30)
def generate_cover_letter_from_text(self, filtered_text_file_path: str, user_prompt: Optional[str], chain_log_id: str, step_log_id: str) -> Dict[str, Any]:
    """
    필터링된 텍스트 파일(*_filtered.txt)과 사용자 프롬프트를 기반으로 자기소개서를 생성합니다.
    성공 시, 생성된 자기소개서 및 관련 정보를 담은 딕셔너리를 반환하고, 루트 태스크의 상태를 SUCCESS로 업데이트하며 결과도 저장합니다.
    실패 시, 루트 태스크의 상태를 FAILURE로 업데이트합니다.
    """
    task_id = self.request.id
    log_prefix = f"[Task {task_id} / Root {chain_log_id} / Step {step_log_id} / GenerateCoverLetter]"
    logger.info(f"{log_prefix} Starting cover letter generation from file: {filtered_text_file_path}")

    try:
        celery_app.backend.store_result(chain_log_id, None, states.PROGRESS,
                                        meta={'current_step': f'({step_log_id}) 자기소개서 생성 중', 'input_file': filtered_text_file_path, 'user_prompt': user_prompt if user_prompt else "N/A", 'details': f'Task {task_id} running'})
    except Exception as e_update:
        logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state: {e_update}")

    if not filtered_text_file_path or not os.path.exists(filtered_text_file_path):
        error_msg = f"Filtered text file not found at path: {filtered_text_file_path}"
        logger.error(f"{log_prefix} {error_msg}")
        final_error_payload = {'current_step': f'({step_log_id}) 자기소개서 생성 오류', 'error': error_msg, 'input_file': filtered_text_file_path, 'details': f'Task {task_id} failed', 'traceback': traceback.format_exc()}
        try:
            celery_app.backend.store_result(chain_log_id, final_error_payload, states.FAILURE, meta=final_error_payload)
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on file not found: {e_update}")
        raise FileNotFoundError(error_msg)

    try:
        with open(filtered_text_file_path, "r", encoding="utf-8") as f:
            filtered_job_text = f.read()

        if not filtered_job_text.strip() or filtered_job_text.strip() == "<!-- LLM 필터링 결과 내용 없음 -->" or filtered_job_text.strip() == "<!-- 원본 내용 없음 (LLM 필터링 건너뜀) -->":
            logger.warning(f"{log_prefix} Filtered job text is empty or placeholder. Cannot generate cover letter.")
            result_data = {
                "cover_letter": "자기소개서 생성 불가: 채용 공고 내용이 부족합니다.",
                "job_details_summary": filtered_job_text,
                "status": "No content to process",
                "generated_at": datetime.datetime.now().isoformat()
            }
            try:
                celery_app.backend.store_result(chain_log_id, result_data, states.SUCCESS,
                                                meta={'current_step': f'({step_log_id}) 자기소개서 생성 완료 (내용 부족)', 'final_result': result_data, 'details': f'Task {task_id} completed with no content'})
            except Exception as e_update:
                logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on no content: {e_update}")
            return result_data

        logger.info(f"{log_prefix} Calling generate_cover_letter. User prompt: {repr(user_prompt) if user_prompt else 'Default prompt'}")
        
        generated_data = generate_cover_letter(
            job_post_text=filtered_job_text,
            user_instructions=user_prompt,
        )
        logger.info(f"{log_prefix} Cover letter generation finished by generate_cover_letter function.")

        result_data = {
            "cover_letter": generated_data.get("cover_letter", "자기소개서 생성 중 오류 발생 또는 내용 없음."),
            "job_details_summary": generated_data.get("job_summary", "요약 정보 없음."),
            "original_filtered_text_path": filtered_text_file_path,
            "status": "Success",
            "generated_at": datetime.datetime.now().isoformat(),
            "model_used": generated_data.get("model_name", "N/A")
        }
        
        try:
            celery_app.backend.store_result(chain_log_id, result_data, states.SUCCESS,
                                            meta={'current_step': f'({step_log_id}) 자기소개서 생성 완료', 'final_result_summary': result_data.get("cover_letter", "")[:200] + "...", 'details': f'Task {task_id} success'})
            logger.info(f"{log_prefix} Successfully updated root task {chain_log_id} as SUCCESS with final result.")
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on final success: {e_update}")

        return result_data

    except Exception as e:
        detailed_error = traceback.format_exc()
        logger.error(f"{log_prefix} Error during cover letter generation: {e}\n{detailed_error}", exc_info=True)
        final_error_payload = {'current_step': f'({step_log_id}) 자기소개서 생성 실패', 'error': str(e), 'input_file': filtered_text_file_path, 'details': f'Task {task_id} failed', 'traceback': detailed_error}
        try:
            celery_app.backend.store_result(chain_log_id, final_error_payload, states.FAILURE, meta=final_error_payload)
        except Exception as e_update:
            logger.warning(f"{log_prefix} Failed to update root task {chain_log_id} state on error: {e_update}")
        raise

@celery_app.task(bind=True, name='celery_tasks.process_job_posting_pipeline')
def process_job_posting_pipeline(self, url: str, user_prompt: Optional[str] = None) -> str:
    """
    전체 채용 공고 처리 파이프라인을 정의하고 실행합니다.
    루트 작업 ID를 반환하여 진행 상황을 추적할 수 있도록 합니다.
    """
    root_task_id = self.request.id
    logger.info(f"[Pipeline {root_task_id}] Starting job posting processing for URL: {url}")
    self.update_state(state=states.STARTED, meta={'current_step': 'Pipeline Schedulling', 'url': url, 'user_prompt': user_prompt if user_prompt else "N/A"})

    try:
        task_chain = (
            extract_body_html_from_url.s(url=url, chain_log_id=root_task_id, step_log_id="1_extract_html") |
            extract_text_from_html_file.s(chain_log_id=root_task_id, step_log_id="2_extract_text") |
            filter_job_posting_with_llm.s(chain_log_id=root_task_id, step_log_id="3_filter_content") |
            generate_cover_letter_from_text.s(user_prompt=user_prompt, chain_log_id=root_task_id, step_log_id="4_generate_cover_letter")
        )
        
        chain_result = task_chain.apply_async() 
        logger.info(f"[Pipeline {root_task_id}] Job posting processing chain initiated. First task ID in chain: {chain_result.id}")
        return root_task_id

    except Exception as e:
        detailed_error = traceback.format_exc()
        logger.error(f"[Pipeline {root_task_id}] Error creating or dispatching job posting processing chain: {e}\n{detailed_error}", exc_info=True)
        self.update_state(state=states.FAILURE, meta={'current_step': 'Pipeline Error', 'error': str(e), 'traceback': detailed_error})
        raise