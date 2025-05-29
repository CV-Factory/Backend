# CVFactory_Server/tasks/__init__.py

# 이 파일은 tasks 디렉토리를 파이썬 패키지로 만듭니다.
# 필요한 경우, 이 패키지에서 공통으로 사용될 초기화 코드나
# 특정 모듈들을 미리 임포트하여 외부에서 쉽게 접근하도록 할 수 있습니다.

# 예시: tasks 패키지 내의 모든 .py 파일에서 @celery_app.task로 데코레이트된 함수들을
# 자동으로 로드하도록 Celery의 autodiscover_tasks와 유사한 기능을 구현하거나,
# 또는 단순히 하위 모듈들을 임포트 할 수 있습니다.

# 현재는 각 태스크 모듈이 celery_app 인스턴스를 직접 임포트하여 사용하므로,
# 이 __init__.py 파일이 복잡한 로직을 가질 필요는 없습니다.
# Celery가 태스크를 발견하려면 celery_app.autodiscover_tasks()가 호출될 때
# 이 패키지 (또는 상위 패키지)가 검색 경로에 포함되어야 합니다.

# 각 태스크 모듈에서 직접 celery_app을 사용하므로, 여기서는 간단히 비워둡니다.
# 또는, 명시적으로 각 태스크 함수를 임포트하여 tasks 패키지의 네임스페이스에 추가할 수 있습니다.
# from .html_extraction import step_1_extract_html
# from .text_extraction import step_2_extract_text
# from .content_filtering import step_3_filter_content
# from .cover_letter_generation import step_4_generate_cover_letter
# from .pipeline_callbacks import handle_pipeline_completion

# __all__ = [
#     "step_1_extract_html",
#     "step_2_extract_text",
#     "step_3_filter_content",
#     "step_4_generate_cover_letter",
#     "handle_pipeline_completion"
# ]

# logger = logging.getLogger(__name__)
# logger.debug("CVFactory_Server.tasks package initialized.") 