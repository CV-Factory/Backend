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

logger = logging.getLogger(__name__)

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

                sanitized_name = sanitize_filename(url)
                # 저장될 파일의 이름 (logs 디렉토리 제외)
                file_basename = f"body_html_recursive_{sanitized_name}.html"
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

@celery_app.task(name='celery_tasks.perform_processing', bind=True)
def perform_processing(self, job_url: str, user_story: str | None = None):
    task_id = self.request.id
    logger.info(f"Task {task_id}: perform_processing 시작, job_url: {job_url}")

    if not job_url:
        logger.error(f"Task {task_id}: job_url이 제공되지 않았습니다.")
        self.update_state(state='FAILURE', meta={'exc_type': 'ValueError', 'exc_message': 'job_url is required'})
        raise ValueError("job_url is required")

    logger.info(f"Task {task_id}: 사용자 스토리 (앞부분): {user_story[:100] if user_story else 'N/A'}...")

    try:
        # 1. URL에서 HTML 스크래핑 및 저장
        logger.info(f"Task {task_id}: 1단계: {job_url} 에서 HTML 추출 시도")
        html_file_basename = extract_body_html_from_url(job_url) # .func 제거
        if not html_file_basename:
            logger.error(f"Task {task_id}: HTML 추출 실패 (extract_body_html_from_url 반환값 없음)")
            self.update_state(state='FAILURE', meta={'exc_type': 'RuntimeError', 'exc_message': 'Failed to extract HTML content.'})
            raise RuntimeError("Failed to extract HTML content.")
        logger.info(f"Task {task_id}: HTML 추출 성공, 파일명: {html_file_basename}")

        # 2. HTML 파일에서 텍스트 추출 및 저장
        logger.info(f"Task {task_id}: 2단계: {html_file_basename} 에서 텍스트 추출 시도")
        text_file_basename = extract_text_from_html_file(html_file_basename) # .func 제거
        if not text_file_basename:
            logger.error(f"Task {task_id}: 텍스트 추출 실패 (extract_text_from_html_file 반환값 없음)")
            self.update_state(state='FAILURE', meta={'exc_type': 'RuntimeError', 'exc_message': 'Failed to extract text from HTML.'})
            raise RuntimeError("Failed to extract text from HTML.")
        logger.info(f"Task {task_id}: 텍스트 추출 성공, 파일명: {text_file_basename}")

        # 3. 텍스트 파일 포맷팅 및 저장
        logger.info(f"Task {task_id}: 3단계: {text_file_basename} 포맷팅 시도")
        formatted_text_file_basename = format_text_file(text_file_basename) # .func 제거
        if not formatted_text_file_basename:
            logger.error(f"Task {task_id}: 텍스트 포맷팅 실패 (format_text_file 반환값 없음)")
            self.update_state(state='FAILURE', meta={'exc_type': 'RuntimeError', 'exc_message': 'Failed to format extracted text.'})
            raise RuntimeError("Failed to format extracted text.")
        logger.info(f"Task {task_id}: 텍스트 포맷팅 성공, 파일명: {formatted_text_file_basename}")

        # 4. 포맷팅된 텍스트 파일 내용 읽기
        logger.info(f"Task {task_id}: 4단계: 포맷팅된 텍스트 파일 ({formatted_text_file_basename}) 내용 읽기 시도")
        job_posting_content = ""
        # logs 디렉토리는 celery_tasks.py와 같은 레벨 또는 entrypoint.sh가 실행되는 /app 기준
        formatted_text_file_path = os.path.join("logs", formatted_text_file_basename)
        try:
            with open(formatted_text_file_path, "r", encoding="utf-8") as f:
                job_posting_content = f.read()
            logger.info(f"Task {task_id}: 포맷팅된 텍스트 파일 읽기 성공. 내용 길이: {len(job_posting_content)}")
        except FileNotFoundError:
            logger.error(f"Task {task_id}: 포맷팅된 텍스트 파일을 찾을 수 없음: {formatted_text_file_path}", exc_info=True)
            self.update_state(state='FAILURE', meta={'exc_type': 'FileNotFoundError', 'exc_message': f'Formatted text file not found: {formatted_text_file_basename}'})
            raise
        except Exception as e:
            logger.error(f"Task {task_id}: 포맷팅된 텍스트 파일 읽기 중 오류: {e}", exc_info=True)
            self.update_state(state='FAILURE', meta={'exc_type': type(e).__name__, 'exc_message': str(e)})
            raise

        if not user_story:
            logger.warning(f"Task {task_id}: 사용자 스토리가 제공되지 않았습니다. 자기소개서 생성은 채용 공고 기반으로만 진행됩니다.")
            # user_story가 None이거나 비어있을 경우, generate_cover_letter 함수가 이를 처리할 수 있도록 빈 문자열 전달
            user_story_for_generation = ""
        else:
            user_story_for_generation = user_story

        # 5. 자기소개서 생성
        logger.info(f"Task {task_id}: 5단계: 자기소개서 생성 시도. User story (앞부분): {user_story_for_generation[:100]}... Job posting (앞부분): {job_posting_content[:100]}...")
        raw_cover_letter, formatted_cover_letter = generate_cover_letter(
            user_story=user_story_for_generation, 
            job_posting_content=job_posting_content
        )
        logger.info(f"Task {task_id}: 자기소개서 생성 완료. 원본 길이: {len(raw_cover_letter)}, 포맷된 버전 길이: {len(formatted_cover_letter)}")

        return {
            "status": "SUCCESS",
            "message": "Cover letter generated successfully.",
            "job_url": job_url,
            "html_file": html_file_basename,
            "text_file": text_file_basename,
            "formatted_text_file": formatted_text_file_basename,
            "raw_cover_letter": raw_cover_letter,
            "formatted_cover_letter": formatted_cover_letter,
            "user_story_preview": user_story_for_generation[:200] + "..." if user_story_for_generation else "N/A"
        }

    except Exception as e:
        logger.error(f"Task {task_id}: perform_processing 중 예외 발생: {e}", exc_info=True)
        # Celery 작업 상태를 FAILURE로 업데이트하고, 예외 정보를 meta에 포함
        self.update_state(state='FAILURE', meta={'exc_type': type(e).__name__, 'exc_message': str(e)})
        # 오류를 다시 발생시켜 Celery가 실패로 처리하도록 함
        raise # raise e 보다 그냥 raise가 스택 트레이스를 보존하는 데 더 좋습니다.

