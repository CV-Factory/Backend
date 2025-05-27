from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import hashlib
import time
from generate_cover_letter_semantic import generate_cover_letter
import uuid
from celery.exceptions import MaxRetriesExceededError
from dotenv import load_dotenv
import datetime # datetime 모듈 추가
from langchain_groq import ChatGroq # Groq import 추가
from langchain_core.prompts import ChatPromptTemplate # Langchain Prompt 추가
from langchain_core.output_parsers import StrOutputParser # Langchain Output Parser 추가
from typing import Optional # Optional 타입 어노테이션을 위해 추가

# 전역 로깅 레벨 및 라이브러리 로깅 레벨 조정
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("cohere").setLevel(logging.WARNING)

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
IFRAME_LOAD_TIMEOUT = 15000  # iframe 로드 타임아웃 (밀리초)
ELEMENT_HANDLE_TIMEOUT = 30000 # element handle 가져오기 타임아웃 (밀리초)

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
        sanitized_part = re.sub(r'[^a-zA-Z0-9]', '_', path_and_query)
        sanitized_part = '_'.join(part for part in sanitized_part.split('_') if part)[:30] # 각 부분 합쳐서 짧게

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
        return f"error_filename_{timestamp}"

def _flatten_iframes_in_live_dom(current_playwright_context, # Playwright Page 또는 Frame 객체
                                 current_depth: int,
                                 max_depth: int,
                                 original_page_url_for_logging: str):
    """
    현재 Playwright 컨텍스트(페이지 또는 프레임) 내의 iframe들을 재귀적으로 평탄화합니다.
    iframe의 내용을 가져와 원래 iframe 태그를 DOM에서 교체합니다.
    """
    if current_depth > max_depth:
        logger.warning(f"Max iframe depth {max_depth} reached for a frame within {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}). Stopping recursion for this branch.")
        return

    processed_iframe_count_at_this_level = 0
    while True:
        # 아직 처리되지 않았거나 오류로 표시되지 않은 iframe을 찾습니다.
        iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-error="true"])').first
        
        try:
            # 처리할 iframe이 더 있는지 확인합니다.
            if iframe_locator.count() == 0:
                logger.info(f"No more processable iframes found at depth {current_depth} for {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}).")
                break 
        except Exception as e:
            logger.warning(f"Error checking iframe count at depth {current_depth} for {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}): {e}. Assuming no iframes left.")
            break # 안전을 위해 현재 레벨의 루프 종료

        iframe_handle = None
        try:
            iframe_handle = iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"Located an iframe but could not get its element_handle at depth {current_depth} for {original_page_url_for_logging}. Breaking loop for this level.")
                break # 현재 레벨의 루프 종료

            processed_iframe_count_at_this_level += 1
            iframe_src_for_log = iframe_handle.get_attribute('src') or "[src not found]"
            logger.info(f"Processing iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) at depth {current_depth} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}).")

            child_frame = iframe_handle.content_frame()
            
            if not child_frame:
                logger.warning(f"Could not get content_frame for iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}). Marking and skipping.")
                iframe_handle.evaluate("el => el.setAttribute('data-cvf-error', 'true')")
                continue # 다음 while 루프 반복 (다른 iframe.first 찾기)

            # 자식 프레임 내부의 iframe들을 재귀적으로 처리
            _flatten_iframes_in_live_dom(child_frame, current_depth + 1, max_depth, original_page_url_for_logging)
            
            # 자식 프레임의 (이제는 평탄화된) 내용을 가져옵니다.
            child_frame_html_content = ""
            try:
                child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT)
                child_frame_html_content = child_frame.content()
            except Exception as frame_content_err:
                logger.error(f"Error getting content from child_frame (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}, depth: {current_depth + 1}): {frame_content_err}", exc_info=True)
                iframe_handle.evaluate("el => el.setAttribute('data-cvf-error', 'true')") # 부모 iframe 태그를 에러로 표시
                continue

            replacement_html_string = ""
            if not child_frame_html_content:
                logger.warning(f"Child frame (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}, depth: {current_depth + 1}) returned empty content.")
                replacement_html_string = f"<!-- Iframe (src: {iframe_src_for_log[:200]}) content was empty -->"
            else:
                try:
                    child_soup = BeautifulSoup(child_frame_html_content, 'html.parser')
                    content_to_insert_bs = child_soup.body if child_soup.body else child_soup
                    replacement_html_string = content_to_insert_bs.prettify() if content_to_insert_bs else f"<!-- Iframe (src: {iframe_src_for_log[:200]}) content could not be prettified -->"
                except Exception as bs_parse_err:
                    logger.error(f"Error parsing/prettifying child frame content (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}): {bs_parse_err}", exc_info=True)
                    replacement_html_string = f"<!-- Error processing iframe content (src: {iframe_src_for_log[:200]}): {str(bs_parse_err)}. Raw content snippet: {child_frame_html_content[:200]} -->"
            
            # 원래 iframe 태그를 DOM에서 교체합니다.
            try:
                logger.info(f"Attempting to replace iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) with its content at depth {current_depth}.")
                iframe_handle.evaluate("function(el, html) { el.outerHTML = html; }", replacement_html_string)
                logger.info(f"Successfully replaced iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) at depth {current_depth}.")
            except Exception as eval_error:
                logger.error(f"Failed to replace iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) in DOM using evaluate: {eval_error}", exc_info=True)
                try: 
                    iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true') }")
                except Exception as mark_err:
                    logger.error(f"Failed to mark iframe (src: {iframe_src_for_log[:100]}) as error after evaluate failed: {mark_err}", exc_info=True)
        
        except Exception as e:
            logger.error(f"Outer error processing an iframe at depth {current_depth} for {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}): {e}", exc_info=True)
            if iframe_handle: # 에러 발생 시 핸들이 있다면 마킹 시도
                try: iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true') }")
                except: pass 
            # 현재 레벨의 iframe 처리 루프를 중단하고 다음 단계로 넘어가지 않도록 합니다 (예: 상위 레벨로 돌아감).
            # 또는, 상황에 따라 break 대신 continue를 사용하여 다른 iframe 처리를 시도할 수 있으나,
            # 예측 불가능한 동작을 피하기 위해 여기서는 break로 현재 레벨 처리를 중단합니다.
            logger.warning(f"Breaking iframe processing loop at depth {current_depth} due to an error.")
            break 

        finally:
            if iframe_handle:
                try: iframe_handle.dispose()
                except: pass # dispose 오류는 무시

    logger.info(f"Finished iframe processing at depth {current_depth} for {original_page_url_for_logging}. Processed {processed_iframe_count_at_this_level} direct iframe(s) at this level.")

@celery_app.task(name='celery_tasks.extract_body_html_from_url')
def extract_body_html_from_url(url: str):
    """
    Playwright를 사용하여 지정된 URL의 <body> 내부 전체 HTML을 가져와 파일에 저장합니다.
    iframe 내부 컨텐츠를 재귀적으로 파싱하여 부모 HTML에 통합합니다.
    파일 저장 후, 저장된 파일의 상대 경로를 반환합니다.
    """
    logger.info(f"Attempting to extract body HTML from URL: {url} with iframe processing.")
    try:
        with sync_playwright() as p:
            browser = None
            try:
                browser = p.chromium.launch(headless=True) # 헤드리스 모드로 실행
                logger.info(f"Playwright browser launched: {browser.version}")
            except Exception as browser_launch_error:
                logger.error(f"Failed to launch Playwright chromium browser: {browser_launch_error}", exc_info=True)
                try:
                    logger.info("Attempting to launch Firefox as a fallback.")
                    browser = p.firefox.launch(headless=True)
                    logger.info(f"Playwright Firefox browser launched: {browser.version}")
                except Exception as firefox_launch_error:
                    logger.error(f"Failed to launch Playwright Firefox browser: {firefox_launch_error}", exc_info=True)
                    raise ConnectionError(f"Failed to launch any Playwright browser. Last error (Firefox): {firefox_launch_error}") from firefox_launch_error
            
            page = None
            try:
                page = browser.new_page()
                logger.info(f"New page created. Navigating to URL: {url}")
                page.goto(url, timeout=180000, wait_until='domcontentloaded')
                logger.info(f"Successfully navigated to URL: {url}")

                logger.info(f"Starting to flatten iframes for URL: {url}")
                _flatten_iframes_in_live_dom(page, 0, MAX_IFRAME_DEPTH, url)
                logger.info(f"Finished flattening iframes for URL: {url}")

                final_full_html_content = page.content()
                logger.info(f"Successfully retrieved final full page content for URL: {url} after iframe processing. Content length: {len(final_full_html_content)}")

                soup = BeautifulSoup(final_full_html_content, 'html.parser')
                body_content_tag = soup.body
                if body_content_tag:
                    body_html = body_content_tag.prettify() 
                    logger.info(f"Successfully extracted body HTML for URL: {url}. Body HTML length: {len(body_html)}")
                else:
                    logger.warning(f"Could not find body tag in the final page content for URL: {url}. Using full HTML.")
                    body_html = soup.prettify() if soup else "<!-- BeautifulSoup found no content -->"

                logs_dir_name = "logs" # 디렉토리 이름만
                if not os.path.exists(logs_dir_name):
                    try:
                        os.makedirs(logs_dir_name)
                        logger.info(f"Created directory: {logs_dir_name}")
                    except OSError as e:
                        logger.error(f"Error creating directory {logs_dir_name}: {e}", exc_info=True)
                        # logs_dir_name = "." # 현재 디렉토리에 저장하는 대신 오류 발생
                        raise # 디렉토리 생성 실패 시 오류 발생시킴

                # 새로운 파일명 생성 로직: YYYYMMDD_UUIDshort.html
                current_date_str = datetime.datetime.now().strftime("%Y%m%d")
                unique_id = uuid.uuid4().hex[:8] # 8자리 고유 ID
                file_basename = f"{current_date_str}_{unique_id}.html"
                absolute_file_path = os.path.join(logs_dir_name, file_basename)
                
                try:
                    with open(absolute_file_path, "w", encoding="utf-8") as f:
                        f.write(body_html)
                    logger.info(f"Recursively processed body HTML successfully saved to: {absolute_file_path}")
                    return file_basename # logs 디렉토리 기준 상대 경로 (파일명) 반환
                except IOError as e:
                    logger.error(f"Failed to write HTML to file {absolute_file_path}: {e}", exc_info=True)
                    raise

            except Exception as page_error:
                logger.error(f"Error during Playwright page operations or iframe processing for URL \'{url}\': {page_error}", exc_info=True)
                raise
            finally:
                if page:
                    try: page.close(); logger.info(f"Page closed for URL: {url}")
                    except Exception as e: logger.warning(f"Error closing page for URL \'{url}\': {e}", exc_info=True)
                if browser and browser.is_connected():
                    try: browser.close(); logger.info("Playwright browser closed.")
                    except Exception as e: logger.warning(f"Error closing browser: {e}", exc_info=True)
                        
    except ConnectionError as conn_err: 
        logger.error(f"Playwright browser connection error while processing {url}: {conn_err}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred in extract_body_html_from_url (recursive) for URL \'{url}\': {e}", exc_info=True)
        raise

