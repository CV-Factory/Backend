import os
from celery import Celery
import logging
import ssl

logger = logging.getLogger(__name__)

# Upstash Redis 연결 정보 환경 변수
UPSTASH_REDIS_ENDPOINT = os.environ.get('UPSTASH_REDIS_ENDPOINT', 'gusc1-inviting-kit-31726.upstash.io')
UPSTASH_REDIS_PORT = os.environ.get('UPSTASH_REDIS_PORT', '31726')
UPSTASH_REDIS_PASSWORD = os.environ.get('UPSTASH_REDIS_PASSWORD') # 실제 비밀번호는 Secret Manager 또는 환경변수로 주입

# 로컬 테스트 시 REDIS_URL 환경 변수 또는 직접 Upstash 정보 사용 가능
# 예: REDIS_URL = "rediss://default:YOUR_PASSWORD@YOUR_UPSTASH_ENDPOINT:YOUR_UPSTASH_PORT"
LOCAL_REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

if UPSTASH_REDIS_PASSWORD and UPSTASH_REDIS_ENDPOINT and UPSTASH_REDIS_PORT:
    # Cloud Run 환경 또는 Upstash 정보가 모두 제공된 경우
    FINAL_REDIS_URL = f"rediss://default:{UPSTASH_REDIS_PASSWORD}@{UPSTASH_REDIS_ENDPOINT}:{UPSTASH_REDIS_PORT}"
    logger.info("Using Upstash Redis for Celery (rediss basic URL).")
else:
    # 로컬 환경 또는 Upstash 정보가 불완전한 경우 (기존 로컬 Redis 또는 REDIS_URL 사용)
    FINAL_REDIS_URL = LOCAL_REDIS_URL
    logger.info("Using local Redis or REDIS_URL for Celery.")
    if not UPSTASH_REDIS_PASSWORD:
        logger.warning("UPSTASH_REDIS_PASSWORD not set. Falling back to local Redis or REDIS_URL.")

logger.info(f"Celery: FINAL_REDIS_URL (host and port only for logging): {'rediss://' + UPSTASH_REDIS_ENDPOINT + ':' + UPSTASH_REDIS_PORT if UPSTASH_REDIS_PASSWORD else FINAL_REDIS_URL.split('@')[-1]}")


try:
    celery_app = Celery(
        'tasks',
        broker=FINAL_REDIS_URL,
        backend=FINAL_REDIS_URL,
        include=['celery_tasks'],
        broker_connection_retry_on_startup=True # GCP Cloud Run 환경에서 시작시 네트워크 연결 재시도
    )
    logger.info("Celery app instance created successfully.")
except Exception as e:
    logger.error(f"Error creating Celery app instance: {e}", exc_info=True)
    raise

# 선택적 Celery 설정 (필요에 따라 추가)
# Redis SSL 설정은 URL에서 처리하므로 주석 유지
# if FINAL_REDIS_URL.startswith("rediss://"):
#    celery_app.conf.broker_use_ssl = {'ssl_cert_reqs': ssl.CERT_REQUIRED} # ssl.CERT_REQUIRED 사용
#    celery_app.conf.redis_backend_use_ssl = {'ssl_cert_reqs': ssl.CERT_REQUIRED} # ssl.CERT_REQUIRED 사용
#    logger.info("Celery SSL/TLS enabled for Upstash Redis with ssl.CERT_REQUIRED.")

celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],  # 허용할 콘텐츠 타입
    result_serializer='json',
    timezone='Asia/Seoul', # 시간대 설정
    enable_utc=True,
    # 작업 재시도 설정 등
    task_acks_late = True, # 작업 완료 후 ack (메시지 손실 방지)
    worker_prefetch_multiplier = 1 # 한번에 하나의 작업만 가져오도록 (Playwright 같은 리소스 집중 작업에 유리할 수 있음)
)
logger.info("Celery app configuration updated.")

if __name__ == '__main__':
    # 이 파일은 직접 실행되지 않고, 'celery -A celery_app.celery_app worker -l info' 와 같이 CLI로 워커를 실행합니다.
    logger.warning("This script is intended to be used by Celery CLI, not executed directly for worker startup.") 