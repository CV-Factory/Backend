import logging
import traceback
from typing import Optional, Dict, Any
from celery import states, current_app # current_app 대신 celery_app 직접 참조 제거
from celery.result import AsyncResult

logger = logging.getLogger(__name__)

# try_format_log 함수 추가 시작
def try_format_log(data, max_len=200):
    if data is None:
        return "None"
    try:
        if isinstance(data, bytes): # bytes 타입 처리 추가
            s = data.decode('utf-8', errors='replace')
        else:
            s = str(data)
        if len(s) > max_len:
            return s[:max_len] + f"... (len: {len(s)})"
        return s
    except Exception:
        return f"[Unloggable data of type {type(data).__name__}]"
# try_format_log 함수 추가 끝

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

        # traceback_str 준비 (exc가 제공된 경우)
        if exc and not traceback_str:
            traceback_str = traceback.format_exc()

        if state == states.SUCCESS:
            # SUCCESS 상태일 경우, 전달된 meta로 덮어쓴다.
            final_meta_for_update = current_meta_to_store
            logger.info(f"{log_prefix} SUCCESS 상태. meta를 병합하지 않고 전달된 값으로 덮어씁니다: {try_format_log(final_meta_for_update)}")
        else: # SUCCESS 상태가 아닐 때만 기존 meta와 병합
            try:
                # 기존 메타를 직접 가져오기보다는, 새로운 정보로 덮어쓰거나 추가하는 로직에 집중합니다.
                # Celery의 상태 업데이트는 보통 새로운 정보로 기존 정보를 대체하는 방식입니다.
                # 따라서, 여기서는 복잡한 병합 로직 대신 전달된 current_meta_to_store를 사용하고,
                # 필요한 경우 호출하는 쪽에서 병합 로직을 처리하도록 단순화합니다.
                # 혹은, 명시적으로 기존 메타를 읽어와서 병합할 수도 있지만,
                # store_result는 기본적으로 meta를 덮어쓰므로, 여기서의 병합은 큰 의미가 없을 수 있습니다.
                # 여기서는 로그 목적으로만 기존 메타를 조회하고, 실제 업데이트는 final_meta_for_update를 사용합니다.
                
                existing_task_result = AsyncResult(root_task_id, app=celery_app_instance)
                existing_meta = existing_task_result.info if isinstance(existing_task_result.info, dict) else {}
                
                if existing_meta and isinstance(current_meta_to_store, dict):
                    # 실제 상태 업데이트 시에는 final_meta_for_update가 사용되지만,
                    # 로깅을 위해 병합된 메타를 보여줄 수 있습니다.
                    # 하지만 상태 업데이트의 표준 동작은 덮어쓰기이므로, 
                    # 여기서는 current_meta_to_store를 final_meta_for_update로 사용합니다.
                    # 만약 병합이 꼭 필요하다면, 이 부분의 로직을 재검토해야 합니다.
                    # 현재는 로깅 개선 및 호출 단순화에 집중.
                    final_meta_for_update = {**existing_meta, **current_meta_to_store} # 병합된 메타 (로깅 및 잠재적 사용)
                    logger.info(f"{log_prefix} 기존 meta ({try_format_log(existing_meta)})와 전달된 meta ({try_format_log(current_meta_to_store)})를 병합하여 업데이트합니다: {try_format_log(final_meta_for_update)}")
                elif not existing_meta:
                    final_meta_for_update = current_meta_to_store
                    logger.info(f"{log_prefix} 기존 meta가 없어 전달된 값으로 meta를 설정합니다: {try_format_log(final_meta_for_update)}")
                else: # existing_meta는 있지만 current_meta_to_store가 dict가 아닌 경우 등
                    final_meta_for_update = current_meta_to_store
                    logger.warning(f"{log_prefix} 기존 meta는 있으나 ({try_format_log(existing_meta)}), 전달된 meta ({try_format_log(current_meta_to_store)})의 타입 문제로 병합하지 않고 전달된 값으로 설정합니다.")

            except Exception as e_fetch_meta:
                logger.error(f"{log_prefix} 기존 meta 조회 중 오류 발생. 전달된 meta로 업데이트 시도: {e_fetch_meta}", exc_info=True)
                final_meta_for_update = current_meta_to_store

        # 실제 상태 업데이트 (AsyncResult 객체의 update_state 대신 backend.store_result 사용)
        celery_app_instance.backend.store_result(
            task_id=root_task_id, 
            result=final_meta_for_update, # Celery에서 meta는 result 필드의 일부로 다뤄짐
            state=state,
            traceback=traceback_str,
            # request=request # task 컨텍스트 외부이므로 request 객체 전달 어려움
        )
        logger.info(f"{log_prefix} 상태 '{state}' 및 메타 정보 업데이트 시도 완료 (store_result 호출). 저장 시도 meta: {try_format_log(final_meta_for_update)}")

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