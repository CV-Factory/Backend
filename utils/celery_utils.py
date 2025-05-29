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

        if state != states.SUCCESS: # SUCCESS 상태가 아닐 때만 기존 meta와 병합
            try:
                existing_task_result = AsyncResult(root_task_id, app=celery_app_instance) # app 인자 전달
                existing_meta = existing_task_result.info if isinstance(existing_task_result.info, dict) else {}
                if existing_meta:
                    # current_meta_to_store가 딕셔너리일 때만 병합 시도
                    if isinstance(current_meta_to_store, dict):
                        merged_meta = {**existing_meta, **current_meta_to_store}
                        current_meta_to_store = merged_meta
                    else:
                        # current_meta_to_store가 딕셔너리가 아니면 기존 메타를 유지하거나,
                        # 혹은 current_meta_to_store로 덮어쓸지 결정해야 함.
                        # 여기서는 SUCCESS가 아니므로, 기존 meta를 유지하는 방향으로 생각할 수 있으나,
                        # pipeline_callbacks.py에서 FAILURE 시에는 이미 딕셔너리 형태의 full meta를 전달하므로
                        # 이 경우는 잘 발생하지 않을 것으로 예상됨. 로깅 추가.
                        logger.warning(f"{log_prefix} Non-SUCCESS state update, but current_meta_to_store is not a dict (type: {type(current_meta_to_store)}). Using current_meta_to_store as is, existing_meta will be overwritten if current_meta_to_store is not a dict by store_result's expectation for 'result'.")
            except Exception as e_meta:
                logger.warning(f"{log_prefix} 기존 메타 정보 로드/병합 중 오류: {e_meta}", exc_info=True)
        # else: SUCCESS 상태일 때는 전달된 meta (current_meta_to_store)를 그대로 사용 (병합 X)

        # Celery 백엔드를 통해 상태와 메타데이터 저장
        celery_app_instance.backend.store_result(
            task_id=root_task_id,
            result=current_meta_to_store, # SUCCESS일 경우 문자열, 아닐 경우 병합된 딕셔너리
            state=state,
            traceback=traceback_str,
            request=None
        )
        logger.info(f"{log_prefix} 상태 '{state}' 및 메타 정보 업데이트 성공.")
        
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