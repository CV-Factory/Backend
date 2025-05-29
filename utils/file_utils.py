import logging
import re
import hashlib
import time
import uuid
from urllib.parse import urlparse
from typing import Any
import datetime
import os

logger = logging.getLogger(__name__)

MAX_FILENAME_LENGTH = 100

def try_format_log(data: Any, max_len: int = 250) -> str:
    """로깅을 위해 데이터를 안전하게 문자열로 변환하고, 너무 길면 축약합니다."""
    try:
        if isinstance(data, dict):
            s_data = str({k: (v[:max_len // len(data.keys())] + '...' if isinstance(v, str) and len(v) > max_len // len(data.keys()) else v) for k, v in data.items()})
        elif isinstance(data, str):
            s_data = data
        elif isinstance(data, list):
            s_data = str(data)
        else:
            s_data = repr(data)

        if len(s_data) > max_len:
            return s_data[:max_len] + f"... (truncated, original_len={len(s_data)})"
        return s_data
    except Exception as e:
        return f"[Error formatting log data: {e}]"

def sanitize_filename(url_or_name: str, extension: str = "", ensure_unique: bool = False) -> str:
    """URL 또는 임의의 문자열을 기반으로 안전하고 유효한 파일 이름을 생성합니다."""
    try:
        if url_or_name.startswith(('http://', 'https://')):
            parsed_url = urlparse(url_or_name)
            name_part = parsed_url.netloc.replace('www.', '') + "_" + parsed_url.path.replace('/', '_')
        else:
            name_part = url_or_name

        name_part = re.sub(r'[^a-zA-Z0-9_.-]', '_', name_part)
        name_part = re.sub(r'_+', '_', name_part).strip('_')

        reserved_len = (len(extension) + 1 if extension else 0) + (8 + 1 if ensure_unique else 0)
        if len(name_part) > MAX_FILENAME_LENGTH - reserved_len:
            name_part = name_part[:MAX_FILENAME_LENGTH - reserved_len]
        
        if ensure_unique:
            unique_suffix = hashlib.md5(name_part.encode('utf-8')).hexdigest()[:8]
            base_name = f"{name_part}_{unique_suffix}"
        else:
            base_name = name_part

        final_name = f"{base_name}{'.' + extension if extension else ''}".lower()
        logger.debug(f"Sanitized filename for '{url_or_name}': {final_name}")
        return final_name

    except Exception as e:
        logger.error(f"Error sanitizing filename for '{url_or_name}': {e}", exc_info=True)
        timestamp = int(time.time())
        safe_ext = f".{extension}" if extension else ""
        error_name = f"error_filename_{timestamp}_{uuid.uuid4().hex[:4]}{safe_ext}"
        logger.warning(f"Returning error-fallback filename: {error_name}")
        return error_name

def get_datetime_prefix():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def save_content_to_file(file_path: str, content: str, encoding: str = "utf-8"):
    """주어진 내용을 지정된 파일 경로에 저장합니다."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding=encoding) as f:
            f.write(content)
        logger.info(f"Content successfully saved to: {file_path}")
    except Exception as e:
        logger.error(f"Error saving content to file {file_path}: {e}", exc_info=True)
        raise 