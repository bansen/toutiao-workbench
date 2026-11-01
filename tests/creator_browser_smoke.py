"""Offline creator DOM contract: pagination, exact identity, nulls and lock release."""
import asyncio,json,sys,tempfile
from pathlib import Path
from datetime import datetime,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from workbench.browser import Browser
from workbench.creator_analytics import CreatorAnalytics
from workbench.micro_schedule import TIMEZONE

async def main():
    day=(datetime.now(TIMEZONE)-timedelta(days=1)).date().isoformat()
    html='''<meta charset="utf-8"><a href="https://www.toutiao.com/c/user/12345678901/">Fixture</a><main id="list"></main><ul class="fake-pagination"></ul>
<script>
let page=1;
function draw(){
 document.querySelector('#list').innerHTML=Array.from({length:page===1?10:2},(_,i)=>{
 let id=String(1880000000000000+(page-1)*10+i);
 return `<div class="post-item"><div class="wtt-text" onclick="window.open('https://www.toutiao.com/w/${id}/')">测试标题 ${id}\\n事实正文</div><div class="time">DAY 09:00</div><div class="tags">已发布</div><ul class="stat"><li>展现 100</li><li>阅读 0</li><li>点赞 0</li><li>评论 0</li></ul><span onclick="detail()">查看数据</span></div>`;
 }).join('');
 document.querySelector('.fake-pagination').innerHTML=`<li class="fake-pagination-item active">${page}</li><li onclick="next()" class="fake-pagination-item ${page===2?'disabled':''}"><svg class="byte-icon-right"></svg></li>`;
}
function next(){if(page===2)return;document.querySelector('#list').innerHTML='';setTimeout(()=>{page=2;draw()},250)}
function detail(){let e=document.createElement('div');e.className='byte-drawer';e.innerHTML=`<button class="byte-drawer-close-icon" onclick="this.parentNode.remove()">关闭</button><div class="info-time">DAY 09:00</div><div class="single-data-board-group">累计数据<div class="primary">展现量<span class="num">100</span></div><div class="primary">阅读量<span class="num">0</span></div></div><div class="interaction-alone"><div class="inter-item">点赞<span class="num">0</span></div><div class="inter-item">评论<span class="num">0</span></div><div class="inter-item">收藏<span class="num">待更新</span></div><div class="inter-item">分享<span class="num">0</span></div></div>`;document.body.append(e)}
draw();
</script>'''.replace('DAY',day)
    with tempfile.TemporaryDirectory() as directory:
        browser=Browser(Path(directory)/'profile')
        await browser.start()
        async def route(request):
            url=request.request.url
            if '/profile_v4/manage/' in url:await request.fulfill(content_type='text/html',body=html)
            elif 'https://www.toutiao.com/w/' in url:await request.fulfill(content_type='text/html',body='公开作品')
            else:await request.abort()
        await browser.context.route('**/*',route)
        try:
            result=await CreatorAnalytics(browser,lambda:'12345678901',directory,lambda:False).collect(day)
            assert result['complete'] and result['pagination']['pages']==2,result
            assert len(result['rows'])==12
            assert result['verified_samples']==10
            assert all(r['scope']=='cumulative' and r['metrics']['reads']==0 and r['metrics']['favorites'] is None for r in result['rows'])
            assert not browser.lock.locked()
            # An older ordered page can end the requested window even when
            # the platform incorrectly leaves Next enabled for empty history.
            older=(datetime.now(TIMEZONE)-timedelta(days=4)).date().isoformat()
            html=html.replace('<div class="time">'+day+' 09:00', '<div class="time">${page===1?"'+day+'":"'+older+'"} 09:00')
            html=html.replace("${page===2?'disabled':''}", "")
            bounded=await CreatorAnalytics(browser,lambda:'12345678901',directory,lambda:False).collect(day)
            assert bounded['complete'] and len(bounded['rows'])==10,bounded
            assert bounded['pagination']['stop_reason']=='verified_date_boundary'
            assert not browser.lock.locked()
            print('PASS: delayed pagination; visible max is not final; 12 unique public IDs; ten list/drawer matches; zero/unknown; lock released. No real platform requests.')
        finally:await browser.close()

asyncio.run(main())
