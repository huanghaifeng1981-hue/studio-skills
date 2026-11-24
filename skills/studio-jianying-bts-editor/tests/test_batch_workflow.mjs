import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createBatch,initialState} from '../scripts/batch_session.mjs';
import {advanceCheckpoint} from '../scripts/checkpoint.mjs';
import {advanceWorkflow,assertReadyToExport} from '../scripts/batch_workflow.mjs';
const batch=createBatch({source:'/photos/A',output:'/photos/A/out.mp4',selected:Array.from({length:22},(_,i)=>({slot:i+1,source:`/photos/A/123/${i}.jpg`}))});
test('21 slots and pending slot cannot export',()=>{
 const s=initialState(batch,'copy');s.completed_slots=Array.from({length:21},(_,i)=>i+1);
 assert.throws(()=>assertReadyToExport(batch,s));s.completed_slots.push(22);s.pending_slot=22;
 assert.throws(()=>assertReadyToExport(batch,s));
});
test('first replacement segment retains metrics after checkpoint reload',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'workflow-'));const file=path.join(dir,'state.json');
 try {
  await advanceCheckpoint(file,initialState(batch,'copy'));
  const s=await advanceWorkflow({file,batch,replace:async({row})=>({...row,submitted:true})});
  assert.equal(s.completed_slots.length,6);assert.equal(s.phase_metrics[0].phase,'replace');assert.equal(s.workflow_phase,'replace');
  assert.deepEqual(JSON.parse(await readFile(file,'utf8')),s);
  assert.equal(s.phase_metrics.length,1);
 }finally{await rm(dir,{recursive:true,force:true});}
});
test('fixed phases require export proof before cleanup; completed phase is not replayed',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'workflow-'));const file=path.join(dir,'state.json');
 try {
  let s=initialState(batch,'copy');s.completed_slots=Array.from({length:22},(_,i)=>i+1);await advanceCheckpoint(file,s);
  const calls=[];const adapters={export:async()=>{calls.push('export');return {output:batch.output,submitted:true};},'confirm-export':async()=>({output:batch.output,completed:true,bytes:123}),cleanup:async()=>{calls.push('cleanup');return {output:batch.output,draft_name:'copy',cleaned:true};}};
  for(let i=0;i<5;i++)s=await advanceWorkflow({file,batch,adapters});
  assert.equal(s.workflow_phase,'complete');assert.deepEqual(calls,['export','cleanup']);
  assert.equal(s.status,'exported-awaiting-user-review');assert.equal(s.phase_metrics.length,4);
 } finally {await rm(dir,{recursive:true,force:true});}
});
test('uncertain export stays pending and blocks repeat submission',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'workflow-'));const file=path.join(dir,'state.json');
 try {
  const s=initialState(batch,'copy');s.completed_slots=Array.from({length:22},(_,i)=>i+1);s.workflow_phase='export';await advanceCheckpoint(file,s);
  await assert.rejects(advanceWorkflow({file,batch,adapters:{export:async()=>{throw Error('timeout');}}}),/timeout/);
  assert.equal(JSON.parse(await readFile(file,'utf8')).phase_pending,'export');
  await assert.rejects(advanceWorkflow({file,batch}),/Reconcile/);
 } finally {await rm(dir,{recursive:true,force:true});}
});
test('workflow forwards smaller budgets and slot limits before UI input',async()=>{
 const dir=await mkdtemp(path.join(os.tmpdir(),'workflow-'));const file=path.join(dir,'state.json');
 try {
  await advanceCheckpoint(file,initialState(batch,'copy'));
  let calls=0;const replace=async({row})=>{calls++;return {...row,submitted:true}};
  let s=await advanceWorkflow({file,batch,replace,budgetMs:1});
  assert.equal(calls,0);assert.equal(s.completed_slots.length,0);
  assert.equal(s.next_step,'adjust-segment-budget');assert.match(s.blocker,/budget too small/);
  s=await advanceWorkflow({file,batch,replace,maxSlots:2});
  assert.equal(calls,2);assert.equal(s.completed_slots.length,2);
  await assert.rejects(advanceWorkflow({file,batch,replace,budgetMs:60000}),/budget/);
  assert.equal(calls,2);
 }finally{await rm(dir,{recursive:true,force:true});}
});
