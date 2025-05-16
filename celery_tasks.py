from celery_app import celery_app
import logging
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

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
    logger.info(f"perform_processing_stub called with URL: {target_url}, Query: {query}")
    # 여기에 실제 처리 로직 구현
    return {"url": target_url, "query": query, "status": "processed_stub"} 