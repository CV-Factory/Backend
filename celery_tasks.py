from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright
import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

# 상수 정의
MAX_IFRAME_DEPTH = 3  # iframe 최대 재귀 깊이
IFRAME_LOAD_TIMEOUT = 15000  # iframe 로드 타임아웃 (밀리초)
ELEMENT_HANDLE_TIMEOUT = 5000 # element handle 가져오기 타임아웃 (밀리초)

def sanitize_filename(url: str) -> str:
    """URL을 안전한 파일 이름으로 변환합니다."""
    try:
        filename = url.replace("https://", "").replace("http://", "")
        filename = re.sub(r'[\\/*?:"<>|&%=]', "_", filename)
        # 파일 이름 길이 제한 (OS 및 파일 시스템에 따라 다를 수 있음)
        if len(filename) > 200:
            filename = filename[:200]
        logger.debug(f"Sanitized filename: {filename}")
        return filename
    except Exception as e:
        logger.error(f"Error sanitizing filename for URL \'{url}\': {e}", exc_info=True)
        # 오류 발생 시 대체 파일명 사용
        return "error_sanitizing_filename.html"

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

                # iframe 평탄화 시작
                logger.info(f"Starting to flatten iframes for URL: {url}")
                _flatten_iframes_in_live_dom(page, 0, MAX_IFRAME_DEPTH, url)
                logger.info(f"Finished flattening iframes for URL: {url}")

                # DOM 수정 후 최종 페이지 내용 가져오기
                final_full_html_content = page.content()
                logger.info(f"Successfully retrieved final full page content for URL: {url} after iframe processing. Content length: {len(final_full_html_content)}")

                soup = BeautifulSoup(final_full_html_content, 'html.parser')
                body_content_tag = soup.body
                if body_content_tag:
                    body_html = body_content_tag.prettify() 
                    logger.info(f"Successfully extracted body HTML for URL: {url}. Body HTML length: {len(body_html)}")
                else:
                    logger.warning(f"Could not find body tag in the final page content for URL: {url}. Using full HTML.")
                    # body가 없으면 전체 HTML을 사용하거나, 에러 처리를 할 수 있습니다. 여기서는 전체 HTML을 사용합니다.
                    body_html = soup.prettify() if soup else "<!-- BeautifulSoup found no content -->"

                logs_dir = "logs"
                if not os.path.exists(logs_dir):
                    try:
                        os.makedirs(logs_dir)
                        logger.info(f"Created directory: {logs_dir}")
                    except OSError as e:
                        logger.error(f"Error creating directory {logs_dir}: {e}", exc_info=True)
                        logs_dir = "." 

                sanitized_name = sanitize_filename(url)
                file_path = os.path.join(logs_dir, f"body_html_recursive_{sanitized_name}.html") # 파일명 변경
                
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(body_html)
                    logger.info(f"Recursively processed body HTML successfully saved to: {file_path}")
                    return f"Recursively processed body HTML from {url} saved to {file_path}"
                except IOError as e:
                    logger.error(f"Failed to write HTML to file {file_path}: {e}", exc_info=True)
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

# 이전에 tasks.py에 있던 다른 작업들 (예: perform_processing)도 이 파일로 옮기거나, 
# 해당 작업이 없다면 이 주석은 삭제해도 됩니다.
# from tasks import perform_processing # 만약 main.py에서 이 작업을 호출한다면, 이 파일로 옮겨야 합니다.

# 예시:
@celery_app.task(name='celery_tasks.perform_processing')
def perform_processing(target_url: str, query: str | None = None):
    logger.info(f"perform_processing 작업 시작: target_url='{target_url}', query='{query}'")
    # 여기에 실제 처리 로직을 추가하세요.
    # 예: 웹 스크래핑, 데이터 분석 등
    result_message = f"Processing completed for {target_url} with query '{query}'"
    logger.info(result_message)
    return result_message

