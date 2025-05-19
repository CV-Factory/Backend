#!/bin/sh
set -e # 오류 발생 시 즉시 스크립트 종료

# APP_MODULE 환경 변수가 설정되어 있지 않으면 기본값으로 celery_app.celery_app 사용
APP_MODULE=${APP_MODULE:-celery_app.celery_app}

# 로깅
echo "Entrypoint: Received command: $1"
echo "Entrypoint: PORT environment variable: $PORT"
echo "Entrypoint: REDIS_URL environment variable: $REDIS_URL"
echo "Entrypoint: APP_MODULE for Celery: $APP_MODULE"

if [ "$1" = "web" ]; then
  echo "Starting FastAPI web server on port $PORT..."
  # Gunicorn을 사용하는 경우 (더 많은 설정 옵션, 여러 워커 등)
  # exec gunicorn -w ${WEB_WORKERS:-1} -k uvicorn.workers.UvicornWorker main:app --bind 0.0.0.0:${PORT}
  # Uvicorn을 직접 사용하는 경우
  exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --log-level info --reload
elif [ "$1" = "worker" ]; then
  echo "Starting Celery worker..."
  # -A: Celery 애플리케이션 인스턴스 지정 (celery_app.py 안의 celery_app 객체)
  # -l: 로그 레벨
  # -Q: 특정 큐만 처리 (필요에 따라 사용)
  # --concurrency: 동시에 실행할 워커 프로세스 수 (Playwright 사용 시 리소스 고려하여 조절)
  # Cloud Run에서는 보통 concurrency 1로 시작하고, 인스턴스 수를 늘려 스케일 아웃합니다.
  # CPU가 여러 개인 환경에서는 concurrency를 늘릴 수 있습니다.
  # WORKER_CONCURRENCY 환경 변수가 설정되어 있지 않으면 기본값 1 사용
  CONCURRENCY=${WORKER_CONCURRENCY:-1}
  echo "Celery worker concurrency: $CONCURRENCY"
  exec celery -A $APP_MODULE worker -l info --concurrency=$CONCURRENCY
elif [ "$1" = "beat" ]; then
  echo "Starting Celery beat scheduler..."
  # 정기적인 작업 스케줄링이 필요할 경우 Celery Beat 실행
  # Cloud Run에서 Beat를 단일 인스턴스로 실행하려면 추가적인 고려가 필요할 수 있습니다.
  # (예: 리더 선출 또는 단일 인스턴스만 실행되도록 보장)
  exec celery -A $APP_MODULE beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
  # 또는 기본 스케줄러 사용: exec celery -A $APP_MODULE beat -l info
else
  echo "Unknown command: $1"
  echo "Available commands: web, worker, beat"
  exit 1
fi 