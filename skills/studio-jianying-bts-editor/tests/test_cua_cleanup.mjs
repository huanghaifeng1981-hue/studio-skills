import test from 'node:test';
import assert from 'node:assert/strict';
import {createCleanupAdapter} from '../scripts/cua_cleanup.mjs';

function fixture({master='母版',listed='HomePageDraftTitle:母版-副本 HomePageDraftTitle:母版',after='HomePageDraftTitle:母版'}={}) {
 const clicks=[];let step=0;
 const png=new Uint8Array(24);png.set([137,80,78,71,13,10,26,10]);
 const view=new DataView(png.buffer);view.setUint32(16,1168);view.setUint32(20,744);
 const app={getScreenshot:async()=>png,
  getAXState:async()=>step===0?listed:step===1?'1 删除, Secondary Actions: Raise':step===2?'最近删除 保留30天':after,
  click:async target=>{clicks.push(target);step++;}};
 const batch={output:'/photos/out.mp4'},state={status:'exported-awaiting-user-review',draft_name:'母版-副本'};
 const run=createCleanupAdapter({app,stat:async()=>({size:123}),
  verifyHome:async()=>({draft_name:state.draft_name,master_name:master,draftPoint:[295,620],size:[1168,744]}),
  verifyDeleteDialog:async()=>({confirmed:true,submit:[137,170]})});
 return {run:()=>run({batch,state}),clicks};
}
test('cleanup refuses deleting the master or proceeding without a separately listed master',async()=>{
 for(const options of [{master:'母版-副本'},{listed:'HomePageDraftTitle:母版-副本'}]) {
  const f=fixture(options);await assert.rejects(f.run());assert.equal(f.clicks.length,0);
 }
});
test('cleanup confirms recoverable deletion, exact draft removal, and retained master',async()=>{
 const f=fixture();const result=await f.run();assert.equal(result.cleaned,true);assert.equal(f.clicks.length,3);
 const bad=fixture({after:'HomePageDraftTitle:母版-其他副本'});
 await assert.rejects(bad.run(),/Master not confirmed/);
});
