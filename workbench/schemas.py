from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,model_validator,field_validator

from .parsing import canonical_url

DOMAINS=['生活','影视','体育','情感','娱乐','财经','三农','国际','军事','搞笑','美食','汽车','时尚','音乐','健康','历史','游戏','法律','养老','育儿','房产','科普','综艺','摄影','动漫','科技','其他']
Style=Literal['正式','口语化','自媒体叙述','新闻式表达']


class StrictModel(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Generation(StrictModel):
    mode:Literal['single','multi']='single'
    style:Style='口语化'
    styles:list[Style]|None=Field(default=None,max_length=4)
    degree:Literal['轻','中','深']='中'
    length:Literal['接近原文','精简','300','500','800']='500'
    versions:int=Field(default=1,ge=1,le=3)
    notes:str=Field(default='',max_length=10000)

    @model_validator(mode='after')
    def selected_styles(self):
        self.styles=list(dict.fromkeys(self.styles if self.styles is not None else [self.style]))
        if not self.styles:
            raise ValueError('请至少选择一种改写风格')
        self.style=self.styles[0]
        return self


class GenerateRequest(Generation):
    ids:list[str]=Field(min_length=1,max_length=30)

    @model_validator(mode='after')
    def check_sources(self):
        self.ids=list(dict.fromkeys(self.ids))
        if self.mode=='multi' and not 2<=len(self.ids)<=5:
            raise ValueError('多来源成稿需要选择 2～5 篇素材')
        return self


class Collection(StrictModel):
    keyword:str=Field(default='',max_length=100)
    domain:str=Field(default='',max_length=20)
    kind:Literal['article','micro']='article'
    domains:list[str]|None=Field(default=None,max_length=30)
    kinds:list[Literal['article','micro']]|None=Field(default=None,max_length=3)
    hours:Literal[0,3,12,24,48,168]=168
    read_min:int|None=Field(default=None,ge=0)
    read_max:int|None=Field(default=None,ge=1)
    sort:Literal['reads','comments','likes','published_at']='published_at'
    limit:int=Field(default=30,ge=1,le=100)
    auto_generate:bool=False
    generation:Generation=Field(default_factory=Generation)

    @model_validator(mode='after')
    def check_values(self):
        self.keyword=self.keyword.strip()
        self.domains=list(dict.fromkeys(self.domains if self.domains is not None else ([self.domain] if self.domain else [])))
        self.kinds=list(dict.fromkeys(self.kinds if self.kinds is not None else [self.kind]))
        if not self.kinds:
            raise ValueError('请至少选择一种内容体裁')
        self.kind=self.kinds[0]
        self.domain=self.domains[0] if self.domains else ''
        if any(domain not in DOMAINS for domain in self.domains):
            raise ValueError('未知领域')
        if self.read_min is not None and self.read_max is not None and self.read_min>=self.read_max:
            raise ValueError('阅读量区间无效')
        if self.auto_generate and self.generation.mode!='single':
            raise ValueError('自动生成仅支持单篇模式；多来源成稿请手动选择素材')
        return self


class ImportRequest(StrictModel):
    urls:list[str]=Field(min_length=1,max_length=30)

    @field_validator('urls')
    @classmethod
    def links(cls,urls):
        return list(dict.fromkeys(canonical_url(u.strip()) for u in urls))


class Schedule(StrictModel):
    enabled:bool=False
    minutes:Literal[30,60,180]=30
    collection:Collection|None=None

    @model_validator(mode='after')
    def required_options(self):
        if self.enabled and self.collection is None:
            raise ValueError('开启定时需设置采集条件')
        return self


class DraftEdit(StrictModel):
    title:str=Field(min_length=1,max_length=300)
    body:str=Field(min_length=1,max_length=150000)
    verify:list[str]=Field(default_factory=list,max_length=100)
