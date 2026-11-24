import {axIndex,screenshotSize} from './cua_replace.mjs';

// verifyHome observes the current screenshot and confirms the exact task draft.
// It must return current-session coordinates, not infer a draft from '副本' alone.
export function createCleanupAdapter({app,verifyHome,verifyDeleteDialog,stat}) {
 if([verifyHome,verifyDeleteDialog,stat].some(f=>typeof f!=='function'))throw Error('Observation adapters required');
 const observe=()=>app.getAXState({emit:false,disableDiffing:true});
 const hasDraft=(a,name)=>a.split('HomePageDraftTitle:').slice(1)
   .some(part=>part.split('\n')[0].trim()===name);
 return async({batch,state})=>{
  if(state.status!=='exported-awaiting-user-review' || !(await stat(batch.output)).size)
   throw Error('Export must be confirmed before cleanup');
  const home=await verifyHome({batch,state});
  if(home?.draft_name!==state.draft_name || !home.draftPoint || !home.size ||
    typeof home.master_name!=='string' || !home.master_name || home.master_name===state.draft_name)
    throw Error('Exact task draft and distinct master unconfirmed');
  const before=await observe();
  if(!hasDraft(before,state.draft_name) || !hasDraft(before,home.master_name))
    throw Error('Draft and master must be listed before cleanup');
  const size=screenshotSize(await app.getScreenshot({emit:false}));
  if(size.some((v,i)=>v!==home.size[i]))throw Error('Home layout changed');
  await app.click(home.draftPoint,{mouseButton:'right'});let a=await observe();
  await app.click(axIndex(a,/删除, Secondary/));a=await observe();
  if(!a.includes('保留30天'))throw Error('Expected recoverable deletion');
  const proof=await verifyDeleteDialog({batch,state});
  if(!proof?.confirmed || !proof.submit)throw Error('Deletion dialog unconfirmed');
  await app.click(proof.submit);a=await observe();
  if(hasDraft(a,state.draft_name))throw Error('Draft still listed');
  if(!hasDraft(a,home.master_name))throw Error('Master not confirmed');
  if(!(await stat(batch.output)).size)throw Error('Output missing after cleanup');
  return {output:batch.output,draft_name:state.draft_name,cleaned:true};
 };
}