@celery_app.task(name='celery_tasks.open_url_with_playwright_inspector')
def open_url_with_playwright_inspector(url: str):
    """
    Playwright를 사용하여 지정된 URL을 열고 Playwright Inspector를 실행합니다.
    """
    logger.info(f"Attempting to open URL with Playwright Inspector: {url}")
    try:
        with sync_playwright() as p:
            try:
                # 브라우저 선택 (예: chromium)
                # browser = p.chromium.launch(headless=False)
                # WSL 또는 특정 환경에서는 chromium 대신 firefox나 webkit을 사용해야 할 수 있습니다.
                # 또는, 채널을 명시적으로 지정해야 할 수 있습니다 (예: browser = p.chromium.launch(headless=False, channel="chrome"))
                browser = p.chromium.launch(headless=False)
                logger.info(f"Playwright browser launched: {browser.version}")
            except Exception as browser_launch_error:
                logger.error(f"Failed to launch Playwright browser: {browser_launch_error}", exc_info=True)
                # 대체 브라우저 시도 (예: Firefox)
                try:
                    logger.info("Attempting to launch Firefox as a fallback.")
                    browser = p.firefox.launch(headless=False)
                    logger.info(f"Playwright Firefox browser launched: {browser.version}")
                except Exception as firefox_launch_error:
                    logger.error(f"Failed to launch Playwright Firefox browser: {firefox_launch_error}", exc_info=True)
                    # Webkit 시도
                    try:
                        logger.info("Attempting to launch WebKit as a fallback.")
                        browser = p.webkit.launch(headless=False)
                        logger.info(f"Playwright WebKit browser launched: {browser.version}")
                    except Exception as webkit_launch_error:
                        logger.error(f"Failed to launch Playwright WebKit browser: {webkit_launch_error}", exc_info=True)
                        raise ConnectionError(f"Failed to launch any Playwright browser (Chromium, Firefox, WebKit). Last error (WebKit): {webkit_launch_error}") from webkit_launch_error
            
            try:
                page = browser.new_page()
                logger.info(f"New page created. Navigating to URL: {url}")
                page.goto(url, timeout=60000) # 60초 타임아웃
                logger.info(f"Successfully navigated to URL: {url}")
                
                logger.info("Opening Playwright Inspector. Execution will pause here until the Inspector is closed.")
                page.pause() # Playwright Inspector 실행
                
                logger.info("Playwright Inspector closed by user. Closing browser.")
                browser.close()
                logger.info("Playwright browser closed.")
                return f"Successfully opened {url} and closed Playwright Inspector."
            except Exception as page_error:
                logger.error(f"Error during Playwright page operations for URL '{url}': {page_error}", exc_info=True)
                if 'browser' in locals() and browser.is_connected():
                    browser.close()
                raise
    except ConnectionError as conn_err: # 브라우저 실행 실패 시
        logger.error(f"Playwright browser connection error: {conn_err}", exc_info=True)
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred in open_url_with_playwright_inspector for URL '{url}': {e}", exc_info=True)
        raise

