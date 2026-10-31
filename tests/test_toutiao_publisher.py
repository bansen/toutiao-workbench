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

class PublisherEditorTests(unittest.IsolatedAsyncioTestCase):
    def publisher(self, text):
        from unittest.mock import AsyncMock, Mock
        from workbench.toutiao_publisher import ToutiaoPublisher
        self.editor=Mock()
        self.editor.inner_text=AsyncMock(side_effect=lambda:text if not self.editor.fill.await_count else '正文\n\n第二段')
        self.editor.fill=AsyncMock()
        self.editor.count=AsyncMock(return_value=1)
        self.page=Mock()
        self.page.is_closed.return_value=False
        self.page.locator.return_value=self.editor
        self.page.close=AsyncMock()
        self.page.evaluate=AsyncMock(return_value=True)
        publisher=ToutiaoPublisher(Mock(),lambda:'account')
        async def opened(*args):publisher.page=self.page
        publisher._open=opened
        publisher.check_account=AsyncMock(return_value='account')
        publisher.declarations=AsyncMock(return_value={})
        return publisher

    async def test_same_restored_candidate_is_reusable(self):
        publisher=self.publisher('正文\n\n\n第二段')
        await publisher.preflight({'body':'正文\n\n第二段'})
        self.editor.fill.assert_awaited_once_with('正文\n\n第二段')

    async def test_other_restored_text_is_never_overwritten(self):
        from workbench.browser import NeedsAttention
        publisher=self.publisher('用户自己的未完成正文')
        with self.assertRaises(NeedsAttention):await publisher.preflight({'body':'正文\n\n第二段'})
        self.editor.fill.assert_not_awaited()

    async def test_close_cleans_only_unchanged_owned_editor(self):
        publisher=self.publisher('')
        await publisher.preflight({'body':'正文\n\n第二段'})
        await publisher.close()
        self.assertEqual(self.editor.fill.await_args.args, ('',))

    async def test_close_preserves_user_edit_after_preflight(self):
        from unittest.mock import AsyncMock
        publisher=self.publisher('')
        await publisher.preflight({'body':'正文\n\n第二段'})
        self.editor.inner_text=AsyncMock(return_value='用户后来修改的正文')
        await publisher.close()
        self.editor.fill.assert_awaited_once_with('正文\n\n第二段')

class PublisherDeclarationTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_receipt_is_only_looked_up_again_not_submitted_again(self):
        from unittest.mock import AsyncMock,Mock,patch
        from workbench.toutiao_publisher import ToutiaoPublisher,ReceiptPending
        publisher=ToutiaoPublisher(Mock(),lambda:'account')
        publisher.page=Mock();publisher.page.goto=AsyncMock()
        publisher.page.locator.return_value.first.wait_for=AsyncMock()
        publisher.check_account=AsyncMock(return_value='account')
        publisher._lookup=AsyncMock(side_effect=[ReceiptPending('尚未出现'),{'status':'submitted'}])
        with patch('workbench.toutiao_publisher.asyncio.sleep',new=AsyncMock()):
            self.assertEqual((await publisher._lookup_with_grace({'body':'正文'}))['status'],'submitted')
        self.assertEqual(publisher._lookup.await_count,2)
        self.assertEqual(publisher.page.goto.await_count,1)

    async def test_declarations_wait_for_delayed_checkbox_render_without_submitting(self):
        from unittest.mock import AsyncMock, Mock, patch
        from workbench.toutiao_publisher import ToutiaoPublisher
        publisher=ToutiaoPublisher(Mock(),lambda:'account',expected_ai=lambda:False)
        labels={}
        for name in ('头条首发','引用AI'):
            label=Mock();control=Mock()
            label.count=AsyncMock(side_effect=[0,0,1,1,1])
            label.locator.return_value=control
            control.count=AsyncMock(return_value=1)
            control.is_checked=AsyncMock(return_value=False)
            label.get_attribute=AsyncMock(return_value='byte-checkbox')
            label.get_by_text.return_value.click=AsyncMock()
            labels[name]=label
        publisher.label=lambda name:labels[name]
        with patch('workbench.toutiao_publisher.asyncio.sleep',new=AsyncMock()):
            result=await publisher.declarations(apply=True)
        self.assertFalse(result['exclusive']['checked'])
        self.assertFalse(result['ai']['checked'])
        labels['头条首发'].get_by_text.return_value.click.assert_not_awaited()

    async def test_persistently_ambiguous_declaration_is_not_clicked(self):
        from unittest.mock import AsyncMock, Mock, patch
        from workbench.toutiao_publisher import ToutiaoPublisher
        from workbench.browser import NeedsAttention
        publisher=ToutiaoPublisher(Mock(),lambda:'account',expected_ai=lambda:False)
        label=Mock();label.count=AsyncMock(return_value=2)
        label.get_by_text.return_value.click=AsyncMock()
        publisher.label=lambda name:label
        with patch('workbench.toutiao_publisher.asyncio.sleep',new=AsyncMock()):
            with self.assertRaises(NeedsAttention):await publisher.declarations(apply=True)
        label.get_by_text.return_value.click.assert_not_awaited()

    async def test_waits_for_component_style_after_native_value_changes(self):
        from unittest.mock import AsyncMock, Mock
        from workbench.toutiao_publisher import ToutiaoPublisher
        publisher=ToutiaoPublisher(Mock(),lambda:'account',expected_ai=lambda:False)
        labels={}
        for name in ('头条首发','引用AI'):
            label=Mock();control=Mock()
            label.count=AsyncMock(return_value=1);control.count=AsyncMock(return_value=1)
            label.locator.return_value=control
            control.is_checked=AsyncMock(side_effect=[True]+[False]*12) if name=='头条首发' else AsyncMock(return_value=False)
            label.get_attribute=AsyncMock(side_effect=['byte-checkbox byte-checkbox-checked']+['byte-checkbox']*12) if name=='头条首发' else AsyncMock(return_value='byte-checkbox')
            label.get_by_text.return_value.click=AsyncMock()
            labels[name]=label
        publisher.label=lambda name:labels[name]
        result=await publisher.declarations(apply=True)
        self.assertFalse(result['exclusive']['checked'])
        labels['头条首发'].get_by_text.return_value.click.assert_awaited_once()

    async def test_late_platform_auto_selection_is_cancelled_once(self):
        from unittest.mock import AsyncMock, Mock
        from workbench.toutiao_publisher import ToutiaoPublisher
        publisher=ToutiaoPublisher(Mock(),lambda:'account',expected_ai=lambda:False)
        labels={};state={'reads':0,'clicked':False,'checked':False}
        def checked():
            state['reads']+=1
            state['checked']=state['reads']>=2 and not state['clicked']
            return state['checked']
        def click():state['clicked']=True
        for name in ('头条首发','引用AI'):
            label=Mock();control=Mock()
            label.count=AsyncMock(return_value=1);control.count=AsyncMock(return_value=1)
            label.locator.return_value=control
            control.is_checked=AsyncMock(side_effect=checked) if name=='头条首发' else AsyncMock(return_value=False)
            label.get_attribute=AsyncMock(side_effect=lambda *args:'byte-checkbox byte-checkbox-checked' if state['checked'] else 'byte-checkbox') if name=='头条首发' else AsyncMock(return_value='byte-checkbox')
            label.get_by_text.return_value.click=AsyncMock(side_effect=click)
            labels[name]=label
        publisher.label=lambda name:labels[name]
        result=await publisher.declarations(apply=True)
        self.assertFalse(result['exclusive']['checked'])
        labels['头条首发'].get_by_text.return_value.click.assert_awaited_once()

class PublisherIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_duplicate_links_for_same_bound_account_are_one_identity(self):
        from workbench.toutiao_publisher import identity_from_links
        result=identity_from_links([
            {'href':'//www.toutiao.com/c/user/123/?enter_from=mp','text':'账号','visible':True},
            {'href':'https://www.toutiao.com/c/user/123/','text':'','visible':True}])
        self.assertEqual(result['id'],'123')

    def test_conflicting_or_foreign_links_never_identify_account(self):
        from workbench.toutiao_publisher import identity_from_links
        from workbench.browser import NeedsAttention
        for links in ([{'href':'https://evil.test/c/user/123/','visible':True}],
                      [{'href':'//www.toutiao.com/c/user/123/','visible':True}, {'href':'//www.toutiao.com/c/user/456/','visible':True}]):
            with self.assertRaises(NeedsAttention):identity_from_links(links)

    async def test_account_loading_waits_before_reading_identity(self):
        from unittest.mock import Mock,AsyncMock
        from workbench.toutiao_publisher import ToutiaoPublisher
        browser=Mock();browser.check_blocking_ui=AsyncMock()
        publisher=ToutiaoPublisher(browser,lambda:'123')
        page=Mock();page.wait_for_function=AsyncMock()
        links=Mock();links.count=AsyncMock(return_value=0)
        links.evaluate_all=AsyncMock(return_value=[{'href':'//www.toutiao.com/c/user/123/','text':'','visible':True}])
        body=Mock();body.inner_text=AsyncMock(return_value='创作者后台')
        page.locator.side_effect=lambda selector:links if selector.startswith('a[') else body
        publisher.page=page
        self.assertEqual(await publisher.check_account(),'123')
        page.wait_for_function.assert_awaited_once()
