<div align="center">
  <!-- 여기에 프로젝트 로고 이미지를 넣어주세요 -->
  <h1>CVFactory Server</h1>
  <br>
  
  [![English](https://img.shields.io/badge/language-English-blue.svg)](README.md) [![한국어](https://img.shields.io/badge/language-한국어-red.svg)](README_ko.md)
</div>

## 📖 개요

이 저장소는 CVFactory 프로젝트의 백엔드 서버 코드를 포함합니다.
주로 웹 페이지 및 기타 텍스트 소스에서 정보를 처리하고 추출하여 CV 및 관련 콘텐츠를 생성하도록 설계되었습니다.
서버는 웹 스크래핑을 사용하여 정보 추출을 자동화하고, 검색 증강 생성(RAG)과 함께 거대 언어 모델(LLM)을 활용하여 자기소개서 작성과 같은 고급 텍스트 생성 작업을 수행합니다.
핵심 기능들은 비동기 백그라운드 작업으로 관리됩니다.

## ✨ 핵심 기술

서버는 다음과 같은 몇 가지 주요 기술과 방법론을 사용하며, 이는 다음과 같이 그룹화할 수 있습니다.

### 데이터 추출 및 전처리

- **웹 스크래핑/크롤링**: Playwright를 사용하여 동적으로 웹 페이지를 가져오고 렌더링합니다. 이를 통해 JavaScript가 많은 사이트에서도 콘텐츠를 추출할 수 있습니다. `celery_tasks.py`의 `extract_body_html_recursive` 작업 예시처럼 iframe을 통해 재귀적으로 탐색하여 포괄적인 HTML 데이터를 수집할 수 있습니다.
- **HTML 파싱**: BeautifulSoup를 사용하여 가져온 HTML을 파싱합니다. 이를 통해 원시 HTML 콘텐츠에서 대상 텍스트 및 구조화된 데이터를 추출할 수 있습니다.
- **텍스트 처리**: 추출된 텍스트를 정리하고 형식화하는 기능을 포함합니다 (예: `celery_tasks.py`의 `format_text_file`). 이는 LLM 입력이나 저장과 같이 추가 사용을 위해 데이터를 준비합니다.

### 비동기 작업 오케스트레이션

- **백그라운드 작업 관리**: Redis를 메시지 브로커로 사용하는 Celery를 활용합니다. 이 시스템은 잠재적으로 오래 실행되는 작업(스크래핑, 파싱, 형식화, LLM 호출)을 비동기적으로 관리하여 API 응답성을 보장하고 여러 요청을 효율적으로 처리할 수 있도록 합니다.

### 생성형 AI 및 고급 텍스트 처리

- **거대 언어 모델(LLM) 통합**: `langchain_groq`의 `ChatGroq`를 통해 Groq API를 통합하여 자기소개서와 같은 특정 애플리케이션 텍스트를 생성합니다. `generate_cover_letter_semantic.py` 스크립트는 LLM이 원하는 출력을 생성하기 위해 문맥적으로 관련된 정보로 어떻게 프롬프트되는지 보여줍니다.
- **검색 증강 생성(RAG)**: Langchain을 사용하여 RAG 파이프라인을 구현하여 자기소개서 생성과 같은 작업에 대한 LLM의 컨텍스트 이해를 향상시킵니다. `generate_cover_letter_semantic.py`에 자세히 설명된 이 프로세스에는 다음이 포함됩니다.
    - 소스 문서 로드 (예: `logs/` 디렉토리의 채용 공고 텍스트).
    - 텍스트를 관리 가능한 조각으로 청킹 ( `langchain_experimental.text_splitter`의 `SemanticChunker` 또는 `langchain.text_splitter`의 `RecursiveCharacterTextSplitter` 사용).
    - `langchain_cohere`의 `CohereEmbeddings`를 사용하여 이러한 청크에 대한 벡터 임베딩 생성.
    - 효율적인 유사성 검색을 위해 이러한 임베딩을 FAISS 벡터 저장소 (`faiss-cpu`)에 저장.
    - 생성 작업을 기반으로 관련 텍스트 청크를 검색하고 이를 Groq LLM에 증강된 컨텍스트로 제공합니다. 이를 통해 LLM을 일반적인 프롬프트와 함께 사용하는 것보다 더 많은 정보에 입각하고 구체적이며 관련성 높은 텍스트 생성이 가능합니다.

## 🛠 기술 스택

| 분류 | 기술 요소 |
|----------|--------------|
| 언어 | Python 3.x |
| 웹 프레임워크 | FastAPI |
| 비동기 태스크 | Celery |
| 태스크 브로커/백엔드 | Upstash Redis (Cloud Run 배포 및 로컬 개발 시 선택적 사용) / 로컬 Redis (Upstash를 사용하지 않는 로컬 개발 전용) |
| 웹 스크래핑/자동화 | Playwright |
| HTML 파싱 | BeautifulSoup4 |
| 데이터 처리 | Pydantic (요청/응답 모델용) |
| 로깅 | 표준 Python `logging` |
| 컨테이너화 | Docker, Docker Compose |
| AI/ML | Langchain, Groq API, Cohere (임베딩용) |
| RAG | FAISS (벡터 저장소) |

## 🚀 시작하기

### 필수 요구 사항

- Docker
- Docker Compose
- Conda (자기소개서 생성 스크립트 실행용)

### 설치 방법

1.  저장소를 클론합니다.
2.  `CVFactory_Server` 디렉토리로 이동합니다.
3.  (선택 사항) 자기소개서 생성 스크립트 실행을 위한 Conda 환경을 생성하고 활성화합니다:
    ```bash
    conda create -n cvfactory_env python=3.10 -y
    conda activate cvfactory_env
    pip install langchain langchain-community faiss-cpu cohere python-dotenv langchain-experimental langchain-groq langchain-cohere --upgrade
    ```
4.  Redis 환경 설정:
    *   Cloud Run (프로덕션/스테이징 환경): 애플리케이션은 Upstash Redis를 사용하도록 설정되어 있습니다. 연결 정보(`UPSTASH_REDIS_ENDPOINT`, `UPSTASH_REDIS_PORT`)는 `cloudbuild.yaml` 파일에 환경 변수로 설정되며, 비밀번호(`UPSTASH_REDIS_PASSWORD`)는 Google Secret Manager를 통해 주입됩니다.
    *   로컬 개발 환경:
        *   옵션 1 (일관성을 위해 권장): Upstash Redis 사용.
            로컬 셸 또는 `.env` 파일( `CVFactory_Server` 루트에 없다면 생성)에 다음 환경 변수를 설정합니다:
            ```env
            UPSTASH_REDIS_ENDPOINT="your_upstash_endpoint.upstash.io"
            UPSTASH_REDIS_PORT="your_upstash_port"
            UPSTASH_REDIS_PASSWORD="your_upstash_password"
            ```
            이 변수들이 설정되면 `celery_app.py`가 자동으로 사용합니다.
        *   옵션 2: 로컬 Redis 사용.
            로컬 Redis 인스턴스(예: `docker run -d -p 6379:6379 redis`로 시작) 사용을 선호하는 경우, Upstash 관련 환경 변수를 설정하지 않거나 `REDIS_URL="redis://localhost:6379/0"`으로 설정합니다. `docker-compose.yml` 파일은 더 이상 로컬 Redis 서비스를 관리하지 않습니다.

5.  Docker 컨테이너를 빌드하고 실행합니다 (위의 로컬 Redis 옵션 2를 선택한 경우 로컬 Redis 서비스 제외):
    ```bash
    docker-compose up --build
    ```
    이 명령어는 Docker 이미지를 빌드하고 `web`(FastAPI) 및 `worker`(Celery) 서비스를 시작합니다. 이 서비스들은 설정된 환경 변수에 따라 Redis에 연결됩니다.

## 🖥 사용법

FastAPI 서버는 `docker-compose.yml` 파일에 매핑된 포트(`8001` 기본값)를 통해 접근 가능합니다. 정의된 API 엔드포인트와 상호작용하여 태스크를 시작할 수 있습니다.

Celery에 의해 관리되는 백그라운드 작업은 자동으로 처리됩니다.

자기소개서 생성 스크립트 사용법:

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

## ⚙️ CI/CD 파이프라인

이 프로젝트는 CI/CD 파이프라인을 위해 Google Cloud Build를 사용합니다.

-   트리거: GitHub 저장소의 `develop` 브랜치에 새로운 커밋이 푸시될 때 자동으로 시작됩니다.
-   플랫폼: Google Cloud Build.
-   설정: 빌드 및 배포 단계는 `cloudbuild.yaml` 파일에 정의되어 있습니다.
-   주요 단계:
    1.  Docker 이미지 빌드: 애플리케이션의 Docker 이미지를 빌드합니다.
    2.  Artifact Registry에 푸시: 빌드된 이미지를 Google Artifact Registry에 푸시합니다.
    3.  Cloud Run에 배포: 새 이미지를 Google Cloud Run의 `cvfactory-server` 서비스에 배포합니다.
    4.  리소스 설정: 특정 CPU, 메모리 및 인스턴스 수 설정을 적용합니다.
    5.  환경 변수 (Cloud Run):
        *   `PYTHONUNBUFFERED=1`
        *   `UPSTASH_REDIS_ENDPOINT`: 사용자의 Upstash Redis 엔드포인트 (예: `gusc1-inviting-kit-31726.upstash.io`)
        *   `UPSTASH_REDIS_PORT`: 사용자의 Upstash Redis 포트 (예: `31726`)
    6.  보안 비밀 관리 (Cloud Run): 다음과 같은 민감한 데이터를 Google Secret Manager를 사용하여 환경 변수로 안전하게 주입합니다:
        *   `GROQ_API_KEY`
        *   `COHERE_API_KEY`
        *   `UPSTASH_REDIS_PASSWORD` (Upstash Redis 인스턴스의 비밀번호)
    7.  서비스 계정: 최소 권한 원칙에 따른 전용 서비스 계정을 활용합니다.

## 📁 프로젝트 구조

```
.
├── main.py           # FastAPI 애플리케이션 진입점 및 API 엔드포인트
├── celery_app.py     # Celery 애플리케이션 인스턴스 설정
├── celery_tasks.py   # Celery 백그라운드 작업 정의 (웹 스크레이핑, 파싱, 포맷팅 등)
├── Dockerfile        # 웹 및 워커 서비스를 위한 Docker 이미지 정의 (의존성, Supervisor, Playwright 설정 포함; 로컬 Redis 서버 설치 제거됨)
├── docker-compose.yml# 로컬 개발을 위한 멀티 컨테이너 Docker 애플리케이션 정의 및 설정 (web, worker 서비스; 로컬 Redis 서비스 정의 제거됨)
├── requirements.txt  # 프로젝트에 필요한 Python 의존성 목록
├── entrypoint.sh     # Docker 컨테이너 내부에서 Supervisor를 통해 서비스(FastAPI, Celery)를 시작하는 스크립트
├── supervisord.conf  # FastAPI(Uvicorn) 및 Celery 워커 프로세스를 관리하는 Supervisor 설정 파일 (Redis 서버 프로그램 제거됨)
├── cloudbuild.yaml   # CI/CD를 위한 Google Cloud Build 설정 파일
├── generate_cover_letter_semantic.py # RAG 및 Groq API를 사용하여 자기소개서를 생성하는 스크립트
├── logs/             # 로컬 애플리케이션 로그 및 생성된 파일 디렉토리 (로컬 Docker Compose 설정에서 볼륨으로 마운트됨). Cloud Run에서는 로그가 Cloud Logging으로 전송됩니다.
├── LICENSE           # 라이선스 파일 (CC BY NC 4.0)
├── README.md         # 영문 README 파일
```

## 📄 라이선스

CC BY NC 4.0

## 📬 문의

wintrover@gmail.com 