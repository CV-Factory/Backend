import time
import logging
import asyncio # asyncio 임포트
from celery_app import celery_app
from playwright.async_api import async_playwright, Playwright # Playwright 임포트
from bs4 import BeautifulSoup # BeautifulSoup 임포트
# import google.generativeai as genai # Gemini API
# from langchain... # Langchain 관련 모듈
import os
import datetime
from celery import Celery
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# Gemini API 키 설정 등은 애플리케이션 시작 시 또는 작업 내에서 필요에 따라 수행합니다.
# import os
# genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Playwright를 사용하여 웹사이트 내용을 비동기적으로 크롤링하는 함수
async def crawl_website_content(url: str):
    logger.info(f"비동기 크롤링 시작: {url}")
    browser = None # 브라우저 객체를 try 블록 외부에서 초기화
    page = None # 페이지 객체를 try 블록 외부에서 초기화
    try:
        # Playwright 컨텍스트 매니저 사용
        logger.debug(f"크롤링 ({url}): Playwright 컨텍스트 시작")
        async with async_playwright() as p:
            # headless 모드 설정 및 자동화 감지 우회 시도
            logger.debug(f"크롤링 ({url}): 브라우저 실행 시도 (봇 감지 우회 옵션 포함)")
            browser = await p.chromium.launch(
                headless=True, 
                args=['--disable-blink-features=AutomationControlled']
            )
            logger.debug(f"크롤링 ({url}): 브라우저 실행 성공")

            # 사용자 에이전트 설정
            user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            logger.debug(f"크롤링 ({url}): 새 페이지 생성 시도 (사용자 에이전트: {user_agent})")
            page = await browser.new_page(user_agent=user_agent)
            logger.debug(f"크롤링 ({url}): 새 페이지 생성 성공")

            # 페이지 이동 및 로딩 대기 (네트워크 idle 상태까지 대기)
            logger.debug(f"크롤링 ({url}): URL 이동 시도: {url}")
            await page.goto(url, wait_until='networkidle', timeout=60000) # 타임아웃 설정 (ms)
            logger.debug(f"크롤링 ({url}): URL 이동 및 기본 로딩 완료")

            # 특정 요소 대기 로직 (효과 없었으므로 주석 처리 또는 삭제 가능, 일단 유지)
            try:
                logger.debug(f"크롤링 ({url}): '담당업무' 포함 요소 대기 시작 (최대 5초로 줄임)") # 타임아웃 줄임
                await page.wait_for_selector("*:has-text('담당업무')", timeout=5000) 
                logger.debug(f"크롤링 ({url}): '담당업무' 포함 요소 발견 또는 타임아웃")
            except Exception as e:
                logger.warning(f"크롤링 ({url}): '담당업무' 포함 요소 대기 중 오류 또는 타임아웃: {e}")

            # 페이지 아래로 여러 번 스크롤하여 동적 콘텐츠 로드 유도
            scroll_count = 5 # 스크롤 횟수
            scroll_delay = 1000 # 각 스크롤 후 대기 시간 (ms)
            logger.debug(f"크롤링 ({url}): 페이지 아래로 {scroll_count}회 스크롤 시작 (딜레이: {scroll_delay}ms)")
            for i in range(scroll_count):
                await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                logger.debug(f"크롤링 ({url}): {i+1}번째 스크롤 완료")
                await page.wait_for_timeout(scroll_delay) # 스크롤 후 콘텐츠 로드 대기
            logger.debug(f"크롤링 ({url}): 페이지 스크롤 완료")
            
            # 스크롤 후 최종적으로 네트워크 안정화 대기
            logger.debug(f"크롤링 ({url}): 스크롤 후 네트워크 안정화 대기")
            await page.wait_for_load_state('networkidle', timeout=10000) # 10초 타임아웃
            logger.debug(f"크롤링 ({url}): 스크롤 후 네트워크 안정화 완료")

            # --- HTML 전체 내용 추출 로직 ---
            logger.debug(f"크롤링 ({url}): 페이지 전체 HTML 내용 추출 시도")
            html_content = await page.content()
            logger.debug(f"크롤링 ({url}): HTML 내용 추출 완료. 길이: {len(html_content) if html_content else 0}")

            if html_content:
                logger.debug(f"크롤링 ({url}): BeautifulSoup으로 텍스트 추출 시도")
                soup = BeautifulSoup(html_content, 'html.parser')
                # script 태그와 style 태그 제거
                for script_or_style in soup(["script", "style"]):
                    script_or_style.decompose()
                text_content = soup.get_text(separator='\n', strip=True)
                logger.info(f"크롤링 (BeautifulSoup 텍스트) 완료: {url}. 내용 길이: {len(text_content)}")
            else:
                 logger.warning(f"크롤링 완료: {url} 에서 HTML 내용을 가져올 수 없습니다.")
                 text_content = "" # HTML 내용이 없으면 빈 문자열로 초기화
            
            return text_content # 추출된 텍스트 내용 반환

    except Exception as e:
        logger.error(f"크롤링 중 예상치 못한 오류 발생 ({url}): {e}", exc_info=True)
        raise # Celery 작업이 실패하도록 예외 다시 발생
    finally:
        # 브라우저가 열렸다면 닫아줍니다.
        if browser:
            logger.debug(f"크롤링 ({url}): 브라우저 닫기 시도")
            await browser.close()
            logger.debug(f"크롤링 ({url}): 브라우저 닫기 완료")