@celery_app.task(name='celery_tasks.extract_text_from_html_file')
def extract_text_from_html_file(html_file_name: str):
    """
    logs 디렉토리 내의 지정된 HTML 파일에서 텍스트를 추출하여 파일로 저장합니다.
    추출된 텍스트는 하나의 긴 문자열 형태이며, 연속된 공백은 단일 공백으로 처리됩니다.
    저장 후, 생성된 텍스트 파일의 이름(logs 디렉토리 기준 상대 경로)을 반환합니다.
    입력 html_file_name은 logs 디렉토리를 제외한 파일명이어야 합니다.
    """
    logger.info(f"Attempting to extract text from HTML file as a single line: {html_file_name} (expected in logs/)")
    
    logs_dir = "logs"
    html_file_path = os.path.join(logs_dir, html_file_name)

    if not os.path.exists(html_file_path):
        error_msg = f"HTML file not found in logs directory: {html_file_name} (Full path checked: {html_file_path})"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)
    
    try:
        with open(html_file_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 스크립트, 스타일 태그 제거
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
        
        # 텍스트를 공백으로 구분하여 가져오고, strip=True로 각 요소의 앞뒤 공백 제거
        text_content_from_soup = soup.get_text(separator=' ', strip=True)
        # 위에서 얻은 텍스트에서 연속된 공백 (줄바꿈 포함)을 단일 공백으로 변경하고, 전체 문자열의 앞뒤 공백 제거
        final_text_content = re.sub(r'\s+', ' ', text_content_from_soup).strip()
        
        if not final_text_content:
            logger.warning(f"No text content extracted from {html_file_name} after processing. The file might be empty or contain no text.")
        else:
            logger.debug(f"Extracted single-line text (first 300 chars): {final_text_content[:300]}")
            logger.debug(f"Extracted single-line text (last 300 chars): {final_text_content[-300:]}")

        # 원본 HTML 파일명에서 확장자 제거하고 _text.txt 추가
        base_name_without_ext = os.path.splitext(html_file_name)[0]
        # 출력 파일명 (logs 디렉토리 제외)
        output_txt_basename = f"text_{base_name_without_ext}.txt" 
        # 절대 경로
        output_txt_path = os.path.join(logs_dir, output_txt_basename)
        
        with open(output_txt_path, 'w', encoding='utf-8') as f:
            f.write(final_text_content)
            
        logger.info(f"Single-line text content successfully extracted from '{html_file_name}' and saved to '{output_txt_basename}' in logs directory.")
        return output_txt_basename # logs 디렉토리 기준 상대 경로 (파일명) 반환
        
    except FileNotFoundError: # 위에서 이미 체크했지만, 이중 방어
        logger.error(f"Error: HTML file '{html_file_name}' not found at '{html_file_path}'.", exc_info=True) # 이미 위에서 명시적으로 발생시킴
        raise
    except Exception as e:
        logger.error(f"Error extracting single-line text from HTML file '{html_file_name}': {e}", exc_info=True)
        raise

@celery_app.task(name='celery_tasks.format_text_file')
def format_text_file(original_txt_file_name: str):
    """
    logs 디렉토리에 있는 .txt 파일의 내용을 읽어 50자마다 줄바꿈을 추가하고,
    _formatted.txt 접미사를 붙여 새로운 파일로 저장합니다.
    original_txt_file_name: logs 디렉토리 내의 원본 .txt 파일 이름 (한 줄로 된 텍스트를 포함할 것으로 예상)
    """
    logger.info(f"Attempting to format text file (simple 50-char split): {original_txt_file_name}")
    logs_dir = "logs"
    original_file_path = os.path.join(logs_dir, original_txt_file_name)

    if not os.path.exists(original_file_path):
        error_msg = f"Original text file not found: {original_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    try:
        logger.debug(f"Reading original text file (expected to be single line): {original_file_path}")
        with open(original_file_path, "r", encoding="utf-8") as f:
            original_text_content = f.read()
        logger.info(f"Successfully read original text file: {original_file_path}. Content length: {len(original_text_content)}")
        
        # 혹시 모를 줄바꿈과 연속 공백을 최종 정리 (이전 단계에서 처리되었을 것으로 예상되나 안전장치)
        single_long_line = re.sub(r'\s+', ' ', original_text_content).strip()
        if not single_long_line: # 비어있는 경우
             logger.info("Original text content is empty after stripping whitespace, formatted file will be empty.")
             formatted_text_content = ""
        else:
            logger.debug(f"Content to be split (first 200 chars): '{single_long_line[:200]}...'")
            formatted_lines = []
            current_pos = 0
            while current_pos < len(single_long_line):
                segment = single_long_line[current_pos:current_pos+50]
                formatted_lines.append(segment)
                # logger.debug(f"  Added segment: '{segment}'") # 필요시 상세 로깅
                current_pos += 50
            formatted_text_content = "\n".join(formatted_lines)
        
        logger.info(f"Successfully formatted text content (simple 50-char split). New length: {len(formatted_text_content)}")
        if formatted_text_content: # 내용이 있을 때만 앞/뒤 일부 로깅
            logger.debug(f"Formatted content (first 200 chars): {formatted_text_content[:200]}")
            logger.debug(f"Formatted content (last 200 chars): {formatted_text_content[-200:]}")

        base_name, ext = os.path.splitext(original_txt_file_name)
        # 이름 충돌을 피하기 위해 _formatted_simple.txt 와 같이 변경할 수도 있으나, 일단 유지
        formatted_txt_file_name = f"{base_name}_formatted.txt" 
        formatted_file_path = os.path.join(logs_dir, formatted_txt_file_name)

        logger.debug(f"Writing formatted text to: {formatted_file_path}")
        with open(formatted_file_path, "w", encoding="utf-8") as f:
            f.write(formatted_text_content)
        
        success_msg = f"Text content from {original_txt_file_name} was formatted (simple 50-char split) and saved to {formatted_file_path}"
        logger.info(success_msg)

        # 원본 파일 삭제 로직 (필요시 유지)
        try:
            os.remove(original_file_path)
            logger.info(f"Successfully deleted original file: {original_file_path}")
        except OSError as e:
            logger.warning(f"Could not delete original file {original_file_path}: {e}", exc_info=True)
        
        return formatted_txt_file_name

    except FileNotFoundError: # 위에서 이미 처리했지만, 이중 확인
        raise
    except IOError as e:
        error_msg = f"IOError during simple text formatting for {original_txt_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise
    except Exception as e:
        error_msg = f"An unexpected error occurred during simple text formatting for {original_txt_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise 