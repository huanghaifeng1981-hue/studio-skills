import {readFile, writeFile, rename, rm} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {runSegment} from './batch_session.mjs';

// Only use when this runtime has explicit write access to the checkpoint path.
export async function runCheckpointedSegment({file,batch,replace,maxSlots=6,budgetMs=52000,
  finalize=result=>result}) {
  const state=JSON.parse(await readFile(file,'utf8'));
  const first=state.completed_slots.length+1;
  const reserved=state.inflight_slots??[];
  if (state.pending_slot!==null || reserved.some((s,i)=>s!==first+i) ||
      (first<=22 && reserved.length===0)) throw new Error('No reconciled segment reservation');
  const started=Date.now();
  const result=await runSegment({batch,state,replace,maxSlots:Math.min(maxSlots,reserved.length||1),budgetMs});
  const ended=Date.now();
  result.segments=[...(state.segments??[]),{start:started,end:ended,
    slots:result.completed_slots.slice(state.completed_slots.length),pending_slot:result.pending_slot}];
  return advanceCheckpoint(file,await finalize(result),maxSlots);
}

// Commit the observed result AND reserve the next segment in one atomic write.
// Use a separate state path per batch. Never drive UI if this operation fails.
export async function advanceCheckpoint(file, result, maxSlots = 6) {
  if (!result.batch_id || !Number.isInteger(maxSlots) || maxSlots < 1 || maxSlots > 6 ||
      !Array.isArray(result.completed_slots) || result.completed_slots.length > 22 ||
      result.completed_slots.some((slot,i)=>slot!==i+1) ||
      !(result.pending_slot === null || result.pending_slot === result.completed_slots.length+1)) {
    throw new Error('Invalid checkpoint result');
  }
  let prior;
  try {prior = JSON.parse(await readFile(file,'utf8'));}
  catch(e) {if(e.code !== 'ENOENT') throw e;}
  if (prior && (prior.batch_id!==result.batch_id || prior.source!==result.source ||
      prior.output_path!==result.output_path || prior.draft_name!==result.draft_name ||
      prior.completed_slots.length>result.completed_slots.length)) {
    throw new Error('Checkpoint identity changed or progress regressed');
  }
  const first = result.completed_slots.length+1;
  const next = {...result, inflight_slots:result.pending_slot===null
    ? Array.from({length:Math.min(maxSlots,23-first)},(_,i)=>first+i) : [],
    checkpoint_at:new Date().toISOString()};
  const tmp = `${file}.${randomUUID()}.tmp`;
  try {
    await writeFile(tmp,JSON.stringify(next,null,2),{flag:'wx'});
    await rename(tmp,file);
  } finally {await rm(tmp,{force:true});}
  return next;
}

if (typeof process !== 'undefined' && process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [file] = process.argv.slice(2);
    if (!file) throw new Error('Usage: checkpoint.mjs STATE_PATH < result.json');
    let input=''; for await (const chunk of process.stdin) input+=chunk;
    const state = await advanceCheckpoint(file,JSON.parse(input));
    console.log(JSON.stringify({completed:state.completed_slots.length,
      pending:state.pending_slot,inflight_slots:state.inflight_slots}));
  } catch(e) {console.error(e.message);process.exitCode=1;}
}
