# 1. Python 기본 이미지 사용
FROM python:3.11-slim

# 환경 변수 설정
ENV PYTHONUNBUFFERED True
ENV APP_HOME /app
ENV PORT 8080
# Celery가 사용할 Redis URL (Cloud Run 서비스 환경 변수에서 설정)
# ENV REDIS_URL redis://your-redis-host:6379/0 
# Gemini API Key (Cloud Run 서비스 환경 변수 또는 Secret Manager에서 설정)
# ENV GEMINI_API_KEY your_api_key

WORKDIR $APP_HOME

# 시스템 종속성 설치 (Playwright 브라우저 실행에 필요)
# RUN apt-get update && apt-get install -y --no-install-recommends libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2 && rm -rf /var/lib/apt/lists/*
# 위 방법 대신 Playwright의 내장 명령어를 사용합니다.

# requirements.txt 복사 및 패키지 설치
COPY requirements.txt .
# pip 설치 시 로그를 상세히 남기기 위해 -v 옵션 추가 가능
RUN pip install --no-cache-dir -r requirements.txt

# Playwright 브라우저 설치 (chromium만 설치하는 예시)
# --with-deps 옵션으로 시스템 종속성도 함께 설치
# RUN playwright install --with-deps chromium
# 만약 위 명령이 특정 환경(예: 일부 CI/CD)에서 권한 문제 발생 시, 아래와 같이 사용자 생성 후 설치 시도
# RUN useradd --create-home appuser && \
#     chown -R appuser:appuser $APP_HOME
# USER appuser
# RUN playwright install --with-deps chromium
# USER root # 다음 명령을 위해 다시 root로 전환
# 더 간단하게는 다음 명령어로 시도:
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/* # playwright 설치 스크립트 실행에 curl이 필요할 수 있음
RUN python -m playwright install --with-deps chromium


# 애플리케이션 코드 복사
COPY . .

# 기본적으로 FastAPI 웹 서버 실행 (uvicorn)
# Cloud Run 서비스 배포 시 "command" 및 "args"를 오버라이드하여 워커 실행 가능
# CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}

# 아래는 entrypoint.sh 스크립트를 사용하는 예시입니다.
# 실행 모드(web/worker)를 환경변수로 받아 분기 처리할 수 있습니다.
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"] 
# 기본 CMD는 web (FastAPI 서버 실행)
CMD ["web"] 