@celery_app.task(bind=True, name='celery_tasks.perform_processing', max_retries=3, default_retry_delay=60)
def perform_processing(self, job_url: str, prompt: str, job_site_name: str = "unknown_site"):
    """
    전체 자기소개서 생성 파이프라인을 수행하는 Celery 작업입니다.
    Playwright를 사용하여 웹에서 채용 공고 HTML을 추출하고,
    텍스트를 정제한 후, LLM을 사용하여 자기소개서를 생성합니다.
    """
    task_id = self.request.id
    logger.info(f"Task {task_id} (UUID: {uuid.uuid4()}): perform_processing 시작, job_url: {job_url}, job_site_name: {job_site_name}")

    extracted_html_file_path = None
    raw_text_file_path = None
    filtered_text_file_path = None
    final_cover_letter_path = None
    error_occurred = False
    error_message = "An unexpected error occurred."
    current_step = "INITIALIZING"

    try:
        self.update_state(state='PROGRESS', meta={'current_step': '채용 공고 HTML 추출 중...', 'job_url': job_url})
        current_step = "HTML_EXTRACTION"
        logger.info(f"Task {task_id}: HTML 추출 시작 - extract_body_html_from_url 호출")
        extracted_html_file_path = extract_body_html_from_url.s(url=job_url).apply(task_id=f"{task_id}_html").get(timeout=1800) # 30분 타임아웃
        if not extracted_html_file_path or not os.path.exists(extracted_html_file_path):
            raise FileNotFoundError(f"HTML 추출 실패 또는 파일({extracted_html_file_path})을 찾을 수 없습니다.")
        logger.info(f"Task {task_id}: HTML 추출 완료. 파일 경로: {extracted_html_file_path}")

        self.update_state(state='PROGRESS', meta={'current_step': 'HTML에서 텍스트 추출 중...', 'html_file': extracted_html_file_path})
        current_step = "TEXT_EXTRACTION"
        logger.info(f"Task {task_id}: 텍스트 추출 시작 - extract_text_from_html_file 호출")
        # s()를 사용하여 subtask 시그니처를 만들고, task_id를 지정하여 apply_async 대신 apply().get()으로 동기적 실행
        # 단, 이 경우 extract_text_from_html_file이 @celery_app.task로 데코레이팅 되어 있어야 함.
        # 여기서는 이미 그렇게 되어 있다고 가정.
        raw_text_file_path = extract_text_from_html_file.s(html_file_name=extracted_html_file_path).apply(task_id=f"{task_id}_text").get(timeout=600) # 10분 타임아웃
        if not raw_text_file_path or not os.path.exists(raw_text_file_path):
            raise FileNotFoundError(f"텍스트 추출 실패 또는 파일({raw_text_file_path})을 찾을 수 없습니다.")
        logger.info(f"Task {task_id}: 텍스트 추출 완료. 파일 경로: {raw_text_file_path}")

        self.update_state(state='PROGRESS', meta={'current_step': 'LLM으로 채용 공고 필터링 중...', 'raw_text_file': raw_text_file_path})
        current_step = "LLM_FILTERING"
        logger.info(f"Task {task_id}: LLM 필터링 시작 - filter_job_posting_with_llm 호출")
        # filter_job_posting_with_llm도 Celery 작업으로 가정
        filtered_text_file_path = filter_job_posting_with_llm.s(raw_text_file_name=raw_text_file_path).apply(task_id=f"{task_id}_filter").get(timeout=1200) # 20분 타임아웃
        if not filtered_text_file_path or not os.path.exists(filtered_text_file_path):
            raise FileNotFoundError(f"LLM 필터링 실패 또는 파일({filtered_text_file_path})을 찾을 수 없습니다.")
        logger.info(f"Task {task_id}: LLM 필터링 완료. 파일 경로: {filtered_text_file_path}")

        self.update_state(state='PROGRESS', meta={'current_step': '자기소개서 생성 중...', 'filtered_text_file': filtered_text_file_path})
        current_step = "COVER_LETTER_GENERATION"
        logger.info(f"Task {task_id}: 자기소개서 생성 시작 - generate_cover_letter 호출")
        
        # generate_cover_letter 함수는 Celery task가 아닐 수 있으므로, 직접 호출하거나 필요시 task로 래핑
        # 이 함수는 generate_cover_letter_semantic.py 에 정의되어 있음
        # 여기서는 직접 호출한다고 가정. 만약 해당 함수가 오래 걸린다면 별도 task화 고려.
        from generate_cover_letter_semantic import generate_cover_letter # 지연 로딩
        generation_result = generate_cover_letter(
            job_posting_file_path=filtered_text_file_path,
            user_prompt=prompt,
            job_site_name=job_site_name,
            task_id=task_id # task_id 전달
        )
        
        raw_cover_letter = generation_result.get("raw_cover_letter")
        formatted_cover_letter = generation_result.get("formatted_cover_letter")
        final_cover_letter_path = generation_result.get("output_file_path")

        if not raw_cover_letter or not formatted_cover_letter or not final_cover_letter_path or not os.path.exists(final_cover_letter_path):
            raise ValueError(f"자기소개서 생성 실패 또는 결과 파일({final_cover_letter_path})을 찾을 수 없습니다.")
        logger.info(f"Task {task_id}: 자기소개서 생성 완료. 원본 길이: {len(raw_cover_letter)}, 포맷된 버전 길이: {len(formatted_cover_letter)}")
        logger.info(f"Task {task_id}: 생성된 자기소개서 저장 완료: {final_cover_letter_path}")

        self.update_state(state='SUCCESS', meta={'current_step': '완료', 'result_file': final_cover_letter_path, 'job_url': job_url})
        logger.info(f"Task {task_id}: perform_processing 성공적으로 완료.")
        return {
            'status': 'SUCCESS',
            'message': 'Cover letter generated successfully.',
            'job_url': job_url,
            'rag_file_used': os.path.basename(filtered_text_file_path) if filtered_text_file_path else "N/A",
            'raw_cover_letter': raw_cover_letter,
            'formatted_cover_letter': formatted_cover_letter,
            'output_file_path': final_cover_letter_path,
            'task_id': task_id
        }

    except FileNotFoundError as e_fnf:
        error_occurred = True
        error_message = f"파일 관련 오류: {str(e_fnf)}"
        logger.error(f"Task {task_id}: FileNotFoundError in step {current_step} - {error_message}", exc_info=True)
        self.update_state(state='FAILURE', meta={'current_step': current_step, 'error': error_message, 'job_url': job_url, 'task_id': task_id})
    except ValueError as e_val:
        error_occurred = True
        error_message = f"값 관련 오류: {str(e_val)}"
        logger.error(f"Task {task_id}: ValueError in step {current_step} - {error_message}", exc_info=True)
        self.update_state(state='FAILURE', meta={'current_step': current_step, 'error': error_message, 'job_url': job_url, 'task_id': task_id})
    except MaxRetriesExceededError as e_max_retries:
        error_occurred = True
        error_message = f"최대 재시도 횟수 초과: {str(e_max_retries)}"
        logger.error(f"Task {task_id}: MaxRetriesExceededError in step {current_step} - {error_message}", exc_info=True)
        # update_state는 Celery가 자동으로 처리할 수 있으므로 여기서는 로깅만 집중
    except Exception as e_general:
        error_occurred = True
        error_message = f"예상치 못한 오류 발생: {str(e_general)}"
        logger.error(f"Task {task_id}: Exception in step {current_step} - {error_message}", exc_info=True)
        try:
            # 재시도 로직: 현재 self.request.retries는 사용 불가 (apply().get() 사용 시)
            # 대신 직접적인 재시도 로직을 구현하거나, 태스크 호출 방식을 변경해야 함.
            # 여기서는 단순 실패 처리
            self.update_state(state='FAILURE', meta={'current_step': current_step, 'error': error_message, 'job_url': job_url, 'task_id': task_id})
        except Exception as retry_exc: # pragma: no cover
            logger.error(f"Task {task_id}: Error during retry mechanism for {current_step}: {retry_exc}", exc_info=True)
            # 최종 실패 상태 업데이트
            self.update_state(state='FAILURE', meta={'current_step': current_step, 'error': f"General error, then retry mechanism failed: {error_message}, {retry_exc}", 'job_url': job_url, 'task_id': task_id})
    finally:
        if error_occurred:
            logger.error(f"Task {task_id}: perform_processing 실패. 최종 단계: {current_step}, 오류: {error_message}")
            # 실패 시 반환 값 명시 (Celery는 작업이 실패하면 예외를 전파하지만, 명시적 반환도 가능)
            # raise Exception(error_message) # 이렇게 하면 Celery가 FAILURE로 처리
            return {
                'status': 'FAILURE',
                'message': error_message,
                'job_url': job_url,
                'current_step': current_step,
                'task_id': task_id
            }
        # 성공했지만 반환값이 없는 경우 (실제로는 위에서 성공 시 반환)
        elif 'return' not in locals() and not error_occurred : # 성공적으로 끝났으나 명시적 return이 없는 경우 방지
            logger.warning(f"Task {task_id}: Reached end of perform_processing without explicit success return. Forcing SUCCESS state.")
            self.update_state(state='SUCCESS', meta={'current_step': 'COMPLETED_UNEXPECTEDLY', 'job_url': job_url, 'task_id': task_id})
            return {
                'status': 'SUCCESS',
                'message': 'Processing completed, but final result structure might be incomplete.',
                'job_url': job_url,
                'task_id': task_id
            }

