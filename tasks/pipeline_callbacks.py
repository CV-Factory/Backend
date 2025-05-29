from celery_app import celery_app
import logging
import datetime
import traceback
from celery import states
from celery.result import AsyncResult
from typing import Any, Dict, List, Union

from celery import Celery, chord, group
from utils.celery_utils import _update_root_task_state
from utils.file_utils import try_format_log

logger = logging.getLogger(__name__)

@celery_app.task(bind=True, name="celery_tasks.handle_pipeline_completion")
def handle_pipeline_completion(self, result_or_request_obj: Any, *, root_task_id: str, is_success: bool):
    log_prefix = f"[PipelineCompletion / Root {root_task_id} / Task {self.request.id[:4]}]"
    logger.info(f"{log_prefix} 파이프라인 완료 콜백 시작. Success: {is_success}, Result/Request: {try_format_log(result_or_request_obj, max_len=200)}")

    final_status_meta = {
        "pipeline_overall_status": "SUCCESS" if is_success else "FAILURE",
        "completed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "root_task_id": root_task_id,
    }

    if is_success:
        logger.info(f"{log_prefix} 파이프라인 성공적으로 완료. 결과: {try_format_log(result_or_request_obj)}")
        
        cover_letter_text_to_store = None
        status_message_to_store = "파이프라인 성공적으로 완료 (자기소개서 텍스트 확인 필요)"

        if isinstance(result_or_request_obj, dict):
            cover_letter_text_to_store = result_or_request_obj.get("cover_letter_text")
            status_message_to_store = result_or_request_obj.get("status_message", "파이프라인 성공적으로 완료") # 기존 status_message 사용
            if not cover_letter_text_to_store:
                logger.warning(f"{log_prefix} 성공 결과 딕셔너리에 'cover_letter_text' 키가 없습니다. result_or_request_obj: {try_format_log(result_or_request_obj)}")
        else:
            logger.warning(f"{log_prefix} 성공 결과가 dict 타입이 아님: {type(result_or_request_obj)}. 자기소개서 텍스트를 저장할 수 없습니다.")

        # SUCCESS 상태의 meta에는 자기소개서 텍스트 또는 상태 메시지만 저장
        # 다른 정보(예: final_status_meta의 다른 키들)는 여기서는 저장하지 않음.
        # 필요하다면, result_data_for_state에 다른 주요 정보를 추가할 수 있음.
        result_data_for_state = cover_letter_text_to_store if cover_letter_text_to_store else status_message_to_store

        # _update_root_task_state(
        #     root_task_id=root_task_id, 
        #     state=states.SUCCESS,
        #     meta=result_data_for_state # 자기소개서 텍스트 또는 대체 메시지를 meta로 직접 저장
        # )

        # AsyncResult를 사용하여 직접 meta 업데이트
        try:
            root_task_async_result = AsyncResult(root_task_id, app=celery_app)
            root_task_async_result.update_state(
                state=states.SUCCESS,
                meta=result_data_for_state
            )
            logger.info(f"{log_prefix} Root task {root_task_id} 최종 상태 SUCCESS 및 meta 직접 업데이트 성공. 저장된 meta: {try_format_log(result_data_for_state)}")
        except Exception as e_update:
            logger.error(f"{log_prefix} Root task {root_task_id} 상태/meta 직접 업데이트 중 오류: {e_update}", exc_info=True)
            # 실패 시 fallback으로 기존 _update_root_task_state 호출 (선택적)
            # _update_root_task_state(root_task_id=root_task_id, state=states.SUCCESS, meta=result_data_for_state)

    else:
        logger.error(f"{log_prefix} 파이프라인 실패로 완료됨. Request object (or error info): {try_format_log(result_or_request_obj)}")
        
        task_result = AsyncResult(root_task_id, app=celery_app)
        existing_meta = task_result.info if isinstance(task_result.info, dict) else {}
        
        error_details = {
            "status_message": "파이프라인 실패.",
            "error_source": "Unknown (check individual task logs or previous root task meta)",
        }

        current_exc = None
        current_traceback = None

        if isinstance(result_or_request_obj, Exception):
            current_exc = result_or_request_obj
            logger.warning(f"{log_prefix} result_or_request_obj is an Exception. Traceback might not be available here directly.")
            error_details['error'] = str(current_exc)
            error_details['error_type'] = type(current_exc).__name__
            current_traceback = getattr(current_exc, '__traceback__', None)
            if current_traceback:
                current_traceback = traceback.format_tb(current_traceback)
            else:
                pass 

        elif 'exc' in existing_meta:
            error_details['status_message'] = existing_meta.get('status_message', '파이프라인 실패 (기존 에러 정보 존재)')
            error_details['error'] = existing_meta.get('error', 'N/A')
            error_details['error_type'] = existing_meta.get('type', 'N/A')
            error_details['current_step_at_failure'] = existing_meta.get('pipeline_step')
            current_traceback = existing_meta.get('traceback_str', traceback.format_exc())
            try:
                if isinstance(existing_meta.get('exc'), Exception):
                    current_exc = existing_meta['exc']
                elif isinstance(existing_meta.get('error'), str):
                    pass 
            except Exception as e_meta_exc:
                logger.warning(f"{log_prefix} Error processing 'exc' from existing_meta: {e_meta_exc}")
        
        final_status_meta.update(error_details)

        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=current_exc,
            traceback_str=current_traceback if isinstance(current_traceback, str) else traceback.format_exc(),
            meta=final_status_meta
        )
        logger.error(f"{log_prefix} Root task {root_task_id} 최종 상태 FAILURE로 업데이트됨 (콜백에 의해). Exception: {current_exc}")

    logger.info(f"{log_prefix} 파이프라인 완료 콜백 종료.")
    return { "callback_processed": True, "root_task_id": root_task_id, "final_status_reported": final_status_meta.get('pipeline_overall_status') } 