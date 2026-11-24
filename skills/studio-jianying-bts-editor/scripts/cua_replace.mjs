// Called inside the authorized cua_repl runtime; no separate input technology.
import {selectedFileMatches,fileEntryIndex} from './batch_session.mjs';

export function axIndex(ax, re) {
  const line = ax.split('\n').find(l => /^\s*\d+ /.test(l) && re.test(l));
  if (!line) throw new Error(`Missing UI control: ${re}`);
  return Number(line.match(/^\s*(\d+)/)[1]);
}

export function screenshotSize(bytes) {
  if (!ArrayBuffer.isView(bytes) || bytes.BYTES_PER_ELEMENT !== 1) throw new Error('Invalid screenshot bytes');
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length >= 24 && [137,80,78,71,13,10,26,10].every((v,i)=>bytes[i]===v)) {
    return [view.getUint32(16), view.getUint32(20)];
  }
  if (bytes[0] === 255 && bytes[1] === 216) {
    let p=2;
    while (p+3<bytes.length) {
      if (bytes[p++]!==255) break;
      while(bytes[p]===255) p++;
      const marker=bytes[p++];
      if (marker===217 || marker===218) break;
      if (marker===1 || marker>=208 && marker<=215) continue;
      if (p+2>bytes.length) break;
      const size=view.getUint16(p);
      if (size<2 || p+size>bytes.length) break;
      if ([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker) && size>=8) {
        return [view.getUint16(p+5),view.getUint16(p+3)];
      }
      p+=size;
    }
  }
  throw new Error('Screenshot format requires manual layout observation');
}

// layout must come from screenshots inspected in the current session.
// No persisted coordinates are trusted merely because a previous run worked.
export function createReplacer({app, layout, onEvent = () => {}, now=()=>performance.now(),
  geometryBudgetMs=1500, maxGeometryReads=3}) {
  if (!layout?.verified || layout.timeline?.length !== 22 ||
      !layout.editorSize || !layout.dialogSize || !layout.submit) {
    throw new Error('Current editor/dialog layout must be verified first');
  }
  const config = structuredClone(layout);
  if (!(geometryBudgetMs>0) || !Number.isInteger(maxGeometryReads) ||
      maxGeometryReads<1 || maxGeometryReads>3) throw new Error('Invalid geometry recovery limit');
  const observe = () => app.getAXState({emit:false, disableDiffing:true});
  const editor = a => (a.includes('MainWindowTitleBarReviewBtn') || a.includes('MainWindowTitleBarExportBtn'));
  const dialog = a => a.includes('对话框') && !a.includes('OKButton') && !editor(a);
  async function geometry(size, allowTransition = false, phase='editor',slot) {
    const start=now();let actual, reads=0;
    do {
      actual=screenshotSize(await app.getScreenshot({emit:false}));reads++;
      if (actual.every((v,i)=>v===size[i])) return;
      onEvent({type:'geometry-mismatch',slot,phase,expected:size,actual,reads,elapsed_ms:now()-start});
      if (!allowTransition || reads>=maxGeometryReads || now()-start>=geometryBudgetMs) break;
      // Only re-observe the known replacement dialog, never click while unstable.
      if (!dialog(await observe())) throw new Error(`Unexpected window during ${phase} geometry recovery`);
    } while (now()-start<geometryBudgetMs);
    throw new Error(`Layout changed at ${phase}: expected ${size.join('x')}, observed ${actual.join('x')}, reads ${reads}`);
  }
  return async function replace({batch, row}) {
    if (batch.selected[row.slot-1]?.source !== row.source) throw new Error('Wrong batch row');
    const timings={};const start=now();let phase='open_chooser',phaseStart=start,ok=false,failure;
    const nextPhase=name=>{const t=now();timings[phase]=(timings[phase]??0)+t-phaseStart;phase=name;phaseStart=t;};
    try {
    let a = await observe();
    if (!editor(a)) throw new Error('Expected editor before replacement');
    await geometry(config.editorSize,false,'editor',row.slot);
    await app.click(config.timeline[row.slot-1], {mouseButton:'right'});
    a = await observe();
    await app.click(axIndex(a,/替换片段, Secondary/));
    a = await observe();
    if (!a.includes('OKButton')) throw new Error('Expected file chooser');
    nextPhase('select_file');
    let mode='already-selected';
    if (!selectedFileMatches(a,row.source)) {
      const target=fileEntryIndex(a,row.source);
      if (target !== null) {
        await app.click(target);
        a = await observe();
        mode='direct-file-entry';
      }
      if (!selectedFileMatches(a,row.source)) {
        await app.pressKey('super+shift+g');
        a = await observe();
        await app.setValue(axIndex(a,/PathTextField/),row.source);
        a = await observe();
        if (!a.split('\n').some(l=>l.includes('ID: PathTextField') &&
            l.includes(`Value: ${row.source},`))) throw new Error('Path field mismatch');
        await app.pressKey('Return');
        a = await observe();
        mode='full-path-navigation';
      }
    }
    if (!selectedFileMatches(a,row.source)) throw new Error('Selected full path mismatch');
    onEvent({type:'file-selection',slot:row.slot,mode});
    nextPhase('import_dialog');
    await app.click(axIndex(a,/按钮 导入,/));
    a = await observe();
    if (!dialog(a)) throw new Error('Unexpected replacement window');
    await geometry(config.dialogSize,true,'replace-dialog',row.slot);
    nextPhase('submit');
    await app.click(config.submit);
    a = await observe();
    if (!editor(a)) {
      if (!dialog(a)) throw new Error('Unexpected window after submit');
      // A delayed AX update must not cause a duplicate submit into the editor.
      await geometry(config.dialogSize,true,'after-submit',row.slot);
      a = await observe();
      if (!editor(a)) {
        if (!dialog(a)) throw new Error('Unknown dialog after submit');
        await geometry(config.dialogSize,false,'retry-submit',row.slot);
        await app.click(config.submit);
        a = await observe();
        onEvent({type:'submit-retry',slot:row.slot});
      }
    }
    if (!editor(a)) throw new Error('Replacement unconfirmed after bounded recovery');
    ok=true;
    return {slot:row.slot,source:row.source,submitted:true,timings_ms:timings};
    } catch(error) {failure=error;throw error;}
    finally {
      const end=now();timings[phase]=(timings[phase]??0)+end-phaseStart;timings.total=end-start;
      if (failure && typeof failure==='object') failure.metrics={...timings,phase};
      onEvent({type:'slot-timing',slot:row.slot,ok,phase,ms:{...timings}});
    }
  };
}
