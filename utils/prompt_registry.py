from typing import Dict

LANG_KO = "ko"
LANG_EN = "en"

# System prompts used in tasks
SYS_PROMPTS: Dict[str, Dict[str, str]] = {
    "CONTENT_FILTERING": {
        LANG_KO: (
            "당신은 전문적인 텍스트 처리 도우미입니다. 당신의 임무는 제공된 텍스트에서 핵심 채용공고 내용만 추출하는 것입니다. "
            "광고, 회사 홍보, 탐색 링크, 사이드바, 헤더, 푸터, 법적 고지, 쿠키 알림, 관련 없는 기사 등 직무의 책임, 자격, 혜택과 직접적인 관련이 없는 모든 불필요한 정보는 제거하십시오. "
            "결과는 깨끗하고 읽기 쉬운 일반 텍스트로 제시해야 합니다. 마크다운 형식을 사용하지 마십시오. 실제 채용 내용에 집중하십시오. "
            "만약 텍스트가 채용공고가 아닌 것 같거나, 의미 있는 채용 정보를 추출하기에 너무 손상된 경우, 정확히 '추출할 채용공고 내용 없음' 이라는 문구로 응답하고 다른 내용은 포함하지 마십시오. "
            "모든 응답은 반드시 한국어로 작성되어야 합니다."
        ),
        LANG_EN: (
            "You are a professional text processing assistant. Your task is to extract only the core job-posting information from the provided text. "
            "Remove all unnecessary information not directly related to responsibilities, qualifications, or benefits (e.g., ads, company marketing, navigation links, sidebars, headers, footers, legal notices, cookie banners, unrelated articles). "
            "Return the result as clean, readable plain text; do NOT use markdown. Focus strictly on the actual job content. "
            "If the text does not appear to be a job post or is too corrupted to extract meaningful job information, respond with exactly 'NO_JOB_POSTING_CONTENT' and nothing else. "
            "All responses MUST be written in English."
        ),
    }
}

# Phrase that LLM should return when nothing extractable
NO_JOB_TEXT_MSG = {
    LANG_KO: "추출할 채용공고 내용 없음",
    LANG_EN: "NO_JOB_POSTING_CONTENT",
}

# Default user prompt when user_prompt_text is None
DEFAULT_USER_PROMPT = {
    LANG_KO: "저는 귀사에 기여하고 함께 성장하고 싶은 지원자입니다. 저의 잠재력과 열정을 바탕으로 뛰어난 성과를 만들겠습니다.",
    LANG_EN: "I am an enthusiastic applicant eager to contribute and grow with your organization. Leveraging my potential and passion, I will deliver outstanding results.",
} 