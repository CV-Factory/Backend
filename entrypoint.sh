#!/bin/sh
set -e # 오류 발생 시 즉시 스크립트 종료

# APP_MODULE 환경 변수가 설정되어 있지 않으면 기본값으로 celery_app.celery_app 사용
APP_MODULE=${APP_MODULE:-celery_app.celery_app}

# 로깅
echo "Entrypoint: Received command: $1"
echo "Entrypoint: PORT environment variable: $PORT"
# REDIS_URL은 이제 내부 Redis를 사용하므로, 외부 주소 설정은 필요 없을 수 있습니다.
# echo "Entrypoint: REDIS_URL environment variable: $REDIS_URL"
echo "Entrypoint: APP_MODULE for Celery: $APP_MODULE"

# Supervisor 로그 디렉터리 생성
mkdir -p /var/log/supervisor

if [ "$1" = "all" ]; then
  echo "Starting supervisor to manage all services (Redis, FastAPI, Celery worker)..."
  exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
elif [ "$1" = "web" ]; then
  # 이 옵션은 supervisord.conf 를 통해 관리되므로, 직접 실행할 일은 줄어들지만,
  # 개별 테스트 등을 위해 남겨둘 수 있습니다.
  echo "Starting FastAPI web server directly on port $PORT..."
  exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info
elif [ "$1" = "worker" ]; then
  # 이 옵션도 supervisord.conf 를 통해 관리됩니다.
  echo "Starting Celery worker directly..."
  CONCURRENCY=${WORKER_CONCURRENCY:-1}
  echo "Celery worker concurrency: $CONCURRENCY"
  exec celery -A $APP_MODULE worker -l info --concurrency=$CONCURRENCY
elif [ "$1" = "beat" ]; then
  # 이 옵션도 supervisord.conf 를 통해 관리됩니다. (필요시 주석 해제)
  echo "Starting Celery beat scheduler directly..."
  exec celery -A $APP_MODULE beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
else
  echo "Unknown command: $1"
  echo "Available commands: all, web, worker, beat"
  echo "Defaulting to 'all' to start supervisor."
  exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
fi 