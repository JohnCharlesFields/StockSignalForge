const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const http=require('node:http');
let base=process.env.QA_BASE||'http://127.0.0.1:19090';
const out=path.resolve(process.env.QA_OUT || path.join(__dirname,'../../.tmp/chart-viewport-20261016'));
const candles=Array.from({length:240},(_,i)=>({t:Date.UTC(2025,0,i+1),o:100+i*.1,h:103+i*.1,l:98+i*.1,c:101+i*.1,v:10000+i*100}));
const payload={symbol:'AAPL',page_revision:'fixture-1',row:{current_price:125,short_reason:'Fixture only',open_risk_status:'ALLOW_OPEN'},
 day_change:{price:125,prev_close:124,change:1,change_pct:1/124,as_of:'2025-08-28'},
 company_profile:{name:'Synthetic chart fixture'},deepseek_summary:{status:'unavailable'}};
async function main(){
 fs.mkdirSync(out,{recursive:true});
 let server;
 if(process.env.QA_DIST){
  const dist=path.resolve(process.env.QA_DIST);
  server=http.createServer((req,res)=>{
   const p=new URL(req.url,'http://localhost').pathname;
   const file=p.startsWith('/assets/')?path.resolve(dist,'.'+p):path.join(dist,'index.html');
   if(!file.startsWith(dist+path.sep)||!fs.existsSync(file))return res.writeHead(404).end();
   res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');
   res.end(fs.readFileSync(file));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  base='http://127.0.0.1:'+server.address().port;
 }
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const checks=[],errors=[],writes=[];
 try{for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:1000},colorScheme:'dark'});
  const routes=[];
  await context.route('**/*',route=>{
   const r=route.request(),u=new URL(r.url());
   if(r.method()!=='GET'){writes.push(u.pathname);return route.abort();}
   if(u.origin===base&&(r.resourceType()==='document'||u.pathname.startsWith('/assets/')))return route.continue();
   routes.push(u.pathname);
   if(u.pathname==='/single-stock-overnight/AAPL')return route.fulfill({json:payload});
   if(u.pathname.endsWith('/candles'))return route.fulfill({json:{candles,markers:[{time:'2025-06-02',price:116,side:'BUY',research_only:true,stage:'WATCH',text:'观'},
     {time:'2025-06-03',price:117,side:'SELL',direction:'SHORT',research_only:true,stage:'WATCH',text:'空观'},
     {time:'2025-06-04',price:116,side:'SELL',direction:'SHORT',research_only:true,stage:'ENTRY',text:'空入'}],
    entry_research:{status:'research_only',latest:{strategy:'countertrend',trend_down:true,reason:'Fixture downtrend: watch only'},contract:{hold_sessions:8}},
    short_entry_research:{status:'research_only',latest:{stage:'RESEARCH_HOLD',entry_price:116,stop_price:120,target_price:108,reason:'Fixture short only'},contract:{hold_sessions:8,direction:'SHORT'}}}});
   if(u.pathname.endsWith('/enrich'))return route.fulfill({json:{done:true,company_profile:{name:'Background fixture update'}}});
   return route.fulfill({json:{status:'idle',exists:false,items:[],picks:[],rows:[],counts:{},snapshot:{},validation:{}}});
  });
  await context.addInitScript(()=>{
   window.__chartDates=[];
   const original=CanvasRenderingContext2D.prototype.fillText;
   const clear=CanvasRenderingContext2D.prototype.clearRect;
   CanvasRenderingContext2D.prototype.clearRect=function(...args){
    if(this.canvas===document.querySelector('#stock-chart canvas')&&args[2]>=this.canvas.width*.9&&args[3]>=this.canvas.height*.9)window.__chartDates=[];
    return clear.apply(this,args);
   };
   CanvasRenderingContext2D.prototype.fillText=function(text,...args){
    if(this.canvas===document.querySelector('#stock-chart canvas')&&/^\d{4}-\d{2}-\d{2}$/.test(String(text)))window.__chartDates.push(String(text));
    return original.call(this,text,...args);
   };
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/single-stock-overnight?symbol=AAPL');
  assert.equal(await page.title(),'StockSignalForge · easymoneysniper');
  if(width===1440)assert.equal(await page.locator('aside').getByText('easymoneysniper',{exact:true}).count(),1);
  if(width===390){await page.getByTitle('Collapse',{exact:true}).click();await page.waitForTimeout(250);}
  const canvas=page.locator('#stock-chart canvas').first();await canvas.waitFor();
  assert.equal(await page.getByText('预算校验通过 · 非入场信号',{exact:true}).count(),1);
  assert.equal(await page.getByText('ALLOW_OPEN',{exact:true}).count(),0);
  await page.getByText(/中期下行 · 不产生确认入场/).waitFor();
  assert(await page.getByLabel('回调规则研究标记',{exact:true}).isChecked());
  assert(await page.getByLabel('反弹做空研究标记',{exact:true}).isChecked());
  await page.getByText('空头：假设持有 · 等待回补',{exact:true}).waitFor();
  assert.equal(await page.getByText('假设开空 $116.00',{exact:true}).count(),1);
  assert.equal(await page.getByText('回补参考 $108.00',{exact:true}).count(),1);
  await page.getByLabel('反弹做空研究标记',{exact:true}).uncheck();
  assert(await page.getByLabel('回调规则研究标记',{exact:true}).isChecked());
  await page.getByLabel('反弹做空研究标记',{exact:true}).check();
  await page.waitForTimeout(500);
  await canvas.scrollIntoViewIfNeeded();
  const box=await canvas.boundingBox();
  const old=await page.evaluate(()=>window.__chartDates.slice().sort()[0]);
  await page.evaluate(()=>window.__chartDates=[]);
  const left=box.x+55,right=box.x+box.width-24,y=box.y+box.height-14;
  await page.mouse.move(left+(right-left)*.736,y);await page.mouse.down();
  await page.mouse.move(left+(right-left)*.35,y,{steps:12});await page.mouse.up();
  await page.mouse.move(box.x+box.width-2,box.y+5);
  await page.waitForTimeout(300);
  const dates=await page.evaluate(()=>[...new Set(window.__chartDates)].sort());
  if(dates.length<2){await page.screenshot({path:path.join(out,`drag-failure-${width}.png`)});console.log(JSON.stringify({width,box,old,dates,left,right,y}));}
  assert(dates.length>1,'drag must repaint date ticks');assert(dates[0]<old,'drag must widen the history');
  await page.evaluate(()=>window.__chartDates=[]);
  // Existing background enrichment updates parent props after five seconds.
  await page.waitForTimeout(5300);
  const after=await page.evaluate(()=>[...new Set(window.__chartDates)].sort());
  assert(routes.includes('/single-stock-overnight/AAPL/enrich'));
  assert.deepEqual(after,dates,'background enrichment must not reset chart dates');
  assert(await canvas.evaluate(c=>{const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;let n=0;for(let i=3;i<d.length;i+=4)if(d[i])n++;return n>1000;}));
  await page.screenshot({path:path.join(out,`deployed-${width}.png`)});
  checks.push({width,first:dates[0],last:dates[dates.length-1],background_update_retained:true});
  await context.close();
 }}finally{await browser.close();if(server)await new Promise(resolve=>server.close(resolve));}
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
 fs.writeFileSync(path.join(out,'deployed-page.json'),JSON.stringify({checks,errors,writes},null,2));
 console.log(JSON.stringify({checks,errors,writes}));
}
main().catch(e=>{console.error(e);process.exitCode=1;});
