import test from 'node:test';
import assert from 'node:assert/strict';
import {createBatch, selectedFileMatches, initialState, runSegment} from '../scripts/batch_session.mjs';
const make = (name) => createBatch({source: `/photos/${name}`, output: `/photos/${name}/out.mp4`,
  selected: Array.from({length:22}, (_, i) => ({slot:i+1, source:`/photos/${name}/123/${i+1}.jpg`}))});

test('batch switch passes current source; previous batch cannot resume into next', async () => {
  const a = make('A'), b = make('B');
  await assert.rejects(runSegment({batch:b, state:initialState(a,'copy'), replace:()=>{throw Error('unexpected')}}), /changed/);
  const seen = [];
  const state = await runSegment({batch:b, state:initialState(b,'copy'), maxSlots:2,
    replace:async ({batch,row}) => {seen.push(row.source);assert.equal(batch.id,b.id);
      return {source:row.source, slot:row.slot, submitted:true};}});
  assert.deepEqual(seen, ['/photos/B/123/1.jpg','/photos/B/123/2.jpg']);
  assert.deepEqual(state.completed_slots,[1,2]);
  assert.throws(()=>{b.selected[0].source='/photos/A/123/1.jpg'}, TypeError);
});
test('seven-second slots fit six per segment, but prior shorter reservations remain binding',async()=>{
  const batch=make('six');let clock=0;
  const replace=async({row})=>{clock+=7000;return {...row,submitted:true,timings_ms:{total:7000}}};
  const state=await runSegment({batch,state:initialState(batch,'copy'),replace,now:()=>clock});
  assert.equal(state.completed_slots.length,6);assert.equal(clock,42000);
  assert.equal(state.slot_metrics.length,6);
  const reserved={...initialState(batch,'copy'),inflight_slots:[1,2]};
  const short=await runSegment({batch,state:reserved,replace,now:()=>clock});
  assert.deepEqual(short.completed_slots,[1,2]);
  await assert.rejects(runSegment({batch,state:{...reserved,inflight_slots:[]},replace}),/reservation/);
  await assert.rejects(runSegment({batch,state:reserved,replace,maxSlots:7}),/budget/);
});
test('same basename in another directory and pending navigation are rejected', () => {
  const expected='/photos/新 客片/123/1.jpg';
  const ax='97 文本栏 (selected, settable) Value: 1.jpg, URL: file:///photos/'+encodeURIComponent('新 客片')+'/123/1.jpg';
  assert.equal(selectedFileMatches(ax,expected),true);
  assert.equal(selectedFileMatches(ax,'/photos/old/123/1.jpg'),false);
  assert.equal(selectedFileMatches(ax+'\n PathTextField',expected),false);
});
test('unconfirmed submission stays pending and cannot replay automatically', async () => {
  const b=make('A'), original=initialState(b,'copy');
  const s=await runSegment({batch:b,state:original,replace:async()=>({submitted:false})});
  assert.deepEqual(s.completed_slots,[]);assert.equal(s.pending_slot,1);
  assert.deepEqual(original.completed_slots,[]);
  await assert.rejects(runSegment({batch:b,state:s,replace:async()=>{}}),/pending/);
});
test('observed latency limits segment and completed run does no extra input', async () => {
  const b=make('A');let clock=0,calls=0;
  const replace=async ({row})=>{clock+=9000;calls++;return {...row,submitted:true}};
  const s=await runSegment({batch:b,state:initialState(b,'copy'),replace,now:()=>clock});
  assert.equal(calls,5);assert.equal(s.next_step,'replace-next-slot');
  const complete={...s,completed_slots:Array.from({length:22},(_,i)=>i+1)};
  const result=await runSegment({batch:b,state:complete,replace,now:()=>clock});
  assert.equal(calls,5);assert.equal(result.next_step,'export');
});
test('measured 9.6-second slots need five segments with 52s budget, versus six with 45s',async()=>{
  const batch=make('measured');
  for(const [budgetMs,expected] of [[45000,6],[52000,5]]) {
    let clock=0,segments=0,state=initialState(batch,'copy');
    while(state.completed_slots.length<22) {
      const start=clock;
      state=await runSegment({batch,state,budgetMs,now:()=>clock,
        replace:async({row})=>{clock+=9600;return {...row,submitted:true}}});
      assert.ok(clock-start<=budgetMs);
      segments++;
    }
    assert.equal(segments,expected);
  }
});

test('explicit materials directory accepts selected photos and rejects other folders',()=>{
  const input={source:'/photos/A',output:'/photos/A/out.mp4',materials:'/photos/A/已选',
    selected:Array.from({length:22},(_,i)=>({slot:i+1,source:`/photos/A/已选/${i+1}.jpg`}))};
  assert.equal(createBatch(input).selected.length,22);
  assert.throws(()=>createBatch({...input,materials:'/photos/B/已选'}),/mapping/);
});
