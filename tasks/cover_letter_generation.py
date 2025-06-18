from api.celery_app import celery_app
import logging
import os
import traceback
from celery.exceptions import MaxRetriesExceededError, Reject
from celery import states
from typing import Dict, Any, Optional

from api.utils.file_utils import sanitize_filename, try_format_log, get_datetime_prefix, save_content_to_file
from api.utils.celery_utils import _update_root_task_state, get_detailed_error_info
from api.generate_cover_letter_semantic import generate_cover_letter
from langchain_groq import ChatGroq
from langchain.prompts import ChatPromptTemplate
from langchain.chains import LLMChain
from api.core.config import settings

logger = logging.getLogger(__name__)

# LLM 모델 초기화 (모듈 레벨에서 한 번만)
try:
    llm = ChatGroq(groq_api_key=settings.GROQ_API_KEY, model_name=settings.GROQ_LLM_MODEL)
    logger.info("ChatGroq model loaded successfully during module initialization.")
except Exception as e:
    logger.error(f"Failed to load ChatGroq model during module initialization: {e}")
    llm = None

@celery_app.task(bind=True, name='celery_tasks.step_4_generate_cover_letter', max_retries=1, default_retry_delay=20)
def step_4_generate_cover_letter(self, prev_result: Dict[str, Any], chain_log_id: str, user_prompt_text: Optional[str]) -> Dict[str, Any]:
    """Celery 작업: 필터링된 텍스트와 사용자 프롬프트를 기반으로 자기소개서를 생성하고 저장합니다."""
    task_id = self.request.id
    root_task_id = chain_log_id
    log_prefix = f"[Task {task_id} / Root {root_task_id} / Step 4_generate_cover_letter]"
    logger.info(f"{log_prefix} ---------- Task started. Received prev_result: { {k: (v[:100] + '...' if isinstance(v, str) and len(v) > 100 else v) for k, v in prev_result.items()} }, User Prompt: {'Provided' if user_prompt_text else 'Not provided'} ----------")

    filtered_content = prev_result.get("filtered_content")
    original_url = prev_result.get("original_url", 'N/A')
    # html_file_path = prev_result.get("html_file_path", 'N/A') # 현재 사용되지 않음
    # raw_text_file_path = prev_result.get("raw_text_file_path", 'N/A') # 현재 사용되지 않음
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
        # 작업 시작 시 상태 업데이트 (진행률 0%)
        self.update_state(state='PROGRESS', meta={'current_step': '자기소개서 생성 준비 중입니다.', 'percentage': 0, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_STARTED'})
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.STARTED, # STARTED 상태 유지 또는 PROGRESS로 변경 가능
            meta={
                'current_step': '맞춤형 자기소개서 생성을 시작합니다...',
                'status_message': "(4_generate_cover_letter) 자기소개서 생성 시작", 
                'user_prompt': bool(user_prompt_text), 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_STARTED',
                'percentage': 5 # 예시 진행률
            }
        )

        # LLM 호출 전 상태 업데이트 (진행률 30%)
        self.update_state(state='PROGRESS', meta={'current_step': '핵심 내용 분석 및 자기소개서 초안 작성 중...', 'percentage': 30, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_LLM_INPUT_PREP'})
        _update_root_task_state(
            root_task_id=root_task_id,
            state=states.STARTED,
            meta={
                'current_step': '핵심 내용 분석 및 자기소개서 초안 작성 중입니다. 잠시만 기다려주세요.',
                'status_message': "(4_generate_cover_letter) LLM 초안 작성 중",
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_LLM_INPUT_PREP',
                'percentage': 35 # 예시 진행률
            }
        )

        # generate_cover_letter_semantic 모듈의 함수를 직접 호출
        generated_text_tuple = generate_cover_letter(
            job_posting_content=filtered_content,
            prompt=user_prompt_text
        )
        cover_letter_text = generated_text_tuple[0] # 첫 번째 요소를 사용
        # formatted_cv = generated_text_tuple[1] # 포맷팅된 버전, 필요시 사용
        
        # cover_letter_text 결과 유효성 검사 강화
        if not cover_letter_text or "생성 실패" in cover_letter_text or len(cover_letter_text) < 50: # 최소 길이 조건 추가
            error_message_llm = f"LLM cover letter generation failed or returned invalid content. Response: {try_format_log(cover_letter_text)}" # try_format_log 사용
            logger.error(f"{log_prefix} {error_message_llm}")
            # 실패 상태 업데이트
            self.update_state(state=states.FAILURE, meta={'current_step': '오류: 자기소개서 생성 중 LLM 응답에 문제가 발생했습니다.', 'percentage': 50, 'error': error_message_llm, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_LLM_FAILED'})
            _update_root_task_state(
                root_task_id=root_task_id, 
                state=states.FAILURE, 
                meta={
                    'current_step': '오류: 자기소개서 생성에 실패했습니다. (LLM 응답 문제)',
                    'status_message': f"(4_generate_cover_letter) 실패: LLM 생성 오류", 
                    'error': error_message_llm, 
                    'details': 'LLM returned empty, failed, or too short content for cover letter.', 
                    'current_task_id': task_id,
                    'pipeline_step': 'COVER_LETTER_GENERATION_LLM_FAILED'
                }
            )
            raise ValueError(error_message_llm) # 구체적인 에러 메시지와 함께 ValueError 발생

        logger.info(f"{log_prefix} 자기소개서 생성 성공 (길이: {len(cover_letter_text)}) ")
        # LLM 호출 성공 후 상태 업데이트 (진행률 70%)
        self.update_state(state='PROGRESS', meta={'current_step': '자기소개서 초안이 완성되었습니다. 최종 검토 및 저장을 진행합니다.', 'percentage': 70, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_LLM_COMPLETED'})
        _update_root_task_state(
            root_task_id=root_task_id,
            state=states.STARTED,
            meta={
                'current_step': '자기소개서 초안이 완성되었습니다. 최종 검토 및 저장을 진행합니다...',
                'status_message': "(4_generate_cover_letter) LLM 생성 완료, 저장 준비 중",
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_LLM_COMPLETED',
                'percentage': 75 # 예시 진행률
            }
        )

        # 파일 저장 경로 및 이름 생성 (sanitize_filename 사용)
        # base_fn = os.path.splitext(os.path.basename(filtered_text_file_path))[0].replace("_filtered_text", "") if filtered_text_file_path != 'N/A' else sanitize_filename(original_url, ensure_unique=False)
        # cover_letter_filename = sanitize_filename(f"{base_fn}_cover_letter", "txt", ensure_unique=True)
        # cover_letter_file_path = os.path.join("logs", cover_letter_filename)
        
        # 파일명 생성 로직 단순화 (original_url 기반, chain_log_id 일부 포함하여 고유성 증대)
        fn_prefix = sanitize_filename(original_url if original_url and original_url != 'N/A' else "job_posting", ensure_unique=False)
        unique_suffix = chain_log_id[:8] if chain_log_id else os.urandom(4).hex() # chain_log_id가 없을 경우 대비
        cover_letter_filename = sanitize_filename(f"{fn_prefix}_{unique_suffix}_cover_letter", "txt", ensure_unique=True)
        cover_letter_file_path = os.path.join("logs", cover_letter_filename)

        with open(cover_letter_file_path, "w", encoding="utf-8") as f:
            f.write(cover_letter_text)
        logger.info(f"{log_prefix} 생성된 자기소개서 파일 저장 완료: {cover_letter_file_path}")
        # 파일 저장 후 상태 업데이트 (진행률 90%)
        self.update_state(state='PROGRESS', meta={'current_step': '생성된 자기소개서를 안전하게 저장했습니다.', 'percentage': 90, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_SAVED'})
        _update_root_task_state(
            root_task_id=root_task_id,
            state=states.STARTED, # SUCCESS 전 마지막 PROGRESS
            meta={
                'current_step': '자기소개서가 저장되었습니다. 최종 결과를 정리합니다.',
                'status_message': "(4_generate_cover_letter) 파일 저장 완료",
                'cover_letter_file_path': cover_letter_file_path,
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_SAVED',
                'percentage': 95 # 예시 진행률
            }
        )

        # 최종 결과 업데이트
        final_result = {
            "page_title": original_url, # FastAPI 응답에서 사용될 수 있음
            "original_url": original_url,
            "cover_letter_text": cover_letter_text,
            "cover_letter_file_path": cover_letter_file_path,
            "chain_log_id": chain_log_id,
            "status_message": "자기소개서 생성 완료",
            "current_step": "자기소개서 생성이 성공적으로 완료되었습니다!",
            "pipeline_step": "COVER_LETTER_GENERATION_COMPLETED" # 최종 단계 명시
        }
        # 최종 성공 상태 업데이트 (진행률 100%)
        self.update_state(state=states.SUCCESS, meta=final_result) # 여기서는 final_result에 percentage: 100 추가해도 좋음
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.SUCCESS, # 최종 성공 상태
            meta={**final_result, 'percentage': 100} # 기존 final_result에 percentage 추가
        )

        logger.info(f"{log_prefix} ---------- Task finished successfully. Returning result. ----------")
        return final_result

    except ValueError as e_val:
        logger.error(f"{log_prefix} ValueError in step 4: {e_val}", exc_info=True)
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': f'오류: 자기소개서 생성 중 값 관련 문제가 발생했습니다. ({str(e_val)})', 'error': str(e_val), 'type': 'ValueError', 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'})
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_val, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': f'오류: 자기소개서 생성 중 입력값 관련 문제가 발생했습니다. ({str(e_val)})',
                'status_message': f"(4_generate_cover_letter) 실패: {str(e_val)}", 
                'error': str(e_val), 
                'type': 'ValueError', 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise Reject(f"Step 4 failed due to ValueError: {e_val}", requeue=False)

    except MaxRetriesExceededError as e_max_retries: # LLM 호출 관련 재시도 초과 (generate_cover_letter 내부에서 처리될 수도 있음)
        error_message = f"Max retries exceeded for LLM call: {e_max_retries}"
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: 자기소개서 생성 재시도 한도를 초과했습니다.', 'error': error_message, 'type': 'MaxRetriesExceededError', 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'})
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_max_retries, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': '오류: 자기소개서 생성 재시도 한도를 초과했습니다. 잠시 후 다시 시도해주세요.',
                'status_message': f"(4_generate_cover_letter) 실패: {error_message}", 
                'error': error_message, 
                'type': 'MaxRetriesExceededError', 
                'current_task_id': task_id,
                'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'
            }
        )
        raise Reject(f"Step 4 failed due to MaxRetriesExceededError: {e_max_retries}", requeue=False)
        
    except Exception as e_gen: # 그 외 모든 예외 처리
        error_message = f"Unexpected error in cover letter generation: {e_gen}"
        detailed_error_info = get_detailed_error_info(e_gen) # 상세 오류 정보 추출
        logger.error(f"{log_prefix} {error_message}", exc_info=True)
        # 실패 상태 업데이트
        self.update_state(state=states.FAILURE, meta={'current_step': '오류: 자기소개서 생성 중 예기치 않은 문제가 발생했습니다.', 'error': error_message, 'type': str(type(e_gen).__name__), 'details': detailed_error_info, 'current_task_id': task_id, 'pipeline_step': 'COVER_LETTER_GENERATION_FAILED'})
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=e_gen, 
            traceback_str=traceback.format_exc(), 
            meta={
                'current_step': '오류: 자기소개서 생성 중 예기치 않은 문제가 발생했습니다. 관리자에게 문의하세요.',
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