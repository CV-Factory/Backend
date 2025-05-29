<div align="center">
  <!-- Replace with your project logo -->
  <h1>CVFactory Server</h1>
  <br>
  
  [![English](https://img.shields.io/badge/language-English-blue.svg)](README.md) [![한국어](https://img.shields.io/badge/language-한국어-red.svg)](README_ko.md)
</div>

## 📖 Overview

This repository contains the backend server for the CVFactory project.
It's designed to process and extract information from web pages and other text sources, primarily for generating CVs and related content.
The server automates information extraction using web scraping and leverages Large Language Models (LLMs) with Retrieval-Augmented Generation (RAG) for advanced text generation tasks like creating cover letters.
Core functionalities are managed as asynchronous background tasks.

## ✨ Core Technologies

The server employs several key technologies and methodologies, which can be grouped as follows:

### Data Extraction and Preprocessing

- **Web Scraping/Crawling**: Utilizes Playwright to dynamically fetch and render web pages. This enables the extraction of content even from JavaScript-heavy sites. It can recursively navigate through iframes to gather comprehensive HTML data (e.g., `extract_body_html_recursive` task in `celery_tasks.py`).
- **HTML Parsing**: Employs BeautifulSoup to parse the fetched HTML. This allows for targeted extraction of text and structured data from the raw HTML content.
- **Text Processing**: Includes functionalities for cleaning and formatting extracted text (e.g., `format_text_file` in `celery_tasks.py`). This prepares the data for further use, such as input for LLMs or storage.

### Asynchronous Task Orchestration

- **Background Task Management**: Leverages Celery with Redis as a message broker. This system manages potentially long-running operations (scraping, parsing, formatting, LLM calls) asynchronously, ensuring the API remains responsive and can handle multiple requests efficiently.

### Generative AI and Advanced Text Processing

- **Large Language Model (LLM) Integration**: Incorporates Groq API (via `ChatGroq` from `langchain_groq`) to generate application-specific text, such as cover letters. The `generate_cover_letter_semantic.py` script showcases how the LLM is prompted with contextually relevant information to produce desired outputs.
- **Retrieval-Augmented Generation (RAG)**: Implements a RAG pipeline using Langchain to enhance the LLM's context understanding for tasks like cover letter generation. This process, detailed in `generate_cover_letter_semantic.py`, involves:
    - Loading source documents (e.g., job posting text from the `logs/` directory).
    - Chunking the text into manageable pieces (using `SemanticChunker` from `langchain_experimental.text_splitter` or `RecursiveCharacterTextSplitter` from `langchain.text_splitter`).
    - Generating vector embeddings for these chunks using Cohere (via `CohereEmbeddings` from `langchain_cohere`).
    - Storing these embeddings in a FAISS vector store (`faiss-cpu`) for efficient similarity searches.
    - Retrieving relevant text chunks based on the generation task and providing them as augmented context to the Groq LLM. This enables more informed, specific, and relevant text generation compared to using the LLM with a generic prompt alone.

## 🛠 Tech Stack

| Category | Technologies |
|----------|--------------|
| Language | Python 3.x |
| Web Framework | FastAPI |
| Asynchronous Tasks | Celery |
| Task Broker/Backend | Upstash Redis (for Cloud Run deployment and recommended for local development) |
| Web Scraping/Automation | Playwright |
| HTML Parsing | BeautifulSoup4 |
| Data Handling | Pydantic (for request/response models) |
| Logging | Standard Python `logging` |
| Containerization | Docker, Docker Compose |
| AI/ML | Langchain, Groq API, Cohere (for Embeddings) |
| RAG | FAISS (Vector Store) |

## 🚀 Getting Started

### Prerequisites

- Docker
- Docker Compose
- Conda (for running cover letter generation script)

### Installation

1. Clone the repository.
2. Navigate to the `CVFactory_Server` directory.
3. (Optional) Create and activate a Conda environment for the cover letter generation script:
    ```bash
    conda create -n cvfactory_env python=3.10 -y
    conda activate cvfactory_env
    pip install langchain langchain-community faiss-cpu cohere python-dotenv langchain-experimental langchain-groq langchain-cohere --upgrade
    ```
4. Environment Setup for Redis:
    *   For Cloud Run (Production/Staging): The application is configured to use Upstash Redis. Connection details (`UPSTASH_REDIS_ENDPOINT`, `UPSTASH_REDIS_PORT`) are set as environment variables in `cloudbuild.yaml`, and the password (`UPSTASH_REDIS_PASSWORD`) is injected via Google Secret Manager.
    *   For Local Development: Use Upstash Redis.
        Set the following environment variables in your local shell or a `.env` file (create one in the `CVFactory_Server` root if it doesn't exist):
        ```env
        UPSTASH_REDIS_ENDPOINT="your_upstash_endpoint.upstash.io"
        UPSTASH_REDIS_PORT="your_upstash_port"
        UPSTASH_REDIS_PASSWORD="your_upstash_password"
        ```
        The `celery_app.py` will automatically use these if set. Ensure no `REDIS_URL` environment variable is set to override this, as the `docker-compose.yml` no longer manages a local Redis service.

5. Build and run the Docker containers:
    ```bash
    docker-compose up --build
    ```
    This command builds the Docker image and starts the `web` (FastAPI) and `worker` (Celery) services. These services will connect to Redis based on the environment variables set.

## 🖥 Usage

The FastAPI server will be accessible via the port mapped in the `docker-compose.yml` file (default: `8001`). You can interact with the defined API endpoints to initiate tasks. Tasks are processed asynchronously by the Celery worker.

Cover Letter Generation Script:

To run the cover letter generation script using the Conda environment created in the installation steps:

1.  Navigate to the `CVFactory_Server` directory.
2.  Activate the Conda environment:
    ```bash
    conda activate cvfactory_env
    ```
3.  Run the script, specifying the path to the formatted job posting text file in the `logs/` directory:
    ```bash
    python generate_cover_letter_semantic.py
    ```
    The generated cover letter will be printed to the console and saved to `logs/generated_cover_letter_formatted.txt`.

Key Endpoints:
- `POST /`: Initiate the main processing task for a given URL and optional prompt.
- `POST /launch-inspector`: Launch Playwright inspector for a URL (useful for debugging scraping).
- `POST /extract-body`: Initiate task to extract `<body>` HTML from a URL, including flattening iframes.
- `POST /extract-text-from-html`: Initiate task to extract text content from a saved HTML file in the `logs` directory.
- `POST /format-text-file`: Initiate task to reformat a text file in the `logs` directory (e.g., wrapping lines).
- `GET /tasks/{task_id}`: Check the status and result of a submitted Celery task.

Logs and extracted files will be saved to the `logs/` directory, which is mapped as a volume in `docker-compose.yml`.

## ⚙️ CI/CD Pipeline

This project uses Google Cloud Build for its CI/CD pipeline.

-   Trigger: Automatically starts when new commits are pushed to the `develop` branch of the GitHub repository.
-   Platform: Google Cloud Build.
-   Configuration: Build and deployment steps are defined in the `cloudbuild.yaml` file.
-   Key Steps:
    1.  Build Docker Image: Builds the application's Docker image.
    2.  Push to Artifact Registry: Pushes the built image to Google Artifact Registry.
    3.  Deploy to Cloud Run: Deploys the new image to the `cvfactory-server` service on Google Cloud Run.
    4.  Resource Configuration: Applies specific CPU (1), memory (2Gi), and instance count (min 0, max 1) settings. The service is configured to listen on port 8000 internally.
    5.  Environment Variables (Cloud Run):
        *   `PYTHONUNBUFFERED=1`
        *   `UPSTASH_REDIS_ENDPOINT`: Your Upstash Redis endpoint (e.g., `gusc1-inviting-kit-31726.upstash.io`)
        *   `UPSTASH_REDIS_PORT`: Your Upstash Redis port (e.g., `31726`)
    6.  Secrets Management (Cloud Run): Securely injects sensitive data as environment variables using Google Secret Manager:
        *   `GROQ_API_KEY` (latest version)
        *   `COHERE_API_KEY` (latest version)
        *   `UPSTASH_REDIS_PASSWORD` (The password for your Upstash Redis instance, latest version)
    7.  Service Account: Utilizes a dedicated service account with least-privilege permissions. Ensure this service account (or the default Compute Engine service account if no specific service account is set for the Cloud Run service) has the "Secret Manager Secret Accessor" (roles/secretmanager.secretAccessor) role to access the secrets specified.

## 📄 Project Structure

```
.
├── main.py           # FastAPI application entry point and API endpoints
├── celery_app.py     # Celery application instance configuration
├── celery_tasks.py   # Definitions of Celery background tasks (web scraping, parsing, formatting, etc.)
├── generate_cover_letter_semantic.py # Script for generating cover letters using RAG and Groq API
├── Dockerfile        # Defines the Docker image for web and worker services
├── docker-compose.yml# Defines and configures the multi-container Docker application for local development
├── requirements.txt  # Lists Python dependencies required by the project
├── entrypoint.sh     # Script executed inside the Docker container to start services via Supervisor
├── supervisord.conf  # Supervisor configuration file to manage FastAPI (Uvicorn) and Celery worker processes
├── cloudbuild.yaml   # Google Cloud Build configuration file for CI/CD
├── request_body.json # Example JSON payload for API requests
├── .gitignore        # Specifies intentionally untracked files that Git should ignore
├── LICENSE           # License file (CC BY NC 4.0)
├── README.md         # English README file (This file)
├── README_ko.md      # Korean README file
├── core/             # Core utilities and configurations for the application
├── tasks/            # Modules related to specific Celery tasks (if more complex task logic is separated)
├── utils/            # General utility functions and helper scripts
├── logs/             # Directory for local application logs and generated files. In Cloud Run, logs are directed to Cloud Logging.
```

## 📄 License

CC BY NC 4.0

## 📬 Contact

wintrover@gmail.com