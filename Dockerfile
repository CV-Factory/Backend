# 1. Python 기본 이미지 사용
FROM python:3.11-slim

# 환경 변수 설정
ENV PYTHONUNBUFFERED True
ENV APP_HOME /app
ENV PYTHONPATH "/app"
ENV PORT 8080
# Celery가 사용할 Redis URL (컨테이너 내부 Redis 사용 예정이므로 주석 처리 또는 localhost로 변경)
# ENV REDIS_URL redis://your-redis-host:6379/0 

WORKDIR $APP_HOME

# 시스템 종속성 설치 (Playwright 브라우저 실행에 필요) 및 Supervisor 설치
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 \
    supervisor curl && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt 복사 및 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir uv
RUN uv pip install --no-cache-dir --system -r requirements.txt

# non-root 사용자 생성 및 권한 설정
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# non-root 사용자로 전환
USER appuser

# Playwright 환경 변수 설정 (캐시 디렉토리 변경)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# 사용자 전환 후 Playwright 설치
RUN python -m playwright install --with-deps chromium

# Supervisor 설정 파일 복사 (root로 다시 전환할 필요 없음)
# COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 애플리케이션 코드 복사
COPY --chown=appuser:appuser . .

# 로그 디렉토리 생성 및 권한 설정
# 'COPY --chown' 이전에 실행될 필요는 없으나, appuser로 실행되므로 권한 문제 없음.
RUN mkdir -p /app/logs

# chown을 다시 실행할 필요 없음. 모든 것이 appuser로 실행되고 복사됨.
# RUN chown -R appuser:appuser $APP_HOME

# entrypoint.sh 스크립트 복사 및 실행 권한 부여
# COPY --chown=appuser:appuser entrypoint.sh .
# COPY --chown=appuser:appuser wait-for-redis.py .
RUN chmod +x entrypoint.sh

# 포트 노출 (FastAPI 용)
EXPOSE ${PORT}

# Supervisord 실행을 위해 다시 root로 전환
USER root
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 다시 appuser로 전환하여 컨테이너 실행
USER appuser

# Supervisor 실행 (entrypoint.sh 에서 처리)
ENTRYPOINT ["./entrypoint.sh"]
# CMD는 entrypoint.sh 내부 로직에 따라 결정되거나, 여기서 supervisor 직접 실행을 명시할 수도 있습니다.
# CMD ["supervisord", "-n"] # entrypoint.sh 를 사용하지 않을 경우
CMD ["all"] # entrypoint.sh 에서 "all" 명령을 받아 supervisord를 실행 