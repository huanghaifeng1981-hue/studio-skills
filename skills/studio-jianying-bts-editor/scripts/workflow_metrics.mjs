// Read-only timing summary; does not inspect media or drive the editor.
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';

export function summarizeWorkflow(state) {
 const segments=state.segments??[];
 const duration=(a,b)=>Number.isFinite(a)&&Number.isFinite(b)&&b>=a?b-a:null;
 const elapsed=segments.map(s=>duration(s.start,s.end));
 const gaps=segments.slice(1).map((s,i)=>duration(segments[i].end,s.start));
 const sum=values=>values.every(v=>v!==null)?values.reduce((a,b)=>a+b,0):null;
 const samples=(state.slot_metrics??[]).map(s=>s.total).filter(v=>Number.isFinite(v)&&v>=0);
 return {
  batch_id:state.batch_id,output:state.output_path,status:state.status,
  cleanup_status:state.cleanup_status??'pending',
  completed_slots:state.completed_slots?.length??0,
  segment_slots:segments.map(s=>s.slots?.length??0),
  segment_execution_ms:elapsed.length?sum(elapsed):null,
  segment_gaps_ms:gaps,segment_gap_total_ms:gaps.length?sum(gaps):null,
  measured_slots:samples.length,
  mean_slot_ms:samples.length?samples.reduce((a,b)=>a+b,0)/samples.length:null,
  export_observed_ms:duration(state.test_started,state.export_observed_at),
  total_observed_ms:duration(state.test_started,state.test_finished_at),
  phase_metrics:state.phase_metrics??[],
  note:'Observed timings only; missing data stays null. No media quality verification.'
 };
}

if(typeof process!=='undefined' && process.argv[1] &&
   import.meta.url===pathToFileURL(process.argv[1]).href) {
 try {
  const files=process.argv.slice(2);
  if(!files.length)throw Error('Usage: node workflow_metrics.mjs STATE.json [STATE.json ...]');
  const summaries=await Promise.all(files.map(async file=>({file,
    ...summarizeWorkflow(JSON.parse(await readFile(file,'utf8')))})));
  console.log(JSON.stringify(summaries,null,2));
 }catch(error){console.error(error.message);process.exitCode=1;}
}
