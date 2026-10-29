"""Local model credentials: file-only storage, no credential response values."""
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import Field, field_validator
from .schemas import StrictModel

KEYS=('LLM_BASE_URL','LLM_API_KEY','LLM_MODEL')


class ModelConfig(StrictModel):
    base_url:str=Field(min_length=1,max_length=2000)
    api_key:str=Field(default='',max_length=8192,repr=False)
    model:str=Field(min_length=1,max_length=200)

    @field_validator('base_url','api_key','model')
    @classmethod
    def single_line(cls,value):
        value=value.strip()
        if any(c in value for c in ('\n','\r','\x00')):
            raise ValueError('模型配置必须为单行文本')
        return value

    @field_validator('base_url')
    @classmethod
    def service_prefix(cls,value):
        p=urlsplit(value)
        if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError('Base URL 需要 HTTP(S) 服务前缀，不含凭据、参数或片段')
        if p.path.rstrip('/').endswith('/chat/completions'):
            raise ValueError('Base URL 不应包含 /chat/completions')
        return value.rstrip('/')


def load_model_values(project, data_dir):
    values=dict(dotenv_values(Path(project)/'.env'))
    values.update({key:os.environ[key] for key in KEYS if key in os.environ})
    saved=Path(data_dir)/'model.env'
    if saved.exists(): values.update(dotenv_values(saved,interpolate=False))
    return [values.get(key) or '' for key in KEYS]


def save_model_values(data_dir,options,key):
    directory=Path(data_dir)
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    values=dict(zip(KEYS,(options.base_url,key,options.model)))
    handle=tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='.model-',dir=directory,delete=False)
    path=Path(handle.name)
    try:
        with handle:
            os.chmod(path,0o600)
            handle.write(''.join(f'{k}={json.dumps(v,ensure_ascii=False)}\n' for k,v in values.items()))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path,directory/'model.env')
    finally:
        path.unlink(missing_ok=True)
