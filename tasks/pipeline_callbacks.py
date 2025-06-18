from api.celery_app import celery_app
import logging
import datetime
import traceback
from celery import states, current_task, Celery, chord, group
from celery.result import AsyncResult
from typing import Any, Dict, List, Union
from kombu.utils.uuid import uuid

from api.utils.celery_utils import _update_root_task_state
from api.utils.file_utils import try_format_log

logger = logging.getLogger(__name__)

# 전역 변수로 Celery 앱 인스턴스 저장 (필요한 경우)
# celery_app_instance = get_celery_app_instance()

def get_task_logger_prefix(task_request, default_root_id="UNKNOWN_ROOT"):
    root_id = task_request.root_id if task_request and task_request.root_id else default_root_id
    task_id = task_request.id if task_request and task_request.id else "UNKNOWN_TASK"
    return f"[Task {task_id} / Root {root_id} / Callback]"


def handle_task_failure_callback(request, exc, tb):
    # request 객체에서 root_id와 현재 task_id를 가져옵니다.
    # current_task는 콜백 컨텍스트에서 현재 실행 중인 태스크가 아닐 수 있으므로 request에서 가져오는 것이 더 안전합니다.
    task_id = request.id
    root_id = request.root_id if hasattr(request, 'root_id') and request.root_id else task_id
    
    log_prefix = f"[Task {task_id} / Root {root_id} / FailureCallback]"
    logger.error(f"{log_prefix} Pipeline step failed: {request.task}(*{request.args}, **{request.kwargs}) raised {exc!r}")
    logger.error(f"{log_prefix} Traceback: {tb}")

    current_step_message = f'파이프라인 작업 중 오류 발생: {request.task}'
    if exc:
        current_step_message += f' (오류: {type(exc).__name__})'

    # 루트 태스크 상태 업데이트 (실패)
    _update_root_task_state(
        root_task_id=root_id,
        state=states.FAILURE,
        meta={
            'current_step': current_step_message,
            'failed_task_id': task_id,
            'failed_task_name': request.task,
            'error_message': str(exc),
            'error_type': type(exc).__name__,
            # 'traceback': tb # tb는 이미 문자열화된 traceback일 수 있음, 필요시 검토
        },
        exc=exc, # 실제 예외 객체 전달
        traceback_str=tb if isinstance(tb, str) else traceback.format_exception_only(type(exc), exc)[-1].strip() # 예외 메시지만 또는 전체 tb
    )
    logger.info(f"{log_prefix} Root task {root_id} updated to FAILURE due to error in task {task_id}.")


