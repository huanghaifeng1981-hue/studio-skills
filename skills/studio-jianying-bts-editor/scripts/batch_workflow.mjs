// Run inside the authorized UI REPL. UI adapters must use current observations.
import {readFile} from 'node:fs/promises';
import {advanceCheckpoint,runCheckpointedSegment} from './checkpoint.mjs';

export function assertReadyToExport(batch,state) {
  if(state.batch_id!==batch.id || state.source!==batch.source || state.output_path!==batch.output ||
    state.pending_slot!==null || state.completed_slots?.length!==22 ||
    state.completed_slots.some((v,i)=>v!==i+1) || state.blocker) throw Error('Batch not ready to export');
}

// One bounded call per phase/segment; caller supplies immutable batch explicitly.
// Unknown outcomes stay pending on disk and require observation, never blind retry.
export async function advanceWorkflow({file,batch,replace,adapters,
  budgetMs=52000,maxSlots=6,now=()=>Date.now()}) {
  let state=JSON.parse(await readFile(file,'utf8'));
  if(state.batch_id!==batch.id || state.source!==batch.source || state.output_path!==batch.output)
    throw Error('Batch identity changed');
  if(state.phase_pending) throw Error('Reconcile pending phase before continuing');
  if(!state.workflow_phase) state.workflow_phase='replace';
  const phase=state.workflow_phase;
  if(phase==='complete') return state;
  const began=now();
  state.phase_metrics=state.phase_metrics??[];
  if(phase==='replace') {
    if(state.pending_slot!==null) throw Error('Reconcile pending slot');
    if(state.completed_slots.length<22) {
      return runCheckpointedSegment({file,batch,replace,budgetMs,maxSlots,
        finalize:result=>{
          result.workflow_phase='replace';
          if(result.pending_slot===null && !result.blocker && result.completed_slots.length===22) {
            assertReadyToExport(batch,result);result.workflow_phase='export';
          }
          const ended=now();
          result.phase_metrics=[...(result.phase_metrics??[]),{phase,start:began,end:ended,
            gap_ms:result.last_phase_end ? began-result.last_phase_end : null}];
          result.last_phase_end=ended;
          return result;
        }});
    }
    if(state.completed_slots.length===22) {
      assertReadyToExport(batch,state);
      state.workflow_phase='export';
    }
  } else {
    if(!['export','confirm-export','cleanup'].includes(phase)) throw Error('Unknown workflow phase');
    assertReadyToExport(batch,state);
    const action=adapters?.[phase];
    if(typeof action!=='function') throw Error('Missing phase adapter');
    state.phase_pending=phase;
    await advanceCheckpoint(file,state);
    const proof=await action({batch,state:structuredClone(state)});
    if(phase==='export') {
      if(proof?.output!==batch.output || proof.submitted!==true) throw Error('Export submission unconfirmed');
      state.workflow_phase='confirm-export';
    } else if(phase==='confirm-export') {
      if(proof?.output!==batch.output || proof.completed!==true || !(proof.bytes>0))
        throw Error('Export completion unconfirmed');
      state.status='exported-awaiting-user-review';state.output_bytes=proof.bytes;
      state.workflow_phase='cleanup';
    } else {
      if(proof?.draft_name!==state.draft_name || proof.cleaned!==true || proof.output!==batch.output)
        throw Error('Cleanup unconfirmed');
      state.cleanup_status='cleaned';state.workflow_phase='complete';
    }
    state.phase_pending=null;
  }
  state.workflow_phase=state.workflow_phase??phase;
  const ended=now();
  state.phase_metrics=state.phase_metrics??[];
  state.phase_metrics.push({phase,start:began,end:ended,
    gap_ms:state.last_phase_end ? began-state.last_phase_end : null});
  state.last_phase_end=ended;
  return advanceCheckpoint(file,state);
}
