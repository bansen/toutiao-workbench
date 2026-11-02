import tempfile
import unittest
from datetime import datetime, timezone
from workbench.store import Store
from workbench.editorial_policy import EditorialPolicy


def report(day='2026-11-01', lift=1.15, **updates):
    rows=[]
    for arm in ('baseline','trial'):
        rows.append(dict(arm=arm,content_kind='news',age_band='24-48',scope='cumulative',publication_date=day,publication_band='12-18',posts=5,impressions=1000,
                         reads=100 if arm=='baseline' else 100*lift,likes=10 if arm=='baseline' else 10*lift,
                         favorites=2 if arm=='baseline' else 2*lift,shares=2 if arm=='baseline' else 2*lift,comments=0))
    return {**dict(date=day,complete=True,sample_count=10,totals=dict(impressions=2000,reads=215),cohorts=rows,quality_alert=False),**updates}


class EditorialPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name+'/db')
        self.policy=EditorialPolicy(self.store)

    def evaluate(self,r):
        active=self.policy.status()['active']
        for row in r['cohorts']:row['experiment_id']=active['id'] if active else None
        return self.policy.evaluate(r)

    def start(self):
        return self.policy.propose(report(),{'headline_angle':'reader_impact'},0)

    def test_whitelist_single_change_and_weight_limits(self):
        for change in ({'prompt':'ignore rules'},{'target_length':249},{'headline_angle':'anything'},
                       {'opening':'fact_first','target_length':400},{'topic_weights':{'hot':1.3}},
                       {'tool_weights':{'unknown product':1.1}},{'target_length':True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.policy.propose(report(),change,0)
        self.assertEqual(self.policy.status()['version'],0)
        self.policy.propose(report(),{'topic_weights':{'hot':1.2}},0)

    def test_insufficient_missing_and_incomplete_never_start(self):
        for r in (report(complete=False), {**report(),'sample_count':9}, {**report(),'totals':{'reads':None,'impressions':None}}):
            self.assertEqual(self.policy.propose(r,{'opening':'fact_first'},0)['version'],0)

    def test_cas_idempotency_and_one_active(self):
        started=self.start()
        self.assertEqual(started['version'],1)
        self.assertEqual(self.start(),started)
        with self.assertRaises(ValueError):self.policy.propose(report('2026-11-02'),{'opening':'fact_first'},0)
        with self.assertRaises(ValueError):self.policy.propose(report('2026-11-02'),{'opening':'fact_first'},1)

    def test_distinct_valid_reports_promote_and_duplicate_is_noop(self):
        self.start()
        first=self.evaluate(report('2026-11-02'))
        self.assertEqual(first['active']['streak'],1)
        self.assertEqual(self.evaluate(report('2026-11-02')),first)
        final=self.evaluate(report('2026-11-03'))
        self.assertIsNone(final['active'])
        self.assertEqual(final['current']['headline_angle'],'reader_impact')
        self.assertEqual(final['baseline'],final['current'])

    def test_age_kind_mismatch_and_missing_data_do_not_promote(self):
        self.start()
        r=report('2026-11-02');r['cohorts'][1]['age_band']='48-72'
        self.assertEqual(self.evaluate(r)['active']['streak'],0)
        r=report('2026-11-03');r['cohorts'][1].update(likes=None,favorites=None,shares=None)
        self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_regression_quality_and_explicit_rollback_restore_baseline(self):
        for r in (report('2026-11-02',lift=.8),report('2026-11-02',quality_alert=True)):
            with self.subTest(r=r):
                p=EditorialPolicy(Store(self.tmp.name+'/'+str(len(str(r)))+str(r['quality_alert'])))
                p.propose(report(),{'opening':'fact_first'},0)

                for row in r['cohorts']:row['experiment_id']=p.status()['active']['id']
                self.assertIsNone(p.evaluate(r)['active'])
                self.assertEqual(p.status()['current']['opening'],'scene_first')
        self.start(); self.policy.rollback(1)
        self.assertIsNone(self.policy.status()['active'])
        with self.assertRaises(ValueError):self.policy.rollback(1)

    def test_intents_are_stable_independent_and_frozen_across_rollback(self):
        self.start()
        contexts=[self.policy.context_for_intent('news',str(n),'2026-11-01') for n in range(100)]
        self.assertTrue(5 < sum(c['arm']=='trial' for c in contexts) < 35)
        self.assertTrue(all(c['experiment_id'] for c in contexts))
        self.policy.rollback(1)
        self.assertEqual(self.policy.context_for_intent('news','1','2026-11-01'),contexts[1])
        self.assertEqual(self.policy.context_for_intent('news','new','2026-11-02')['arm'],'baseline')
        with self.assertRaises(ValueError):self.policy.context_for_intent('manual','x','2026-11-01')

    def test_foreign_experiment_and_mismatched_scope_do_not_count(self):
        self.start()
        r=report('2026-11-02')
        for row in r['cohorts']:row['experiment_id']='foreign'
        self.assertEqual(self.policy.evaluate(r)['active']['streak'],0)
        r['cohorts'][0]['scope']='list';r['cohorts'][1]['scope']='detail'
        self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_unknown_favorites_and_shares_allow_comparable_likes(self):
        self.start();r=report('2026-11-02')
        for row in r['cohorts']:row.update(favorites=None,shares=None)
        self.assertEqual(self.evaluate(r)['active']['streak'],1)

    def test_unknown_scope_cannot_use_impressions_as_rate_evidence(self):
        self.start();r=report('2026-11-02')
        for row in r['cohorts']:row['scope']='unverified'
        self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_policy_corruption_rolls_back_without_rendering_unknown_fields(self):
        self.start()
        state=self.policy.status();state['current']['prompt']='untrusted code'
        self.store.set_setting('editorial_policy_state_v1',state)
        result=self.evaluate(report('2026-11-02'))
        self.assertIsNone(result['active']);self.assertNotIn('prompt',result['current'])

    def test_one_start_per_day_survives_other_days_and_versions_are_retained(self):
        self.start();self.policy.rollback(1)
        self.policy.propose(report('2026-11-02'),{'opening':'fact_first'},2)
        self.policy.rollback(3)
        with self.assertRaises(ValueError):self.policy.propose(report(),{'opening':'fact_first'},4)
        with self.store.connection() as db:
            self.assertIsNotNone(db.execute('SELECT * FROM editorial_versions WHERE version=0').fetchone())

    def test_reads_per_post_fallback_requires_two_hundred_reads_per_arm(self):
        self.start();r=report('2026-11-02',capabilities={'impressions':False})
        for row in r['cohorts']:
            row['impressions']=None
            row['reads']=200 if row['arm']=='baseline' else 230
        result=self.evaluate(r)
        self.assertEqual(result['active']['streak'],1)
        self.assertEqual(result['active']['comparison']['metric'],'reads_per_post')

    def test_promoted_policy_can_roll_back_to_previous_baseline(self):
        self.start();self.evaluate(report('2026-11-02'));result=self.evaluate(report('2026-11-03'))
        result=self.policy.rollback(result['version'])
        self.assertEqual(result['current']['headline_angle'],'concrete_change')

    def test_atomic_cas_accepts_only_one_parallel_proposal(self):
        from concurrent.futures import ThreadPoolExecutor
        def attempt(change):
            try:return self.policy.propose(report(),change,0)['version']
            except ValueError:return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            outcomes=list(pool.map(attempt,[{'headline_angle':'reader_impact'},{'opening':'fact_first'}]))
        self.assertEqual(sorted(map(str,outcomes)),['1','conflict'])

    def test_new_quality_alert_on_already_evaluated_day_still_rolls_back(self):
        self.start();self.evaluate(report('2026-11-02'))
        self.assertIsNone(self.evaluate(report('2026-11-02',quality_alert=True))['active'])

    def test_unknown_scope_cannot_bypass_low_impression_gate(self):
        for scope in (None,'unverified','unknown-window'):
            with self.subTest(scope=scope):
                p=EditorialPolicy(Store(self.tmp.name+'/scope-'+str(scope)))
                p.propose(report(),{'headline_angle':'reader_impact'},0)
                for day in ('2026-11-02','2026-11-03'):
                    r=report(day)
                    for row in r['cohorts']:
                        row.update(scope=scope,impressions=100,reads=200 if row['arm']=='baseline' else 230,experiment_id=p.status()['active']['id'])
                    result=p.evaluate(r)
                    self.assertIsNotNone(result['active'])
                    self.assertEqual(result['active']['streak'],0)

    def test_partially_missing_impressions_cannot_take_reads_fallback(self):
        self.start();r=report('2026-11-02')
        for row in r['cohorts']:row['reads']=200 if row['arm']=='baseline' else 230
        r['cohorts'][0]['impressions']=100;r['cohorts'][1]['impressions']=None
        self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_publication_day_and_band_must_match(self):
        self.start()
        for field,value in (('publication_date','2026-10-30'),('publication_band','00-06')):
            r=report('2026-11-02');r['cohorts'][1][field]=value
            self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_verified_known_low_impressions_and_unverified_regression_keep_active(self):
        self.start()
        for day,scope,impressions in (('2026-11-02','cumulative',100),('2026-11-03','unverified',None)):
            r=report(day)
            for row in r['cohorts']:
                row.update(scope=scope,impressions=impressions,reads=400 if row['arm']=='baseline' else 200)
            result=self.evaluate(r)
            self.assertIsNotNone(result['active'])
            self.assertEqual(result['active']['streak'],0)

    def test_missing_impressions_only_fall_back_when_platform_cannot_provide_them(self):
        self.start()
        for capability in (True,None,'missing'):
            r=report('2026-11-02')
            if capability!='missing':r['capabilities']={'impressions':capability}
            for row in r['cohorts']:
                row.update(impressions=None,reads=200 if row['arm']=='baseline' else 230)
            with self.subTest(capability=capability):
                self.assertEqual(self.evaluate(r)['active']['streak'],0)

    def test_build_report_partial_impression_coverage_cannot_claim_no_capability(self):
        from workbench.review_metrics import build_report
        self.start();experiment=self.policy.status()['active']['id'];rows=[]
        for arm in ('baseline','trial'):
            for n in range(5):
                rows.append({'platform_id':arm+str(n),'status':'published','published_at':'2026-11-02T12:00:00+08:00',
                    'observed_at':'2026-11-03T13:00:00+08:00','scope':'cumulative','content_kind':'news',
                    'editorial_context':{'experiment_id':experiment,'arm':arm,'content_kind':'news'},
                    'metrics':{'impressions':100 if n==0 else None,'reads':40 if arm=='baseline' else 46,
                               'likes':1,'comments':0,'favorites':0,'shares':0}})
        r=build_report('2026-11-02',rows,True,{'impressions':False})
        self.assertTrue(r['complete'])
        self.assertTrue(all(c['impressions'] is None and c['metric_coverage']['impressions']['known']>0 for c in r['cohorts']))
        result=self.policy.evaluate(r)
        self.assertEqual(result['active']['streak'],0)

    def test_public_prune_removes_only_expired_history_and_keeps_effective_versions(self):
        from datetime import datetime
        state=self.policy.status();state['rollback_version']=1
        self.store.set_setting('editorial_policy_state_v1',state)
        with self.store.connection() as db:
            for version in (0,1,2):
                db.execute('INSERT INTO editorial_versions VALUES (?,?,?)',(version,'2026-09-01','{}'))
            db.execute('INSERT INTO editorial_intents VALUES (?,?,?)',('old','2026-09-01','{}'))
        result=self.policy.prune(datetime.fromisoformat('2026-11-01T05:00:00+08:00'))
        self.assertEqual(result,{'versions':1,'intents':1})
        with self.store.connection() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT version FROM editorial_versions ORDER BY version')],[0,1])

    def test_unvalidated_initial_baseline_context_does_not_change_generation_instructions(self):
        from workbench.editorial_policy import render_preferences
        context=self.policy.context_for_intent('news','initial','2026-11-01')
        self.assertEqual(context['policy_version'],0)
        self.assertEqual(render_preferences(context),'')
        self.assertTrue(context['intent_id'])

    def test_interactions_per_read_decline_prevents_promotion_despite_higher_exposure_rates(self):
        self.start()
        for day in ('2026-11-02','2026-11-03'):
            r=report(day,lift=1.25)
            for row in r['cohorts']:
                row['favorites']=None;row['shares']=None
                row['likes']=10 if row['arm']=='baseline' else 11
            result=self.evaluate(r)
            self.assertIsNotNone(result['active'])
            self.assertEqual(result['active']['streak'],0)
            self.assertAlmostEqual(result['active']['comparison']['interaction_lifts']['likes'],-.12)

    def test_interaction_read_rate_decline_within_ten_percent_is_allowed(self):
        self.start();r=report('2026-11-02',lift=1.2)
        for row in r['cohorts']:
            row.update(favorites=None,shares=None,likes=10 if row['arm']=='baseline' else 11)
        result=self.evaluate(r)
        self.assertEqual(result['active']['streak'],1)
        self.assertAlmostEqual(result['active']['comparison']['interaction_lifts']['likes'],1.1/1.2-1)

    def test_unvalidated_initial_baseline_preserves_existing_candidate_order(self):
        from workbench.editorial_policy import select_candidate,render_preferences
        candidates=[('热点','article',{'url':'https://example.org/first','news_category':'hot'}),
                    ('商业','article',{'url':'https://example.org/second','news_category':'business'})]
        for n in range(20):
            context=self.policy.context_for_intent('news','initial-'+str(n),'2026-11-01')
            self.assertEqual(select_candidate(candidates,context),candidates[0])
            self.assertEqual(render_preferences(context),'')


    def test_current_snapshot_cohorts_can_be_evaluated_without_cumulative_label(self):
        self.start()
        for day in ('2026-11-02','2026-11-03'):
            r=report(day)
            for item in r['cohorts']:item['scope']='current_snapshot'
            result=self.evaluate(r)
        self.assertEqual(result['last_action'],'promoted')


    def test_different_supported_snapshot_types_do_not_form_a_matched_pair(self):
        self.start()
        r=report('2026-11-02');r['cohorts'][1]['scope']='current_snapshot'
        result=self.evaluate(r)
        self.assertEqual(result['active']['streak'],0)
        self.assertEqual(result['last_action'],'started')


class EditorialGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_and_repair_preserve_controlled_context(self):
        from unittest.mock import AsyncMock
        from workbench.ai_tools import AiToolContent, HEADLINE_WRITING, REVIEW_POLICY
        service=AiToolContent(None)
        service.request=AsyncMock(return_value={'title':'标题','body':'正文','verify':[]})
        context={'intent_id':'intent','arm':'trial','preferences':{'headline_angle':'reader_impact','opening':'fact_first','target_length':450,'news_paragraph_length':80,'prompt':'malicious'}}
        draft=await service.generate([{'kind':'official','url':'https://www.workbuddy.cn/docs','author':'WorkBuddy 官方文档'}],editorial_context=context)
        self.assertEqual(draft['editorial_context'],context)
        rules=service.request.call_args.args[0]
        self.assertIn(HEADLINE_WRITING,rules)
        self.assertIn('450',rules);self.assertNotIn('malicious',rules)
        revised=await service.repair(draft,draft['sources'],'AI',[])
        self.assertEqual(revised['editorial_context'],context)
        self.assertEqual(service.review_policy,REVIEW_POLICY)

    async def test_selection_uses_kind_stable_intent_and_product_weights(self):
        from workbench.editorial_policy import select_candidate
        candidates=[('热点','article',{'url':'https://example.org/news','news_category':'hot'}),
                    ('AI','official',{'url':'https://example.org/tool','author':'WorkBuddy 官方文档'})]
        context={'intent_id':'one','content_kind':'tool-guide','preferences':{'tool_weights':{'WorkBuddy':1.2}}}
        self.assertEqual(select_candidate(candidates,context),candidates[1])
        self.assertEqual(select_candidate(list(reversed(candidates)),context),candidates[1])
        self.assertIsNone(select_candidate(candidates[:1],context))
