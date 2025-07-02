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
    supervisor curl dos2unix && \
    rm -rf /var/lib/apt/lists/*

# requirements.txt 복사 및 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir uv
RUN uv pip install --no-cache-dir --system -r requirements.txt

# Playwright 브라우저 설치
RUN python -m playwright install --with-deps chromium

# Supervisor 설정 파일 복사
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# 애플리케이션 코드 및 entrypoint 복사
COPY . .
# Windows 줄바꿈(\r) 제거 후 실행 권한 부여
RUN dos2unix entrypoint.sh && chmod +x entrypoint.sh

# 포트 노출 (FastAPI 용)
EXPOSE ${PORT}

# Supervisor 실행 (entrypoint.sh 에서 처리)
ENTRYPOINT ["./entrypoint.sh"] 
# CMD는 entrypoint.sh 내부 로직에 따라 결정되거나, 여기서 supervisor 직접 실행을 명시할 수도 있습니다.
# CMD ["supervisord", "-n"] # entrypoint.sh 를 사용하지 않을 경우
CMD ["all"] # entrypoint.sh 에서 "all" 명령을 받아 supervisor를 실행하도록 수정 예정 