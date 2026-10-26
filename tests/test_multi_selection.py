import unittest
from pydantic import ValidationError

from workbench.schemas import Collection,Generation
import test_engine as fixtures


class SelectionSchemaTests(unittest.TestCase):
    def test_legacy_scalar_settings_are_preserved(self):
        old=Collection(keyword='AI',domain='科技',kind='micro',generation={'style':'正式'})
        self.assertEqual(old.domains,['科技'])
        self.assertEqual(old.kinds,['micro'])
        self.assertEqual(old.generation.styles,['正式'])

    def test_multiple_choices_are_deduplicated_and_empty_required_choices_rejected(self):
        options=Collection(kinds=['article','micro','article'],domains=['科技','生活','科技'])
        self.assertEqual(options.kinds,['article','micro'])
        self.assertEqual(options.domains,['科技','生活'])
        self.assertEqual(Generation(styles=['正式','口语化','正式']).styles,['正式','口语化'])
        for factory,data in [(Generation,{'styles':[]}),(Collection,{'kinds':[]})]:
            with self.assertRaises(ValidationError):factory(**data)


class MultiSelectionEngineTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.EngineTests.asyncSetUp
    asyncTearDown=fixtures.EngineTests.asyncTearDown
    async def test_collection_covers_each_type_and_domain_with_shared_limit(self):
        calls=[]
        async def search(keyword,kind,limit):
            calls.append((keyword,kind,limit))
            return [{'url':f'https://www.toutiao.com/{"w" if kind=="micro" else "article"}/{7699775205312300000+len(calls)*10+i}/','kind':kind} for i in range(limit)]
        self.browser.search=search
        self.engine.capabilities['micro']=True
        task=self.store.create_task('collect',{'keyword':'AI','kinds':['article','micro'],'domains':['科技','生活'],'limit':8})
        await self.engine.execute(task['id'])
        self.assertEqual({(k,t) for k,t,_ in calls},{('AI 科技','article'),('AI 科技','micro'),('AI 生活','article'),('AI 生活','micro')})
        self.assertEqual(sum(limit for _,_,limit in calls),8)
        self.assertEqual(len(self.store.materials()),8)
        self.assertEqual({x['kind'] for x in self.store.materials()},{'article','micro'})

    async def test_each_style_gets_its_own_versions_and_retry_skips_them(self):
        row,_=self.store.upsert_material(await self.browser.detail('https://www.toutiao.com/article/7699775205312315954/'))
        task=self.store.create_task('generate',{'ids':[row['id']],'mode':'single','styles':['正式','口语化'],'versions':2})
        await self.engine.execute(task['id'])
        self.assertEqual(len(self.store.drafts()),4)
        self.assertEqual({d['options']['style'] for d in self.store.drafts()},{'正式','口语化'})
        await self.engine.execute(task['id'])
        self.assertEqual(len(self.llm.calls),4)