@celery_app.task(name='celery_tasks.extract_text_from_html_file')
def extract_text_from_html_file(html_file_name: str):
    """
    logs 디렉토리 내의 지정된 HTML 파일에서 텍스트를 추출하여 파일로 저장합니다.
    추출된 텍스트는 하나의 긴 문자열 형태이며, 연속된 공백은 단일 공백으로 처리됩니다.
    저장 후, 생성된 텍스트 파일의 이름(logs 디렉토리 기준 상대 경로)을 반환합니다.
    입력 html_file_name은 logs 디렉토리를 제외한 파일명이어야 합니다.
    """
    logger.info(f"Attempting to extract text from HTML file as a single line: {html_file_name} (expected in logs/)")
    
    logs_dir = "logs" # CWD 기준 logs 디렉토리 사용으로 통일
    html_file_path = os.path.join(logs_dir, html_file_name)

    if not os.path.exists(html_file_path):
        error_msg = f"HTML file not found in logs directory: {html_file_name} (Full path checked: {html_file_path})"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    extracted_text_file_name = "" # 초기화
    
    try:
        logger.info(f"Reading HTML file: {html_file_path}")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        logger.info(f"Successfully read HTML file: {html_file_path}")
    except FileNotFoundError:
        logger.error(f"HTML file not found: {html_file_path}")
        return None
    except Exception as e:
        logger.error(f"Error reading HTML file {html_file_path}: {e}")
        return None

    try:
        soup = BeautifulSoup(html_content, "html.parser")
        # 스크립트 및 스타일 태그 제거 (원본 로직 유지 또는 필요시 get_text() 옵션으로 대체 가능)
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
            logger.debug(f"Removed tag: {script_or_style.name}")
        
        text = soup.get_text(separator=' ', strip=True) # separator와 strip 옵션 원복
        text = ' '.join(text.split()) # 연속 공백 정리
        logger.info(f"Successfully extracted text from HTML. Text length: {len(text)}")
    except Exception as e:
        logger.error(f"Error parsing HTML or extracting text with BeautifulSoup from {html_file_path}: {e}")
        return None

    # 원본 HTML 파일 이름에서 확장자를 변경하고 접두사를 추가하여 새 텍스트 파일 이름 생성
    # 예: 20231027_1a2b3c4d.html -> text_content_from_html_20231027_1a2b3c4d.txt
    # 또는 body_html_recursive_jobkorea.co.kr_... .html -> text_content_from_html_recursive_jobkorea.co.kr_... .txt
    base_name_from_html_file = os.path.basename(html_file_path)
    if base_name_from_html_file.startswith("body_html_recursive_"):
        # 이전 파일명 형식 ("body_html_recursive_...") 처리
        base_name = base_name_from_html_file.replace("body_html_recursive_", "").replace(".html", "")
        output_filename_leaf = f"text_content_from_html_recursive_{base_name}.txt"
    elif "_" in base_name_from_html_file and base_name_from_html_file.count("_") == 1 and base_name_from_html_file.endswith(".html"):
        # 새로운 파일명 형식 ("날짜_고유번호.html") 처리
        base_name = base_name_from_html_file.replace(".html", "")
        output_filename_leaf = f"text_content_from_html_{base_name}.txt"
    else:
        # 예상치 못한 파일명 형식일 경우 기본값 또는 오류 처리
        logger.warning(f"Unexpected html_file_path format: {html_file_path}. Using a default output name.")
        output_filename_leaf = f"text_content_from_html_{uuid.uuid4().hex[:8]}.txt"


    # logs_dir = os.path.join(os.path.dirname(__file__), '..', 'logs') # 이 부분은 이미 위에서 logs_dir = "logs"로 정의
    os.makedirs(logs_dir, exist_ok=True) # os.makedirs는 이미 있는 디렉토리에 대해 오류를 발생시키지 않음
    output_filename_path = os.path.join(logs_dir, output_filename_leaf)
    
    formatted_lines = []
    try:
        for i in range(0, len(text), 50):
            formatted_lines.append(text[i:i+50])
        text_to_write = "\n".join(formatted_lines)
        logger.info(f"Formatted text with newlines. Preview of text_to_write:\n{text_to_write[:200]}")

        # 파일에 쓰기 직전 터미널에 내용 출력
        print(f"--- Content to be written to {output_filename_path} ---")
        print(text_to_write)
        print(f"--- Raw text preview (before join): {formatted_lines[:5]} ---")
        print("--- End of content ---")

    except Exception as e:
        logger.error(f"Error formatting text for {output_filename_path}: {e}")
        text_to_write = text

    try:
        with open(output_filename_path, "w", encoding="utf-8") as f:
            f.write(text_to_write)
        logger.info(f"Text content saved to {output_filename_path}")
        return output_filename_leaf # 전체 경로 대신 파일명만 반환
    except Exception as e:
        logger.error(f"Error writing text content to {output_filename_path}: {e}")
        return None

