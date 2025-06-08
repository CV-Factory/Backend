import os
import sys
import time
import logging
from urllib.parse import urlparse

import redis

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def wait_for_redis():
    """
    Redis가 사용 가능해질 때까지 대기하고, 성공하면 전달된 명령어를 실행합니다.
    """
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logging.error("REDIS_URL 환경 변수가 설정되지 않았습니다.")
        sys.exit(1)

    logging.info(f"Redis 연결 시도: {redis_url}")

    # redis-py는 rediss:// URL 스킴을 지원하므로 별도 파싱이 크게 필요 없지만,
    # SSL 설정을 명시적으로 확인하기 위해 파싱할 수 있습니다.
    parsed_url = urlparse(redis_url)
    ssl_enabled = parsed_url.scheme == 'rediss'

    # 연결 시도 (최대 60초)
    start_time = time.time()
    while time.time() - start_time < 60:
        try:
            # redis-py 4.2.0 이상부터는 from_url에서 바로 ssl_cert_reqs를 지원합니다.
            # Upstash Redis와 같은 TLS/SSL 연결을 위해 이 옵션이 중요할 수 있습니다.
            r = redis.from_url(redis_url, ssl_cert_reqs='required' if ssl_enabled else None, socket_connect_timeout=5)
            if r.ping():
                logging.info("Redis 연결 성공!")
                # 연결 성공 시, 이 스크립트에 전달된 나머지 인자들을 명령어로 실행
                process_args = sys.argv[1:]
                if not process_args:
                    logging.info("실행할 명령어가 없습니다. 스크립트를 종료합니다.")
                    sys.exit(0)
                
                logging.info(f"다음 명령어 실행: {' '.join(process_args)}")
                os.execvp(process_args[0], process_args)
                return # execvp는 현재 프로세스를 대체하므로 이 라인은 실행되지 않음
        except redis.exceptions.ConnectionError as e:
            logging.warning(f"Redis 연결 실패: {e}. 2초 후 재시도합니다.")
            time.sleep(2)
        except Exception as e:
            logging.error(f"예상치 못한 오류 발생: {e}")
            time.sleep(2)

    logging.error("60초 내에 Redis에 연결할 수 없습니다. 스크립트를 종료합니다.")
    sys.exit(1)

if __name__ == "__main__":
    wait_for_redis() 