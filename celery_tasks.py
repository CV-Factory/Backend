from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
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
import google.generativeai as genai
import datetime # datetime 모듈 추가

# 전역 로깅 레벨 및 라이브러리 로깅 레벨 조정
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("cohere").setLevel(logging.WARNING)
logging.getLogger("google.generativeai").setLevel(logging.INFO) # Gemini API 자체 로그는 INFO 유지

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
ELEMENT_HANDLE_TIMEOUT = 5000 # element handle 가져오기 타임아웃 (밀리초)

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
                page.goto(url, timeout=90000, wait_until='domcontentloaded') 
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
    task_id = self.request.id
    logger.info(f"Task {task_id} (UUID: {uuid.uuid4()}): perform_processing 시작, job_url: {job_url}, job_site_name: {job_site_name}")

    if not job_url:
        logger.error(f"Task {task_id}: job_url이 제공되지 않았습니다.")
        self.update_state(state='FAILURE', meta={'exc_type': 'ValueError', 'exc_message': 'job_url is required'})
        raise ValueError("job_url is required") # Celery는 이 예외를 잡고 작업을 실패로 표시

    # prompt가 None일 경우를 대비한 처리
    prompt_for_generation = prompt if prompt is not None else ""
    if not prompt:
        logger.warning(f"Task {task_id}: 사용자 프롬프트가 제공되지 않았습니다. 자기소개서 생성은 채용 공고 기반으로만 진행될 수 있습니다.")
    else:
        logger.info(f"Task {task_id}: 사용자 프롬프트 (앞부분): {prompt_for_generation[:100]}...")
    
    html_file_name = None
    raw_text_file_name = None
    llm_filtered_file_name = None
    rag_ready_file_name = None
    logs_dir = "logs" # 일관성을 위해 여기서도 정의

    try:
        # 1단계: URL에서 HTML 스크래핑 및 저장
        logger.info(f"Task {task_id}: 1단계: {job_url} 에서 HTML 추출 시도 (job_site_name: {job_site_name})")
        html_file_name = extract_body_html_from_url(url=job_url) 
        if not html_file_name:
            logger.error(f"Task {task_id}: HTML 추출 실패 (extract_body_html_from_url 반환값 없음)")
            # update_state는 여기서 할 필요 없음. 예외 발생 시 Celery가 처리.
            raise RuntimeError("Failed to extract HTML content from URL.")
        logger.info(f"Task {task_id}: HTML 추출 성공, 파일명: {html_file_name}")

        # 2단계: 저장된 HTML 파일에서 전체 텍스트 추출 (한 줄로)
        logger.info(f"Task {task_id}: 2단계: {html_file_name} 에서 전체 텍스트 추출 시도")
        raw_text_file_name = extract_text_from_html_file(html_file_name)
        if not raw_text_file_name:
            logger.error(f"Task {task_id}: 텍스트 추출 실패 (extract_text_from_html_file 반환값 없음)")
            raise RuntimeError("Failed to extract text from HTML file.")
        logger.info(f"Task {task_id}: 전체 텍스트 추출 성공, 파일명: {raw_text_file_name}")

        # 3단계: 추출된 전체 텍스트를 LLM으로 필터링
        logger.info(f"Task {task_id}: 3단계: {raw_text_file_name} LLM 필터링 시도")
        llm_filtered_file_name = filter_job_posting_with_llm(raw_text_file_name)
        if not llm_filtered_file_name:
            logger.error(f"Task {task_id}: LLM 필터링 실패 (filter_job_posting_with_llm 반환값 없음)")
            raise RuntimeError("Failed to filter text using LLM.")
        logger.info(f"Task {task_id}: LLM 필터링 성공, 파일명: {llm_filtered_file_name}")

        # 4단계: LLM 필터링된 텍스트를 RAG용으로 직접 사용 (format_text_file 단계 제거)
        logger.info(f"Task {task_id}: 4단계: {llm_filtered_file_name}을 RAG용 최종 텍스트로 사용")
        rag_ready_file_name = llm_filtered_file_name # format_text_file 호출 제거하고 직접 할당
        if not rag_ready_file_name: # llm_filtered_file_name이 없을 경우를 대비
            logger.error(f"Task {task_id}: LLM 필터링된 파일명이 없습니다. RAG 처리를 진행할 수 없습니다.")
            raise RuntimeError("LLM filtered file name is missing.")
        logger.info(f"Task {task_id}: RAG용 최종 텍스트 파일명 설정 완료: {rag_ready_file_name}")

        # 5단계: RAG용 최종 텍스트 파일 내용 읽기
        logger.info(f"Task {task_id}: 5단계: RAG용 최종 텍스트 파일 ({rag_ready_file_name}) 내용 읽기 시도")
        rag_ready_file_path = os.path.join(logs_dir, rag_ready_file_name)
        
        job_posting_content_for_rag = ""
        if not os.path.exists(rag_ready_file_path):
            error_msg = f"Task {task_id}: RAG용 최종 포맷팅된 파일 ({rag_ready_file_path})을 찾을 수 없습니다."
            logger.error(error_msg)
            raise FileNotFoundError(error_msg) # FileNotFoundError는 명시적으로 발생

        with open(rag_ready_file_path, 'r', encoding='utf-8') as f:
            job_posting_content_for_rag = f.read()
        logger.info(f"Task {task_id}: RAG용 최종 텍스트 파일 읽기 성공. 내용 길이: {len(job_posting_content_for_rag)}")
        logger.debug(f"Task {task_id}: RAG에 사용될 최종 콘텐츠 (처음 200자): {job_posting_content_for_rag[:200]}")

        # 6단계: 커버 레터 생성 (RAG)
        logger.info(f"Task {task_id}: 6단계: 자기소개서 생성 시도. User prompt (앞부분): {prompt_for_generation[:100]}... Job posting (앞부분): {job_posting_content_for_rag[:100]}...")
        # generate_cover_letter_semantic.py의 generate_cover_letter 함수가 (raw, formatted) 튜플을 반환한다고 가정
        raw_cover_letter, formatted_cover_letter = generate_cover_letter(
            job_posting_content=job_posting_content_for_rag, 
            prompt=prompt_for_generation 
        )
        logger.info(f"Task {task_id}: 자기소개서 생성 완료. 원본 길이: {len(raw_cover_letter)}, 포맷된 버전 길이: {len(formatted_cover_letter)}")
        
        # 생성된 자기소개서 파일로 저장
        # 파일명 형식: cover_letter_YYYYMMDD_UUIDshort.txt
        try:
            current_date_str = datetime.datetime.now().strftime("%Y%m%d")
            unique_id_for_cv = uuid.uuid4().hex[:8]
            cover_letter_filename = f"cover_letter_{current_date_str}_{unique_id_for_cv}.txt"
            cover_letter_path = os.path.join(logs_dir, cover_letter_filename)
            with open(cover_letter_path, "w", encoding="utf-8") as f:
                f.write(formatted_cover_letter) # 포맷팅된 버전 저장
            logger.info(f"Task {task_id}: 생성된 자기소개서 저장 완료: {cover_letter_path}")
        except Exception as e_save_cv:
            logger.error(f"Task {task_id}: 생성된 자기소개서 파일 저장 중 오류 발생: {e_save_cv}", exc_info=True)
            # 저장 실패가 전체 작업 실패를 의미하지는 않도록 처리 (로깅만 하고 결과는 계속 반환)

        # 임시 파일들 삭제 (선택 사항, 최종 RAG 파일은 남겨둠)
        # files_to_delete_intermediate = [html_file_name, raw_text_file_name, llm_filtered_file_name] 
        # for f_name in files_to_delete_intermediate:
        #     try:
        #         if f_name: 
        #             f_path = os.path.join(logs_dir, f_name)
        #             if os.path.exists(f_path):
        #                 os.remove(f_path)
        #                 logger.info(f"Task {task_id}: 중간 파일 삭제 성공: {f_path}")
        #     except Exception as e_del: # 변수명 변경
        #         logger.warning(f"Task {task_id}: 중간 파일 삭제 중 오류 ({f_name}): {e_del}")


        return {
            "status": "SUCCESS", # Celery 표준은 SUCCESS/FAILURE
            "message": "Cover letter generated successfully.",
            "job_url": job_url,
            "rag_file_used": rag_ready_file_name,
            "raw_cover_letter": raw_cover_letter,
            "formatted_cover_letter": formatted_cover_letter,
            "generated_cover_letter_filename": cover_letter_filename if 'cover_letter_filename' in locals() else None, # 저장된 파일명도 결과에 추가
            "user_story_preview": prompt_for_generation[:200] + "..." if prompt_for_generation else "N/A"
        }

    except FileNotFoundError as e_fnf: # 변수명 변경
        logger.error(f"Task {task_id}: perform_processing 중 FileNotFoundError 발생: {e_fnf}", exc_info=True)
        self.update_state(state='FAILURE', meta={'exc_type': 'FileNotFoundError', 'exc_message': str(e_fnf)})
        raise 
    except ValueError as e_val: # 변수명 변경
        logger.error(f"Task {task_id}: perform_processing 중 ValueError 발생: {e_val}", exc_info=True)
        self.update_state(state='FAILURE', meta={'exc_type': 'ValueError', 'exc_message': str(e_val)})
        raise
    except RuntimeError as e_rt: # 변수명 변경
        logger.error(f"Task {task_id}: perform_processing 중 RuntimeError 발생: {e_rt}", exc_info=True)
        self.update_state(state='FAILURE', meta={'exc_type': 'RuntimeError', 'exc_message': str(e_rt)})
        raise
    except MaxRetriesExceededError as e_mre: # 변수명 변경
        logger.error(f"Task {task_id}: Max retries exceeded for perform_processing: {e_mre}", exc_info=True)
        # update_state는 Celery가 자동으로 처리할 수 있으므로, 여기서는 재발생만으로 충분할 수 있음.
        # 단, 추가 정보를 meta에 담고 싶다면 여기서 update_state를 호출.
        self.update_state(state='FAILURE', meta={'exc_type': 'MaxRetriesExceededError', 'exc_message': f"Max retries exceeded: {str(e_mre)}"})
        raise # 최종 실패를 알림
    except Exception as e_gen: # 변수명 변경, 가장 마지막에 위치
        logger.error(f"Task {task_id}: perform_processing 중 예측하지 못한 예외 발생: {e_gen}", exc_info=True)
        try:
            self.retry(exc=e_gen) # Celery의 내장 재시도 메커니즘 사용
        except MaxRetriesExceededError as e_retry_max: # retry 호출 후 MaxRetriesExceededError
            logger.error(f"Task {task_id}: Retry Succeeded by MaxRetriesExceededError for perform_processing: {e_retry_max}", exc_info=True)
            self.update_state(state='FAILURE', meta={'exc_type': 'MaxRetriesExceededError', 'exc_message': f"Max retries exceeded after explicit retry: {str(e_retry_max)}"})
            raise # 최종 실패를 알림
        except Exception as e_retry_other: # retry 중 다른 예외 (거의 발생 안 함)
             logger.error(f"Task {task_id}: Unexpected error during retry attempt for perform_processing: {e_retry_other}", exc_info=True)
             self.update_state(state='FAILURE', meta={'exc_type': type(e_retry_other).__name__, 'exc_message': f"Unexpected error during retry: {str(e_retry_other)}"})
             raise

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
    logs 디렉토리에 있는 원본 텍스트 파일(raw_text_file_name)을 읽어,
    LLM(예: Gemini API)을 사용하여 채용 공고와 관련된 내용만 필터링하고,
    결과를 _llm_filtered.txt 접미사를 붙여 새로운 파일로 저장합니다.
    raw_text_file_name: logs 디렉토리 내의 원본 .txt 파일 이름 (예: text_content_from_html_... .txt)
    """
    logger.info(f"Attempting to filter job posting content using LLM for file: {raw_text_file_name}")
    logs_dir = "logs" # 현재 파일 위치 기준 상대 경로
    # raw_text_file_name이 이제 순수 파일명이므로, logs_dir와 결합하여 전체 경로 생성
    raw_text_file_path = os.path.join(logs_dir, raw_text_file_name)

    if not os.path.exists(raw_text_file_path):
        error_msg = f"Raw text file not found for LLM filtering: {raw_text_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    llm_filtered_file_name = ""
    filtered_text_content = ""

    try:
        with open(raw_text_file_path, "r", encoding="utf-8") as f:
            raw_text_content = f.read()
        logger.debug(f"Successfully read raw text file: {raw_text_file_path}. Content length: {len(raw_text_content)}")
        
        if not raw_text_content.strip():
            logger.warning(f"Raw text content for {raw_text_file_name} is empty or whitespace. Skipping LLM filtering.")
            filtered_text_content = "원본 내용 없음 (LLM 필터링 건너뜀)"
        else:
            # API 전송 전 줄바꿈 문자를 공백으로 치환하는 로직을 제거하고 원본 텍스트를 그대로 사용합니다.
            text_for_llm = raw_text_content
            logger.debug(f"Raw text (first 200 chars for LLM, newlines preserved): {text_for_llm[:200]}")

            gemini_api_key = os.getenv("GEMINI_API_KEY")
            if not gemini_api_key:
                logger.error("GEMINI_API_KEY not found in environment variables.")
                raise ValueError("GEMINI_API_KEY is not set.")

            genai.configure(api_key=gemini_api_key)
            
            generation_config_gemini = genai.types.GenerationConfig(
                temperature=0.2,
                top_p=0.8,
                top_k=20,
                max_output_tokens=8192,
            )

            try:
                logger.info("Attempting to call Gemini API for text filtering...")
                # 모델명을 'gemini-2.5-flash-preview-04-17'로 설정하고 try 블록 내부로 이동
                model = genai.GenerativeModel('gemini-2.5-flash-preview-04-17')
                
                # 프롬프트 문자열 따옴표 수정: f"""...""" 형태를 유지
                prompt = f"""다음 텍스트에서 실제 채용 공고 내용과 직접적으로 관련된 부분만 추출해주세요.

                            추출할 내용이 없다면 '추출할 내용 없음'이라고 답변해주세요.

                            {text_for_llm}
                            """
                
                response = model.generate_content(
                    prompt,
                    generation_config=generation_config_gemini,
                )
                
                if response.parts:
                    filtered_text_content = response.text
                    logger.info(f"Successfully filtered text using Gemini API. Filtered text length: {len(filtered_text_content)}")
                    if not filtered_text_content.strip() or filtered_text_content.strip() == "추출할 내용 없음":
                        logger.warning("LLM returned empty or 'no content' response.")
                        filtered_text_content = "LLM 필터링 결과 내용 없음"
                else:
                    logger.warning("Gemini API response did not contain any parts (candidates).")
                    if response.prompt_feedback:
                        logger.warning(f"Prompt feedback from API: {response.prompt_feedback}")
                    filtered_text_content = "LLM 응답 없음 (API 반환 내용 없음)"

            except google.api_core.exceptions.InvalidArgument as e:
                logger.error(f"Gemini API InvalidArgument error: {e}", exc_info=True)
                filtered_text_content = f"LLM API 요청 오류 (InvalidArgument): {str(e)}"
            except Exception as e:
                logger.error(f"Error during Gemini API call: {e}", exc_info=True)
                filtered_text_content = f"LLM API 호출 중 알 수 없는 오류: {str(e)}"

        if not filtered_text_content or filtered_text_content.isspace():
            logger.warning(f"LLM filtering resulted in empty or whitespace content for {raw_text_file_name} (after potential API call). Using a placeholder.")
            filtered_text_content = "LLM 필터링 결과 내용이 비어있거나 공백임"

        logger.debug(f"LLM filtered text (first 200 chars): {filtered_text_content[:200]}")
        
        # raw_text_file_name은 순수 파일명이므로, 경로 제거 로직 불필요
        # base_name = raw_text_file_name.replace("text_content_from_html_", "").replace(".txt", "") 
        # 위 로직 대신 파일명에서 확장자만 제거하여 base_name 구성
        base_name_without_ext = os.path.splitext(raw_text_file_name)[0]
        # "text_content_from_html_" 프리픽스가 있다면 제거, 없다면 그대로 사용
        if base_name_without_ext.startswith("text_content_from_html_"):
            cleaned_base_name = base_name_without_ext.replace("text_content_from_html_", "")
        else:
            cleaned_base_name = base_name_without_ext # 예: 날짜_고유번호 형식 그대로 사용
            
        llm_filtered_file_name = f"llm_filtered_job_posting_{cleaned_base_name}.txt"
        llm_filtered_file_path = os.path.join(logs_dir, llm_filtered_file_name)

        with open(llm_filtered_file_path, "w", encoding="utf-8") as f:
            f.write(filtered_text_content)
        
        logger.info(f"Successfully filtered text using LLM and saved to '{llm_filtered_file_name}'.")
        
        return llm_filtered_file_name

    except FileNotFoundError:
        raise
    except ValueError as ve: # API 키 관련 오류
        logger.error(f"ValueError in LLM filtering for {raw_text_file_name}: {ve}", exc_info=True)
        raise
    except Exception as e:
        error_msg = f"Failed to filter text with LLM for file {raw_text_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        # 실패 시 부분적으로 생성된 파일이 있다면 삭제 시도
        if llm_filtered_file_name and os.path.exists(os.path.join(logs_dir, llm_filtered_file_name)):
             logger.debug(f"Attempting to remove partially created LLM filtered file: {llm_filtered_file_name}")
             try:
                 os.remove(os.path.join(logs_dir, llm_filtered_file_name))
             except Exception as e_del_llm:
                 logger.error(f"Error removing partially created LLM file {llm_filtered_file_name}: {e_del_llm}")
        raise 