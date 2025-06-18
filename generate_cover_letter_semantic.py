import os
import logging
from typing import Optional, Union
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_cohere import CohereEmbeddings
from langchain_experimental.text_splitter import SemanticChunker
from langchain.chains import RetrievalQA
from langchain_groq import ChatGroq
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

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

def generate_cover_letter(job_posting_content: str, prompt: Union[str, None] = None):
    logger.debug("자기소개서 생성 함수 시작...")
    
    if not prompt or not prompt.strip():
        logger.info("사용자 프롬프트가 제공되지 않았거나 비어있습니다. 기본 프롬프트를 사용합니다.")
        prompt = "저는 귀사에 기여하고 함께 성장하고 싶은 지원자입니다. 저의 잠재력과 열정을 바탕으로 뛰어난 성과를 만들겠습니다." # 기본 사용자 프롬프트
    else:
        logger.debug(f"입력된 사용자 프롬프트 (일부): {prompt[:100]}...")

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
        logger.warning(".env 파일이 없거나 비어 있습니다. API 키에 대해 환경 변수를 신뢰합니다.")

    GROQ_API_KEY = os.getenv("GROQ_API_KEY")
    COHERE_API_KEY = os.getenv("COHERE_API_KEY")

    if not GROQ_API_KEY:
        logger.error("GROQ_API_KEY가 설정되지 않았습니다.")
        raise ValueError("GROQ_API_KEY가 설정되지 않았습니다.")
    if not COHERE_API_KEY:
        logger.error("COHERE_API_KEY가 설정되지 않았습니다.")
        raise ValueError("COHERE_API_KEY가 설정되지 않았습니다.")
    logger.debug("API 키 로드 완료")

    # 모델 초기화
    try:
        llm = ChatGroq(model="meta-llama/llama-4-maverick-17b-128e-instruct", groq_api_key=GROQ_API_KEY)
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

    # 자기소개서 생성 요청 프롬프트 (RAG 활용 명시)
    query = f"""
    {prompt if prompt else "프롬프트가 제공되지 않았습니다. 전달된 채용 공고를 바탕으로 자기소개서를 작성하세요."}
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

    test_prompt = """저는 창의적이고 문제 해결 능력이 뛰어난 개발자입니다. 
    다양한 AI 프로젝트를 경험하며 Python과 PyTorch를 능숙하게 다룰 수 있게 되었습니다. 
    특히 자연어 처리 분야에 관심이 많아 개인적으로 챗봇 개발 프로젝트를 진행한 경험이 있습니다.
    새로운 기술을 배우는 데 관심이 많고, 빠르게 습득하여 실제 문제 해결에 적용하는 것을 좋아합니다."""
    logger.info("generate_cover_letter_semantic.py 스크립트 직접 실행 (테스트용)")

    try:
        # 테스트 시나리오 1: prompt 제공
        logger.info("테스트 시나리오 1: 사용자 프롬프트 제공")
        raw_cv_with_prompt, formatted_cv_with_prompt = generate_cover_letter(job_posting_content=test_job_content, prompt=test_prompt)
        
        if raw_cv_with_prompt:
            print("\n--- 자기소개서 원본 (사용자 프롬프트 제공) ---")
            print(raw_cv_with_prompt)
            # print("\n--- 40자 개행 자기소개서 (사용자 프롬프트 제공) ---") # 로컬 테스트 시에만 출력
            # print(formatted_cv_with_prompt)

            output_path_with_prompt = "logs/generated_cover_letter_with_prompt_test.txt"
            logger.debug(f"테스트 자기소개서 (사용자 프롬프트 제공) 저장 경로: {output_path_with_prompt}")
            try:
                with open(output_path_with_prompt, "w", encoding="utf-8") as f:
                    f.write(formatted_cv_with_prompt) # 포맷팅된 버전 저장
                logger.info(f"테스트 자기소개서 (사용자 프롬프트 제공)가 성공적으로 저장되었습니다: {output_path_with_prompt}")
            except Exception as e:
                logger.error(f"테스트 자기소개서 (사용자 프롬프트 제공) 파일 저장 중 오류 발생: {e}", exc_info=True)
        else:
            logger.warning(f"테스트 자기소개서 (사용자 프롬프트 제공) 생성 실패: {formatted_cv_with_prompt}") 
            print(f"테스트 자기소개서 (사용자 프롬프트 제공) 생성에 실패했습니다. 메시지: {formatted_cv_with_prompt}")

        # 테스트 시나리오 2: prompt 미제공 (None)
        logger.info("테스트 시나리오 2: 사용자 프롬프트 미제공 (None)")
        raw_cv_no_prompt, formatted_cv_no_prompt = generate_cover_letter(job_posting_content=test_job_content, prompt=None)

        if raw_cv_no_prompt:
            print("\n--- 자기소개서 원본 (사용자 프롬프트 미제공) ---")
            print(raw_cv_no_prompt)
            # print("\n--- 40자 개행 자기소개서 (사용자 프롬프트 미제공) ---")
            # print(formatted_cv_no_prompt)

            output_path_no_prompt = "logs/generated_cover_letter_no_prompt_test.txt"
            logger.debug(f"테스트 자기소개서 (사용자 프롬프트 미제공) 저장 경로: {output_path_no_prompt}")
            try:
                with open(output_path_no_prompt, "w", encoding="utf-8") as f:
                    f.write(formatted_cv_no_prompt) # 포맷팅된 버전 저장
                logger.info(f"테스트 자기소개서 (사용자 프롬프트 미제공)가 성공적으로 저장되었습니다: {output_path_no_prompt}")
            except Exception as e:
                logger.error(f"테스트 자기소개서 (사용자 프롬프트 미제공) 파일 저장 중 오류 발생: {e}", exc_info=True)
        else:
            logger.warning(f"테스트 자기소개서 (사용자 프롬프트 미제공) 생성 실패: {formatted_cv_no_prompt}")
            print(f"테스트 자기소개서 (사용자 프롬프트 미제공) 생성에 실패했습니다. 메시지: {formatted_cv_no_prompt}")
            
        # 테스트 시나리오 3: prompt 빈 문자열
        logger.info("테스트 시나리오 3: 사용자 프롬프트 빈 문자열")
        raw_cv_empty_prompt, formatted_cv_empty_prompt = generate_cover_letter(job_posting_content=test_job_content, prompt="")

        if raw_cv_empty_prompt:
            print("\n--- 자기소개서 원본 (사용자 프롬프트 빈 문자열) ---")
            print(raw_cv_empty_prompt)
            # print("\n--- 40자 개행 자기소개서 (사용자 프롬프트 빈 문자열) ---")
            # print(formatted_cv_empty_prompt)

            output_path_empty_prompt = "logs/generated_cover_letter_empty_prompt_test.txt"
            logger.debug(f"테스트 자기소개서 (사용자 프롬프트 빈 문자열) 저장 경로: {output_path_empty_prompt}")
            try:
                with open(output_path_empty_prompt, "w", encoding="utf-8") as f:
                    f.write(formatted_cv_empty_prompt) # 포맷팅된 버전 저장
                logger.info(f"테스트 자기소개서 (사용자 프롬프트 빈 문자열)가 성공적으로 저장되었습니다: {output_path_empty_prompt}")
            except Exception as e:
                logger.error(f"테스트 자기소개서 (사용자 프롬프트 빈 문자열) 파일 저장 중 오류 발생: {e}", exc_info=True)
        else:
            logger.warning(f"테스트 자기소개서 (사용자 프롬프트 빈 문자열) 생성 실패: {formatted_cv_empty_prompt}")
            print(f"테스트 자기소개서 (사용자 프롬프트 빈 문자열) 생성에 실패했습니다. 메시지: {formatted_cv_empty_prompt}")

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