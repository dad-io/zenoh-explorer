// node rec.mjs <outdir> <mode: nav|repeat|reduced|build|local>
import fs from 'node:fs';
const [,, out, mode] = process.argv;
const tgt = await (await fetch('http://127.0.0.1:9333/json/new?about:blank',{method:'PUT'})).json();
const ws = new WebSocket(tgt.webSocketDebuggerUrl); let id=0; const pend=new Map(); const frames=[];
ws.onmessage=e=>{const m=JSON.parse(e.data); if(m.id&&pend.has(m.id)){pend.get(m.id)(m);pend.delete(m.id);} if(m.method==='Page.screencastFrame'){frames.push({t:m.params.metadata.timestamp,d:m.params.data}); send('Page.screencastFrameAck',{sessionId:m.params.sessionId});}};
await new Promise(r=>ws.onopen=r);
const send=(method,params={})=>new Promise(r=>{const i=++id;pend.set(i,r);ws.send(JSON.stringify({id:i,method,params}));});
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const ev=async js=>(await send('Runtime.evaluate',{expression:js,returnByValue:true,awaitPromise:true})).result?.result?.value;
await send('Page.enable'); await send('Emulation.setDeviceMetricsOverride',{width:1440,height:900,deviceScaleFactor:Number(process.env.DSF||1),mobile:false});
if(mode==='reduced') await send('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
await send('Page.navigate',{url:'http://127.0.0.1:8761/'}); await sleep(3000);
const clickSel=async (js)=>{const p=JSON.parse(await ev(`(()=>{const el=${js};el.scrollIntoView({block:"center"});const r=el.getBoundingClientRect();return JSON.stringify({x:r.x+r.width/2,y:r.y+r.height/2})})()`)); for(const type of ['mousePressed','mouseReleased']) await send('Input.dispatchMouseEvent',{type,x:p.x,y:p.y,button:'left',clickCount:1}); return performance.now();};
const nav=t=>`[...document.querySelectorAll('nav a')].find(a=>a.innerText.includes('${t}'))`;
await send('Page.startScreencast',{format:'png',everyNthFrame:1,maxWidth:2880,maxHeight:1800});
await sleep(400);
const marks=[];
const wall=()=>Date.now()/1000;
if(mode==='nav'||mode==='reduced'){ marks.push(['click Arena',wall()]); await clickSel(nav('Arena')); await sleep(1600); }
if(mode==='local'){ marks.push(['click Jump in & play',wall()]); await clickSel(`[...document.querySelectorAll('button')].find(b=>b.innerText.includes('Jump in'))`); await sleep(1600); }
if(mode==='repeat'){ marks.push(['click Arena',wall()]); await clickSel(nav('Arena')); await sleep(250); marks.push(['click Workshop',wall()]); await clickSel(nav('Workshop')); await sleep(120); marks.push(['click Circle',wall()]); await clickSel(nav('Circle')); await sleep(1700); }
if(mode==='build'){ await clickSel(nav('Workshop')); await sleep(1800); const b=await ev(`(()=>{const b=document.querySelector('[data-action="build"]'); return b? b.innerText.trim():'NONE'})()`);  if(b!=='NONE'){ await ev(`document.querySelector('[data-action="build"]').scrollIntoView({block:'center'})`); await sleep(600); marks.push(['click build',wall()]); await clickSel(`document.querySelector('[data-action="build"]')`); await sleep(4000);} }
await send('Page.stopScreencast'); await sleep(200);
fs.mkdirSync(out,{recursive:true});
const t0=marks[0]?.[1]??frames[0].t;
frames.forEach((f,i)=>fs.writeFileSync(`${out}/f${String(i).padStart(3,'0')}_${Math.round((f.t-t0)*1000)}ms.png`,Buffer.from(f.d,'base64')));
fs.writeFileSync(`${out}/marks.json`,JSON.stringify(marks.map(([k,t])=>[k,Math.round((t-t0)*1000)])));
console.log(mode,'frames',frames.length,'marks',JSON.stringify(marks.map(([k,t])=>[k,Math.round((t-t0)*1000)])));
await fetch(`http://127.0.0.1:9333/json/close/${tgt.id}`); process.exit(0);
