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

    async def test_revision_removes_unsupported_details_but_keeps_audit_for_check(self):
        results=[{'title':'机器人展示','body':'机器人售价999元。','verify':['来源没有售价']},
                 {'title':'机器人展示','body':'甲公司展示配送机器人，售价尚未公布。','verify':[]},
                 {'passed':True,'issues':[]}]
        calls=[]
        async def respond(req):
            calls.append(json.loads(req.content))
            return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(results.pop(0),ensure_ascii=False)}}]})
        service=MicroContent(LLM('https://model.test/v1','key','test',transport=httpx.MockTransport(respond)))
        draft=await service.generate([SOURCE],'科技')
        self.assertNotIn('999',draft['body'])
        self.assertEqual(draft['editorial_review'],['来源没有售价'])
        self.assertTrue((await service.check(draft,[SOURCE],'科技'))['passed'])
        self.assertIn('editorial_review',calls[-1]['messages'][1]['content'])

    async def test_failed_revision_keeps_original_unresolved_items(self):
        counter=0
        async def respond(req):
            nonlocal counter
            counter+=1
            if counter==1:return httpx.Response(200,json={'choices':[{'message':{'content':'{"title":"标题","body":"仍有疑点的稿件","verify":["关键事实待核实"]}'}}]})
            return httpx.Response(400)
        service=MicroContent(LLM('https://model.test/v1','key','test',transport=httpx.MockTransport(respond)))
        draft=await service.generate([SOURCE],'科技')
        self.assertIn('关键事实待核实',draft['verify'])

    async def test_quality_repair_preserves_issues_and_source_identity(self):
        service=self.service({'title':'AI会议','body':'报道说会议讨论AI应用。','verify':[],'topic':'AI'})
        old={'title':'旧稿','body':'专家称经营成效显著。','verify':[],
             'sources':[{'url':SOURCE['url']}],'source_snapshots':[SOURCE],
             'editorial_review':['成效未核实'],'options':{'topic':'民生'}}
        result=await service.repair(old,[SOURCE],'民生',['应删除成效断言'])
        self.assertEqual(result['options']['topic'],'AI')
        self.assertEqual(result['sources'],old['sources'])
        self.assertEqual(result['editorial_review'],['成效未核实','应删除成效断言'])
        self.assertNotIn('显著',result['body'])
        self.assertEqual(old['body'],'专家称经营成效显著。')

    async def test_quality_repair_rejects_missing_topic_or_unresolved_body(self):
        for result in ({'title':'标题','body':'正文','verify':[]},
                       {'title':'标题','body':'','verify':[],'topic':'AI'}):
            with self.assertRaises(ValueError):
                await self.service(result).repair({'body':'正文','sources':[]},[SOURCE],'AI',['检查失败'])

    async def test_check_compares_source_facts_without_collection_metadata(self):
        calls=[];service=self.service({'passed':True,'issues':[]},calls)
        source={**SOURCE,'collected_at':'later','metrics_collected_at':'later','comments':42}
        draft={'body':'报道提到展会展示机器人。','verify':[],'editorial_review':['价格未披露'],
               'source_snapshots':[{**SOURCE,'collected_at':'earlier'}],'options':{'topic':'科技'}}
        await service.check(draft,[source],'科技')
        payload=json.loads(calls[0]['messages'][1]['content'])
        self.assertNotIn('collected_at',json.dumps(payload))
        self.assertEqual(payload['sources'][0]['body'],SOURCE['body'])
        self.assertEqual(payload['draft']['source_snapshots'][0]['body'],SOURCE['body'])
        self.assertEqual(payload['draft']['editorial_review'],['价格未披露'])
