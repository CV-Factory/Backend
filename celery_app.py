import os
from celery import Celery
import logging

logger = logging.getLogger(__name__)

# Redis URL 환경 변수 ( Memorystore for Redis 사용 시 해당 인스턴스 IP, 포트 등으로 설정)
# 예: redis://<REDIS_HOST>:<REDIS_PORT>/0
# 로컬 테스트 시: redis://localhost:6379/0
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
logger.info(f"Celery: REDIS_URL='{REDIS_URL}'")


try:
    celery_app = Celery(
        'tasks', # 현재 모듈의 이름 또는 임의의 이름 (유지하거나 celery_tasks로 변경 가능)
        broker=REDIS_URL,
        backend=REDIS_URL,
        include=['celery_tasks']  # 작업 함수가 포함된 모듈을 celery_tasks.py로 변경
    )
    logger.info("Celery app instance created successfully.")
except Exception as e:
    logger.error(f"Error creating Celery app instance: {e}", exc_info=True)
    raise

# 선택적 Celery 설정 (필요에 따라 추가)
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],  # 허용할 콘텐츠 타입
    result_serializer='json',
    timezone='Asia/Seoul', # 시간대 설정
    enable_utc=True,
    # 작업 재시도 설정 등
    # task_acks_late = True # 작업 완료 후 ack (메시지 손실 방지)
    # worker_prefetch_multiplier = 1 # 한번에 하나의 작업만 가져오도록 (Playwright 같은 리소스 집중 작업에 유리할 수 있음)
)
logger.info("Celery app configuration updated.")

if __name__ == '__main__':
    # 이 파일은 직접 실행되지 않고, 'celery -A celery_app.celery_app worker -l info' 와 같이 CLI로 워커를 실행합니다.
    logger.warning("This script is intended to be used by Celery CLI, not executed directly for worker startup.") 