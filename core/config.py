import os
from dotenv import load_dotenv

load_dotenv() # .env 파일에서 환경 변수 로드

class Settings:
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY")
    GROQ_LLM_MODEL: str = os.getenv("GROQ_LLM_MODEL", "mixtral-8x7b-32768")

settings = Settings() 