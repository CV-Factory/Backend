import logging
import traceback
from typing import Optional, Dict, Any
from celery import states, current_app # current_app 대신 celery_app 직접 참조 제거
from celery.result import AsyncResult

logger = logging.getLogger(__name__)

def _update_root_task_state(root_task_id: str, state: str, meta: Optional[Dict[str, Any]] = None,
                            exc: Optional[Exception] = None, traceback_str: Optional[str] = None):
    log_prefix = f"[StateUpdate / Root {root_task_id}]"
    try:
        if not root_task_id:
            logger.warning(f"{log_prefix} root_task_id가 제공되지 않아 상태 업데이트를 건너<0xE1><0x8A><0xB5>니다.")
            return

        current_meta_to_store = meta if meta is not None else {}
        
        # celery_app 대신 current_app 사용
        celery_app_instance = current_app._get_current_object() # 현재 Celery 앱 인스턴스 가져오기

        if state == states.SUCCESS:
            # SUCCESS 상태일 경우, 전달된 meta로 덮어쓴다.
            final_meta_for_update = current_meta_to_store
            logger.info(f"{log_prefix} SUCCESS 상태. meta를 병합하지 않고 전달된 값으로 덮어씁니다: {try_format_log(final_meta_for_update)}")
        else: # SUCCESS 상태가 아닐 때만 기존 meta와 병합
            try:
                existing_task_result = AsyncResult(root_task_id, app=celery_app_instance) 
                existing_meta = existing_task_result.info if isinstance(existing_task_result.info, dict) else {}
                if existing_meta:
                    if isinstance(current_meta_to_store, dict):
                        merged_meta = {**existing_meta, **current_meta_to_store}
                        final_meta_for_update = merged_meta
                        logger.info(f"{log_prefix} 기존 meta와 전달된 meta를 병합했습니다: {try_format_log(final_meta_for_update)}")
                    else:
                        final_meta_for_update = current_meta_to_store # current_meta가 dict가 아니면 병합 불가, 덮어쓰기
                        logger.warning(f"{log_prefix} 기존 meta는 있으나, 전달된 meta가 딕셔너리가 아니므로 병합하지 않고 전달된 값으로 설정합니다: {try_format_log(final_meta_for_update)}")
                else:
                    final_meta_for_update = current_meta_to_store # 기존 meta가 없으면 전달된 값으로 설정
                    logger.info(f"{log_prefix} 기존 meta가 없어 전달된 값으로 meta를 설정합니다: {try_format_log(final_meta_for_update)}")
            except Exception as e_fetch_meta:
                logger.error(f"{log_prefix} 기존 meta 조회 중 오류 발생. 전달된 meta로 덮어씁니다: {e_fetch_meta}", exc_info=True)
                final_meta_for_update = current_meta_to_store

        # 실제 상태 업데이트
        task_instance = celery_app_instance.AsyncResult(root_task_id) # AsyncResult로 태스크 인스턴스를 가져옴
        task_instance.update_state(state=state, meta=final_meta_for_update)
        logger.info(f"{log_prefix} 상태 '{state}' 및 메타 정보 업데이트 성공. 저장된 meta: {try_format_log(final_meta_for_update)}")

    except Exception as e:
        logger.critical(f"[StateUpdateFailureCritical] Critically failed to update root task {root_task_id} state: {e}", exc_info=True)
        if state == 'FAILURE': # 여기서는 mark_as_failure를 직접 호출하지 않고, store_result에 의존.
             # 필요하다면 mark_as_failure 로직을 여기에 추가할 수 있으나, store_result로도 충분할 수 있음.
            logger.error(f"[StateUpdateFailure] Root task {root_task_id} is being marked as FAILURE. Meta: {meta}, Exc: {exc}")

def get_detailed_error_info(exception_obj: Exception) -> Dict[str, str]:
    """예외 객체로부터 상세 정보를 추출합니다."""
    return {
        "error_type": type(exception_obj).__name__,
        "error_message": str(exception_obj),
        "traceback": traceback.format_exc()
    } 