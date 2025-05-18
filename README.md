<div align="center">
  <!-- Replace with your project logo -->
  <h1>CVFactory Server</h1>
  <br>
  
  [![English](https://img.shields.io/badge/language-English-blue.svg)](README.md) [![한국어](https://img.shields.io/badge/language-한국어-red.svg)](README_ko.md)
</div>

## 📖 Overview

This repository contains the backend server for the CVFactory project. It is responsible for handling API requests, processing data, and managing background tasks using Celery. The server is designed to be containerized using Docker.

## 🛠 Tech Stack

| Category | Technologies |
|----------|--------------|
| Language | Python |
| Framework | FastAPI (or similar) |
| Background Tasks | Celery, Redis |
| Database | (Specify database if used) |
| Containerization | Docker, Docker Compose |

## 🚀 Getting Started

### Prerequisites

- Docker
- Docker Compose

### Installation

1. Clone the repository.
2. Navigate to the `CVFactory_Server` directory.
3. Build and run the Docker containers:

```bash
docker-compose up --build
```

This will start the web server and the Celery worker.

## 🖥 Usage

The server will be accessible via the port specified in the `docker-compose.yml` file. You can interact with the API endpoints.

Background tasks managed by Celery will be processed automatically.

## 📁 Project Structure

```
.
├── main.py           # Main entry point for the web server
├── celery_app.py     # Celery application configuration
├── celery_tasks.py   # Background tasks definitions
├── Dockerfile        # Docker image definition
├── docker-compose.yml# Docker services configuration
├── requirements.txt  # Python dependencies
├── entrypoint.sh     # Container startup script
├── logs/             # Log directory
└── README_ko.md      # Korean README
```

## 📄 License

CC BY NC 4.0

## 🤝 Team

(Specify team members or contributors here)

## 📬 Contact

(Specify contact information here, e.g., email or project links)

# CVFactory Server (한국어)

## 프로젝트 설명

이 저장소는 CVFactory 프로젝트의 백엔드 서버 코드를 포함합니다. API 요청 처리, 데이터 가공, 그리고 Celery를 사용한 백그라운드 작업 관리를 담당합니다. 서버는 Docker를 사용하여 컨테이너화되도록 설계되었습니다.

## 설정 방법

프로젝트를 설정하려면 Docker와 Docker Compose가 설치되어 있어야 합니다.

1. 저장소를 클론합니다.
2. `CVFactory_Server` 디렉토리로 이동합니다.
3. Docker Compose를 사용하여 Docker 컨테이너를 빌드하고 실행합니다:

```bash
docker-compose up --build
```

이렇게 하면 웹 서버와 Celery 워커가 시작됩니다.

## 사용법

서버는 `docker-compose.yml` 파일에 지정된 포트를 통해 접근 가능합니다. API 엔드포인트와 상호작용할 수 있습니다.

Celery에 의해 관리되는 백그라운드 작업은 자동으로 처리됩니다.

## 프로젝트 구조

- `main.py`: 웹 서버의 메인 진입점입니다.
- `celery_app.py`: Celery 애플리케이션을 설정합니다.
- `celery_tasks.py`: Celery에 의해 처리될 백그라운드 작업을 정의합니다.
- `Dockerfile`: 서버용 Docker 이미지를 정의합니다.
- `docker-compose.yml`: 서비스 (웹 서버, Celery 워커 등)와 해당 구성을 정의합니다.
- `requirements.txt`: Python 종속성 목록입니다.
- `entrypoint.sh`: Docker 컨테이너 시작 시 실행되는 스크립트입니다. 