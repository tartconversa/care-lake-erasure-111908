"""通用工具：可注入时钟、原子文件改写、键控散列、JSON 遍历。"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def parse_time(text):
    """ISO 8601 时间或日期 → 带时区 datetime；无时区按 UTC。"""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def current_iso():
    """真实当前时间；状态变更命令建议用 --now 注入。"""
    return datetime.now(timezone.utc).isoformat()


def stamp(iso_text):
    """文件名安全的时间戳。"""
    return parse_time(iso_text).strftime('%Y%m%dT%H%M%S')


def new_id(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:12]}'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json_atomic(path, obj):
    """同目录临时文件 + fsync + 替换，避免半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(obj, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_secret(path):
    """项目级键控散列密钥；不存在则生成，权限 600。"""
    path = Path(path)
    if path.exists():
        return path.read_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = os.urandom(32)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-key-')
    with os.fdopen(fd, 'wb') as stream:
        stream.write(secret)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return secret


def keyed(secret, *parts):
    """键控散列：墓碑与核对只存散列，不存个人值原文。"""
    material = '\x00'.join(['v1', *parts]).encode('utf-8')
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def walk_strings(obj, path='$'):
    """遍历 JSON 中的字符串值，产出 (JSON 路径, 值)。"""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from walk_strings(item, f'{path}[{i}]')
    elif isinstance(obj, dict):
        for key, item in obj.items():
            yield from walk_strings(item, f'{path}.{key}')
