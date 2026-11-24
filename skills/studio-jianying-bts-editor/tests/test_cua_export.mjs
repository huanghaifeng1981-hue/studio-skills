import test from 'node:test';
import assert from 'node:assert/strict';
import {createExportAdapter} from '../scripts/cua_export.mjs';
const batch={source:'/photos/A',output:'/photos/A/test.mp4'};
function fake(wrong=false){
 const states=['0 text ExportFileNameInput','0 text ExportFileNameInput Value: /old/test.mp4','0 dialog OKButton','1 文本栏 Value: /old, ID: PathTextField','1 文本栏 Value: /photos/A, ID: PathTextField',`2 文本栏 (selected, settable) URL: file:///photos/${wrong?'B':'A'}/\n3 按钮 打开, ID: OKButton`,'0 text ExportFileNameInput Value: /photos/A/test.mp4','0 text ExportProgress'];
 const clicks=[];const bytes=new Uint8Array(24);bytes.set([137,80,78,71,13,10,26,10]);new DataView(bytes.buffer).setUint32(16,640);new DataView(bytes.buffer).setUint32(20,720);
 return {clicks,getAXState:async()=>states.shift(),getScreenshot:async()=>bytes,click:async x=>clicks.push(x),pressKey:async()=>{},paste:async()=>{},setValue:async()=>{}};
}
const layout={verified:true,size:[640,720],title:[1,1],folder:[2,2],submit:[3,3]};
test('fixed export submits exact output once',async()=>{
 const app=fake();const run=createExportAdapter({app,layout,exists:async()=>false});
 assert.deepEqual(await run({batch}),{output:batch.output,submitted:true});
 assert.deepEqual(app.clicks.at(-1),layout.submit);
});
test('wrong full selected directory stops before submit',async()=>{
 const app=fake(true);await assert.rejects(createExportAdapter({app,layout,exists:async()=>false})({batch}),/directory mismatch/);
 assert.equal(app.clicks.some(x=>x===layout.submit),false);
});
test('existing output stops before any UI input',async()=>{
 const app=fake();await assert.rejects(createExportAdapter({app,layout,exists:async()=>true})({batch}),/already exists/);assert.equal(app.clicks.length,0);
});
