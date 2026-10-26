"""Optional browser integration test, isolated DB and mock model (no API spending).

Run: python3 tests/browser_smoke.py
Requires an installed Google Chrome and an unused local port 8766.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
import uvicorn
from playwright.async_api import async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from workbench.app import create_app
from workbench.browser import Browser,NeedsAttention
from workbench.llm import LLM
from workbench.store import Store


async def model_response(request):
    payload=json.loads(request.content)
    user=json.loads(payload['messages'][1]['content'])
    result={'title':f'测试草稿 · {user["task"]} · V{user["version"]}',
            'body':'这是浏览器集成测试生成的内容，不是真实模型输出。\n\n保留来源以便核实。',
            'verify':['核对原始数据。']}
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]})


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        llm=LLM('https://test-model.invalid/v1','not-a-real-key','integration-test',transport=httpx.MockTransport(model_response))
        app=create_app(Path(tmp),llm=llm)
        for number in range(2):
            app.state.engine.store.upsert_material({'url':f'https://www.toutiao.com/article/{7699775205312315954+number}/',
                'content_id':str(7699775205312315954+number),'title':f'测试来源 {number+1}','body':'测试资料，不对应线上文章。',
                'kind':'article','author':'测试作者','published_at':'2026-10-25T00:00:00+08:00'})
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8766,log_level='error'))
        serving=asyncio.create_task(server.serve())
        try:
            while not server.started:
                if serving.done(): await serving
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser=await p.chromium.launch(channel='chrome',headless=True)
                try:
                    context=await browser.new_context(viewport={'width':1440,'height':1000},permissions=['clipboard-read','clipboard-write'])
                    # The live search first renders an empty card shell. Do not
                    # treat that intermediate DOM as an empty search result.
                    async def delayed_search(route):
                        await route.fulfill(content_type='text/html; charset=utf-8',body='''<div class="result-content"></div><script>
                          setTimeout(() => {document.querySelector('.result-content').innerHTML=
                          '<a href="https://www.toutiao.com/article/7699775205312315954/">延迟出现的标题</a>';},250);
                        </script>''')
                    await context.route('https://so.toutiao.com/**',delayed_search)
                    collector=Browser(Path(tmp)/'isolated-test-profile')
                    collector.context=context
                    results=await collector.search('测试',limit=1)
                    assert len(results)==1, 'Search must wait for the real link, not the empty card shell'
                    await context.unroute('https://so.toutiao.com/**',delayed_search)
                    async def blocked_article(route):
                        await route.fulfill(content_type='text/html; charset=utf-8',body='<article>仍然保留的正文</article><div role="dialog">请完成安全验证</div>')
                    await context.route('https://www.toutiao.com/article/**',blocked_article)
                    blocked_page=await context.new_page()
                    try:
                        await collector.navigate(blocked_page,'https://www.toutiao.com/article/7699775205312315954/','article')
                    except NeedsAttention:
                        pass
                    else:
                        raise AssertionError('A verification overlay must pause even when article remains in the DOM')
                    await blocked_page.close()
                    await context.unroute('https://www.toutiao.com/article/**',blocked_article)
                    async def article_discussing_verification(route):
                        await route.fulfill(content_type='text/html; charset=utf-8',body='<article>如何识别“请完成安全验证”提示</article>')
                    await context.route('https://www.toutiao.com/article/**',article_discussing_verification)
                    normal_page=await context.new_page()
                    await collector.navigate(normal_page,'https://www.toutiao.com/article/7699775205312315954/','article')
                    await normal_page.close()
                    await context.unroute('https://www.toutiao.com/article/**',article_discussing_verification)
                    page=await context.new_page()
                    errors=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    await page.goto('http://127.0.0.1:8766/')
                    await page.locator('[data-select]').first.wait_for()
                    await page.locator('#domain-choices').get_by_role('button',name='科技',exact=True).click()
                    await page.locator('#domain-choices').get_by_role('button',name='生活',exact=True).click()
                    await page.locator('#domain-choices').get_by_role('button',name='科技',exact=True).click()
                    assert await page.locator('#domain-choices button[aria-pressed="true"]').count()==1
                    await page.locator('#domain-choices').get_by_role('button',name='科技',exact=True).click()
                    await page.locator('input[name="kind"][value="micro"]').check()
                    await page.locator('#style-choices').get_by_role('button',name='正式',exact=True).click()
                    assert await page.locator('#style-choices button[aria-pressed="true"]').count()==2
                    assert await page.locator('#reads-choices button:enabled').count()==0
                    await page.locator('#save-settings').click()
                    await page.locator('#toast').filter(has_text='已保存').wait_for()
                    await page.reload()
                    await page.locator('#domain-choices button[aria-pressed="true"]').first.wait_for()
                    assert await page.locator('#domain-choices button[aria-pressed="true"]').count()==2
                    assert await page.locator('input[name="kind"]:checked').count()==2
                    assert await page.locator('#style-choices button[aria-pressed="true"]').count()==2
                    await page.locator('#select-all').check()
                    await page.locator('#versions-choices').get_by_role('button',name='2 版',exact=True).click()
                    await page.locator('#generate').click()
                    await page.wait_for_function("() => document.querySelector('#draft-count').innerText==='8'",timeout=20000)
                    await page.locator('#mode-choices').get_by_role('button',name='多来源成稿 · 选择 2～5 篇',exact=True).click()
                    await page.locator('#versions-choices').get_by_role('button',name='1 版',exact=True).click()
                    await page.locator('#generate').click()
                    await page.wait_for_function("() => document.querySelector('#draft-count').innerText==='10'",timeout=20000)
                    await page.locator('[data-tab="drafts"]').click()
                    await page.locator('[data-draft]').first.click()
                    await page.locator('#draft-title').fill('浏览器测试：修改后的标题')
                    await page.locator('#draft-body').fill('保存后的测试正文。')
                    await page.locator('#draft-save').click()
                    await page.locator('#dirty-mark').filter(has_text='已保存').wait_for()
                    await page.locator('#draft-copy').click()
                    clipboard=await page.evaluate('navigator.clipboard.readText()')
                    assert '保存后的测试正文' in clipboard
                    for kind in ('md','txt'):
                        async with page.expect_download() as download_info:
                            await page.locator('#draft-export-'+kind).click()
                        download=await download_info.value
                        path=await download.path()
                        exported=Path(path).read_text()
                        assert '保存后的测试正文' in exported and '资料来源' in exported and '核对原始数据' in exported
                    await page.locator('#draft-close').click()
                    await page.reload()
                    await page.locator('[data-tab="drafts"]').click()
                    await page.locator('[data-draft]').filter(has_text='修改后的标题').wait_for()
                    restored=Store(Path(tmp)/'workbench.db')
                    assert len(restored.drafts())==10
                    await page.locator('#session-inspect').click()
                    await page.locator('#session-detail').filter(has_text='未启动').wait_for()
                    await page.locator('[data-close="session-dialog"]').click()
                    output=Path(__file__).resolve().parent.parent/'output/playwright'
                    output.mkdir(parents=True,exist_ok=True)
                    await page.screenshot(path=str(output/'drafts-test.png'),full_page=True)
                    assert not errors, errors
                    print('PASS: multi-select toggling/persistence; two styles × versions; single/multi-source; edit; clipboard; MD/TXT download; SQLite reopen; no JS errors.')
                finally:
                    await browser.close()
        finally:
            server.should_exit=True
            await serving


if __name__=='__main__':
    asyncio.run(main())
