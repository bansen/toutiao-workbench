import unittest
from workbench.toutiao_publisher import compact,platform_status,rendered_text

class PublisherParsingTests(unittest.TestCase):
    def test_review_state_is_separate_from_submission(self):
        self.assertEqual(platform_status('已发布'),'published')
        self.assertEqual(platform_status('审核中'),'submitted')
        self.assertEqual(platform_status('未通过'),'rejected')
        self.assertEqual(platform_status(''),'uncertain')
    def test_compact_only_used_for_matching_not_snapshot_digest(self):
        self.assertEqual(compact('正文\n\n第二段'),'正文第二段')
        self.assertNotEqual(compact('正文 新事实'),compact('正文'))

    def test_rendered_newline_equivalence_preserves_word_boundaries(self):
        self.assertEqual(rendered_text('第一段\n\n第二段'),rendered_text('第一段\n\n\n第二段'))
        self.assertNotEqual(rendered_text('now here'),rendered_text('nowhere'))
