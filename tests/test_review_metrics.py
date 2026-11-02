import json
import unittest
from workbench.review_metrics import build_report, local_markdown


def row(**changes):
    value={'platform_id':'secret-post','title':'秘密标题7？但更快','body':'秘密正文','published_at':'2026-10-31T18:00:00+08:00','observed_at':'2026-11-01T05:13:00+08:00','status':'published','metrics':{'impressions':100,'reads':20,'likes':0,'comments':2},'scope':'cumulative','source_url':'https://private.invalid/post','content_kind':'news','subtopic':'unknown','editorial_context':None}
    value.update(changes)
    return value


class ReviewMetricsTests(unittest.TestCase):
    def test_unknown_is_not_zero_and_partial_totals_have_coverage(self):
        report=build_report('2026-10-31',[row(),row(platform_id='second',metrics={'reads':None,'likes':3})],True)
        self.assertEqual(report['sample_count'],1)
        self.assertIsNone(report['totals']['reads'])
        self.assertEqual(report['metric_coverage']['reads'],{'known':1,'total':2,'known_sum':20})
        self.assertIsNone(report['totals']['shares'])
        self.assertEqual(report['totals']['likes'],3)
        self.assertEqual(report['cohorts'],[])

    def test_cohorts_require_explicit_context_and_separate_publication_period(self):
        context={'experiment_id':'exp-1','arm':'trial','content_kind':'news'}
        report=build_report('2026-10-31',[row(editorial_context=context),row(platform_id='other',published_at='2026-10-31T10:00:00+08:00',observed_at='2026-10-31T21:13:00+08:00',editorial_context=context),row(platform_id='manual')],True)
        self.assertEqual(len(report['cohorts']),2)
        self.assertEqual({r['publication_band'] for r in report['cohorts']},{'06-12','18-24'})
        self.assertEqual({r['age_band'] for r in report['cohorts']},{'6-12'})
        self.assertEqual(sum(r['posts'] for r in report['cohorts']),2)
        for secret in ('secret-post','秘密标题','秘密正文','private.invalid'):
            self.assertNotIn(secret,json.dumps(report,ensure_ascii=False))

    def test_unverified_missing_and_wrong_dates_are_never_comparable(self):
        r=build_report('2026-10-31',[row(scope='unverified'),row(platform_id='wrong',published_at='2026-10-30T18:00:00+08:00'),row(platform_id='bad',published_at=None)],False)
        self.assertFalse(r['complete'])
        self.assertFalse(r['quality_alert'])
        self.assertIsNone(r['ratios']['read_rate'])
        self.assertEqual(r['post_count'],1)

    def test_zero_denominator_and_missing_interactions_have_no_ratio(self):
        r=build_report('2026-10-31',[row(metrics={'impressions':0,'reads':0,'likes':0})],True)
        self.assertIsNone(r['ratios']['read_rate'])
        self.assertIsNone(r['ratios']['like_rate'])
        self.assertEqual(r['totals']['reads'],0)

    def test_invalid_numbers_remain_unknown_and_duplicates_are_not_double_counted(self):
        r=build_report('2026-10-31',[row(metrics={'reads':True,'impressions':float('nan'),'likes':-1}),row()],True)
        self.assertEqual(r['post_count'],1)
        self.assertFalse(r['quality_alert'])
        self.assertIsNone(r['totals']['reads'])

    def test_collection_warnings_never_become_content_quality_alerts(self):
        cases=[
            ([row()],False,['raw private error'],'collection_incomplete'),
            ([row(scope='unverified')],True,[],'unverified_scope'),
            ([row(published_at='broken')],True,[],'invalid_timestamp'),
            ([row(status='uncertain')],True,[],'uncertain_status'),
            ([row(metrics={'reads':float('nan')})],True,[],'invalid_metric'),
        ]
        for rows,complete,warnings,code in cases:
            with self.subTest(code=code):
                report=build_report('2026-10-31',rows,complete,warnings=warnings)
                self.assertFalse(report['quality_alert'])
                self.assertFalse(report['complete'])
                self.assertIn(code,report['warnings'])
                self.assertGreater(report['warning_count'],0)
                self.assertNotIn('raw private error',json.dumps(report))

    def test_clean_collection_has_no_quality_alert_or_warning(self):
        report=build_report('2026-10-31',[row()],True)
        self.assertTrue(report['complete'])
        self.assertFalse(report['quality_alert'])
        self.assertEqual(report['warnings'],[])

    def test_mature_summaries_and_cohorts_keep_publication_days_separate(self):
        context={'experiment_id':'exp-1','arm':'baseline','content_kind':'news'}
        older=row(platform_id='older-secret',published_at='2026-10-30T18:00:00+08:00',editorial_context=context,metrics={'reads':60,'likes':2})
        oldest=row(platform_id='oldest-secret',published_at='2026-10-29T18:00:00+08:00',editorial_context=context,metrics={'reads':90,'likes':3})
        report=build_report('2026-10-31',[row(),older,oldest],True)
        self.assertEqual(report['totals']['reads'],20)
        self.assertEqual(report['post_count'],1)
        self.assertEqual(sum(t['posts'] for t in report['traits']),1)
        self.assertEqual([r['date'] for r in report['mature']],['2026-10-29','2026-10-30'])
        self.assertEqual([r['totals']['reads'] for r in report['mature']],[90,60])
        self.assertTrue(all('mature' not in r for r in report['mature']))
        self.assertEqual({(c['publication_date'],c['age_band']) for c in report['cohorts']},{('2026-10-29','48-72'),('2026-10-30','24-48')})
        for secret in ('older-secret','oldest-secret','秘密标题','秘密正文','private.invalid'):
            self.assertNotIn(secret,json.dumps(report,ensure_ascii=False))

    def test_mature_excludes_expired_unverified_future_and_bad_rows(self):
        context={'experiment_id':'exp-1','arm':'trial','content_kind':'news'}
        rows=[row(platform_id='expired',published_at='2026-10-29T05:13:00+08:00',editorial_context=context),
              row(platform_id='unverified',published_at='2026-10-30T18:00:00+08:00',scope='unverified',editorial_context=context),
              row(platform_id='future',published_at='2026-11-01T01:00:00+08:00',editorial_context=context),
              row(platform_id='bad',published_at='2026-10-30T18:00:00+08:00',observed_at='broken',editorial_context=context),
              row(platform_id='bad-number',published_at='2026-10-30T18:00:00+08:00',metrics={'reads':-1},editorial_context=context)]
        report=build_report('2026-10-31',[row(),*rows],False)
        self.assertEqual(report['mature'],[])
        self.assertEqual(report['cohorts'],[])
        self.assertFalse(report['complete'])
        self.assertFalse(report['quality_alert'])

    def test_local_details_keep_status_timestamps_source_and_missing_metrics(self):
        text=local_markdown('2026-10-31',[row(metrics={'reads':20,'favorites':4,'shares':5}),row(platform_id='rejected-id',status='rejected')])
        for value in ('收藏','分享','状态','发布时间','口径','采集时间','平台ID','来源','secret-post','private.invalid','rejected-id','rejected','cumulative','2026-11-01T05:13:00+08:00','未知'):
            self.assertIn(value,text)

class CurrentSnapshotTests(unittest.TestCase):
    def test_current_snapshots_are_valid_and_refresh_mature_comparisons(self):
        r=build_report('2026-10-31',[row(scope='current_snapshot'),
            row(platform_id='older',published_at='2026-10-30T18:00:00+08:00',scope='current_snapshot')],True)
        self.assertTrue(r['complete'])
        self.assertEqual(r['ratios']['read_rate'],.2)
        self.assertEqual(len(r['mature']),1)
        self.assertEqual(r['data_basis'],'per_post_display_at_collection')
        self.assertTrue(r['as_of']['to'].startswith('2026-11-01T05:13'))
        self.assertNotIn('unverified_scope',r['warnings'])
    def test_different_snapshot_definitions_do_not_share_a_ratio(self):
        r=build_report('2026-10-31',[row(),row(platform_id='snapshot',scope='current_snapshot')],True)
        self.assertIsNone(r['ratios']['read_rate'])
