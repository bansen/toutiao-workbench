import unittest
from workbench.browser import parse_card


class SearchCardTests(unittest.TestCase):
    def test_micro_author_link_before_content_is_not_mistaken_for_article(self):
        row=parse_card({'links':[
            {'text':'新京报\n2小时前','href':'https://www.toutiao.com/c/user/5540918998/'},
            {'text':'AI 进展','href':'https://www.toutiao.com/a1879943948871687/'}],
            'text':'新京报\n2小时前\nAI 进展\n3点赞\n1评论\n0转发','source':''},'micro','https://so.toutiao.com/search/')
        self.assertEqual(row['url'],'https://www.toutiao.com/w/1879943948871687/')
        self.assertEqual(row['title'],'AI 进展')
        self.assertEqual(row['comments'],1)
        self.assertEqual(row['likes'],3)
        self.assertEqual(row['author'],'新京报')

    def test_article_without_visible_metrics_keeps_unknown(self):
        row=parse_card({'links':[{'text':'文章标题','href':'https://www.toutiao.com/article/7699775205312315954/'}],
            'text':'文章标题\n有人说有 100评论，这不是指标\n作者\n昨天12:00','source':'作者\n昨天12:00'},'article','search')
        self.assertIsNone(row['comments'])
        self.assertIsNone(row['likes'])


if __name__=='__main__':unittest.main()
