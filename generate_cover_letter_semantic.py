import os
import logging
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_cohere import CohereEmbeddings
from langchain_experimental.text_splitter import SemanticChunker
from langchain.chains import RetrievalQA
from langchain_google_genai import GoogleGenerativeAI

# 로깅 설정
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def format_text_by_length(text, length=50):
    logger.debug(f"{length}자 단위로 텍스트 포맷팅 시도...")
    try:
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        lines = text.split('\n')
        formatted_lines = []
        for line in lines:
            if not line.strip():
                formatted_lines.append("")
                continue
            wrapped_line = '\n'.join([line[i:i+length] for i in range(0, len(line), length)])
            formatted_lines.append(wrapped_line)
        formatted_text = '\n'.join(formatted_lines)
        logger.debug("텍스트 포맷팅 완료.")
        return formatted_text
    except Exception as e:
        logger.error(f"텍스트 포맷팅 중 오류 발생: {e}", exc_info=True)
        return text

def generate_cover_letter(user_story: str, job_posting_content: str):
    logger.debug("자기소개서 생성 함수 시작...")
    logger.debug(f"입력된 사용자 스토리 (일부): {user_story[:100]}...")
    logger.debug(f"입력된 채용 공고 내용 (일부): {job_posting_content[:200]}...")

    if not job_posting_content or not job_posting_content.strip():
        logger.warning("채용 공고 내용이 비어있거나 유효하지 않습니다.")
        return "", "채용 공고 내용이 비어 있어 자기소개서를 생성할 수 없습니다."

    # .env 파일에서 환경 변수 로드 (필요시 호출)
    # load_dotenv() # API 핸들러 등 상위 레벨에서 한 번만 로드하는 것이 더 효율적일 수 있습니다.
    # 여기서는 각 호출마다 로드하도록 두거나, 필요에 따라 조정합니다.
    if load_dotenv():
        logger.debug(".env 파일 로드 성공 (generate_cover_letter 내부)")
    else:
        logger.warning(".env 파일 로드 실패 또는 찾을 수 없음 (generate_cover_letter 내부)")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    COHERE_API_KEY = os.getenv("COHERE_API_KEY")

    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY가 설정되지 않았습니다.")
        raise ValueError("GEMINI_API_KEY가 설정되지 않았습니다.")
    if not COHERE_API_KEY:
        logger.error("COHERE_API_KEY가 설정되지 않았습니다.")
        raise ValueError("COHERE_API_KEY가 설정되지 않았습니다.")
    logger.debug("API 키 로드 완료")

    # 모델 초기화
    try:
        llm = GoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=GEMINI_API_KEY)
        embeddings = CohereEmbeddings(model="embed-multilingual-v3.0", cohere_api_key=COHERE_API_KEY, user_agent="langchain")
        logger.debug("LLM 및 Embeddings 모델 초기화 성공")
    except Exception as e:
        logger.error(f"모델 초기화 중 오류 발생: {e}", exc_info=True)
        raise

    # 의미론적 청킹 (이제 job_posting_content를 직접 사용)
    logger.debug("의미론적 청킹 시도...")
    try:
        text_splitter = SemanticChunker(embeddings)
        docs = text_splitter.create_documents([job_posting_content])
        logger.debug(f"의미론적 청킹 완료. 생성된 문서 수: {len(docs)}")
        if not docs:
            logger.warning("의미론적 청킹 결과 문서가 없습니다 (입력된 채용공고 기반).")
            return "", "채용공고 내용 분석 결과, 자기소개서 생성을 위한 정보를 추출할 수 없었습니다."
    except Exception as e:
        logger.error(f"의미론적 청킹 중 오류 발생: {e}", exc_info=True)
        raise

    # FAISS 벡터 저장소 생성
    logger.debug("FAISS 벡터 저장소 생성 시도...")
    try:
        vectorstore = FAISS.from_documents(docs, embeddings)
        logger.debug("FAISS 벡터 저장소 생성 성공")
    except Exception as e:
        logger.error(f"FAISS 벡터 저장소 생성 중 오류 발생: {e}", exc_info=True)
        raise

    # RAG 체인 설정
    logger.debug("RAG 체인 설정 시도...")
    try:
        qa_chain = RetrievalQA.from_chain_type(
            llm,
            retriever=vectorstore.as_retriever(),
            chain_type="stuff"
        )
        logger.debug("RAG 체인 설정 성공")
    except Exception as e:
        logger.error(f"RAG 체인 설정 중 오류 발생: {e}", exc_info=True)
        raise

    # 자기소개서 생성 요청 프롬프트
    query = f"""다음은 저의 이야기입니다:
{user_story}

이 이야기를 바탕으로, (RAG 시스템에 의해 컨텍스트로 제공될) 채용 공고에 가장 적합한 자기소개서를 작성해주세요. 
자기소개서는 한국어로 작성해주시고, 다음 사항들을 고려해주세요:
- 저의 강점과 경험이 채용 공고의 어떤 필요 역량 및 직무 내용과 연결되는지 명확히 설명해주세요.
- 이 회사와 제시된 포지션에 제가 왜 깊은 관심을 가지게 되었는지 구체적인 이유를 포함해주세요.
- 저의 핵심 역량과 경험을 통해 회사에 어떻게 기여할 수 있을지 보여주세요.
"""
    logger.debug(f"자기소개서 생성 요청 프롬프트 (일부): {query[:200]}...")

    logger.debug("자기소개서 생성 시도...")
    try:
        result = qa_chain.invoke({"query": query})
        generated_text = result["result"]
        logger.debug("자기소개서 생성 성공")
        
        formatted_cover_letter = format_text_by_length(generated_text, 40)
        
        return generated_text, formatted_cover_letter
    except Exception as e:
        logger.error(f"자기소개서 생성 중 오류 발생: {e}", exc_info=True)
        raise

