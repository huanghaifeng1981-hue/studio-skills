"""Read-only contact sheets and source inventory; requires Pillow."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageOps, ImageDraw

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve(strict=True)
    output = Path(args.output).resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Preview output must be outside source directory')
    output.mkdir(exist_ok=False)
    extensions = {'.jpg','.jpeg','.png','.webp','.tif','.tiff'}
    paths = sorted(p for p in source.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() in extensions)
    inventory = []
    for page in range((len(paths)+29)//30):
        sheet = Image.new('RGB', (1500,1800), 'white')
        draw = ImageDraw.Draw(sheet)
        for i,path in enumerate(paths[page*30:(page+1)*30]):
            with Image.open(path) as raw:
                picture = ImageOps.exif_transpose(raw).convert('RGB')
                w,h = picture.size
                picture.thumbnail((290,270))
            x,y=i%5*300,i//5*300
            sheet.paste(picture,(x+(300-picture.width)//2,y))
            draw.text((x+5,y+277),path.name,fill='black')
            hsh=hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda:stream.read(1024*1024),b''): hsh.update(block)
            inventory.append(dict(filename=path.name,sha256=hsh.hexdigest(),size=path.stat().st_size,
                                  width=w,height=h,orientation='landscape' if w>h else 'portrait' if h>w else 'square'))
        sheet.save(output/f'preview-{page+1}.jpg')
    (output/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2))
    print('Photos:',len(inventory))
