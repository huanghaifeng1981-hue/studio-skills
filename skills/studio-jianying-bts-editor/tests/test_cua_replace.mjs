import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createReplacer,screenshotSize} from '../scripts/cua_replace.mjs';

const editor='0 标准窗口\n1 text MainWindowTitleBarReviewBtn';
const dialog='0 对话框\n1 (disabled) 剪映专业版';
const source='/photos/客片/123/1.jpg';
const row={slot:1,source};
const batch={selected:[row]};
const layout={verified:true,timeline:Array.from({length:22},()=>[213,785]),
  editorSize:[1508,865],dialogSize:[724,596],submit:[672,560]};
function png(w,h) {
  const a=new Uint8Array(24);a.set([137,80,78,71,13,10,26,10]);
  const v=new DataView(a.buffer);v.setUint32(16,w);v.setUint32(20,h);return a;
}
test('PNG and baseline/progressive JPEG screenshot sizes are read without decoding',()=>{
  assert.deepEqual(screenshotSize(png(1508,865)),[1508,865]);
  const foreign=vm.runInNewContext('new Uint8Array(24)');foreign.set(png(1508,865));
  assert.deepEqual(screenshotSize(foreign),[1508,865]);
  for(const marker of [192,194]) {
    const jpeg=Uint8Array.from([255,216,255,224,0,4,74,70,255,marker,0,11,8,3,97,5,228,1,1,17,0]);
    assert.deepEqual(screenshotSize(jpeg),[1508,865]);
    assert.throws(()=>screenshotSize(jpeg.slice(0,13)),/format/);
  }
});
function fixture(tail, wrongPath=false) {
  const states=[editor,'0 菜单\n1 替换片段, Secondary Actions: Raise',
    '0 表单\n2 按钮 导入, ID: OKButton','3 文本栏 ID: PathTextField',
    `3 文本栏 Value: ${source}, ID: PathTextField`,
    `4 文本栏 (selected, settable) URL: file:///photos/${encodeURIComponent('客片')}/${wrongPath?'other':'123'}/1.jpg\n2 按钮 导入, ID: OKButton`,
    dialog,...tail];
  const clicks=[],keys=[];let current='';
  return {clicks,keys,app:{
    getAXState:async()=>{current=states.shift()??dialog;return current;},
    getScreenshot:async()=>current.includes('MainWindow')?png(1508,865):png(724,596),
    click:async(...args)=>{clicks.push(args)},pressKey:async(k)=>{keys.push(k)},setValue:async()=>{}
  }};
}
test('normal replacement observes current state and submits once',async()=>{
  const f=fixture([editor]);const result=await createReplacer({app:f.app,layout})({batch,row});
  assert.equal(result.source,source);assert.equal(result.submitted,true);
  assert.equal(f.clicks.filter(c=>Array.isArray(c[0])&&c[0][0]===672).length,1);
});
test('delayed AX completion causes no second submit',async()=>{
  const f=fixture([dialog,editor]);
  await createReplacer({app:f.app,layout})({batch,row});
  assert.equal(f.clicks.filter(c=>Array.isArray(c[0])&&c[0][0]===672).length,1);
});
test('remaining known dialog is retried only once then stops if unresolved',async()=>{
  for(const final of [editor,dialog]) {
    const f=fixture([dialog,dialog,final]);const events=[];
    const action=createReplacer({app:f.app,layout,onEvent:e=>events.push(e)})({batch,row});
    if(final===editor) await action;else await assert.rejects(action,/unconfirmed/);
    assert.equal(f.clicks.filter(c=>Array.isArray(c[0])&&c[0][0]===672).length,2);
    assert.equal(events.filter(e=>e.type==='submit-retry').length,1);
  }
});
test('exact visible file entry skips folder navigation; selected URL is still verified',async()=>{
  const url='file:///photos/'+encodeURIComponent('客片')+'/123/1.jpg';
  const states=[editor,'0 菜单\n1 替换片段, Secondary Actions: Raise',
    `4 文本栏 URL: ${url}\n5 文本栏 URL: file:///photos/other/123/1.jpg\n2 按钮 导入, ID: OKButton`,
    `4 文本栏 (selected, settable) URL: ${url}\n2 按钮 导入, ID: OKButton`,dialog,editor];
  let current='';const clicks=[],keys=[],events=[];
  const app={getAXState:async()=>current=states.shift(),
    getScreenshot:async()=>current.includes('MainWindow')?png(1508,865):png(724,596),
    click:async(x)=>clicks.push(x),pressKey:async(x)=>keys.push(x),setValue:async()=>{throw Error('No path input expected')}};
  await createReplacer({app,layout,onEvent:e=>events.push(e)})({batch,row});
  assert.ok(clicks.includes(4));assert.equal(keys.length,0);
  assert.equal(events[0].mode,'direct-file-entry');
});
test('same basename from wrong directory cannot be imported',async()=>{
  const f=fixture([editor],true);
  await assert.rejects(createReplacer({app:f.app,layout})({batch,row}),/full path/);
  assert.equal(f.clicks.some(c=>c[0]===2),false);
});
test('unverified or changed layout stops before coordinate input',async()=>{
  assert.throws(()=>createReplacer({app:{},layout:{...layout,verified:false}}),/verified/);
  const f=fixture([editor]);f.app.getScreenshot=async()=>png(1168,744);
  await assert.rejects(createReplacer({app:f.app,layout})({batch,row}),/Layout changed/);
  assert.equal(f.clicks.length,0);
});
test('transient dialog can settle on third observation without a premature click',async()=>{
  const f=fixture([dialog,dialog,editor]);let shots=0;const events=[];
  f.app.getScreenshot=async()=>{shots++;return shots===1?png(1508,865):shots<4?png(396,326):png(724,596)};
  const result=await createReplacer({app:f.app,layout,onEvent:e=>events.push(e)})({batch,row});
  assert.equal(result.submitted,true);assert.equal(shots,4);
  assert.equal(events.filter(e=>e.type==='geometry-mismatch').length,2);
  assert.ok(events.find(e=>e.type==='slot-timing').ms.import_dialog>=0);
});
test('persistent wrong geometry stops after three observations and records actual dimensions',async()=>{
  const f=fixture([dialog,dialog,dialog]);let shots=0;const events=[];
  f.app.getScreenshot=async()=>++shots===1?png(1508,865):png(396,326);
  await assert.rejects(createReplacer({app:f.app,layout,onEvent:e=>events.push(e)})({batch,row}),error=>{
    assert.match(error.message,/replace-dialog.*724x596.*396x326.*reads 3/);
    assert.equal(error.metrics.phase,'import_dialog');return true;
  });
  assert.equal(shots,4);assert.equal(events.filter(e=>e.type==='geometry-mismatch').length,3);
  assert.equal(events.find(e=>e.type==='slot-timing').ok,false);
  assert.equal(f.clicks.filter(c=>Array.isArray(c[0])&&c[0][0]===672).length,0);
});
test('geometry observation time budget stops before the read-count limit',async()=>{
  const f=fixture([dialog,dialog,dialog]);let shots=0,clock=0;
  f.app.getScreenshot=async()=>{if(++shots===1)return png(1508,865);clock+=800;return png(396,326)};
  await assert.rejects(createReplacer({app:f.app,layout,now:()=>clock})({batch,row}),/reads 2/);
  assert.equal(shots,3);
});

test('current editor export marker is accepted before and after replacement',async()=>{
  const f=fixture([editor]);
  const original=f.app.getAXState;
  f.app.getAXState=async()=> (await original()).replaceAll('MainWindowTitleBarReviewBtn','MainWindowTitleBarExportBtn');
  assert.equal((await createReplacer({app:f.app,layout})({batch,row})).submitted,true);
});