@celery_app.task(name='celery_tasks.filter_job_posting_with_llm')
def filter_job_posting_with_llm(raw_text_file_name: str): # raw_text_file_name은 이제 순수 파일명
    """
    LLM (Groq)을 사용하여 원본 채용 공고 텍스트에서 관련 정보만 필터링합니다.
    결과를 새로운 .txt 파일에 저장하고, 그 파일 경로를 반환합니다.
    """
    from langchain_groq import ChatGroq
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.output_parsers import StrOutputParser

    logger.info(f"채용 공고 필터링 작업 시작: {raw_text_file_name}")
    task_id = celery_app.current_task.request.id if celery_app.current_task else "N/A_filter_llm"

    base_name = os.path.splitext(os.path.basename(raw_text_file_name))[0]
    if base_name.startswith("text_content_from_html_"):
        cleaned_base_name = base_name.replace("text_content_from_html_", "")
    else:
        cleaned_base_name = base_name
    llm_filtered_file_name = f"llm_filtered_job_posting_{cleaned_base_name}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    llm_filtered_file_path = os.path.join("logs", llm_filtered_file_name)

    filtered_text_content = ""

    try:
        logger.info(f"Reading raw text file: {raw_text_file_name}")
        if not os.path.exists(raw_text_file_name):
            logger.error(f"Raw text file not found: {raw_text_file_name}")
            raise FileNotFoundError(f"Raw text file not found: {raw_text_file_name}")
        
        with open(raw_text_file_name, "r", encoding="utf-8") as f:
            raw_text_content = f.read()
        logger.info(f"Successfully read raw text file: {raw_text_file_name}. Content length: {len(raw_text_content)}")
        
        if not raw_text_content.strip():
            logger.warning(f"Raw text content for {raw_text_file_name} is empty or whitespace. Skipping LLM filtering.")
            filtered_text_content = "원본 내용 없음 (LLM 필터링 건너뜀)"
        else:
            groq_api_key = os.getenv("GROQ_API_KEY")
            if not groq_api_key:
                logger.error("CRITICAL: GROQ_API_KEY is NOT SET in the Celery task environment!")
                placeholder_content = "GROQ_API_KEY not configured. LLM filtering skipped."
                with open(llm_filtered_file_path, "w", encoding="utf-8") as f:
                    f.write(placeholder_content)
                logger.info(f"Placeholder content written to {llm_filtered_file_path} due to missing GROQ_API_KEY.")
                return llm_filtered_file_name

            try:
                logger.info("Initializing Groq Chat LLM (meta-llama/llama-4-maverick-17b-128e-instruct)...")
                model = ChatGroq(
                    temperature=0.1, 
                    groq_api_key=groq_api_key,
                    model_name="meta-llama/llama-4-maverick-17b-128e-instruct", 
                    max_tokens=8000 
                )
                logger.info("Groq Chat LLM initialized successfully.")

                prompt_template_str = """You are an expert assistant that extracts only the core job posting content from the given text. 
Your goal is to identify and isolate the actual job advertisement.
Carefully review the entire text provided.
Extract ONLY the following sections if present:
- Job Title
- Company Name (if clearly part of the posting)
- Job Description / Responsibilities
- Qualifications / Requirements (skills, experience, education)
- Preferred Qualifications (if any)
- What the company offers / Benefits (if specifically listed for the job)
- Location
- How to Apply (if mentioned as part of the posting itself, not general site instructions)

IGNORE everything else, including but not limited to:
- Website navigation elements (menus, links, headers, footers)
- Advertisements for other jobs or services
- Cookie consent banners
- General company promotional text not tied to this specific job
- User comments or discussions
- Irrelevant metadata or timestamps

If the text does not appear to contain a job posting, or if the content is too garbled to be a job posting, respond with the exact phrase '추출할 내용 없음' and nothing else.
Do not add any introductory phrases like "Here is the job posting content:" or any explanations. Just provide the extracted text or '추출할 내용 없음'.

Here is the text to analyze:
{text_input}
                            """
                
                prompt = ChatPromptTemplate.from_template(prompt_template_str)
                
                output_parser = StrOutputParser()
                
                chain = prompt | model | output_parser
                
                logger.info(f"Invoking Groq LLM chain for file: {raw_text_file_name}...")
                filtered_text_content = chain.invoke({"text_input": raw_text_content})
                logger.info(f"Groq LLM chain invocation complete. Raw output length: {len(filtered_text_content)}")

                    if not filtered_text_content.strip() or filtered_text_content.strip() == "추출할 내용 없음":
                    logger.warning("LLM returned empty or '추출할 내용 없음' response.")
                        filtered_text_content = "LLM 필터링 결과 내용 없음"
                else:
                    logger.info("Successfully filtered text using Groq LLM.")
                    logger.debug(f"Filtered text (first 300 chars): {filtered_text_content[:300]}")

            except Exception as e_groq:
                logger.error(f"Error during Groq LLM call for {raw_text_file_name}: {e_groq}", exc_info=True)
                filtered_text_content = f"LLM API 호출 중 오류 발생: {str(e_groq)}"

        logger.info(f"Writing filtered content to: {llm_filtered_file_path}")
        with open(llm_filtered_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_text_content)
        logger.info(f"Successfully saved filtered text to '{llm_filtered_file_name}'.")
        
        return llm_filtered_file_name

    except FileNotFoundError as e_fnf:
        logger.error(f"FileNotFoundError in filter_job_posting_with_llm for {raw_text_file_name}: {e_fnf}", exc_info=True)
        raise
    except Exception as e_general:
        logger.error(f"An unexpected error occurred in filter_job_posting_with_llm for {raw_text_file_name}: {e_general}", exc_info=True)
        if os.path.exists(llm_filtered_file_path):
            try:
                os.remove(llm_filtered_file_path)
                logger.info(f"Removed partially created file due to error: {llm_filtered_file_path}")
            except Exception as e_del:
                logger.error(f"Error removing partially created file {llm_filtered_file_path}: {e_del}")
        raise

