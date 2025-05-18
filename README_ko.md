<div align="center">
  <!-- 여기에 프로젝트 로고 이미지를 넣어주세요 -->
  <h1>CVFactory Server</h1>
  <br>
  
  [![English](https://img.shields.io/badge/language-English-blue.svg)](README.md) [![한국어](https://img.shields.io/badge/language-한국어-red.svg)](README_ko.md)
</div>

## 📖 개요

이 저장소는 CVFactory 프로젝트의 백엔드 서버 코드를 포함합니다. API 요청 처리, 데이터 가공, 그리고 Celery를 사용한 백그라운드 작업 관리를 담당합니다. 서버는 Docker를 사용하여 컨테이너화되도록 설계되었습니다.

## 🛠 기술 스택

| 분류 | 기술 요소 |
|----------|--------------|
| 언어 | Python |
| 프레임워크 | FastAPI (또는 유사) |
| 백그라운드 작업 | Celery, Redis |
| 데이터베이스 | (사용하는 경우 데이터베이스 명시) |
| 컨테이너화 | Docker, Docker Compose |

## 🚀 시작하기

### 필수 요구 사항

- Docker
- Docker Compose

### 설치 방법

1. 저장소를 클론합니다.
2. `CVFactory_Server` 디렉토리로 이동합니다.
3. Docker Compose를 사용하여 Docker 컨테이너를 빌드하고 실행합니다:

```bash
docker-compose up --build
```

이렇게 하면 웹 서버와 Celery 워커가 시작됩니다.

## 🖥 사용법

서버는 `docker-compose.yml` 파일에 지정된 포트를 통해 접근 가능합니다. API 엔드포인트와 상호작용할 수 있습니다.

Celery에 의해 관리되는 백그라운드 작업은 자동으로 처리됩니다.

## 📁 프로젝트 구조

```
.
├── main.py           # 웹 서버의 메인 진입점
├── celery_app.py     # Celery 애플리케이션 설정
├── celery_tasks.py   # 백그라운드 작업 정의
├── Dockerfile        # Docker 이미지 정의
├── docker-compose.yml# Docker 서비스 설정
├── requirements.txt  # Python 종속성
├── entrypoint.sh     # 컨테이너 시작 스크립트
├── logs/             # 로그 디렉토리
└── README.md         # English README
```

## 📄 라이선스

CC BY NC 4.0

## 🤝 팀

(여기에 팀 구성원 또는 기여자를 명시하세요)

## 📬 문의

(여기에 문의 정보를 명시하세요, 예: 이메일 또는 프로젝트 링크) 