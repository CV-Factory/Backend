import time
import logging
import asyncio
# import asyncio # asyncio 임포트
from celery_app import app as celery_app
# from playwright.async_api import async_playwright, Playwright # Playwright 임포트
from bs4 import BeautifulSoup # BeautifulSoup 임포트
# import google.generativeai as genai # Gemini API
# from langchain... # Langchain 관련 모듈
import os
import datetime
from celery import Celery
from playwright.sync_api import sync_playwright, Error as SyncPlaywrightError # Playwright 동기 에러 임포트 (디버깅용)
from playwright.async_api import async_playwright, Error as PlaywrightError # Playwright 비동기 에러 임포트
from celery.utils.log import get_task_logger

# 로거 설정
logger = get_task_logger(__name__)

# Gemini API 키 설정 등은 애플리케이션 시작 시 또는 작업 내에서 필요에 따라 수행합니다.
# import os
# genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Playwright를 사용하여 웹사이트 내용을 비동기적으로 크롤링하는 함수
# async def crawl_website_content(url: str): # 비동기 함수 선언 주석 처리
@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
async def crawl_website_content(self, url: str, query: str):
    logger.info(f"크롤링 시작: {url}, 쿼리: {query}")
    html_content = ""
    extracted_text = ""
    # playwright_version = get_playwright_version()
    # logger.info(f"Playwright Version: {playwright_version}")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True) # Docker 환경에서는 True로 설정
            page = await browser.new_page()
            logger.info(f"페이지 이동: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000) # timeout 증가

            # 페이지 로드 후 추가 대기 (동적 콘텐츠 로드 고려)
            await page.wait_for_timeout(5000)

            # iframe을 찾습니다.
            iframe_locator = page.locator('iframe[name="gib_frame"]')
            if await iframe_locator.count_async() > 0:
                logger.info("iframe 'gib_frame'을 찾았습니다.")
                try:
                    frame = page.frame(name="gib_frame")
                    if frame:
                        logger.info("iframe 내부 프레임에 접근했습니다. (page.frame(name='gib_frame'))")
                        # iframe 내부 컨텐츠가 로드될 시간을 충분히 확보합니다.
                        logger.info("iframe 내부 콘텐츠 로드를 위해 대기합니다.")
                        # CSS 선택자를 사용하여 특정 요소가 나타날 때까지 대기 (예: iframe 내부의 body 또는 특정 컨테이너)
                        # 또는 내용이 채워질 때까지 좀 더 일반적인 선택자로 대기
                        try:
                            await frame.wait_for_selector("body *:not(:empty)", timeout=20000) # 20초로 증가
                            logger.info("iframe 내부 요소가 나타났습니다.")
                        except Exception as e_wait: # Playwright의 TimeoutError는 Error의 하위 클래스일 수 있음
                            logger.warning(f"iframe 내부 요소 대기 시간 초과 또는 오류: {e_wait}, 추가 대기 시도")
                        
                        await page.wait_for_timeout(7000) # 충분한 대기 시간

                        html_content = await frame.content()
                        logger.info(f"iframe HTML 길이: {len(html_content)}")

                        # iframe HTML을 파일에 저장 (디버깅 목적이었으나, 운영에서는 제거 또는 조건부로 변경)
                        # sanitized_url_for_filename = "".join(c if c.isalnum() else "_" for c in url)
                        # debug_iframe_html_file_path = f"logs_inspector_debug/debug_iframe_html_async_{sanitized_url_for_filename}.html"
                        # os.makedirs(os.path.dirname(debug_iframe_html_file_path), exist_ok=True)
                        # with open(debug_iframe_html_file_path, "w", encoding="utf-8") as f:
                        #     f.write(html_content)
                        # logger.info(f"iframe HTML content saved to {debug_iframe_html_file_path}")
                    else:
                        logger.warning("iframe 'gib_frame'을 찾았으나, 프레임에 접근할 수 없습니다. 페이지 전체 HTML을 사용합니다.")
                        html_content = await page.content()
                except Exception as e_iframe:
                    logger.error(f"iframe 처리 중 오류 발생: {e_iframe}. 페이지 전체 HTML을 사용합니다.")
                    html_content = await page.content()
            else:
                logger.info("iframe 'gib_frame'을 찾지 못했습니다. 페이지 전체 HTML을 사용합니다.")
                html_content = await page.content()
            
            # 전체 페이지 HTML을 가져오는 부분 (iframe을 못찾거나 오류 발생 시)
            # html_content = await page.content() # 위 로직에서 이미 처리됨

            # HTML 파일로 저장 (디버깅 용도)
            # sanitized_url_for_filename = "".join(c if c.isalnum() else "_" for c in url)
            # debug_html_file_path = f"logs_inspector_debug/debug_html_async_{sanitized_url_for_filename}.html"
            # os.makedirs(os.path.dirname(debug_html_file_path), exist_ok=True)
            # with open(debug_html_file_path, "w", encoding="utf-8") as f:
            #    f.write(html_content)
            # logger.info(f"HTML content saved to {debug_html_file_path} (length: {len(html_content)})")

            await browser.close()
            logger.info("브라우저를 닫았습니다.")

        # BeautifulSoup으로 텍스트 추출
        if html_content:
            soup = BeautifulSoup(html_content, "html.parser")
            
            # 특정 클래스나 ID를 가진 요소를 찾아 텍스트 추출 (예시)
            # target_element = soup.find(class_="job-description") # 예시 선택자
            # if target_element:
            #     extracted_text = target_element.get_text(separator="\\n", strip=True)
            # else:
            #     extracted_text = soup.get_text(separator="\\n", strip=True) # 전체 텍스트 추출

            # "담당업무", "자격요건" 등을 포함하는 일반적인 텍스트 추출 강화
            texts = []
            # 주요 키워드를 포함할 가능성이 높은 태그들 우선 탐색
            for element in soup.find_all(['p', 'div', 'span', 'li', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
                text_content = element.get_text(separator=" ", strip=True)
                # 매우 짧은 텍스트는 제외 (예: 공백 또는 단일 문자)
                if len(text_content) > 10:  # 너무 짧은 텍스트는 의미 없을 가능성이 높음
                    # 중복 방지를 위해 이미 추가된 텍스트인지 확인 (옵션)
                    # if text_content not in texts:
                    texts.append(text_content)
            
            if texts:
                extracted_text = "\\n".join(texts)
                logger.info(f"텍스트 추출 성공 (길이: {len(extracted_text)}).")
            else:
                logger.warning("BeautifulSoup으로 텍스트를 추출하지 못했습니다. HTML 내용은 있었으나, 유의미한 텍스트를 찾지 못했습니다.")
                # 이 경우, html_content를 그대로 사용하거나, 다른 전략을 고려할 수 있습니다.
                # extracted_text = "텍스트 추출 실패" # 또는 html_content 일부를 요약
        else:
            logger.warning("HTML 내용이 비어있어 텍스트를 추출할 수 없습니다.")
            extracted_text = "HTML 내용을 가져오지 못했습니다."

    except asyncio.TimeoutError as e_timeout: # Python의 asyncio.TimeoutError
        logger.error(f"페이지 로드 또는 작업 시간 초과: {url}, 오류: {e_timeout}")
        # self.retry(exc=e_timeout) # Celery 재시도
        extracted_text = f"페이지 로드 또는 작업 시간 초과: {e_timeout}"
    except PlaywrightError as e_playwright: # Playwright의 공통 Error
        logger.error(f"Playwright 오류 발생: {url}, 오류: {e_playwright}")
        if "net::ERR_CONNECTION_REFUSED" in str(e_playwright):
             logger.error("대상 서버에 연결할 수 없습니다. URL을 확인하거나 네트워크 상태를 점검하세요.")
        # 특정 오류 유형에 따라 재시도 결정 (예: 일시적인 네트워크 문제)
        # self.retry(exc=e_playwright) 
        extracted_text = f"Playwright 오류: {e_playwright}"
    except Exception as e:
        logger.error(f"크롤링 중 알 수 없는 오류 발생: {url}, 오류: {e}", exc_info=True)
        # self.retry(exc=e) # 모든 예외에 대해 재시도할지 결정
        extracted_text = f"알 수 없는 오류: {e}"
    
    finally:
        logger.info(f"크롤링 완료: {url}, 추출된 텍스트 길이: {len(extracted_text)}")
        # 디버깅을 위해 추출된 텍스트의 일부를 로깅
        # logger.debug(f"추출된 텍스트 (일부): {extracted_text[:500]}")

    return {"url": url, "query": query, "content": extracted_text, "html_content_length": len(html_content)}

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
    html_content = "" # html_content 초기화
    try:
        # 1. Playwright를 사용한 크롤링 (디버깅용 동기 함수 호출)
        logger.info(f"[Task ID: {task_id}] 디버깅용 동기 크롤링 작업 시작")
        # 디버깅용 동기 함수 호출
        crawled_text, html_content = crawl_website_content(target_url, query)
        logger.info(f"[Task ID: {task_id}] 디버깅용 동기 크롤링 작업 완료.")
        
        if not crawled_text:
             logger.warning(f"[Task ID: {task_id}] 크롤링된 내용이 없습니다. (디버그 모드)")
             # Inspector 사용 시에는 결과 반환이 중요하지 않으므로 간단히 로그만 남길 수 있습니다.
             return {"status": "debug_no_content", "original_url": target_url, "query": query, "result": "No content crawled during debug."}

        # 크롤링 결과 파일 저장 (디버깅 중에는 이 부분은 생략하거나 간단히 할 수 있음)
        # ... (기존 파일 저장 로직, 필요시 주석 처리 또는 경로 수정) ...
        logger.info(f"[{task_id}] (디버그 모드) 크롤링 텍스트 길이: {len(crawled_text)}")


        # 2. RAG 파이프라인 (Langchain + Gemini) (디버깅 중에는 플레이스홀더 사용)
        logger.info(f"[Task ID: {task_id}] (디버그 모드) RAG 및 LLM 처리 시작")
        final_result = f"DEBUG MODE - RAG/LLM processing placeholder for query '{query}' with crawled content (length: {len(crawled_text)})"
        logger.info(f"[Task ID: {task_id}] (디버그 모드) RAG 및 LLM 처리 (플레이스홀더) 완료.")

        return {"status": "debug_success", "original_url": target_url, "query": query, "result": final_result}

    except Exception as e:
        logger.error(f"[Task ID: {task_id}] 작업 처리 중 예상치 못한 최상위 오류 발생: {e}", exc_info=True)
        self.update_state(state='FAILURE', meta={'error': str(e)})
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