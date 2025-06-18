from api.celery_app import celery_app
import logging
import os
import traceback
from celery import states
from typing import Dict, Any
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
import time

from api.utils.file_utils import sanitize_filename
from api.utils.celery_utils import _update_root_task_state

logger = logging.getLogger(__name__)

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

    if not raw_text_file_path or not isinstance(raw_text_file_path, str):
        logger.warning(f"{log_prefix} raw_text_file_path is invalid ({raw_text_file_path}). Will use placeholder for saving filtered file name.")
        base_text_fn_for_saving = sanitize_filename(original_url if original_url != "N/A" else "unknown_source", ensure_unique=False) + f"_{chain_log_id[:8]}"
    else:
        base_text_fn_for_saving = os.path.splitext(os.path.basename(raw_text_file_path))[0].replace("_extracted_text","")

    logger.info(f"{log_prefix} Starting LLM filtering for text (length: {len(extracted_text)}). Associated raw_text_file_path for logging: {raw_text_file_path}")
    self.update_state(state='PROGRESS', meta={'current_step': '채용공고 내용 필터링을 준비 중입니다.', 'percentage': 0, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_STARTED'})
    _update_root_task_state(
        root_task_id=chain_log_id, 
        state=states.STARTED,
        meta={
            'current_step': '추출된 텍스트에서 핵심 채용공고 내용을 선별하고 있습니다...',
            'status_message': f"({step_log_id}) LLM 채용공고 필터링 시작", 
            'current_task_id': task_id, 
            'pipeline_step': 'CONTENT_FILTERING_STARTED',
            'percentage': 5 # 예시 진행률
        }
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
                        'current_step': '채용공고 내용이 너무 길어 일부만 사용하여 분석합니다...',
                        'status_message': f"({step_log_id}) LLM 입력 텍스트 일부 사용 (길이 초과)", 
                        'original_len': len(raw_text), 
                        'truncated_len': len(text_for_llm),
                        'current_task_id': task_id,
                        'pipeline_step': 'CONTENT_FILTERING_INPUT_TRUNCATED'
                    }
                )
            
            logger.info(f"{log_prefix} Text length for LLM: {len(text_for_llm)}")
            logger.debug(f"{log_prefix} Text for LLM (first 500 chars): {text_for_llm[:500]}")

            self.update_state(state='PROGRESS', meta={'current_step': 'LLM을 통해 채용공고 핵심 내용을 분석 중입니다...', 'percentage': 30, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_LLM_INVOKE'})
            _update_root_task_state(
                root_task_id=chain_log_id,
                state=states.STARTED,
                meta={
                    'current_step': 'LLM을 통해 채용공고 핵심 내용을 분석하고 있습니다. 시간이 다소 소요될 수 있습니다.',
                    'status_message': f"({step_log_id}) LLM 호출 중",
                    'current_task_id': task_id,
                    'pipeline_step': 'CONTENT_FILTERING_LLM_INVOKE',
                    'percentage': 35 # 예시 진행률
                }
            )

            try:
                logger.info(f"{log_prefix} >>> Attempting llm_chain.invoke NOW...")
                start_time_llm_invoke = time.time()
                filtered_content = llm_chain.invoke({"text_content": text_for_llm})
                end_time_llm_invoke = time.time()
                duration_llm_invoke = end_time_llm_invoke - start_time_llm_invoke
                logger.info(f"{log_prefix} <<< llm_chain.invoke completed. Duration: {duration_llm_invoke:.2f} seconds.")
                logger.info(f"{log_prefix} LLM filtering complete. Output length: {len(filtered_content)}")
                logger.debug(f"{log_prefix} Filtered content (first 500 chars): {filtered_content[:500]}")
                self.update_state(state='PROGRESS', meta={'current_step': '채용공고 핵심 내용 분석 완료. 결과를 저장합니다.', 'percentage': 70, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_LLM_COMPLETED'})
                _update_root_task_state(
                    root_task_id=chain_log_id,
                    state=states.STARTED,
                    meta={
                        'current_step': '채용공고 핵심 내용 분석이 완료되었습니다. 결과를 저장하고 다음 단계를 준비합니다.',
                        'status_message': f"({step_log_id}) LLM 분석 완료",
                        'current_task_id': task_id,
                        'pipeline_step': 'CONTENT_FILTERING_LLM_COMPLETED',
                        'percentage': 75 # 예시 진행률
                    }
                )
            except Exception as e_llm_invoke:
                logger.error(f"{log_prefix} !!! EXCEPTION during llm_chain.invoke: {type(e_llm_invoke).__name__} - {str(e_llm_invoke)}", exc_info=True)
                err_details_invoke = {'error': str(e_llm_invoke), 'type': type(e_llm_invoke).__name__, 'traceback': traceback.format_exc(), 'context': 'llm_chain.invoke'}
                self.update_state(state=states.FAILURE, meta={'current_step': '오류: LLM 채용공고 분석 중 문제가 발생했습니다.', 'error': str(e_llm_invoke), 'type': type(e_llm_invoke).__name__, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_FAILED'})
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
        self.update_state(state='PROGRESS', meta={'current_step': '분석된 채용공고 내용을 안전하게 저장했습니다.', 'percentage': 90, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_SAVED'})
        _update_root_task_state(
            root_task_id=chain_log_id,
            state=states.STARTED, # SUCCESS 전 마지막 PROGRESS 상태로 간주
            meta={
                'current_step': '핵심 채용공고 내용 선별 완료. 자기소개서 생성을 준비합니다...',
                'status_message': f"({step_log_id}) 필터링된 텍스트 파일 저장 완료", 
                'filtered_text_file_path': filtered_text_file_path, 
                'current_task_id': task_id, 
                'pipeline_step': 'CONTENT_FILTERING_COMPLETED',
                'percentage': 95 # 예시 진행률
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
        self.update_state(state=states.SUCCESS, meta={**result_to_return, 'current_step': '채용공고 내용 필터링이 성공적으로 완료되었습니다.', 'percentage': 100, 'pipeline_step': 'CONTENT_FILTERING_SUCCESS'})
        return result_to_return

    except Exception as e:
        logger.error(f"{log_prefix} Error filtering with LLM: {e}", exc_info=True)
        if filtered_text_file_path and os.path.exists(filtered_text_file_path):
            try: os.remove(filtered_text_file_path)
            except Exception as e_remove: logger.warning(f"{log_prefix} Failed to remove partial filtered file {filtered_text_file_path}: {e_remove}")

        err_details = {'error': str(e), 'type': type(e).__name__, 'filtered_file': raw_text_file_path, 'traceback': traceback.format_exc()}
        logger.error(f"{log_prefix} Attempting to update root task {chain_log_id} with pipeline FAILURE status due to exception. Error details: {err_details}")
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: 채용공고 내용 필터링 중 예기치 않은 문제가 발생했습니다.', **err_details, 'current_task_id': task_id, 'pipeline_step': 'CONTENT_FILTERING_FAILED'})
        _update_root_task_state(
            root_task_id=chain_log_id, 
            state=states.FAILURE, 
            exc=e, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': '오류: 채용공고 내용 필터링 중 문제가 발생했습니다.',
                'status_message': f"({step_log_id}) LLM 필터링 실패", 
                **err_details, 
                'current_task_id': task_id, 
                'pipeline_step': 'CONTENT_FILTERING_FAILED'
            }
        )
        logger.error(f"{log_prefix} Root task {chain_log_id} updated with pipeline FAILURE status.")
        raise 