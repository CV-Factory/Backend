from api.celery_app import celery_app
import logging
import os
import re
from bs4 import BeautifulSoup, Comment, NavigableString
import traceback
from celery import states
from typing import Dict
from api.utils.file_utils import sanitize_filename, try_format_log
from api.utils.celery_utils import _update_root_task_state
from celery.exceptions import MaxRetriesExceededError, Reject

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, name='celery_tasks.step_2_extract_text', max_retries=1, default_retry_delay=5)
def step_2_extract_text(self, prev_result: Dict[str, str], chain_log_id: str) -> Dict[str, str]:
    """(2단계) 저장된 HTML 파일에서 텍스트를 추출하여 새 파일에 저장합니다."""
    task_id = self.request.id
    step_log_id = "2_extract_text"
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

    extracted_text_file_path = None 

    try:
        if not html_file_path or not isinstance(html_file_path, str):
            logger.warning(f"{log_prefix} html_file_path is invalid ({html_file_path}), will use placeholder for saving text file if needed, but proceeding with page_content.")
            base_html_fn_for_saving = sanitize_filename(original_url if original_url != "N/A" else "unknown_source", ensure_unique=False) + f"_{chain_log_id[:8]}"
        else:
            base_html_fn_for_saving = os.path.splitext(os.path.basename(html_file_path))[0]
            base_html_fn_for_saving = re.sub(r'_raw_html_[a-f0-9]{8}_[a-f0-9]{8}$', '', base_html_fn_for_saving)

        logger.info(f"{log_prefix} Starting text extraction from page_content (length: {len(html_content)})")
        self.update_state(state='PROGRESS', meta={'current_step': '텍스트 추출을 준비 중입니다.', 'percentage': 0, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_STARTED'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.STARTED,
            meta={
                'current_step': '추출된 HTML 내용에서 텍스트 정보를 분석하고 있습니다...',
                'status_message': f"({step_log_id}) HTML 내용에서 텍스트 추출 시작", 
                'current_task_id': task_id, 
                'pipeline_step': 'TEXT_EXTRACTION_STARTED',
                'percentage': 5 # 예시 진행률
            }
        )

        logger.debug(f"{log_prefix} HTML content from prev_result successfully received (length verified as {len(html_content)}).")
        
        logger.debug(f"{log_prefix} Initializing BeautifulSoup parser.")
        self.update_state(state='PROGRESS', meta={'current_step': 'HTML 분석기를 초기화하고 있습니다.', 'percentage': 10, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_BS_INIT'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED,
            meta={
                'current_step': 'HTML 구조 분석을 준비하고 있습니다...',
                'status_message': f"({step_log_id}) HTML 파서 초기화 중",
                'current_task_id': task_id,
                'pipeline_step': 'TEXT_EXTRACTION_BS_INIT',
                'percentage': 12 # 예시 진행률
            }
        )
        soup = BeautifulSoup(html_content, "html.parser")
        logger.info(f"{log_prefix} BeautifulSoup initialized.")

        self.update_state(state='PROGRESS', meta={'current_step': 'HTML에서 불필요한 태그(스크립트, 스타일 등)를 제거 중입니다...', 'percentage': 20, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_TAG_CLEANUP'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED,
            meta={
                'current_step': 'HTML 문서 정제 중 (스크립트, 스타일 제거 등)...',
                'status_message': f"({step_log_id}) 불필요 태그 제거 중",
                'current_task_id': task_id,
                'pipeline_step': 'TEXT_EXTRACTION_TAG_CLEANUP',
                'percentage': 22 # 예시 진행률
            }
        )

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
        
        target_soup_object = soup

        self.update_state(state='PROGRESS', meta={'current_step': '정제된 HTML에서 텍스트를 추출하고 있습니다...', 'percentage': 40, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_GET_TEXT'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED,
            meta={
                'current_step': '정제된 HTML에서 주요 텍스트를 추출합니다...',
                'status_message': f"({step_log_id}) 텍스트 추출 중",
                'current_task_id': task_id,
                'pipeline_step': 'TEXT_EXTRACTION_GET_TEXT',
                'percentage': 42 # 예시 진행률
            }
        )

        logger.debug(f"{log_prefix} Extracting text with target_soup_object.get_text().")
        text = target_soup_object.get_text(separator="\n", strip=True)
        logger.info(f"{log_prefix} Initial text extracted. Length: {len(text)}.")
        logger.debug(f"{log_prefix} Initial text (first 500 chars): {text[:500]}")

        logger.debug(f"{log_prefix} Starting specific 'n' cleanup.")
        original_text_before_n_cleanup = text
        text = re.sub(r'\s+n(?=\S)', ' ', text)
        text = re.sub(r'(?<=\S)n\s+', ' ', text)
        text = re.sub(r'\s+n\s+', ' ', text)
        if text != original_text_before_n_cleanup:
            logger.info(f"{log_prefix} Text after specific 'n' cleanup. Length: {len(text)}.")
            logger.debug(f"{log_prefix} Text after 'n' cleanup (first 500 chars): {text[:500]}")
        else:
            logger.debug(f"{log_prefix} No changes made by specific 'n' cleanup.")

        text = re.sub(r'[ \t\r\f\v\xa0]+', ' ', text)
        logger.debug(f"{log_prefix} Text after initial horizontal space/nbsp normalization (newlines preserved for now). Length: {len(text)}")
        
        text = re.sub(r' *\n *', '\n', text)
        text = re.sub(r'\n{2,}', '\n\n', text)
        text = text.strip()
        logger.info(f"{log_prefix} Text after newline and space normalization. Length: {len(text)}.")
        logger.debug(f"{log_prefix} Normalized text (first 500 chars): {text[:500]}")

        logger.debug(f"{log_prefix} Converting to single line by splitting by ANY whitespace and rejoining with single spaces.")
        words = text.split()
        text_single_line = ' '.join(words)
        logger.info(f"{log_prefix} Text converted to single line. Length: {len(text_single_line)}")
        logger.debug(f"{log_prefix} Single line text (first 500 chars): {text_single_line[:500]}")

        logger.debug(f"{log_prefix} Inserting ACTUAL newline (\n) every 50 characters.")
        chars_per_line = 50
        text_formatted = ""
        if text_single_line:
            text_formatted = '\n'.join(text_single_line[i:i+chars_per_line] for i in range(0, len(text_single_line), chars_per_line))
            logger.info(f"{log_prefix} Text formatted with newlines every {chars_per_line} characters. New length: {len(text_formatted)}")
        else:
            logger.info(f"{log_prefix} Single line text was empty, skipping 50-char formatting.")
            text_formatted = text_single_line

        self.update_state(state='PROGRESS', meta={'current_step': '추출된 텍스트 정제 작업이 완료되었습니다. 결과를 저장합니다.', 'percentage': 70, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FORMATTING_DONE'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED,
            meta={
                'current_step': '추출된 텍스트의 줄바꿈 및 공백을 최종 정리했습니다...',
                'status_message': f"({step_log_id}) 텍스트 포맷팅 완료",
                'current_task_id': task_id,
                'pipeline_step': 'TEXT_EXTRACTION_FORMATTING_DONE',
                'percentage': 72 # 예시 진행률
            }
        )

        text = text_formatted
        logger.debug(f"{log_prefix} Final extracted text for saving (first 500 chars): {text[:500]}")

        if not text:
            logger.warning(f"{log_prefix} No text extracted after processing from {html_file_path if html_file_path else 'direct content'}. Resulting file will be empty or placeholder.")
        
        logs_dir = "logs"
        logger.debug(f"{log_prefix} Ensuring logs directory exists: {logs_dir}")
        os.makedirs(logs_dir, exist_ok=True)
        
        logger.debug(f"{log_prefix} Sanitizing filename. Original html_file_path info for naming: {html_file_path if html_file_path else base_html_fn_for_saving}")
        unique_text_fn_stem = f"{base_html_fn_for_saving}_extracted_text"
        unique_text_fn = sanitize_filename(unique_text_fn_stem, "txt", ensure_unique=True)
        extracted_text_file_path = os.path.join(logs_dir, unique_text_fn)
        logger.info(f"{log_prefix} Determined extracted text file path: {extracted_text_file_path}")

        logger.debug(f"{log_prefix} Attempting to write extracted text (length: {len(text)}) to file: {extracted_text_file_path}")
        with open(extracted_text_file_path, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"{log_prefix} Text extracted and saved to: {extracted_text_file_path} (Final Length: {len(text)}) ")
        self.update_state(state='PROGRESS', meta={'current_step': '추출된 텍스트를 안전하게 저장했습니다.', 'percentage': 90, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_SAVED'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED, # SUCCESS 전 마지막 PROGRESS
            meta={
                'current_step': '텍스트 추출 완료. 불필요한 내용 필터링을 준비 중입니다...',
                'status_message': f"({step_log_id}) 텍스트 파일 저장 완료", 
                'text_file_path': extracted_text_file_path, 
                'current_task_id': task_id, 
                'pipeline_step': 'TEXT_EXTRACTION_COMPLETED',
                'percentage': 95 # 예시 진행률
            }
        )
        
        result_to_return = {"text_file_path": extracted_text_file_path, 
                             "original_url": original_url, 
                             "html_file_path": html_file_path,
                             "extracted_text": text
                            }
        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        logger.debug(f"{log_prefix} Returning from step_2: {result_to_return.keys()}, extracted_text length: {len(text)}")
        self.update_state(state=states.SUCCESS, meta={**result_to_return, 'current_step': 'HTML 분석 및 텍스트 추출이 성공적으로 완료되었습니다.', 'percentage': 100, 'pipeline_step': 'TEXT_EXTRACTION_SUCCESS'})
        return result_to_return

    except FileNotFoundError as e_fnf:
        logger.error(f"{log_prefix} FileNotFoundError during text extraction: {e_fnf}. HTML file path: {html_file_path}", exc_info=True)
        err_details_fnf = {'error': str(e_fnf), 'type': type(e_fnf).__name__, 'html_file': str(html_file_path)}
        self.update_state(state=states.FAILURE, meta={'current_step': f'오류: 텍스트 추출 중 파일을 찾지 못했습니다. ({e_fnf})', **err_details_fnf, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_fnf, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': f'오류: 텍스트 추출 중 필요한 파일을 찾지 못했습니다. ({e_fnf})',
                'status_message': f"({step_log_id}) 텍스트 추출 실패 (파일 없음)", 
                **err_details_fnf, 
                'current_task_id': task_id, 
                'pipeline_step': 'TEXT_EXTRACTION_FAILED'
            }
        )
        raise
    except IOError as e_io:
        logger.error(f"{log_prefix} IOError during text extraction: {e_io}. HTML file path: {html_file_path}", exc_info=True)
        err_details_io = {'error': str(e_io), 'type': type(e_io).__name__, 'html_file': str(html_file_path), 'traceback': traceback.format_exc()}
        self.update_state(state=states.FAILURE, meta={'current_step': f'오류: 텍스트 추출 중 파일 입출력 문제가 발생했습니다. ({e_io})', **err_details_io, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_io, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': f'오류: 텍스트 추출 중 파일 입출력 문제가 발생했습니다. ({e_io})',
                'status_message': f"({step_log_id}) 텍스트 추출 실패 (IO 오류)", 
                **err_details_io, 
                'current_task_id': task_id, 
                'pipeline_step': 'TEXT_EXTRACTION_FAILED'
            }
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
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: 텍스트 추출 중 예기치 않은 문제가 발생했습니다.', **err_details_general, 'current_task_id': task_id, 'pipeline_step': 'TEXT_EXTRACTION_FAILED'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e_general, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': '오류: 텍스트 추출 중 예기치 않은 문제가 발생했습니다.',
                'status_message': f"({step_log_id}) 텍스트 추출 중 알 수 없는 오류", 
                **err_details_general, 
                'current_task_id': task_id, 
                'pipeline_step': 'TEXT_EXTRACTION_FAILED'
            }
        )
        raise
    finally:
        logger.info(f"{log_prefix} ---------- Task execution attempt ended. ----------") 