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

# Playwright 시스템 의존성 우선 설치 (root 권한)
RUN python -m playwright install --with-deps chromium

# non-root 사용자 생성 및 권한 설정
RUN groupadd -r appuser && useradd -r -g appuser -d /app appuser

# non-root 사용자로 전환
USER appuser

# Playwright 환경 변수 설정 (캐시 디렉토리 변경)
# 이 경로는 appuser가 소유권을 가지게 될 /app 내부에 위치해야 합니다.
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# 브라우저 실행 파일만 다시 설치 (appuser 권한으로, 캐시 경로에)
RUN python -m playwright install chromium

# 애플리케이션 코드 복사
COPY --chown=appuser:appuser . .

# 로그 디렉토리 생성
RUN mkdir -p /app/logs

# 실행 권한 부여
RUN chmod +x entrypoint.sh

# 포트 노출 (FastAPI 용)
EXPOSE ${PORT}

# Supervisord 설정을 위해 root로 임시 전환
USER root
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 최종 실행은 appuser로
USER appuser

# Supervisor 실행
ENTRYPOINT ["./entrypoint.sh"]
CMD ["all"] 