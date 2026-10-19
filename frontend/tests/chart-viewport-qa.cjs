const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { build } = require('esbuild');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.QA_OUT || path.join(root, '../.tmp/chart-viewport-20261016'));
const fixture = `
import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CandlestickChart } from './src/components/charts/CandlestickChart';
import { echarts } from './src/lib/echarts';
const bars = Array.from({length:240}, (_,i) => ({time:new Date(Date.UTC(2025,0,i+1)).toISOString().slice(0,10),
 open:100+i*.1,close:100.5+i*.1+Math.sin(i),high:103+i*.1,low:98+i*.1,volume:10000+i*100}));
function App(){
 const [revision, bump] = useState(0), [append, setAppend] = useState(false);
 const [identity, setIdentity] = useState('AAPL:daily'), [empty, setEmpty] = useState(false);
 const [research, setResearch] = useState(false);
 const [short, setShort] = useState(false);
 const data = empty ? [] : [...bars, ...(append ? [{...bars[239], time:'2025-08-29'}] : [])];
 window.fixture = {bump:()=>bump(x=>x+1), append:()=>setAppend(true), identity:setIdentity, empty:setEmpty, research:setResearch, short:setShort,
 chart:()=>echarts.getInstanceByDom(document.querySelector('[data-chart] > div > div:last-child'))};
 return <><span id="revision">{revision}</span><div data-chart className="p-4">
 <CandlestickChart key={identity} data={data} markers={short ? ['WATCH','CONFIRMED','ENTRY','EXIT','INVALIDATED'].map((stage,i)=>({
  time:bars[200+i].time,price:120,side:stage==='EXIT'?'BUY':'SELL',direction:'SHORT',research_only:true,stage,text:['空观','空确','空入','回补','空失'][i]})) : research ? ['WATCH','CONFIRMED','ENTRY','EXIT','INVALIDATED'].map((stage,i)=>({
  time:bars[200+i].time,price:120,side:'BUY',research_only:true,stage,text:['观','确','入','离','失'][i]}))
  : [{time:bars[200].time, price:120, side:'BUY'}]}
 priceLines={[{price:110+revision,label:'test level'}]} initialRange="3M" height={520}/>
 </div></>;
}
createRoot(document.getElementById('root')).render(<App/>);
`;