if __name__ == "__main__":
    # 테스트를 위해서는 실제 채용 공고 내용이 필요합니다.
    # 예시: 테스트용 채용 공고 파일 읽기 (또는 직접 문자열로 제공)
    TEST_JOB_POSTING_FILE = "logs/body_html_recursive_jobkorea.co.kr_recruit_gi_read_46819578oem_co_fe880758_formatted.txt" # 실제 테스트 파일 경로
    
    test_job_content = ""
    if os.path.exists(TEST_JOB_POSTING_FILE):
        with open(TEST_JOB_POSTING_FILE, "r", encoding="utf-8") as f:
            test_job_content = f.read()
        logger.info(f"테스트용 채용 공고 로드 완료: {TEST_JOB_POSTING_FILE}")
    else:
        logger.warning(f"테스트용 채용 공고 파일을 찾을 수 없습니다: {TEST_JOB_POSTING_FILE}. 임시 내용으로 테스트합니다.")
        test_job_content = """[테스트 채용 공고]
        회사명: 테스트 주식회사
        모집 분야: AI 엔지니어 (신입/경력)
        주요 업무: 최신 AI 모델 연구 및 개발, 데이터 분석 및 시각화, AI 기반 서비스 프로토타이핑.
        자격 요건: Python, TensorFlow/PyTorch 경험자, 머신러닝/딥러닝 지식 보유자.
        우대 사항: 관련 분야 석사 이상, 클라우드 플랫폼 경험, 자연어 처리 프로젝트 경험자."""

    test_user_story = """저는 창의적이고 문제 해결 능력이 뛰어난 개발자입니다. 
    다양한 AI 프로젝트를 경험하며 Python과 PyTorch를 능숙하게 다룰 수 있게 되었습니다. 
    특히 자연어 처리 분야에 관심이 많아 개인적으로 챗봇 개발 프로젝트를 진행한 경험이 있습니다.
    새로운 기술을 배우는 데 관심이 많고, 빠르게 습득하여 실제 문제 해결에 적용하는 것을 좋아합니다."""
    logger.info("generate_cover_letter_semantic.py 스크립트 직접 실행 (테스트용)")

    try:
        raw_cv, formatted_cv = generate_cover_letter(user_story=test_user_story, job_posting_content=test_job_content)
        
        if raw_cv:
            print("\n--- 자기소개서 원본 ---")
            print(raw_cv)
            print("\n--- 40자 개행 자기소개서 ---")
            print(formatted_cv)

            output_path = "logs/generated_cover_letter_formatted_test.txt"
            logger.debug(f"테스트 자기소개서 저장 경로: {output_path}")
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(formatted_cv)
                logger.info(f"테스트 자기소개서가 성공적으로 저장되었습니다: {output_path}")
            except Exception as e:
                logger.error(f"테스트 자기소개서 파일 저장 중 오류 발생: {e}", exc_info=True)
        else:
            logger.warning(f"테스트 자기소개서 생성 실패: {formatted_cv}") 
            print(f"테스트 자기소개서 생성에 실패했습니다. 메시지: {formatted_cv}")

    except FileNotFoundError as e:
        logger.error(f"테스트 중 파일 오류: {e}", exc_info=True)
        print(f"오류: {e}")
    except ValueError as e:
        logger.error(f"테스트 중 설정 오류: {e}", exc_info=True)
        print(f"오류: {e}")
    except Exception as e:
        logger.error(f"테스트 자기소개서 생성 중 알 수 없는 오류 발생: {e}", exc_info=True)
        print(f"테스트 중 알 수 없는 오류가 발생했습니다: {e}")

    logger.debug("테스트 스크립트 실행 완료.") 