async def extract_body_html_with_playwright_and_iframe(url: str, task_id: str = "N/A") -> Optional[str]:
    """
    비동기 Playwright를 사용하여 지정된 URL의 <body> 내부 전체 HTML을 가져옵니다.
    iframe 내부 컨텐츠를 재귀적으로 파싱하여 부모 HTML에 통합합니다.
    성공 시 HTML 문자열을, 실패 시 None을 반환합니다.
    """
    logger.info(f"Task {task_id}: Async HTML extraction started for URL: {url}")
    try:
        async with async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(headless=True)
                logger.info(f"Task {task_id}: Playwright async browser launched: {browser.version}")
            except Exception as browser_launch_error:
                logger.error(f"Task {task_id}: Failed to launch async browser: {browser_launch_error}", exc_info=True)
                return None
            
            page = None
            try:
                page = await browser.new_page()
                logger.info(f"Task {task_id}: New page created.")

                # 리소스 로드 최적화: 이미지, CSS, 폰트 등 불필요한 리소스 차단
                await page.route("**/*", lambda route: (
                    route.abort() if route.request.resource_type in ["image", "stylesheet", "font", "media"] 
                    else route.continue_()
                ))
                logger.info(f"Task {task_id}: Resource loading rules (image, stylesheet, font, media aborted) applied.")
                
                # 네트워크 요청 로깅 (옵션)
                # page.on(\"request\", lambda request: logger.debug(f\"Request: {request.method} {request.url}\"))
                # page.on(\"response\", lambda response: logger.debug(f\"Response: {response.status} {response.url}\"))

                logger.info(f"Task {task_id}: Navigating to URL: {url}")
                try:
                    await page.goto(url, timeout=60000, wait_until='domcontentloaded') # 타임아웃 60초, domcontentloaded까지 대기
                    logger.info(f"Task {task_id}: Successfully navigated to URL: {url}")
                except Exception as navigation_error:
                    logger.error(f"Task {task_id}: Error navigating to URL {url}: {navigation_error}", exc_info=True)
                    # 페이지 내용이라도 가져오도록 시도
                    content_after_nav_error = await page.content()
                    if content_after_nav_error:
                        logger.warning(f"Task {task_id}: Content fetched after navigation error: {content_after_nav_error[:500]}...") # 처음 500자만 로깅
                        # 여기서 추가 처리 (예: BeautifulSoup으로 파싱 시도) 가능
                    return None # 또는 부분적인 내용이라도 반환할지 결정

                logger.info(f"Task {task_id}: Starting iframe processing for URL: {url}")
                await _async_flatten_iframes_in_live_dom(page, 0, MAX_IFRAME_DEPTH, url, task_id)
                logger.info(f"Task {task_id}: Finished iframe processing for URL: {url}")

                # 최종적으로 body의 outerHTML을 가져옵니다 (iframe이 body 내부에 병합되었으므로).
                body_element = await page.query_selector('body')
                if body_element:
                    full_body_html = await body_element.evaluate('element => element.outerHTML')
                    logger.info(f"Task {task_id}: Successfully extracted body HTML. Length: {len(full_body_html)}")
                    return full_body_html
                else:
                    logger.warning(f"Task {task_id}: <body> tag not found after iframe processing. Returning full page content. URL: {url}")
                    # body가 없으면 전체 페이지 내용이라도 반환
                    return await page.content()

            except Exception as page_processing_error:
                logger.error(f"Task {task_id}: Error processing page {url}: {page_processing_error}", exc_info=True)
                if page: # 페이지 객체가 있다면 현재 내용이라도 로깅 시도
                    try:
                        current_content_on_error = await page.content()
                        logger.info(f"Task {task_id}: Page content on error: {current_content_on_error[:500]}...")
                    except Exception as content_log_error:
                        logger.error(f"Task {task_id}: Failed to get page content during error handling: {content_log_error}")
                return None
            finally:
                if page:
                    try: await page.close()
                    except Exception as e_pg_close: logger.warning(f"Task {task_id}: Error closing page: {e_pg_close}", exc_info=True)
                if browser:
                    try: await browser.close()
                    except Exception as e_br_close: logger.warning(f"Task {task_id}: Error closing browser: {e_br_close}", exc_info=True)
                logger.info(f"Task {task_id}: Playwright async resources released for URL: {url}")
                
    except Exception as outer_error:
        logger.error(f"Task {task_id}: Outer error in async_extract_body_html: {outer_error}", exc_info=True)
        return None

