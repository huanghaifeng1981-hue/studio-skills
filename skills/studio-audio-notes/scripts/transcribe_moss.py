from pathlib import Path
import argparse
import json
import re
import torch
from audio_io import load_audio
from transformers import AutoProcessor,AutoModelForCausalLM

parser=argparse.ArgumentParser(description='Offline MOSS transcription; decode m4a/WAV locally to mono 16kHz.')
parser.add_argument('audio',type=Path)
parser.add_argument('--model',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--max-tokens',type=int,default=8192)
parser.add_argument('--vocabulary',type=Path,help='External JSON vocabulary with a terms list of text entries')
parser.add_argument('--scene',choices=['customer','meeting','training','unspecified'],default='unspecified',help='Output scenario; transcript generation is shared')
args=parser.parse_args()
audio,sr=load_audio(args.audio)
if sr!=16000 or audio.ndim!=1 or len(audio)==0:
    parser.error('Expected nonempty mono 16kHz WAV; convert a copy first')
if not args.model.is_dir():
    parser.error('Local model directory does not exist')
args.output.mkdir(parents=True,exist_ok=False)
torch.set_num_threads(4)
root=args.model.resolve()
proc=AutoProcessor.from_pretrained(str(root),trust_remote_code=True,local_files_only=True)
model=AutoModelForCausalLM.from_pretrained(str(root),trust_remote_code=True,local_files_only=True,dtype=torch.float32,attn_implementation='sdpa').to('cpu').eval()
prompt='请将音频转写为文本，每一段需以起始时间戳和说话人编号（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，并在段末标注结束时间戳，以清晰标明该段语音范围。'
if args.vocabulary:
    vocabulary=json.loads(args.vocabulary.read_text(encoding='utf-8'))
    terms=[entry['text'] for entry in vocabulary['terms']]
    if not all(isinstance(term,str) and term.strip() for term in terms):
        parser.error('Vocabulary terms must be nonempty strings')
    prompt+='热词提示：'+', '.join(terms)+'。热词仅供参考，不要将不匹配的语音强行替换为热词。'
messages=[{'role':'user','content':[{'type':'audio','audio':'local-sample'},{'type':'text','text':prompt}]}]
text=proc.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
inputs=proc(text=[text],audio=[audio],return_tensors='pt',sampling_rate=sr)
with torch.inference_mode():
 ids=model.generate(**inputs,max_new_tokens=args.max_tokens,do_sample=False)
result=proc.batch_decode(ids[:,inputs.input_ids.shape[1]:],skip_special_tokens=True)[0]
out=args.output
(out/'raw.txt').write_text(result,encoding='utf-8')
segments=[{'start':float(a),'end':float(b),'speaker':sp,'text':t}
          for a,sp,t,b in re.findall(r'\[([\d.]+)\]\[(S\d+)\](.*?)\[([\d.]+)\]',result)]
(out/'segments.json').write_text(json.dumps(segments,ensure_ascii=False,indent=2),encoding='utf-8')
(out/'transcript.txt').write_text('\n'.join(f"[{x['start']:.2f}–{x['end']:.2f}秒][{x['speaker']}] {x['text']}" for x in segments),encoding='utf-8')
new_tokens=ids.shape[1]-inputs.input_ids.shape[1]
metadata={'skill':'studio-audio-notes@26.11.15','audio':str(args.audio.resolve()),'model':str(root),'scene':args.scene,
          'duration':len(audio)/sr,'generated_tokens':new_tokens,'token_limit':args.max_tokens,
          'possibly_truncated':new_tokens>=args.max_tokens,'quality_status':'needs_human_review',
          'segments':len(segments),'status':'complete' if segments and new_tokens<args.max_tokens else 'needs_review'}
(out/'metadata.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(metadata,ensure_ascii=False))