def handle_pipeline_completion(result, root_task_id: str):
    # 이 콜백은 체인의 마지막 태스크(step_4_generate_cover_letter)가 성공적으로 완료되었을 때 호출됩니다.
    # `result`는 step_4_generate_cover_letter의 반환값입니다.
    # `root_task_id`는 체인을 시작할 때 link_success로 전달한 값입니다.
    
    # current_task.request는 이 콜백 태스크 자체의 요청이므로, root_id를 명시적으로 받아야 함
    log_prefix = f"[Root {root_task_id} / CompletionCallback]"
    logger.info(f"{log_prefix} Pipeline completed. Result from last step (type: {type(result)}): {try_format_log(result)}")

    final_status_meta = {
        'pipeline_status': 'COMPLETED_SUCCESSFULLY',
        'current_step': '자기소개서 생성 완료 및 최종 처리 중...', # 성공 시 기본 메시지
        'final_result_type': str(type(result)),
        'callback_processed': False # 처리 시작 플래그
    }

    try:
        if isinstance(result, dict):
            # step_4의 결과에서 cover_letter_text를 추출하여 루트 태스크의 meta로 저장
            cover_letter_text = result.get('cover_letter_text')
            if cover_letter_text:
                # 최종 결과는 문자열 그 자체로 저장 (main.py에서 SSE로 result 필드를 그대로 사용하기 위함)
                # 또는 특정 키를 가진 단순 딕셔너리로 저장
                meta_to_store = {
                    'cover_letter_output': cover_letter_text, # main.py에서 이 키를 사용함
                    'original_url': result.get('original_url'),
                    'page_title': result.get('page_title'),
                    'cover_letter_file_path': result.get('cover_letter_file_path'),
                    'chain_log_id': result.get('chain_log_id', root_task_id), # 체인 로그 ID 추가
                    'cover_letter_text': cover_letter_text,
                    'status_message': result.get('status_message', '파이프라인 성공적으로 완료'),
                }
                final_status_meta['current_step'] = '자기소개서 생성 완료. 결과 확인 가능합니다.' # 더 명확한 메시지
                logger.info(f"{log_prefix} Successfully extracted cover_letter_text (len: {len(cover_letter_text)}) from last step result.")
            else:
                logger.warning(f"{log_prefix} 'cover_letter_text' not found or empty in the result from the last step. Storing entire result as meta.")
                meta_to_store = result # 전체 결과를 저장 (이 경우 main.py에서 처리 방식 변경 필요할 수 있음)
                final_status_meta['current_step'] = '자기소개서 생성 완료 (세부 내용 확인 필요).'
                meta_to_store['status_message'] = '자기소개서 생성 완료 (세부 내용 확인 필요)'

        elif isinstance(result, str):
            # 마지막 태스크가 문자열을 반환한 경우 (예: 단순 텍스트 생성)
            meta_to_store = {'cover_letter_output': result} # 이 경우에도 동일한 키 사용
            final_status_meta['current_step'] = '최종 결과 (문자열) 수신 완료.'
            logger.info(f"{log_prefix} Last step returned a string (len: {len(result)}). Storing it under 'cover_letter_output'.")
            meta_to_store['status_message'] = '최종 결과 (문자열) 수신 완료.'

        else:
            logger.warning(f"{log_prefix} Result from last step is not a dict or str. Type: {type(result)}. Storing as is.")
            meta_to_store = result # 예상치 못한 타입이면 그대로 저장
            final_status_meta['current_step'] = f'최종 결과 (타입: {type(result).__name__}) 수신 완료.'
            meta_to_store['status_message'] = f'최종 결과 (타입: {type(result).__name__}) 수신 완료.'

        # 루트 태스크 상태를 SUCCESS로 업데이트하고, 추출된 자기소개서 또는 전체 결과를 meta로 저장
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.SUCCESS,
            meta=meta_to_store # cover_letter_output 키를 가진 딕셔너리를 meta로 전달
        )
        logger.info(f"{log_prefix} Root task {root_task_id} 최종 상태 SUCCESS 및 meta 업데이트 요청됨. 전달된 meta: {try_format_log(meta_to_store)}")

        final_status_meta['callback_processed'] = True
        # final_status_meta도 어딘가에 저장하거나 로깅할 수 있음 (예: 콜백 태스크 자체의 결과)

    except Exception as e:
        current_exc = e
        current_traceback = traceback.format_exc()
        logger.error(f"{log_prefix} Error in handle_pipeline_completion: {current_exc}\nTraceback: {current_traceback}")
        
        final_status_meta['pipeline_status'] = 'COMPLETED_WITH_ERROR_IN_CALLBACK'
        final_status_meta['error_in_callback'] = str(current_exc)
        final_status_meta['current_step'] = '최종 결과 처리 중 오류 발생.'

        # 콜백 처리 중 오류 발생 시에도 루트 태스크를 FAILURE로 업데이트
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.FAILURE, 
            exc=current_exc,
            traceback_str=current_traceback if isinstance(current_traceback, str) else traceback.format_exc(),
            meta=final_status_meta
        )
        logger.error(f"{log_prefix} Root task {root_task_id} updated to FAILURE due to error in completion callback.")
        # 콜백 자체의 실패를 알리기 위해 예외를 다시 발생시킬 수 있으나, 이미 루트 태스크 상태는 업데이트됨
        # raise # 필요한 경우 주석 해제

    # 이 콜백 태스크 자체의 결과 (선택 사항)
    # return final_status_meta


# 등록 (celery_app.task 데코레이터를 사용하거나, celeryconfig.py 등에서 직접 등록)
# 이 콜백들은 Celery가 자동으로 인식하도록 하려면, Celery 앱 설정에 포함되거나
# tasks.py 와 같이 Celery가 스캔하는 모듈 내에 정의되어야 함.
# 또는, link/link_error 시점에 .s() 시그니처로 전달되어야 함.

# 예시: 다른 파일에서 이 콜백들을 사용하려면 아래와 같이 import:
# from .pipeline_callbacks import handle_task_failure_callback, handle_pipeline_completion

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
        result_data_for_state = cover_letter_text_to_store if cover_letter_text_to_store else status_message_to_store

        # meta에 딕셔너리 형태로 저장하여 _update_root_task_state 호출
        meta_to_store = {'cover_letter_output': result_data_for_state}
        
        _update_root_task_state(
            root_task_id=root_task_id, 
            state=states.SUCCESS,
            meta=meta_to_store # cover_letter_output 키를 가진 딕셔너리를 meta로 전달
        )
        logger.info(f"{log_prefix} Root task {root_task_id} 최종 상태 SUCCESS 및 meta 업데이트 요청됨. 전달된 meta: {try_format_log(meta_to_store)}")

        final_status_meta['callback_processed'] = True
        final_status_meta['root_task_id'] = root_task_id

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
    return final_status_meta 