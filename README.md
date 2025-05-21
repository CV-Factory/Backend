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

- **Large Language Model (LLM) Integration**: Incorporates Google's Gemini API (via `ChatGoogleGenerativeAI` from `langchain_google_genai`) to generate application-specific text, such as cover letters. The `generate_cover_letter_semantic.py` script showcases how the LLM is prompted with contextually relevant information to produce desired outputs.
- **Retrieval-Augmented Generation (RAG)**: Implements a RAG pipeline using Langchain to enhance the LLM's context understanding for tasks like cover letter generation. This process, detailed in `generate_cover_letter_semantic.py`, involves:
    - Loading source documents (e.g., job posting text from the `logs/` directory).
    - Chunking the text into manageable pieces (using `SemanticChunker` from `langchain_experimental.text_splitter` or `RecursiveCharacterTextSplitter` from `langchain.text_splitter`).
    - Generating vector embeddings for these chunks using Cohere (via `CohereEmbeddings` from `langchain_cohere`).
    - Storing these embeddings in a FAISS vector store (`faiss-cpu`) for efficient similarity searches.
    - Retrieving relevant text chunks based on the generation task and providing them as augmented context to the Gemini LLM. This enables more informed, specific, and relevant text generation compared to using the LLM with a generic prompt alone.

## 🛠 Tech Stack

| Category | Technologies |
|----------|--------------|
| Language | Python 3.x |
| Web Framework | FastAPI |
| Asynchronous Tasks | Celery |
| Task Broker/Backend | Redis |
| Web Scraping/Automation | Playwright |
| HTML Parsing | BeautifulSoup4 |
| Data Handling | Pydantic (for request/response models) |
| Logging | Standard Python `logging` |
| Containerization | Docker, Docker Compose |
| AI/ML | Langchain, Google Generative AI (Gemini), Cohere (for Embeddings) |
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
pip install google-generativeai langchain langchain-community faiss-cpu cohere python-dotenv langchain-experimental langchain-google-genai langchain-cohere --upgrade
```
4. Build and run the Docker containers:

```bash
docker-compose up --build
```

This command builds the Docker image (installing Python dependencies and Playwright browsers as defined in the `Dockerfile`) and then starts three services:
- `redis`: The Redis server for Celery.
- `web`: The FastAPI web server, handling API requests.
- `worker`: The Celery worker, processing background tasks.

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

## 📁 Project Structure

```
.
├── main.py           # FastAPI application entry point and API endpoints
├── celery_app.py     # Celery application instance configuration
├── celery_tasks.py   # Definitions of Celery background tasks (web scraping, parsing, formatting, etc.)
├── Dockerfile        # Defines the Docker image for web and worker services (includes dependencies and Playwright setup)
├── docker-compose.yml# Defines and configures the multi-container Docker application (web, worker, redis)
├── requirements.txt  # Lists Python dependencies required by the project
├── entrypoint.sh     # Script executed inside containers to start either the web server or the Celery worker
├── logs/             # Directory for application logs and generated files (mounted as a volume)
├── LICENSE           # License file (CC BY NC 4.0)
├── README_ko.md      # Korean README file
├── generate_cover_letter_semantic.py # Script for generating cover letters using RAG and Gemini API
```

## 📄 License

CC BY NC 4.0

## 📬 Contact

wintrover@gmail.com