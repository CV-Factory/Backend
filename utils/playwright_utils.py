import logging
import uuid
import time
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError, Page, Frame, Locator, ElementHandle # Frame, Locator, ElementHandle 추가
from typing import Union # 추가

# 로거 설정
logger = logging.getLogger(__name__)

DEFAULT_PAGE_TIMEOUT = 60000 # 페이지 기본 타임아웃 (밀리초)
PAGE_NAVIGATION_TIMEOUT = 60000 # 페이지 네비게이션 타임아웃 (밀리초)
# 상수 정의 (celery_tasks.py에서 이동)
MAX_IFRAME_DEPTH = 1
IFRAME_LOAD_TIMEOUT = 30000  # 밀리초
ELEMENT_HANDLE_TIMEOUT = 20000 # 밀리초
GET_ATTRIBUTE_TIMEOUT = 10000 # 밀리초
EVALUATE_TIMEOUT_SHORT = 10000 # 밀리초

def _flatten_iframes_in_live_dom_sync(current_playwright_context: Union[Page, Frame],
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
    initial_count = 0

    try:
        # 타입 힌트 명시 (Union[Page, Frame]은 locator 메소드를 가짐)
        initial_iframe_locator: Locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])')
        initial_count = initial_iframe_locator.count()
        logger.info(f"{log_prefix} Initial check: Found {initial_count} processable iframe(s) at this depth.")
        if initial_count == 0:
            logger.info(f"{log_prefix} No processable iframes found at this depth based on initial check.")
            return
    except PlaywrightError as e_initial_count:
        logger.warning(f"{log_prefix} PlaywrightError during initial iframe count: {e_initial_count}. Setting initial_count to 0 and proceeding with loop if possible.", exc_info=True)
        initial_count = 0
    except Exception as e_initial_count_other:
        logger.error(f"{log_prefix} Unexpected error during initial iframe count: {e_initial_count_other}. Setting initial_count to 0.", exc_info=True)
        initial_count = 0

    loop_iteration_count = 0
    max_loop_iterations = initial_count + 20
    logger.debug(f"{log_prefix} Calculated max_loop_iterations: {max_loop_iterations} (based on initial_count: {initial_count})")

    while loop_iteration_count < max_loop_iterations:
        loop_iteration_count += 1
        # 타입 힌트 명시
        iframe_locator: Locator = current_playwright_context.locator('iframe:not([data-cvf-processed="true"]):not([data-cvf-error="true"])').first

        try:
            if iframe_locator.count() == 0:
                logger.info(f"{log_prefix} No more processable iframes found. Exiting loop after {loop_iteration_count-1} iterations.")
                break
        except PlaywrightError as e_no_more_iframes:
            logger.info(f"{log_prefix} No more processable iframes found (locator.first likely timed out or element disappeared). Exiting loop. Error: {e_no_more_iframes}")
            break
        except Exception as e_count_check_unexpected:
            logger.error(f"{log_prefix} Unexpected error checking for remaining iframes: {e_count_check_unexpected}. Exiting loop.", exc_info=True)
            break

        iframe_handle: Optional[ElementHandle] = None # 타입 힌트 추가
        iframe_log_id = f"iframe-gen-{uuid.uuid4().hex[:6]}"

        try:
            logger.debug(f"{log_prefix} Attempting to get/set ID for the first found iframe.")
            try:
                existing_id = iframe_locator.get_attribute('id', timeout=GET_ATTRIBUTE_TIMEOUT)
                if existing_id:
                    iframe_log_id = existing_id
                else:
                    iframe_locator.evaluate("(el, id) => el.id = id", iframe_log_id, timeout=EVALUATE_TIMEOUT_SHORT)
            except PlaywrightError as e_id_timeout:
                 logger.warning(f"{log_prefix} Timeout or PlaywrightError getting/setting ID for an iframe (iteration {loop_iteration_count}). Using generated: {iframe_log_id}. Error: {e_id_timeout}")
            except Exception as e_set_id:
                logger.warning(f"{log_prefix} Could not reliably set/get ID for an iframe (iteration {loop_iteration_count}). Using generated: {iframe_log_id}. Error: {e_set_id}")

            logger.info(f"{log_prefix} Processing iframe (loop iteration #{loop_iteration_count}, Effective ID: {iframe_log_id}).")
            iframe_locator.evaluate("el => el.setAttribute('data-cvf-processing', 'true')", timeout=EVALUATE_TIMEOUT_SHORT)

            iframe_handle = iframe_locator.element_handle(timeout=ELEMENT_HANDLE_TIMEOUT)
            if not iframe_handle:
                logger.warning(f"{log_prefix} Null element_handle for iframe {iframe_log_id}. Marking with error and skipping.")
                iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                continue

            iframe_src_attr = "[src attribute not found or error]"
            try:
                iframe_src_attr = iframe_handle.get_attribute('src') or "[src attribute not found]"
            except Exception as e_get_src:
                logger.warning(f"{log_prefix} Error getting src attribute for iframe {iframe_log_id}: {e_get_src}")

            logger.debug(f"{log_prefix} iframe {iframe_log_id} src attribute: {iframe_src_attr[:150]}")

            child_frame: Optional[Frame] = None # 타입 힌트 추가
            try:
                child_frame = iframe_handle.content_frame()
            except Exception as e_content_frame:
                logger.error(f"{log_prefix} Error getting content_frame for iframe {iframe_log_id}: {e_content_frame}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            if not child_frame:
                logger.warning(f"{log_prefix} content_frame is None for iframe {iframe_log_id}. Marking with error and skipping.")
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            child_frame_url_for_log = "[child frame URL not accessible]"
            try:
                child_frame_url_for_log = child_frame.url
            except Exception:
                pass

            try:
                logger.info(f"{log_prefix} Waiting for child_frame (ID: {iframe_log_id}, URL: {child_frame_url_for_log}) to load (domcontentloaded)...")
                child_frame.wait_for_load_state('domcontentloaded', timeout=IFRAME_LOAD_TIMEOUT)
                final_child_frame_url = "[child frame final URL not accessible]"
                try:
                    final_child_frame_url = child_frame.url
                except Exception:
                    pass
                logger.info(f"{log_prefix} Child_frame (ID: {iframe_log_id}, Final URL: {final_child_frame_url}) loaded.")
            except PlaywrightError as frame_load_ple:
                logger.error(f"{log_prefix} PlaywrightError (Timeout or other) loading child_frame {iframe_log_id} (src attr: {iframe_src_attr[:100]}, initial URL: {child_frame_url_for_log}): {frame_load_ple}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue
            except Exception as frame_load_err:
                logger.error(f"{log_prefix} Generic error loading child_frame {iframe_log_id} (src attr: {iframe_src_attr[:100]}, initial URL: {child_frame_url_for_log}): {frame_load_err}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            _flatten_iframes_in_live_dom_sync(child_frame, current_depth + 1, max_depth, original_page_url_for_logging, chain_log_id, step_log_id)

            child_html_content = ""
            try:
                logger.debug(f"{log_prefix} Getting content from child_frame {iframe_log_id} post-recursion.")
                child_html_content = child_frame.content()
                if not child_html_content:
                    child_html_content = f"<!-- iframe {iframe_log_id} (src: {iframe_src_attr[:100]}) content was empty post-recursion -->"
            except Exception as frame_content_err:
                logger.error(f"{log_prefix} Error getting content from child_frame {iframe_log_id} (src: {iframe_src_attr[:100]}): {frame_content_err}", exc_info=True)
                iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                continue

            replacement_div_html = ""
            try:
                soup = BeautifulSoup(child_html_content, 'html.parser')
                content_to_insert = soup.body if soup.body else soup
                inner_html_str = content_to_insert.decode_contents() if content_to_insert else f"<!-- Parsed content of {iframe_log_id} was empty -->"
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

            try:
                logger.info(f"{log_prefix} Attempting to replace iframe {iframe_log_id} with its content.")
                is_connected_js = False
                if iframe_handle and hasattr(iframe_handle, 'evaluate'):
                    try:
                        is_connected_js = iframe_handle.evaluate('el => el.isConnected')
                    except Exception as e_eval_isconnected:
                        logger.warning(f"{log_prefix} Error evaluating 'el.isConnected' for iframe {iframe_log_id}: {e_eval_isconnected}")
                        is_connected_js = False

                if is_connected_js:
                    iframe_handle.evaluate("(el, html) => { el.outerHTML = html; }", replacement_div_html)
                    logger.info(f"{log_prefix} Successfully replaced iframe {iframe_log_id} with div wrapper.")
                    processed_iframe_count += 1
                else:
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} is not connected or evaluate failed. Skipping replacement.")
            except PlaywrightError as ple:
                if "NoModificationAllowedError" in str(ple) or "no parent node" in str(ple):
                    logger.warning(f"{log_prefix} Failed to replace iframe {iframe_log_id} due to NoModificationAllowedError (element likely detached): {ple}")
                else:
                    logger.error(f"{log_prefix} Failed to replace iframe {iframe_log_id} using evaluate (Playwright Error): {ple}", exc_info=True)
                try:
                    target_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"]:not([data-cvf-error="true"])')
                    if target_locator.count() == 1:
                         target_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                    else:
                        logger.warning(f"{log_prefix} iframe {iframe_log_id} not found or already marked for error after Playwright replacement failure.")
                except Exception as e_mark:
                    logger.warning(f"{log_prefix} Exception while trying to mark iframe {iframe_log_id} as error after replacement failure: {e_mark}")
            except Exception as eval_replace_err:
                logger.error(f"{log_prefix} Generic failed to replace iframe {iframe_log_id} using evaluate: {eval_replace_err}", exc_info=True)
                try:
                    target_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"]:not([data-cvf-error="true"])')
                    if target_locator.count() == 1:
                         target_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
                    else:
                        logger.warning(f"{log_prefix} iframe {iframe_log_id} not found or already marked for error after generic replacement failure.")
                except Exception as e_mark_generic:
                    logger.warning(f"{log_prefix} Exception while trying to mark iframe {iframe_log_id} as error after generic replacement failure: {e_mark_generic}")

        except Exception as e_outer_iframe_loop:
            logger.error(f"{log_prefix} General error processing iframe {iframe_log_id} (loop iteration #{loop_iteration_count}): {e_outer_iframe_loop}", exc_info=True)
            if iframe_handle:
                try:
                    if not iframe_handle.is_hidden():
                         iframe_handle.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }")
                except Exception as e_final_mark_err:
                    logger.warning(f"{log_prefix} Error during final attempt to mark iframe {iframe_log_id} as error in outer_iframe_loop: {e_final_mark_err}")
            continue
        finally:
            if iframe_handle:
                try:
                    iframe_handle.dispose()
                except Exception as e_dispose:
                    logger.warning(f"{log_prefix} Error disposing element_handle for iframe {iframe_log_id}: {e_dispose}", exc_info=True)
            try:
                problematic_iframe_locator = current_playwright_context.locator(f'iframe[id="{iframe_log_id}"][data-cvf-processing="true"]')
                if problematic_iframe_locator.count() == 1:
                    logger.warning(f"{log_prefix} iframe {iframe_log_id} was left in 'processing' state after its loop. Marking as error.")
                    problematic_iframe_locator.evaluate("el => { el.setAttribute('data-cvf-error', 'true'); el.removeAttribute('data-cvf-processing'); }", timeout=EVALUATE_TIMEOUT_SHORT)
            except PlaywrightError as e_final_ple:
                 logger.warning(f"{log_prefix} PlaywrightError during final cleanup check for iframe {iframe_log_id}: {e_final_ple}")
            except Exception as e_final_cleanup_locator:
                 logger.warning(f"{log_prefix} Generic error during final cleanup check for iframe {iframe_log_id}: {e_final_cleanup_locator}")

    if loop_iteration_count >= max_loop_iterations:
        logger.warning(f"{log_prefix} Max loop iterations ({max_loop_iterations}) reached. Exiting iframe processing to prevent infinite loop.")

    logger.info(f"{log_prefix} Finished all iframe processing attempts at depth {current_depth}. Total iterations: {loop_iteration_count-1}. Successfully processed/replaced: {processed_iframe_count}.")


def _get_playwright_page_content_with_iframes_processed(page: Page, original_url: str, chain_log_id: str, step_log_id: str) -> str:
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
        return f"<!-- Error retrieving page content: {str(e_content)} -->" 