"""Local operator entry point; never a network authentication boundary."""
import argparse
import json
import sys
from pathlib import Path
from selection_tool import SelectionTool, Assessment, digest

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--organization', required=True)
    p.add_argument('--count', type=int, required=True)
    p.add_argument('--assessments', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--inventory', help='Source inventory captured before visual review')
    p.add_argument('--profile', help='Explicit organization-owned aesthetic profile JSON')
    p.add_argument('--mode', choices=['best', 'worst'], default='best')
    p.add_argument('--minimum-landscape', type=int, default=0)
    p.add_argument('--minimum-portrait', type=int, default=0)
    p.add_argument('--folder', default='已选')
    p.add_argument('--copy', action='store_true')
    a = p.parse_args()
    output = Path(a.output).resolve()
    source = Path(a.source).resolve(strict=True)
    if output == source or source in output.parents:
        raise ValueError('Plan output must be outside source directory')
    if a.copy and Path(a.output + '.copy.json').exists():
        raise ValueError('Copy report already exists; reconcile prior execution')
    tool = SelectionTool(source=a.source, organization_id=a.organization)
    if a.inventory:
        inventory = json.loads(Path(a.inventory).read_text(encoding='utf-8'))
        for entry in inventory:
            path = tool.source / entry['filename']
            if (Path(entry['filename']).name != entry['filename'] or path.is_symlink()
                    or not path.is_file() or digest(path) != entry['sha256']):
                raise ValueError('Source snapshot changed; stop and review the new inputs')
    rows = [Assessment(**row) for row in json.loads(Path(a.assessments).read_text(encoding='utf-8'))]
    profile = json.loads(Path(a.profile).read_text(encoding='utf-8')) if a.profile else None
    plan = tool.execute(assessments=rows, count=a.count, mode=a.mode,
                        minimum_landscape=a.minimum_landscape,
                        minimum_portrait=a.minimum_portrait, profile=profile)
    # Persist plan before any copy; never overwrite an earlier execution record.
    with Path(a.output).open('x', encoding='utf-8') as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
    if a.copy:
        result = tool.copy(plan, folder=a.folder)
        with Path(a.output + '.copy.json').open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print('Review plan written:', a.output)
