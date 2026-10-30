import json
import unittest
import httpx
from workbench.llm import LLM
from workbench.micro_content import MicroContent

SOURCE={'id':'s1','title':'机器人亮相展会','body':'10月30日，甲公司在深圳展会展示配送机器人，尚未公布售价。','author':'科技日报','url':'https://www.toutiao.com/article/7701979050666902079/'}

class MicroContentTests(unittest.IsolatedAsyncioTestCase):
    def service(self,result,calls=None):
        async def respond(req):
            if calls is not None:calls.append(json.loads(req.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})
        return MicroContent(LLM('https://model.test/v1','key','test',transport=httpx.MockTransport(respond)))

    async def test_plain_micro_body_keeps_source_and_model(self):
        calls=[]
        service=self.service({'title':'配送机器人','body':'配送机器人来到展会。尚未公布售价。','verify':[]},calls)
        result=await service.generate([SOURCE],'科技')
        self.assertIn('来源：科技日报',result['body'])
        self.assertEqual(result['sources'][0]['url'],SOURCE['url'])
        self.assertEqual(result['model'],'test')
        self.assertIn('微头条',calls[0]['messages'][0]['content'])
        self.assertIn('不可信',calls[0]['messages'][0]['content'])

    async def test_check_fails_closed_on_unsupported_or_unknown_verdict(self):
        for verdict in [{'passed':False,'issues':['售价未获来源支持']},{'passed':'yes','issues':[]},{'passed':True,'issues':['有冲突']}]:
            service=self.service(verdict)
            self.assertFalse((await service.check({'body':'售价199元','verify':[]},[SOURCE],'科技'))['passed'])

    async def test_check_requires_sources_and_no_unresolved_items(self):
        service=self.service({'passed':True,'issues':[]})
        self.assertFalse((await service.check({'body':'正文','verify':['待核实']},[SOURCE],'科技'))['passed'])
        self.assertFalse((await service.check({'body':'正文','verify':[]},[],'科技'))['passed'])
        self.assertTrue((await service.check({'body':'正文','verify':[]},[SOURCE],'科技'))['passed'])

    async def test_empty_or_overlong_generated_body_rejected(self):
        for body in ['', '文'*1001]:
            with self.assertRaises(ValueError):await self.service({'title':'标题','body':body,'verify':[]}).generate([SOURCE],'科技')
