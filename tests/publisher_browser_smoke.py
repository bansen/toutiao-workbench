"""Isolated creator DOM fixture; never reaches Toutiao or publishes real content."""
import asyncio,html,tempfile,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from workbench.browser import Browser,NeedsAttention
from workbench.toutiao_publisher import ToutiaoPublisher,CREATE,MANAGE,CLEAR_OWNED_DRAFT_JS
from workbench.publication_guard import digest

async def main():
 with tempfile.TemporaryDirectory() as directory:
  browser=Browser(Path(directory)/'profile')
  clicks=[]
  audit={'tags':'已发布'}
  body='测试第一段。\n\n第二段，来源为测试资料。正文真实结尾：展开'
  try:
   await browser.start()
   async def creator(route):
    if '/manage/content/' in route.request.url:
     document=f'''<a href="https://www.toutiao.com/c/user/12345678901/">账号</a><div class="post-item"><div class="wtt-text" onclick="window.open('https://www.toutiao.com/w/1880433446879241/')"><div class="overflow-handler-container">{html.escape(body)}<span class="expand-span"><span class="more-span">展开</span></span></div></div><div class="tags">{audit["tags"]}</div><div class="time">今天 09:00</div></div>'''
    else:
     document='''<a href="https://www.toutiao.com/c/user/12345678901/">账号</a><div class="ProseMirror" contenteditable="true" style="white-space:pre-wrap"></div>
     <label class="byte-checkbox"><input type="checkbox"><span>头条首发</span></label>
     <label class="byte-checkbox byte-checkbox-checked"><input type="checkbox" checked><span>引用AI</span></label>
     <label class="byte-checkbox byte-checkbox-checked" style="display:none"><input type="checkbox" checked><span>头条首发</span></label>
     <button onclick="window.clicked();location.href='/profile_v4/manage/content/all'">发布</button>
     <script>
     document.querySelectorAll('input').forEach(i=>i.onchange=()=>i.parentElement.classList.toggle('byte-checkbox-checked',i.checked));
     const declarations=[...document.querySelectorAll('label')];
     declarations.forEach(e=>e.remove());
     setTimeout(()=>declarations.forEach(e=>document.body.appendChild(e)),700);
     </script>'''
    await route.fulfill(content_type='text/html; charset=utf-8',body=document)
   async def public(route):await route.fulfill(content_type='text/html; charset=utf-8',body='<article style="white-space:pre-wrap">'+html.escape(body)+'</article>')
   await browser.context.route('https://mp.toutiao.com/**',creator)
   await browser.context.route('https://www.toutiao.com/w/**',public)
   await browser.context.expose_binding('clicked',lambda source:clicks.append('submitted'))
   publisher=ToutiaoPublisher(browser,lambda:'12345678901',expected_ai=lambda:False)
   publisher.allowed=lambda:True
   identity=await publisher.identify()
   assert identity['id']=='12345678901'
   probe=await publisher.probe()
   assert probe['declaration_verified'] and not probe['ai_declaration']
   try:checks=await publisher.preflight({'body':body})
   except NeedsAttention:
    print({'editor_text':repr(await publisher.page.locator('.ProseMirror').inner_text()),'editor_html':await publisher.page.locator('.ProseMirror').inner_html()},flush=True)
    raise
   assert checks['body_sha256']==digest(body) and not checks['ai']['checked'] and not checks['exclusive']['checked']
   receipt=await publisher.submit({'body':body})
   assert receipt['status']=='published' and receipt['content_id']=='1880433446879241' and receipt['body_verified']
   assert clicks==['submitted']
   await publisher.close()
   await publisher.preflight({'body':body})
   publisher.allowed=lambda:False
   try:await publisher.submit({'body':body})
   except NeedsAttention:pass
   else:raise AssertionError('Pause must prevent submit')
   assert clicks==['submitted']
   await publisher.page.evaluate("text => localStorage.setItem('wtt-mpeditordraft', JSON.stringify({content:'<p>'+text+'</p>'}))", body)
   assert not await publisher.page.evaluate(CLEAR_OWNED_DRAFT_JS, 'different body')
   await publisher.close()
   inspection=await browser.context.new_page()
   await inspection.goto(CREATE)
   assert await inspection.evaluate("localStorage.getItem('wtt-mpeditordraft')") is None
   await inspection.close()
   audit['tags']='审核中'
   await publisher.preflight({'body':body})
   publisher.allowed=lambda:True
   receipt=await publisher.submit({'body':body})
   assert receipt['status']=='submitted' and not receipt['body_verified']
   await publisher.close()
   print('PASS: creator DOM preflight; AI off; first release off; exact text snapshot; single submit; record + public body lookup; pause prevents click.')
  finally:await browser.close()
asyncio.run(main())
