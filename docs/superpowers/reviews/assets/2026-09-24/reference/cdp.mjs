// Minimal CDP driver: node cdp.mjs <script.json>; script = [{op,...}]
import fs from 'node:fs';
const [,, scriptPath] = process.argv;
const steps = JSON.parse(fs.readFileSync(scriptPath,'utf8'));
const tgt = await (await fetch('http://127.0.0.1:9333/json/new?about:blank',{method:'PUT'})).json();
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
let id=0; const pending=new Map();
ws.onmessage=e=>{const m=JSON.parse(e.data); if(m.id&&pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);}};
await new Promise(r=>ws.onopen=r);
const send=(method,params={})=>new Promise(r=>{const i=++id;pending.set(i,r);ws.send(JSON.stringify({id:i,method,params}));});
const evalJs=async(expr)=>{const r=await send('Runtime.evaluate',{expression:expr,awaitPromise:true,returnByValue:true});return r.result?.result?.value ?? r.result?.exceptionDetails?.text;};
await send('Page.enable'); await send('Runtime.enable');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
for (const s of steps){
  if(s.op==='size') await send('Emulation.setDeviceMetricsOverride',{width:s.w,height:s.h,deviceScaleFactor:1,mobile:false});
  if(s.op==='goto'){ await send('Page.navigate',{url:s.url}); await sleep(s.wait??2500); }
  if(s.op==='eval'){ const v=await evalJs(s.js); console.log(`[eval ${s.label??''}]`, typeof v==='string'?v:JSON.stringify(v,null,1)); }
  if(s.op==='click'){ const v=await evalJs(`(()=>{const el=${s.sel}; if(!el) return 'NOT FOUND'; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect(); return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2,t:(el.innerText||el.getAttribute('aria-label')||'').trim().slice(0,60)});})()`);
    if(v==='NOT FOUND'){console.log('[click] NOT FOUND',s.sel);continue;}
    const p=JSON.parse(v); for(const type of ['mousePressed','mouseReleased']) await send('Input.dispatchMouseEvent',{type,x:p.x,y:p.y,button:'left',clickCount:1});
    console.log('[click]',p.t,'@',Math.round(p.x),Math.round(p.y)); await sleep(s.wait??1500);}
  if(s.op==='key'){ for(const type of ['keyDown','keyUp']) await send('Input.dispatchKeyEvent',{type,key:s.key,code:s.code??s.key,windowsVirtualKeyCode:s.vk??0}); await sleep(s.wait??400);}
  if(s.op==='shot'){ await sleep(s.wait??0); const r=await send('Page.captureScreenshot',{format:'png'}); fs.writeFileSync(s.path,Buffer.from(r.result.data,'base64')); console.log('[shot]',s.path); }
  if(s.op==='sleep') await sleep(s.ms);
}
await fetch(`http://127.0.0.1:9333/json/close/${tgt.id}`); ws.close(); process.exit(0);