async def _async_flatten_iframes_in_live_dom(current_playwright_context, # Playwright Page 또는 Frame 객체
                                          current_depth: int,
                                          max_depth: int,
                                          original_page_url_for_logging: str,
                                          task_id: str):
    if current_depth > max_depth:
        logger.warning(f"Task {task_id}: Max iframe depth {max_depth} reached for a frame within {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}). Stopping recursion for this branch.")
        return

    processed_iframe_count_at_this_level = 0
    while True:
        iframe_locator = current_playwright_context.locator('iframe:not([data-cvf-error="true"])').first
        
        try:
            if await iframe_locator.count() == 0:
                logger.info(f"Task {task_id}: No more processable iframes found at depth {current_depth} for {original_page_url_for_logging} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}).")
                break
        except Exception as e_count:
            logger.warning(f"Task {task_id}: Error checking iframe count at depth {current_depth} for {original_page_url_for_logging}: {e_count}. Assuming no iframes left.")
            break

        iframe_handle = None
        try:
            iframe_handle = await iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"Task {task_id}: Located an iframe but could not get its element_handle at depth {current_depth} for {original_page_url_for_logging}. Breaking loop.")
                break

            processed_iframe_count_at_this_level += 1
            iframe_src_for_log = (await iframe_handle.get_attribute('src')) or "[src not found]"
            logger.info(f"Task {task_id}: Processing iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) at depth {current_depth} (context URL: {current_playwright_context.url if hasattr(current_playwright_context, 'url') else 'N/A'}).")

            child_frame = await iframe_handle.content_frame()
            
            if not child_frame:
                logger.warning(f"Task {task_id}: Could not get content_frame for iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}). Marking and skipping.")
                await iframe_handle.evaluate("el => el.setAttribute('data-cvf-error', 'true')")
                continue

            await _async_flatten_iframes_in_live_dom(child_frame, current_depth + 1, max_depth, original_page_url_for_logging, task_id)
            
            child_frame_html_content = ""
            try:
                await child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT)
                child_frame_html_content = await child_frame.content()
            except Exception as frame_content_err:
                logger.error(f"Task {task_id}: Error getting content from child_frame (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}, depth: {current_depth + 1}): {frame_content_err}", exc_info=True)
                await iframe_handle.evaluate("el => el.setAttribute('data-cvf-error', 'true')")
                continue
            
            replacement_html_string = ""
            if not child_frame_html_content:
                logger.warning(f"Task {task_id}: Child frame (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}, depth: {current_depth + 1}) returned empty content.")
                replacement_html_string = f"<!-- Iframe (src: {iframe_src_for_log[:200]}) content was empty -->"
            else:
                try:
                    child_soup = BeautifulSoup(child_frame_html_content, 'html.parser')
                    content_to_insert_bs = child_soup.body if child_soup.body else child_soup
                    replacement_html_string = content_to_insert_bs.prettify() if content_to_insert_bs else f"<!-- Iframe (src: {iframe_src_for_log[:200]}) content could not be prettified -->"
                except Exception as bs_parse_err:
                    logger.error(f"Task {task_id}: Error parsing/prettifying child frame content (URL: {child_frame.url}, src: {iframe_src_for_log[:100]}): {bs_parse_err}", exc_info=True)
                    replacement_html_string = f"<!-- Error processing iframe content (src: {iframe_src_for_log[:200]}): {str(bs_parse_err)}. Raw content snippet: {child_frame_html_content[:200]} -->"

            try:
                logger.info(f"Task {task_id}: Attempting to replace iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) with its content at depth {current_depth}.")
                await iframe_handle.evaluate("function(el, html) { el.outerHTML = html; }", replacement_html_string)
                logger.info(f"Task {task_id}: Successfully replaced iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) at depth {current_depth}.")
            except Exception as eval_error:
                logger.error(f"Task {task_id}: Failed to replace iframe #{processed_iframe_count_at_this_level} (src: {iframe_src_for_log[:100]}) in DOM: {eval_error}", exc_info=True)
                try: await iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true') }")
                except Exception as mark_err: logger.error(f"Task {task_id}: Failed to mark iframe as error after eval failed: {mark_err}", exc_info=True)
        
    except Exception as e:
            logger.error(f"Task {task_id}: Outer error processing an iframe at depth {current_depth} for {original_page_url_for_logging}: {e}", exc_info=True)
            if iframe_handle:
                try: await iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true') }")
                except: pass
            logger.warning(f"Task {task_id}: Breaking iframe processing loop at depth {current_depth} due to an error.")
            break
        finally:
            if iframe_handle:
                try: await iframe_handle.dispose()
                except: pass
    
    logger.info(f"Task {task_id}: Finished iframe processing at depth {current_depth} for {original_page_url_for_logging}. Processed {processed_iframe_count_at_this_level} direct iframe(s) at this level.") 