async function main() {
 fs.mkdirSync(output, {recursive:true});
 const bundled = await build({stdin:{contents:fixture,resolveDir:root,loader:'tsx'},bundle:true,write:false,
   format:'iife',platform:'browser',jsx:'automatic',alias:{'@':path.join(root,'src')},
   define:{'process.env.NODE_ENV':'"production"'},logLevel:'silent'});
 const server=http.createServer((req,res)=>{
   if(req.url==='/fixture.js'){res.setHeader('Content-Type','text/javascript');return res.end(bundled.outputFiles[0].contents);}
   if(req.url==='/fixture.css'){
    res.setHeader('Content-Type','text/css');const assets=path.join(root,'dist/assets');
    return res.end(fs.readdirSync(assets).filter(n=>n.endsWith('.css')).map(n=>fs.readFileSync(path.join(assets,n),'utf8')).join('\n'));
   }
   res.setHeader('Content-Type','text/html');res.end('<!doctype html><html class="dark"><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;background:#101318;color:#eee}canvas{display:block}</style><div id="root"></div><script src="/fixture.js"></script>');
 });
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const checks=[], failures=[], errors=[];
 try {
  for(const width of [1440,390]){
   const page=await browser.newPage({viewport:{width,height:850}});
   page.on('pageerror',e=>errors.push(e.message));
   await page.goto('http://127.0.0.1:'+server.address().port);
   await page.waitForFunction(()=>!!window.fixture?.chart());
   const zoom = async (start=20,end=65)=>{await page.evaluate(({start,end})=>window.fixture.chart().dispatchAction({type:'dataZoom',start,end}),{start,end});await page.waitForTimeout(180);};
   const state = ()=>page.evaluate(()=>{
    const o=window.fixture.chart().getOption(), z=o.dataZoom[0], d=o.xAxis[0].data;
    return {start:z.start,end:z.end,first:d[z.startValue],last:d[z.endValue]};
   });
   const check=async(name,fn)=>{try{await fn();checks.push({width,name});}catch(e){failures.push({width,name,message:e.message});}};
   const same=(a,b)=>{assert.equal(a.first,b.first);assert.equal(a.last,b.last);};
   await check('parent update retains dragged window',async()=>{
    await zoom();const before=await state();await page.evaluate(()=>window.fixture.bump());await page.waitForTimeout(200);same(await state(),before);
   });
   await check('indicator toggle retains window',async()=>{
    await zoom();const before=await state();await page.getByRole('button',{name:/^macd$/i}).click();await page.waitForTimeout(200);same(await state(),before);
   });
   await check('zone disabled still records zoom',async()=>{
    await page.getByRole('button',{name:'博弈区',exact:true}).click();await zoom(30,60);const before=await state();
    await page.evaluate(()=>window.fixture.bump());await page.waitForTimeout(200);same(await state(),before);
    await page.getByRole('button',{name:'博弈区',exact:true}).click();
   });
   await check('price lines retain window',async()=>{
    await zoom();const before=await state();await page.getByRole('button',{name:'价位线',exact:true}).click();await page.waitForTimeout(200);same(await state(),before);
   });
   await check('moving average retains window',async()=>{
    await zoom();const before=await state();await page.getByRole('button',{name:/Indicators/}).click();
    await page.getByLabel('MA10',{exact:true}).check();await page.waitForTimeout(200);same(await state(),before);
    await page.getByRole('button',{name:/Indicators/}).click();
   });
   await check('update before debounce retains window',async()=>{
    await page.evaluate(()=>{window.fixture.chart().dispatchAction({type:'dataZoom',start:25,end:55});window.fixture.bump();});
    await page.waitForTimeout(200);const s=await state();assert(Math.abs(s.start-25)<1);assert(Math.abs(s.end-55)<1);
   });
   await check('append keeps historical date anchors',async()=>{
    await zoom();const before=await state();await page.evaluate(()=>window.fixture.append());await page.waitForTimeout(200);same(await state(),before);
   });
   await check('resize retains window',async()=>{
    await zoom();const before=await state();await page.setViewportSize({width:width-20,height:790});await page.waitForTimeout(200);same(await state(),before);
   });
   await check('real slider drag survives rerender',async()=>{
    await zoom(20,65);
    await page.evaluate(()=>window.scrollTo(0,document.body.scrollHeight));
    const box=await page.locator('[data-chart] canvas').first().boundingBox();
    const x=box.x+box.width*.45,y=box.y+box.height-14;
    await page.mouse.move(x,y);await page.mouse.down();await page.mouse.move(x+box.width*.07,y,{steps:8});await page.mouse.up();
    await page.waitForTimeout(200);const before=await state();assert(Math.abs(before.start-20)>.5,'slider did not move');
    await page.evaluate(()=>window.fixture.bump());await page.waitForTimeout(200);same(await state(),before);
   });
   await check('range buttons intentionally reset window',async()=>{
    await page.getByRole('button',{name:'1M',exact:true}).click();await page.waitForTimeout(200);
    const s=await state();assert(s.start>85);assert.equal(s.end,100);
    await zoom(20,65);await page.getByRole('button',{name:'1M',exact:true}).click();await page.waitForTimeout(200);
    assert((await state()).start>85,'same preset should reset custom zoom too');
   });
   await check('identity switch starts at preset',async()=>{
    await zoom();await page.evaluate(()=>window.fixture.identity('NVDA:daily'));await page.waitForTimeout(200);assert((await state()).start>70);
   });
   await check('toolbox restore intentionally resets window',async()=>{
    await zoom();await page.evaluate(()=>window.fixture.chart().dispatchAction({type:'restore'}));await page.waitForTimeout(200);
    assert((await state()).start>70);assert.equal((await state()).end,100);
   });
   await check('empty data recovers without stale zoom',async()=>{
    await page.evaluate(()=>window.fixture.empty(true));await page.waitForTimeout(100);await page.evaluate(()=>window.fixture.empty(false));
    await page.waitForFunction(()=>!!window.fixture.chart());assert(Number.isFinite((await state()).start));
   });
   await check('canvas is nonblank',async()=>{
    const count=await page.locator('[data-chart] canvas').first().evaluate(c=>{
     const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=3;i<d.length;i+=4)if(d[i])n++;return n;
    });assert(count>1000);
   });
   await check('research markers never masquerade as green profitable buys',async()=>{
    await page.evaluate(()=>window.fixture.research(true));await page.waitForTimeout(200);
    const marks=await page.evaluate(()=>window.fixture.chart().getOption().series.find(s=>s.name==='K').markPoint.data);
    assert.deepEqual(marks.map(m=>m.value),['观','确','入','离','失']);
    assert(marks.every(m=>!['#22c55e','#10b981'].includes(m.itemStyle.color)));
   });
   await check('short entry and cover have explicit separate research labels',async()=>{
    await zoom(70,95);const before=await state();
    await page.evaluate(()=>window.fixture.short(true));await page.waitForTimeout(200);
    const marks=await page.evaluate(()=>window.fixture.chart().getOption().series.find(s=>s.name==='K').markPoint.data);
    assert.deepEqual(marks.map(m=>m.value),['空观','空确','空入','回补','空失']);
    assert(marks.every(m=>m.name.includes('空头假设')));
    assert.notEqual(marks[0].itemStyle.color,marks[2].itemStyle.color);
    assert.notEqual(marks[2].itemStyle.color,marks[3].itemStyle.color);
    assert(marks.every(m=>!['#22c55e','#10b981'].includes(m.itemStyle.color)));
    same(await state(),before);
   });
   await page.screenshot({path:path.join(output,`${width}-chart.png`),fullPage:true});
   await page.close();
  }
  assert.deepEqual(errors,[]);
  const parent=fs.readFileSync(path.join(root,'src/pages/SingleStockOvernight.tsx'),'utf8');
  assert.match(parent, /CandlestickChart\s+key=\{.*symbol.*tf/,'stock/timeframe must reset viewport identity');
 } catch(e){failures.push({name:'harness',message:e.message});}
 finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
 fs.writeFileSync(path.join(output,process.env.QA_REPORT||'ui.json'),JSON.stringify({checks,failures,errors},null,2));
 console.log(JSON.stringify({passed:checks.length,failures,errors}));
 assert.equal(failures.length,0);
}
main().catch(e=>{console.error(e);process.exitCode=1;});
