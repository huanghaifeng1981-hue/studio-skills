import path from 'node:path';
import {axIndex,screenshotSize} from './cua_replace.mjs';

// Coordinates and dimensions must be verified from current-session screenshots.
// Opening the export panel and observing completion stay explicit UI steps.
export function createExportAdapter({app,layout,exists}) {
 if(!layout?.verified || !layout.size || !layout.title || !layout.folder || !layout.submit || typeof exists!=='function')
  throw Error('Verified export layout and existence check required');
 const config=structuredClone(layout);
 const observe=()=>app.getAXState({emit:false,disableDiffing:true});
 return async({batch})=>{
  if(await exists(batch.output))throw Error('Output already exists');
  let a=await observe();
  if(!a.includes('ExportFileNameInput'))throw Error('Expected export panel');
  const size=screenshotSize(await app.getScreenshot({emit:false}));
  if(size.some((v,i)=>v!==config.size[i]))throw Error('Export layout changed');
  const title=path.basename(batch.output,'.mp4');
  await app.click(config.title);await app.pressKey('super+a');await app.paste(title);
  a=await observe();
  if(!a.includes(`${title}.mp4`))throw Error('Export title mismatch');
  await app.click(config.folder);a=await observe();
  if(!a.includes('OKButton'))throw Error('Expected output chooser');
  await app.pressKey('super+shift+g');a=await observe();
  await app.setValue(axIndex(a,/PathTextField/),batch.source);a=await observe();
  if(!a.split('\n').some(l=>l.includes('PathTextField') && l.includes(`Value: ${batch.source},`)))
   throw Error('Output directory field mismatch');
  await app.pressKey('Return');a=await observe();
  if(a.includes('PathTextField') || !a.includes('OKButton'))throw Error('Directory navigation unconfirmed');
  // Require the chooser's full selected folder URL, not a coincidentally matching name.
  const selected=a.split('\n').some(l=>{
   if(!l.includes('(selected,'))return false;
   const url=l.match(/URL: (file:\/\/[^\s,]+)/)?.[1];
   return url && decodeURIComponent(new URL(url).pathname).replace(/\/$/,'')===batch.source;
  });
  if(!selected)throw Error('Selected output directory mismatch');
  await app.click(axIndex(a,/按钮 打开, ID: OKButton/));a=await observe();
  if(!a.split('\n').some(l=>l.includes('ExportFileNameInput') && l.includes(`Value: ${batch.output}`)))
   throw Error('Full export path mismatch');
  if(await exists(batch.output))throw Error('Output appeared before submit');
  await app.click(config.submit);await observe();
  return {output:batch.output,submitted:true};
 };
}
