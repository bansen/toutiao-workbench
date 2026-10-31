import unittest
from datetime import datetime, timedelta, timezone
from workbench.draft_review import resolve_concerns, approve, approval_matches


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.source={'url':'https://www.toutiao.com/article/7702620411573420571/','kind':'article',
                     'author':'央视新闻','published_at':self.now.isoformat(),'body':'中央气象台预报，明天东北部分地区预计降温4至8度。'}
        self.draft={'title':'中央气象台预报东北降温','body':'据中央气象台预报，明天东北部分地区预计降温4至8度。',
                    'sources':[self.source],'verify':['降温是预报值，尚待实测确认']}
        self.resolution={'issue_index':0,'decision':'resolved','kind':'forecast_or_plan',
            'body_quote':'明天东北部分地区预计降温4至8度','source_url':self.source['url'],
            'source_quote':'明天东北部分地区预计降温4至8度','reason':'正文明确归属预报，没有声称已经观测到降温'}

    def test_attributed_forecast_requires_grounded_resolution(self):
        result={'passed':True,'issues':[],'resolutions':[self.resolution]}
        audit=resolve_concerns(self.draft,[self.source],result)
        self.assertEqual(audit[0]['kind'],'forecast_or_plan')
        for invalid in [{**result,'resolutions':[]},
                        {**result,'resolutions':[{**self.resolution,'source_quote':'原文没有出现的测量结果'}]},
                        {**result,'resolutions':[{**self.resolution,'body_quote':'正文没有出现的表述'}]},
                        {**result,'resolutions':[{**self.resolution,'kind':'format_corrected'}]},
                        {**result,'resolutions':[{**self.resolution,'source_url':'https://example.com/'}]}]:
            with self.assertRaises(ValueError):resolve_concerns(self.draft,[self.source],invalid)

    def test_approval_invalidates_for_edits_source_changes_age_and_policy(self):
        draft={**self.draft,'verify':[]}
        approved=approve(draft,[self.source],self.now,'policy-v1')
        self.assertTrue(approval_matches(approved,[self.source],self.now,'policy-v1'))
        self.assertFalse(approval_matches({**approved,'body':draft['body']+'已实测'},[self.source],self.now,'policy-v1'))
        self.assertFalse(approval_matches(approved,[{**self.source,'body':'官方已撤回预报'}],self.now,'policy-v1'))
        self.assertFalse(approval_matches(approved,[self.source],self.now+timedelta(hours=4),'policy-v1'))
        self.assertFalse(approval_matches(approved,[self.source],self.now,'new-policy'))
        with self.assertRaises(ValueError):approve(self.draft,[self.source],self.now,'policy-v1')

    def test_collection_timestamp_only_does_not_change_evidence(self):
        approved=approve({**self.draft,'verify':[]},[self.source],self.now,'v1')
        self.assertTrue(approval_matches(approved,[{**self.source,'collected_at':'later'}],self.now,'v1'))
