import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {advanceCheckpoint,runCheckpointedSegment} from '../scripts/checkpoint.mjs';
import {createBatch,initialState} from '../scripts/batch_session.mjs';
test('atomic checkpoint commits progress and reserves next segment; pending never advances',async()=>{
  const dir=await mkdtemp(path.join(process.env.TEST_TMP_ROOT??os.tmpdir(),'checkpoint-'));
  const file=path.join(dir,'state.json');
  try {
    const base={batch_id:'A',source:'/photos/A',output_path:'/photos/A/out.mp4',draft_name:'copy',completed_slots:[],pending_slot:null};
    let s=await advanceCheckpoint(file,base,5);assert.deepEqual(s.inflight_slots,[1,2,3,4,5]);
    s=await advanceCheckpoint(file,{...base,completed_slots:[1,2,3,4,5]},5);
    assert.deepEqual(s.inflight_slots,[6,7,8,9,10]);
    s=await advanceCheckpoint(file,{...s,pending_slot:6});assert.deepEqual(s.inflight_slots,[]);
    const before=await readFile(file,'utf8');
    await assert.rejects(advanceCheckpoint(file,{...s,batch_id:'B'}),/identity/);
    await assert.rejects(advanceCheckpoint(file,base),/regressed/);
    assert.equal(await readFile(file,'utf8'),before);
    s=await advanceCheckpoint(file,{...s,pending_slot:null,completed_slots:Array.from({length:22},(_,i)=>i+1)});
    assert.deepEqual(s.inflight_slots,[]);
  } finally {await rm(dir,{recursive:true,force:true});}
});
test('checkpointed run preserves samples and complete result before next segment',async()=>{
  const dir=await mkdtemp(path.join(process.env.TEST_TMP_ROOT??os.tmpdir(),'checkpoint-run-'));
  try {
    const file=path.join(dir,'state.json');const batch=createBatch({source:'/photos/A',output:'/photos/A/out.mp4',
      selected:Array.from({length:22},(_,i)=>({slot:i+1,source:`/photos/A/123/${i+1}.jpg`}))});
    await advanceCheckpoint(file,initialState(batch,'copy'));
    const result=await runCheckpointedSegment({file,batch,replace:async({row})=>({...row,submitted:true})});
    const saved=JSON.parse(await readFile(file,'utf8'));
    assert.equal(saved.samples_ms.length,6);assert.deepEqual(saved,result);
    assert.deepEqual(saved.completed_slots,[1,2,3,4,5,6]);assert.deepEqual(saved.inflight_slots,[7,8,9,10,11,12]);
    assert.deepEqual(saved.segments[0].slots,[1,2,3,4,5,6]);
  }finally{await rm(dir,{recursive:true,force:true});}
});
