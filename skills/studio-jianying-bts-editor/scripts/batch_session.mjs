// Pure orchestration helpers. Actual app input must use the authorized UI tool.
import path from 'node:path';
import {createHash} from 'node:crypto';

export function createBatch(input) {
  const source = path.resolve(input.source);
  const output = path.resolve(input.output);
  if (!path.isAbsolute(input.source) || !path.isAbsolute(input.output) ||
      path.dirname(output) !== source || !output.endsWith('.mp4')) {
    throw new Error('Invalid source/output paths');
  }
  if (input.selected?.length !== 22) throw new Error('Exactly 22 slots required');
  const selected = input.selected.map((row, i) => {
    if (row.slot !== i + 1 || !path.isAbsolute(row.source) ||
        path.dirname(path.resolve(row.source)) !== path.resolve(input.materials ?? path.join(source, '123'))) {
      throw new Error('Invalid slot/source mapping');
    }
    return Object.freeze({slot: row.slot, source: path.resolve(row.source)});
  });
  if (new Set(selected.map(r => r.source)).size !== 22) throw new Error('Duplicate sources');
  const digest = createHash('sha256').update(JSON.stringify({source, output, selected})).digest('hex');
  return Object.freeze({id: digest, source, output, selected: Object.freeze(selected)});
}

// Compare the selected row's full decoded file URL, never just its basename.
function fileUrlMatches(line, expected) {
  const value = line.match(/URL: (file:\/\/[^\s,]+)/)?.[1];
  if (!value) return false;
  try {
    const url = new URL(value);
    return (!url.hostname || url.hostname === 'localhost') && decodeURIComponent(url.pathname) === expected;
  } catch {return false;}
}

export function fileEntryIndex(ax, expected) {
  if (ax.includes('PathTextField')) return null;
  const line=ax.split('\n').find(l=>/^\s*\d+ /.test(l) && fileUrlMatches(l,expected));
  return line ? Number(line.match(/^\s*(\d+)/)[1]) : null;
}

export function selectedFileMatches(ax, expected) {
  if (ax.includes('PathTextField')) return false;
  return ax.split('\n').some(line => {
    if (!/\(selected(?:,|\))/.test(line)) return false;
    return fileUrlMatches(line,expected);
  });
}

export function initialState(batch, draftName) {
  return {batch_id: batch.id, source: batch.source, output_path: batch.output,
    draft_name: draftName, completed_slots: [], pending_slot: null,
    status: 'replacing', quality_check: 'skipped-by-user', samples_ms: []};
}

export async function runSegment({batch, state, replace, budgetMs = 52000,
  maxSlots = 6, now = () => performance.now()}) {
  if (state.batch_id !== batch.id || state.source !== batch.source ||
      state.output_path !== batch.output) throw new Error('Batch mapping changed');
  if (state.pending_slot !== null) throw new Error('Reconcile pending slot before resuming');
  const done = state.completed_slots;
  if (!Array.isArray(done) || done.length > 22 || done.some((n, i) => n !== i + 1)) {
    throw new Error('Invalid completed slots');
  }
  if (!(Number.isFinite(budgetMs) && budgetMs > 0 && budgetMs <= 52000) ||
      !Number.isInteger(maxSlots) || maxSlots < 1 || maxSlots > 6) throw new Error('Invalid budget');
  const reserved=state.inflight_slots;
  if (reserved && (!Array.isArray(reserved) || reserved.length>6 ||
      reserved.some((n,i)=>n!==done.length+i+1) || (done.length<22 && reserved.length===0))) {
    throw new Error('Invalid segment reservation');
  }
  const limit=reserved ? Math.min(maxSlots,reserved.length) : maxSlots;
  const next = structuredClone(state);
  next.blocker = null;
  const start = now();
  let count = 0;
  while (next.completed_slots.length < 22 && count < limit) {
    const samples = next.samples_ms ?? [];
    const estimate = samples.length ? Math.max(...samples.slice(-3)) * 1.25 : 15000;
    if (now() - start + estimate > budgetMs) break;
    const slot = next.completed_slots.length + 1;
    next.pending_slot = slot;
    const began = now();
    try {
      // The adapter receives the immutable batch and exact row as arguments.
      // It must observe current UI, verify full path/effects, then return proof
      // only after the replace dialog has closed and editor is visible.
      const proof = await replace({batch, row: batch.selected[slot - 1]});
      if (proof?.source !== batch.selected[slot - 1].source ||
          proof.slot !== slot || proof.submitted !== true) throw new Error('Replacement unconfirmed');
      next.samples_ms = [...samples, now() - began].slice(-6);
      if (proof.timings_ms) next.slot_metrics=[...(next.slot_metrics??[]),
        {slot,...proof.timings_ms}].slice(-22);
      next.completed_slots.push(slot);
      next.pending_slot = null;
      count++;
    } catch (error) {
      next.blocker = String(error.message ?? error);
      next.next_step = 'reconcile-pending-slot';
      next.last_segment_ms = now() - start;
      if (error.metrics) next.failed_slot_metrics={slot,...error.metrics};
      return next;
    }
  }
  next.next_step = next.completed_slots.length === 22 ? 'export' : 'replace-next-slot';
  if(count===0 && next.completed_slots.length<22) {
    next.blocker='Segment budget too small for estimated slot';
    next.next_step='adjust-segment-budget';
  }
  next.last_segment_ms = now() - start;
  return next;
}