@celery_app.task(name='celery_tasks.extract_text_from_html_file')
def extract_text_from_html_file(html_file_name: str):
    """
    logs 디렉토리에 저장된 HTML 파일에서 순수 텍스트를 추출하여 .txt 파일로 저장합니다.
    html_file_name: logs 디렉토리 내의 HTML 파일 이름 (예: 'body_html_recursive_some_url.html')
    """
    logger.info(f"Attempting to extract text from HTML file: {html_file_name}")
    logs_dir = "logs"
    html_file_path = os.path.join(logs_dir, html_file_name)

    if not os.path.exists(html_file_path):
        error_msg = f"HTML file not found: {html_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    try:
        logger.debug(f"Reading HTML file: {html_file_path}")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        logger.info(f"Successfully read HTML file: {html_file_path}. Content length: {len(html_content)}")

        soup = BeautifulSoup(html_content, 'html.parser')
        
        # 스크립트 및 스타일 태그 제거 (선택 사항)
        for script_or_style in soup(["script", "style"]):
            script_or_style.decompose()
            logger.debug(f"Removed tag: <{script_or_style.name}>")

        text_content = soup.get_text(separator='\n', strip=True) # 수정: separator='\n'
        logger.info(f"Successfully extracted text content. Text length: {len(text_content)}")

        base_name, _ = os.path.splitext(html_file_name)
        txt_file_name = f"{base_name}.txt"
        txt_file_path = os.path.join(logs_dir, txt_file_name)

        logger.debug(f"Writing extracted text to: {txt_file_path}")
        with open(txt_file_path, "w", encoding="utf-8") as f:
            f.write(text_content)
        
        success_msg = f"Text content extracted from {html_file_name} and saved to {txt_file_path}"
        logger.info(success_msg)
        return success_msg

    except FileNotFoundError: # 위에서 이미 처리했지만, 이중 확인
        raise
    except IOError as e:
        error_msg = f"IOError during text extraction from {html_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise
    except Exception as e:
        error_msg = f"An unexpected error occurred during text extraction from {html_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise

@celery_app.task(name='celery_tasks.format_text_file')
def format_text_file(original_txt_file_name: str):
    """
    logs 디렉토리에 있는 .txt 파일의 내용을 읽어 50자마다 줄바꿈을 추가하고,
    _formatted.txt 접미사를 붙여 새로운 파일로 저장합니다.
    original_txt_file_name: logs 디렉토리 내의 원본 .txt 파일 이름
    """
    logger.info(f"Attempting to format text file: {original_txt_file_name}")
    logs_dir = "logs"
    original_file_path = os.path.join(logs_dir, original_txt_file_name)

    if not os.path.exists(original_file_path):
        error_msg = f"Original text file not found: {original_file_path}"
        logger.error(error_msg)
        raise FileNotFoundError(error_msg)

    try:
        logger.debug(f"Reading original text file: {original_file_path}")
        with open(original_file_path, "r", encoding="utf-8") as f:
            original_text_content = f.read()
        logger.info(f"Successfully read original text file: {original_file_path}. Content length: {len(original_text_content)}")

        # 기존 줄바꿈을 기준으로 먼저 분리하고, 각 줄에 대해 50자 처리를 할 수도 있으나,
        # 여기서는 전체 텍스트를 하나의 긴 문자열로 보고 50자마다 줄바꿈을 삽입합니다.
        # 먼저 기존 줄바꿈 문자를 공백으로 대체하여 한 줄로 만듭니다 (선택 사항).
        # text_for_formatting = original_text_content.replace("\\n", " ")
        text_for_formatting = original_text_content # 또는 기존 줄바꿈 유지

        formatted_lines = []
        for i in range(0, len(text_for_formatting), 50):
            formatted_lines.append(text_for_formatting[i:i+50])
        
        formatted_text_content = "\n".join(formatted_lines) # 이 부분을 수정합니다.
        logger.info(f"Successfully formatted text content. New length: {len(formatted_text_content)}")

        base_name, ext = os.path.splitext(original_txt_file_name)
        # formatted_txt_file_name = f"{base_name}_formatted{ext}" # 이전 파일명 방식
        formatted_txt_file_name = f"{base_name}_formatted.txt" # 명시적으로 .txt 사용
        formatted_file_path = os.path.join(logs_dir, formatted_txt_file_name)

        logger.debug(f"Writing formatted text to: {formatted_file_path}")
        with open(formatted_file_path, "w", encoding="utf-8") as f:
            f.write(formatted_text_content)
        
        success_msg = f"Text content from {original_txt_file_name} was formatted and saved to {formatted_file_path}"
        logger.info(success_msg)

        # 원본 파일 삭제 로직 추가
        try:
            os.remove(original_file_path)
            logger.info(f"Successfully deleted original file: {original_file_path}")
        except OSError as e:
            logger.warning(f"Could not delete original file {original_file_path}: {e}", exc_info=True)
        
        return success_msg

    except FileNotFoundError: # 위에서 이미 처리했지만, 이중 확인
        raise
    except IOError as e:
        error_msg = f"IOError during text formatting for {original_txt_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise
    except Exception as e:
        error_msg = f"An unexpected error occurred during text formatting for {original_txt_file_name}: {e}"
        logger.error(error_msg, exc_info=True)
        raise 