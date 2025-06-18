import logging
import sys

def setup_logging():
    """
    애플리케이션 전체의 로깅을 중앙에서 설정합니다.
    이 함수는 main.py와 celery_app.py의 시작 부분에서 한 번만 호출되어야 합니다.
    """
    # 루트 로거를 가져옵니다.
    root_logger = logging.getLogger()
    
    # 이미 설정된 핸들러가 있다면 모두 제거합니다 (중복 로깅 방지).
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # 기본 로그 레벨을 INFO로 설정합니다.
    root_logger.setLevel(logging.INFO)

    # 새로운 스트림 핸들러를 추가합니다.
    handler = logging.StreamHandler(sys.stdout)
    
    # 로그 포맷을 설정합니다.
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # 핸들러를 루트 로거에 추가합니다.
    root_logger.addHandler(handler)

    # 너무 상세한 로그를 남기는 서드파티 라이브러리들의 로그 레벨을 WARNING으로 조정합니다.
    noisy_libraries = ["httpcore", "httpx", "cohere", "playwright", "celery", "kombu", "redis"]
    for lib_name in noisy_libraries:
        logging.getLogger(lib_name).setLevel(logging.WARNING)

    logging.info("Logging configured successfully.")

# 이 파일이 직접 실행될 때 테스트용으로 로깅 설정을 적용해볼 수 있습니다.
if __name__ == '__main__':
    setup_logging()
    logging.debug("This is a debug message.") # 출력되지 않아야 함
    logging.info("This is an info message.")
    logging.warning("This is a warning message.")
    logging.getLogger("httpx").info("This is an httpx info message.") # 출력되지 않아야 함
    logging.getLogger("httpx").warning("This is an httpx warning message.") 