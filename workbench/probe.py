"""Reproducible real-page gate. Run before enabling the full workbench."""
import argparse
import asyncio
import json
from pathlib import Path

from .browser import Browser, NeedsAttention
from .parsing import now_iso


async def run(args):
    out = Path(args.output)
    out.mkdir(parents=True,exist_ok=True)
    browser = Browser(Path(args.profile),headless=not args.visible)
    report = {'checked_at':now_iso(),'keyword':args.keyword,'mode':'rendered DOM / dedicated profile','kinds':{},'gate_passed':False}
    try:
        async with browser.lock:
            for kind in (('article','micro') if args.kind=='all' else (args.kind,)):
                samples=[]
                result={'samples':samples,'supported':False,'fields':{'reads':False,'likes':False,'comments':'搜索卡片有显示时可读'},'domain_mode':'keyword_mapping'}
                report['kinds'][kind]=result
                try:
                    candidates=await browser.search(args.keyword,kind,10)
                    result['candidate_count']=len(candidates)
                    for i,card in enumerate(candidates):
                        row={'url':card['url'],'list_title':card['title'],'list_text':card['list_text'],'list_time':card['published_at'],'list_comments':card['comments'],'list_likes':card.get('likes')}
                        samples.append(row)
                        try:
                            item=await browser.detail(card['url'],kind)
                            row.update({'success':True,'detail_title':item['title'],'body_length':len(item['body']),'published_at':item['published_at'],'author':item['author'],'title_matches':item['title']==card['title']})
                            # Evidence is from rendered DOM, not an internal response or search snippet.
                            row['body_excerpt']=item['body'][:80]
                        except NeedsAttention as exc:
                            row.update(success=False,error=str(exc))
                            result['needs_attention']=True
                            break
                        except Exception as exc:
                            row.update(success=False,error=str(exc)[:250])
                        print(kind,i+1,row['success'],card['url'],flush=True)
                    good=[r for r in samples if r.get('success')]
                    result['fields']['likes']=any(r.get('list_likes') is not None for r in samples)
                    result['successful_count']=len(good)
                    if good:
                        again=await browser.detail(good[0]['url'],kind)
                        result['repeat_success']=again['title']==good[0]['detail_title'] and bool(again['body'])
                    result['supported']=len(good)>=8 and bool(result.get('repeat_success'))
                except Exception as exc:
                    result['error']=str(exc)[:250]
                (out/'probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            report['gate_passed']=any(r['supported'] for r in report['kinds'].values())
            (out/'probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print('GATE',report['gate_passed'],flush=True)
    finally:
        await browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--keyword',default='AI')
    parser.add_argument('--profile',default='data/browser')
    parser.add_argument('--output',default='data/probe')
    parser.add_argument('--visible',action='store_true')
    parser.add_argument('--kind',choices=['all','article','micro'],default='all')
    asyncio.run(run(parser.parse_args()))
