#!/bin/sh
set -e # 오류 발생 시 즉시 스크립트 종료

echo "Entrypoint: Received command: $@"
# APP_MODULE=${APP_MODULE:-celery_app.celery_app} # 더 이상 필요하지 않으므로 제거

# 로깅
echo "Entrypoint: Received command: $1"
echo "Entrypoint: PORT environment variable: $PORT"
# REDIS_URL은 이제 내부 Redis를 사용하므로, 외부 주소 설정은 필요 없을 수 있습니다.
# echo "Entrypoint: REDIS_URL environment variable: $REDIS_URL"
echo "Entrypoint: APP_MODULE for Celery: $APP_MODULE"

if [ "$1" = "all" ]; then
  echo "Starting supervisor to manage all services (Redis, FastAPI, Celery worker)..."
  exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
elif [ "$1" = "web" ]; then
  echo "Starting FastAPI web server directly on port $PORT..."
  exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info

elif [ "$1" = "worker" ]; then
  echo "Starting Celery worker directly..."
  CONCURRENCY=${WORKER_CONCURRENCY:-1}
  # 디버깅 시에도 Redis 연결을 기다리도록 수정
  exec python /app/wait-for-redis.py celery -A celery_app.celery_app worker -l info --concurrency=$CONCURRENCY

elif [ "$1" = "beat" ]; then
  echo "Starting Celery beat scheduler directly..."
  exec celery -A celery_app.celery_app beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler

else
  # Dockerfile의 CMD에서 'all'을 받거나, 아무 인자도 받지 않은 경우 (Cloud Run 기본 동작)
  # supervisord를 실행하여 모든 서비스를 시작합니다.
  echo "Starting supervisor to manage all services..."
  exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf
fi 