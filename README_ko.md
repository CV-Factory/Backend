<div align="center">
  <!-- 여기에 프로젝트 로고 이미지를 넣어주세요 -->
  <h1>CVFactory Server</h1>
  <br>
  
  [![English](https://img.shields.io/badge/language-English-blue.svg)](README.md) [![한국어](https://img.shields.io/badge/language-한국어-red.svg)](README_ko.md)
</div>

## 📖 개요

이 저장소는 CVFactory 프로젝트의 백엔드 서버 코드를 포함하며, 주로 웹 페이지 및 기타 텍스트 소스에서 정보를 처리하고 추출하여 CV와 같은 콘텐츠를 생성하는 데 사용됩니다. API 요청을 처리하고, 웹 스크래핑(Playwright 사용), HTML 파싱(BeautifulSoup 사용), 텍스트 추출 및 형식화 작업을 수행하며, 이러한 작업들을 Redis와 함께 Celery를 사용하여 백그라운드 태스크로 관리합니다. 또한, **채용공고 기반 자기소개서 생성 스크립트**도 포함하고 있습니다.

## 🛠 기술 스택

| 분류 | 기술 요소 |
|----------|--------------|
| 언어 | Python 3.x |
| 웹 프레임워크 | FastAPI |
| 비동기 태스크 | Celery |
| 태스크 브로커/백엔드 | Redis |
| 웹 스크래핑/자동화 | Playwright |
| HTML 파싱 | BeautifulSoup4 |
| 데이터 처리 | Pydantic (요청/응답 모델용) |
| 로깅 | 표준 Python `logging` |
| 컨테이너화 | Docker, Docker Compose |

## 🚀 시작하기

### 필수 요구 사항

- Docker
- Docker Compose
- **Conda (자기소개서 생성 스크립트 실행용)**

### 설치 방법

1. 저장소를 클론합니다.
2. `CVFactory_Server` 디렉토리로 이동합니다.
3. **(선택 사항) 자기소개서 생성 스크립트 실행을 위한 Conda 환경을 생성하고 활성화합니다:**
```bash
conda create -n cvfactory_env python=3.10 -y
conda activate cvfactory_env
pip install google-generativeai langchain langchain-community faiss-cpu cohere python-dotenv langchain-experimental langchain-google-genai langchain-cohere --upgrade
```
4. Docker Compose를 사용하여 Docker 컨테이너를 빌드하고 실행합니다:

```bash
docker-compose up --build
```

이 명령어는 Docker 이미지를 빌드하고(`Dockerfile`에 정의된 Python 종속성 및 Playwright 브라우저 설치 포함), 다음 세 가지 서비스를 시작합니다:
- `redis`: Celery를 위한 Redis 서버.
- `web`: FastAPI 웹 서버, API 요청 처리.
- `worker`: Celery 워커, 백그라운드 태스크 처리.

## 🖥 사용법

FastAPI 서버는 `docker-compose.yml` 파일에 매핑된 포트(`8001` 기본값)를 통해 접근 가능합니다. 정의된 API 엔드포인트와 상호작용하여 태스크를 시작할 수 있습니다.

Celery에 의해 관리되는 백그라운드 작업은 자동으로 처리됩니다.

**자기소개서 생성 스크립트 사용법:**

설치 단계에서 생성한 Conda 환경을 사용하여 자기소개서 생성 스크립트를 실행하려면:

1.  `CVFactory_Server` 디렉토리로 이동합니다.
2.  Conda 환경을 활성화합니다:
    ```bash
    conda activate cvfactory_env
    ```
3.  `logs/` 디렉토리에 있는 포맷팅된 채용공고 텍스트 파일의 경로를 지정하여 스크립트를 실행합니다:
    ```bash
    python generate_cover_letter_semantic.py
    ```
    생성된 자기소개서는 터미널에 출력되고 `logs/generated_cover_letter_formatted.txt` 파일에 저장됩니다.

## 📁 프로젝트 구조

```
.
├── main.py           # FastAPI 애플리케이션 진입점 및 API 엔드포인트
├── celery_app.py     # Celery 애플리케이션 인스턴스 설정
├── celery_tasks.py   # Celery 백그라운드 태스크 정의 (웹 스크래핑, 파싱, 형식화 등)
├── Dockerfile        # 웹 및 워커 서비스용 Docker 이미지 정의 (종속성 및 Playwright 설정 포함)
├── docker-compose.yml# 다중 컨테이너 Docker 애플리케이션 정의 및 설정 (web, worker, redis)
├── requirements.txt  # 프로젝트에 필요한 Python 종속성 목록
├── entrypoint.sh     # 컨테이너 내부에서 웹 서버 또는 Celery 워커를 시작하기 위해 실행되는 스크립트
├── logs/             # 애플리케이션 로그 및 생성된 파일을 위한 디렉토리 (볼륨으로 마운트됨)
├── LICENSE           # 라이선스 파일 (CC BY NC 4.0)
├── README.md         # 영어 README 파일
└── **generate_cover_letter_semantic.py**: RAG 및 Gemini API를 활용한 자기소개서 생성 스크립트입니다.
```

## 📄 라이선스

CC BY NC 4.0

## 📬 문의

wintrover@gmail.com 