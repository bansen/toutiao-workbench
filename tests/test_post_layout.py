import unittest

from workbench.post_layout import format_post, layout_issues


class PostLayoutTests(unittest.TestCase):
    def test_title_items_and_tags_have_consistent_spacing(self):
        original='  WorkBuddy技能入口 \r\n1. 技能市场：选择技能。 \r\n2. 上传技能：导入本地技能包。\r\n\r\n\r\n#WorkBuddy  #Skills\r\n#AI办公  '
        expected='WorkBuddy技能入口\n\n1. 技能市场：选择技能。\n\n2. 上传技能：导入本地技能包。\n\n#WorkBuddy #Skills #AI办公'
        self.assertEqual(format_post(original),expected)
        self.assertEqual(format_post(expected),expected)
        self.assertEqual(layout_issues(expected),[])

    def test_formatting_preserves_numbers_quotes_commands_and_feature_names(self):
        original='TRAE 工作树\n1. Worktree：输入“创建 worktree”，支持 Python 3.13 和 SKILL.md。\n参考指令：\n请为 feature/login 创建工作树，保留 main。\n#TRAE #AI编程 #工作流'
        formatted=format_post(original)
        self.assertEqual(''.join(original.split()),''.join(formatted.split()))
        self.assertIn('Python 3.13',formatted)

    def test_news_uses_short_paragraphs_without_inventing_numbered_items(self):
        original='机器人新进展\n据科技日报报道，甲公司展示了新设备。\n实际效果仍需后续验证。\n#机器人 #科技新闻 #智能设备'
        result=format_post(original)
        self.assertIn('报道，甲公司展示了新设备。\n\n实际效果',result)
        self.assertNotIn('1.',result)

    def test_unsupported_markdown_long_blocks_and_missing_spacing_are_reported(self):
        for body in ['标题\n正文\n#AI #科技 #工具',
                     '标题\n\n**加粗内容**\n\n#AI #科技 #工具',
                     '标题\n\n|功能|说明|\n|---|---|\n\n#AI #科技 #工具',
                     '标题\n\n'+('很长的正文'*40)+'\n\n#AI #科技 #工具']:
            self.assertTrue(layout_issues(body))

    def test_short_legitimate_code_names_are_not_stripped(self):
        body='Codex 文件规则\n\n1. SKILL.md：保留文件名里的符号，版本号是3.13。\n\n#Codex #Skills #AI编程'
        self.assertEqual(format_post(body),body)
