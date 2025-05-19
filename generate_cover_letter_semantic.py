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

def format_text_by_length(text, length=50):
    logging.debug(f"{length}자 단위로 텍스트 포맷팅 시도...")
    try:
        # 기존 줄바꿈 문자를 모두 제거하거나 공백으로 대체 (선택)
        # text = text.replace('\n', ' ') # 모든 줄바꿈을 공백으로 변경
        text = text.replace('\r\n', '\n').replace('\r', '\n') # 다양한 줄바꿈을 \n으로 통일
        lines = text.split('\n')
        
        formatted_lines = []
        for line in lines:
            if not line.strip(): # 빈 줄이거나 공백만 있는 줄
                formatted_lines.append("") # 빈 줄 유지
                continue

            # 50자마다 줄바꿈 추가
            wrapped_line = '\n'.join([line[i:i+length] for i in range(0, len(line), length)])
            formatted_lines.append(wrapped_line)
        
        formatted_text = '\n'.join(formatted_lines)
        logging.debug("텍스트 포맷팅 완료.")
        return formatted_text
    except Exception as e:
        logging.error(f"텍스트 포맷팅 중 오류 발생: {e}")
        return text # 오류 발생 시 원본 텍스트 반환

# .env 파일에서 환경 변수 로드
logging.debug("환경 변수 로드 시도...")
if load_dotenv():
    logging.debug(".env 파일 로드 성공")
else:
    logging.warning(".env 파일을 찾을 수 없거나 로드에 실패했습니다.")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
COHERE_API_KEY = os.getenv("COHERE_API_KEY")

if not GEMINI_API_KEY:
    logging.error("GEMINI_API_KEY가 설정되지 않았습니다.")
    exit()
if not COHERE_API_KEY:
    logging.error("COHERE_API_KEY가 설정되지 않았습니다.")
    exit()

logging.debug("API 키 로드 완료")

# 모델 초기화
logging.debug("GoogleGenerativeAI 모델 초기화 시도...")
try:
    llm = GoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=GEMINI_API_KEY)
    logging.debug("GoogleGenerativeAI 모델 초기화 성공")
except Exception as e:
    logging.error(f"GoogleGenerativeAI 모델 초기화 중 오류 발생: {e}")
    exit()

logging.debug("CohereEmbeddings 모델 초기화 시도...")
try:
    embeddings = CohereEmbeddings(model="embed-multilingual-v3.0", cohere_api_key=COHERE_API_KEY, user_agent="langchain")
    logging.debug("CohereEmbeddings 모델 초기화 성공")
except Exception as e:
    logging.error(f"CohereEmbeddings 모델 초기화 중 오류 발생: {e}")
    exit()

# 채용공고 파일 경로 (스크립트와 동일한 위치에 있다고 가정)
file_path = "logs/body_html_recursive_jobkorea.co.kr_recruit_gi_read_46819578oem_co_fe880758_formatted.txt"
logging.debug(f"채용공고 파일 경로: {file_path}")

# 파일 존재 확인
if not os.path.exists(file_path):
    logging.error(f"파일을 찾을 수 없습니다: {file_path}")
    exit()

logging.debug("채용공고 파일 읽기 시도...")
try:
    with open(file_path, "r", encoding="utf-8") as f:
        job_posting_text = f.read()
    logging.debug("채용공고 파일 읽기 성공")
except Exception as e:
    logging.error(f"채용공고 파일 읽기 중 오류 발생: {e}")
    exit()

# 의미론적 청킹
logging.debug("의미론적 청킹 시도...")
try:
    text_splitter = SemanticChunker(embeddings)
    docs = text_splitter.create_documents([job_posting_text])
    logging.debug(f"의미론적 청킹 완료. 생성된 문서 수: {len(docs)}")
    if not docs:
        logging.warning("의미론적 청킹 결과 문서가 없습니다.")
except Exception as e:
    logging.error(f"의미론적 청킹 중 오류 발생: {e}")
    # 의미론적 청킹 실패 시 재귀 분할로 대체 (선택 사항)
    # logging.info("의미론적 청킹 실패. 재귀 분할 시도...")
    # from langchain_text_splitters import RecursiveCharacterTextSplitter
    # text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    # docs = text_splitter.create_documents([job_posting_text])
    # logging.debug(f"재귀 분할 완료. 생성된 문서 수: {len(docs)}")
    exit()


# FAISS 벡터 저장소 생성
logging.debug("FAISS 벡터 저장소 생성 시도...")
try:
    vectorstore = FAISS.from_documents(docs, embeddings)
    logging.debug("FAISS 벡터 저장소 생성 성공")
except Exception as e:
    logging.error(f"FAISS 벡터 저장소 생성 중 오류 발생: {e}")
    exit()

# RAG 체인 설정
logging.debug("RAG 체인 설정 시도...")
try:
    qa_chain = RetrievalQA.from_chain_type(
        llm,
        retriever=vectorstore.as_retriever(),
        chain_type="stuff" # 또는 다른 chain_type (e.g., "map_reduce", "refine")
    )
    logging.debug("RAG 체인 설정 성공")
except Exception as e:
    logging.error(f"RAG 체인 설정 중 오류 발생: {e}")
    exit()

# 자기소개서 생성 요청 프롬프트
query = '''
위 내용을 바탕으로 자기소개서를 작성해주세요. 
'''
logging.debug(f"자기소개서 생성 요청 프롬프트: {query}")

logging.debug("자기소개서 생성 시도...")
try:
    result = qa_chain.invoke({"query": query})
    generated_text = result["result"]
    logging.debug("자기소개서 생성 성공")
    print("--- 자기소개서 원본 ---")
    print(generated_text)

    # 생성된 자기소개서를 40자 단위로 포맷팅
    formatted_cover_letter = format_text_by_length(generated_text, 40)
    print("\n--- 40자 개행 자기소개서 ---")
    print(formatted_cover_letter)

    # 포맷팅된 자기소개서를 파일에 저장
    output_path = "logs/generated_cover_letter_formatted.txt"
    logging.debug(f"자기소개서 저장 경로: {output_path}")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(formatted_cover_letter)
        logging.info(f"자기소개서가 성공적으로 저장되었습니다: {output_path}")
    except Exception as e:
        logging.error(f"자기소개서 파일 저장 중 오류 발생: {e}")

except Exception as e:
    logging.error(f"자기소개서 생성 중 오류 발생: {e}")

logging.debug("스크립트 실행 완료.") 