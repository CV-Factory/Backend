import os
from celery import Celery
from dotenv import load_dotenv
import logging
import ssl

from api.logging_config import setup_logging
setup_logging() # 중앙 로깅 설정 적용

logger = logging.getLogger(__name__)

# 환경 변수 로드
load_dotenv()

# Upstash Redis 연결 정보 환경 변수
UPSTASH_REDIS_ENDPOINT = os.environ.get('UPSTASH_REDIS_ENDPOINT')
UPSTASH_REDIS_PORT = os.environ.get('UPSTASH_REDIS_PORT')
UPSTASH_REDIS_PASSWORD = os.environ.get('UPSTASH_REDIS_PASSWORD')

# 로컬 테스트 시 REDIS_URL 환경 변수 또는 직접 Upstash 정보 사용 가능
LOCAL_REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0').strip() # 기본 로컬 Redis, URL 앞뒤 공백 제거

# FINAL_REDIS_URL 결정 로직
if UPSTASH_REDIS_PASSWORD and UPSTASH_REDIS_ENDPOINT and UPSTASH_REDIS_PORT:
    # Cloud Run 환경 또는 Upstash 정보가 모두 제공된 경우
    FINAL_REDIS_URL = f"rediss://default:{UPSTASH_REDIS_PASSWORD}@{UPSTASH_REDIS_ENDPOINT}:{UPSTASH_REDIS_PORT}" # ssl_cert_reqs 제거
    logger.info(f"Using Upstash Redis for Celery. Endpoint: {UPSTASH_REDIS_ENDPOINT}:{UPSTASH_REDIS_PORT}")
    
    # transport_options 초기화 (Celery 5.x SSL 설정을 위해 broker_use_ssl/redis_backend_use_ssl 사용)
    CELERY_BROKER_TRANSPORT_OPTIONS = {}
    CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {}
    
elif LOCAL_REDIS_URL.startswith("rediss://"):
    # REDIS_URL이 rediss:// 스킴을 사용하는 경우 (예: 다른 Upstash 인스턴스 또는 외부 SSL Redis)
    # ssl_cert_reqs는 broker_use_ssl/redis_backend_use_ssl을 통해 관리하므로 URL에서 제거하거나 기본값 유지
    # 여기서는 URL에 명시적으로 포함된 ssl_cert_reqs를 제거하는 방향으로 수정
    if "?ssl_cert_reqs" in LOCAL_REDIS_URL:
        FINAL_REDIS_URL = LOCAL_REDIS_URL.split("?ssl_cert_reqs")[0]
        logger.info(f"Using REDIS_URL (rediss://) and removing existing ssl_cert_reqs. Base URL: {FINAL_REDIS_URL.split('@')[0]}@...")
    elif "&ssl_cert_reqs" in LOCAL_REDIS_URL: # 다른 파라미터 뒤에 오는 경우
        base_url, params_part = LOCAL_REDIS_URL.split("?", 1)
        params = params_part.split("&")
        filtered_params = [p for p in params if not p.startswith("ssl_cert_reqs=")]
        if filtered_params:
            FINAL_REDIS_URL = f"{base_url}?{'&'.join(filtered_params)}"
        else:
            FINAL_REDIS_URL = base_url
        logger.info(f"Using REDIS_URL (rediss://) and removing existing ssl_cert_reqs. Base URL: {FINAL_REDIS_URL.split('@')[0]}@...")
    else:
        FINAL_REDIS_URL = LOCAL_REDIS_URL
        logger.info(f"Using REDIS_URL (rediss://) as is (no ssl_cert_reqs found to remove): {FINAL_REDIS_URL.split('@')[0]}@...")

    CELERY_BROKER_TRANSPORT_OPTIONS = {}
    CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {}

else:
    # 로컬 Redis (redis://) 또는 Upstash 정보가 불완전하여 로컬로 폴백
    FINAL_REDIS_URL = LOCAL_REDIS_URL
    logger.info(f"Using local Redis (redis://) or incomplete Upstash config, falling back to: {FINAL_REDIS_URL}")
    if not UPSTASH_REDIS_PASSWORD and (UPSTASH_REDIS_ENDPOINT or UPSTASH_REDIS_PORT): # 부분적으로만 설정된 경우 경고
        logger.warning("Upstash Redis configuration is incomplete (e.g., missing password). Ensure all UPSTASH_REDIS_... variables are set for Upstash, or REDIS_URL for other Redis instances.")
    CELERY_BROKER_TRANSPORT_OPTIONS = {}
    CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS = {}


# 비밀번호를 제외한 URL 로깅
log_url = FINAL_REDIS_URL
if "@" in log_url:
    log_url = f"{log_url.split('://')[0]}://{log_url.split('@')[-1]}"
logger.info(f"Celery: Effective broker/backend URL for Celery (credentials masked): {log_url}")
logger.info(f"Celery: BROKER_TRANSPORT_OPTIONS: {CELERY_BROKER_TRANSPORT_OPTIONS}")
logger.info(f"Celery: RESULT_BACKEND_TRANSPORT_OPTIONS: {CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS}")


try:
    celery_app = Celery(
        'tasks',
        broker=FINAL_REDIS_URL,
        backend=FINAL_REDIS_URL,
        include=['celery_tasks'],
        broker_connection_retry_on_startup=True
    )
    
    # transport_options 설정 (Celery 5.x 이상)
    if CELERY_BROKER_TRANSPORT_OPTIONS:
        celery_app.conf.broker_transport_options = CELERY_BROKER_TRANSPORT_OPTIONS
    if CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS:
        celery_app.conf.result_backend_transport_options = CELERY_RESULT_BACKEND_TRANSPORT_OPTIONS
        
    logger.info(f"Celery app instance created. Broker transport options: {celery_app.conf.broker_transport_options}, Result backend transport options: {celery_app.conf.result_backend_transport_options}")

except Exception as e:
    logger.error(f"Error creating Celery app instance: {e}", exc_info=True)
    raise

# 선택적 Celery 설정 (필요에 따라 추가)
# Redis SSL 설정은 URL에서 처리하므로 주석 유지
if FINAL_REDIS_URL.startswith("rediss://"):
   celery_app.conf.broker_use_ssl = {'ssl_cert_reqs': ssl.CERT_REQUIRED} # ssl.CERT_REQUIRED 사용
   celery_app.conf.redis_backend_use_ssl = {'ssl_cert_reqs': ssl.CERT_REQUIRED} # ssl.CERT_REQUIRED 사용
   logger.info("Celery SSL/TLS enabled for Upstash Redis with ssl.CERT_REQUIRED.")

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

app = celery_app # main.py에서 import app 할 수 있도록 추가

if __name__ == '__main__':
    # 이 파일은 직접 실행되지 않고, 'celery -A celery_app.celery_app worker -l info' 와 같이 CLI로 워커를 실행합니다.
    logger.warning("This script is intended to be used by Celery CLI, not executed directly for worker startup.") 