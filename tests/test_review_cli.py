import io
import json
import unittest
from unittest.mock import Mock,patch
from urllib.error import URLError
from workbench import review_cli


class ReviewCliTests(unittest.TestCase):
    def test_request_stays_on_fixed_localhost_with_required_headers(self):
        response=Mock();response.read.return_value=b'{"state":"noop"}'
        opener=Mock();opener.open.return_value.__enter__=Mock(return_value=response)
        opener.open.return_value.__exit__=Mock(return_value=False)
        with patch.object(review_cli,'build_opener',return_value=opener):
            self.assertEqual(review_cli.request('POST','/api/reviews/check',{}),{'state':'noop'})
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'http://127.0.0.1:8765/api/reviews/check')
        self.assertEqual(req.get_header('Origin'),'http://127.0.0.1:8765')
        self.assertEqual(req.get_header('X-workbench-request'),'1')
        with self.assertRaises(ValueError):review_cli.request('GET','https://outside.invalid')

    def test_down_server_is_reported_without_starting_anything(self):
        out=io.StringIO()
        with patch.object(review_cli,'request',side_effect=URLError('private detail')),patch('sys.stdout',out):
            self.assertEqual(review_cli.main(['check']),2)
        result=json.loads(out.getvalue())
        self.assertEqual(result['state'],'unavailable')
        self.assertNotIn('private',out.getvalue())

    def test_run_wait_prints_only_anonymous_aggregate(self):
        out=io.StringIO()
        with patch.object(review_cli,'request',side_effect=[{'id':'opaque','state':'collecting'}, {'id':'opaque','state':'ready'}, {'totals':{'reads':3}}]) as request,patch('sys.stdout',out):
            self.assertEqual(review_cli.main(['run','--date','2026-10-31','--wait']),0)
        self.assertEqual(request.call_args_list[0].args,('POST','/api/reviews/run',{'target_date':'2026-10-31','validation':False}))
        self.assertEqual(request.call_args_list[-1].args,('GET','/api/reviews/opaque/anonymous'))
        self.assertEqual(json.loads(out.getvalue())['report'],{'totals':{'reads':3}})

    def test_check_noop_does_not_run_or_archive(self):
        out=io.StringIO()
        with patch.object(review_cli,'request',return_value={'due':False,'reason':'already_completed'}) as request,patch('sys.stdout',out):
            self.assertEqual(review_cli.main(['check','--run']),0)
        request.assert_called_once()
        self.assertEqual(json.loads(out.getvalue())['state'],'noop')

    def test_finish_accepts_inert_notes_and_rejects_client_report(self):
        with patch('sys.stdin',io.StringIO('{"report":{"reads":999}}')),patch.object(review_cli,'request') as request,patch('sys.stdout',io.StringIO()):
            self.assertEqual(review_cli.main(['finish','opaque','--input','-']),2)
            request.assert_not_called()