# RAG/LLM 처리를 비동기적으로 수행하는 함수 (구현 필요)
# async def rag_llm_process_content(context: str, query: str):
#     logger.info(f"비동기 RAG/LLM 처리 시작. Query: {query}")
#     try:
#         # 여기에 Langchain 및 Gemini API 사용하여 처리하는 로직 구현
#         # 예시: Gemini API 호출
#         # model = genai.GenerativeModel('gemini-pro')
#         # response = await model.generate_content_async(f"Context: {context}\n\nQuery: {query}")
#         # processed_text = response.text

#         # 임시 반환값
#         processed_text = f"LLM result for query '{query}' based on context (비동기 처리됨)"
#         logger.info("RAG/LLM 처리 성공 (비동기)")
#         return processed_text
#     except Exception as e:
#         logger.error(f"RAG/LLM 처리 중 오류 발생 (Query: {query}): {e}", exc_info=True)
#         raise

@celery_app.task(bind=True, name='tasks.perform_processing')
def perform_processing(self, target_url: str, query: str):
    """메인 처리 작업을 수행하는 Celery 태스크"""
    task_id = self.request.id
    logger.info(f"[Task ID: {task_id}] 처리 시작: URL='{target_url}', Query='{query}'")

    crawled_text = ""
    try:
        # 1. Playwright를 사용한 크롤링 (비동기 함수를 동기적으로 실행)
        logger.info(f"[Task ID: {task_id}] 크롤링 작업 위임 시작")
        # asyncio.run()을 사용하여 비동기 크롤링 함수 실행
        crawled_text = asyncio.run(crawl_website_content(target_url))
        logger.info(f"[Task ID: {task_id}] 크롤링 작업 완료.")
        
        if not crawled_text:
             logger.warning(f"[Task ID: {task_id}] 크롤링된 내용이 없습니다. RAG/LLM 처리를 건너뜁니다.")
             return {"status": "warning", "original_url": target_url, "query": query, "result": "No content crawled."}

        # 크롤링 결과 파일 저장
        timestamp = datetime.datetime.now().strftime("%H%M%S")
        # 파일을 저장할 경로를 컨테이너 내부의 안전한 경로로 지정 (예: /app/crawl_logs)
        # 컨테이너 외부에서 접근하려면 Docker 볼륨 마운트 설정이 필요합니다.
        # 현재는 프로젝트 루트 디렉토리에 저장하도록 되어 있으나, 컨테이너 내부 경로 사용을 권장합니다.
        # filename = f"/app/crawl_logs/crawl_result_{task_id}_{timestamp}.log"
        # 임시로 현재 설정대로 프로젝트 루트에 저장
        # filename = f"./crawl_result_{task_id}_{timestamp}.log"
        # 호스트와 마운트될 logs 디렉토리에 저장
        log_dir = "/app/logs"
        os.makedirs(log_dir, exist_ok=True) # 로그 디렉토리 생성 (이미 존재하면 무시)
        # filename = os.path.join(log_dir, f"crawl_result_{task_id}_{timestamp}.log") # 기존 파일명
        filename = os.path.join(log_dir, f"crawl_result_{timestamp}.log") # 작업 ID 제거
        
        logger.info(f"[{task_id}] 크롤링 내용 파일 저장 시도: {filename}")
        try:
            with open(filename, "w", encoding="utf-8") as f:
                f.write(crawled_text)
            logger.info(f"[{task_id}] 크롤링 내용 파일 저장 성공: {filename}")
        except IOError as e: # 파일 입출력 관련 구체적인 예외 처리
            logger.error(f"[{task_id}] 파일 쓰기 오류 발생: {filename}: {e}", exc_info=True)
            # 파일 저장 실패가 전체 작업 실패의 원인이 아니라면 예외를 다시 발생시키지 않을 수 있습니다.
            # 여기서는 로깅만 하고 작업을 계속 진행하도록 합니다.
        except Exception as e: # 그 외 예상치 못한 파일 저장 오류
            logger.error(f"[{task_id}] 파일 저장 중 예상치 못한 오류 발생: {filename}: {e}", exc_info=True)
            # 역시 로깅만 하고 작업을 계속 진행하도록 합니다.

        # 2. RAG 파이프라인 (Langchain + Gemini) (비동기 함수를 동기적으로 실행)
        logger.info(f"[Task ID: {task_id}] RAG 및 LLM 처리 시작")
        # asyncio.run()을 사용하여 비동기 RAG/LLM 처리 함수 실행 (구현 시 주석 해제)
        # final_result = asyncio.run(rag_llm_process_content(crawled_text, query))
        
        # 임시 결과 반환 (RAG/LLM 처리 로직 구현 전까지 사용)
        final_result = f"RAG/LLM processing placeholder for query '{query}' with crawled content (length: {len(crawled_text)})"
        logger.info(f"[Task ID: {task_id}] RAG 및 LLM 처리 (플레이스홀더) 완료.")

        # 작업 성공 상태 및 결과 반환
        # self.update_state(state='SUCCESS', meta={'result': final_result})
        return {"status": "success", "original_url": target_url, "query": query, "result": final_result}

    except Exception as e:
        logger.error(f"[Task ID: {task_id}] 작업 처리 중 예상치 못한 최상위 오류 발생: {e}", exc_info=True)
        # 실패 시 예외를 다시 발생시켜 Celery가 오류로 처리하도록 함
        # 오류 발생 시 작업 상태를 업데이트하여 실패했음을 표시
        self.update_state(state='FAILURE', meta={'error': str(e)})
        # 예외를 다시 발생시켜 Celery가 실패한 작업으로 기록하도록 함
        raise

# 기존 asyncio.run() 예시 래퍼 함수는 위 main 태스크 내에서 직접 asyncio.run()을 사용하므로 필요 없어졌습니다.
# 하지만 복잡한 비동기 로직을 분리하고 싶다면 계속 사용할 수 있습니다.
# # 예: asyncio.run() 사용 (동기적 실행)
# @celery_app.task(bind=True, name='tasks.perform_processing_sync_wrapper')
# def perform_processing_sync_wrapper(self, target_url: str, query: str):
#     import asyncio
#     task_id = self.request.id
#     logger.info(f"[Task ID: {task_id}] SYNC WRAPPER: URL='{target_url}', Query='{query}'")
#     try:
#         crawled_content = asyncio.run(crawl_website_actual(target_url))
#         final_result = asyncio.run(rag_llm_process_actual(crawled_content, query))
#         return {"status": "success", "result": final_result}
#     except Exception as e:
#         logger.error(f"[Task ID: {task_id}] SYNC WRAPPER 오류: {e}", exc_info=True)
#         raise 