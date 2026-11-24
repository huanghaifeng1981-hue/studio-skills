"""Local draft selection workflow. No source mutation or network access."""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import math
import shutil

FORMATS = {'.jpg', '.jpeg', '.png', '.webp', '.tif', '.tiff', '.heic'}


def digest(path):
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
        return h.hexdigest()


@dataclass(frozen=True)
class Assessment:
    filename: str
    score: float
    reason: str
    orientation: str = 'unknown'
    group: str = ''


class SelectionTool:
    """Compatible with existing ToolManager; authorization comes from local operator.

    Not a public service endpoint. A future Runtime must authenticate context
    before constructing this adapter; request data cannot choose its tenant/root.
    """
    name = 'studio-photo-selection'

    def __init__(self, *, source, organization_id):
        self.source = Path(source).resolve(strict=True)
        if not self.source.is_dir() or not organization_id:
            raise ValueError('Trusted source directory and organization are required')
        self.organization_id = organization_id

    def execute(self, *, assessments, count, mode='best', minimum_landscape=0,
                minimum_portrait=0, organization_id=None, profile=None):
        if organization_id is not None and organization_id != self.organization_id:
            raise ValueError('Organization mismatch')
        if profile is not None and profile.get('organization_id') != self.organization_id:
            raise ValueError('Profile organization mismatch')
        for number in (count, minimum_landscape, minimum_portrait):
            if type(number) is not int or number < 0:
                raise ValueError('Counts must be non-negative integers')
        if count < 1 or mode not in ('best', 'worst') or minimum_landscape + minimum_portrait > count:
            raise ValueError('Invalid selection constraints')
        groups, seen = {}, set()
        for a in assessments:
            path = self.source / a.filename
            if (Path(a.filename).name != a.filename or a.filename in seen
                    or path.is_symlink() or not path.is_file()
                    or path.suffix.lower() not in FORMATS):
                raise ValueError('Invalid, missing or repeated source asset')
            if (type(a.score) not in (int, float) or not math.isfinite(a.score)
                    or not isinstance(a.reason, str) or not a.reason.strip()
                    or a.orientation not in ('landscape', 'portrait', 'square', 'unknown')):
                raise ValueError('Invalid assessment')
            seen.add(a.filename)
            asset_hash = digest(path)
            # Exact duplicates cannot be selected twice; near duplicates use adapter group.
            groups.setdefault(a.group or asset_hash, []).append((a, asset_hash))
        # Merge groups connected by byte-identical assets even if the reviewer
        # supplied different near-duplicate group labels.
        merged = []
        for members in groups.values():
            hashes = {h for _, h in members}
            overlaps = [g for g in merged if hashes & {h for _, h in g}]
            for g in overlaps:
                members = members + g
                merged.remove(g)
            merged.append(members)
        # Dynamic programming selects a feasible set, including group/quota interactions.
        states = {(0, 0, 0): (0.0, [])}
        direction = 1 if mode == 'best' else -1
        for members in merged:
            next_states = dict(states)
            for (n, l, p), (score, rows) in states.items():
                if n == count:
                    continue
                for a, asset_hash in members:
                    if any(row['sha256'] == asset_hash for row in rows):
                        continue
                    key = (n + 1, min(minimum_landscape, l + (a.orientation == 'landscape')),
                           min(minimum_portrait, p + (a.orientation == 'portrait')))
                    value = score + direction * a.score
                    if key not in next_states or value > next_states[key][0]:
                        next_states[key] = (value, rows + [dict(filename=a.filename, sha256=asset_hash,
                            score=a.score, reason=a.reason, orientation=a.orientation, group=a.group)])
            states = next_states
        result = states.get((count, minimum_landscape, minimum_portrait))
        if result is None:
            raise ValueError('Insufficient distinct assets or impossible orientation constraints')
        return dict(schema_version=1, organization_id=self.organization_id, source=str(self.source),
                    count=count, mode=mode, minimum_landscape=minimum_landscape,
                    minimum_portrait=minimum_portrait, profile=profile,
                    selected=sorted(result[1], key=lambda x: direction * -x['score']),
                    status='review', quality_validation='not-tested')

    def copy(self, plan, *, folder='已选'):
        """Only a new child folder. Interrupted folders remain for manual reconciliation."""
        if (not folder or Path(folder).name != folder or folder in ('.', '..')
                or plan.get('organization_id') != self.organization_id
                or plan.get('source') != str(self.source)):
            raise ValueError('Invalid destination or plan context')
        rows = plan['selected']
        if len(rows) != plan['count'] or len({r['filename'] for r in rows}) != len(rows):
            raise ValueError('Invalid plan count')
        if len({r['sha256'] for r in rows}) != len(rows):
            raise ValueError('Duplicate asset content')
        for row in rows:
            path = self.source / row['filename']
            if (Path(row['filename']).name != row['filename'] or path.is_symlink()
                    or path.suffix.lower() not in FORMATS or digest(path) != row['sha256']):
                raise ValueError('Source changed or plan invalid; regenerate plan')
        target = self.source / folder
        if target.is_symlink():
            raise ValueError('Destination symlink is forbidden')
        if target.exists():
            if not target.is_dir() or any(target.iterdir()):
                raise ValueError('Destination must be empty; never overwrite')
        else:
            target.mkdir()
        completed = []
        try:
            for row in rows:
                source = self.source / row['filename']
                destination = target / row['filename']
                with source.open('rb') as src, destination.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
                if digest(destination) != row['sha256'] or digest(source) != row['sha256']:
                    raise ValueError('Hash verification failed')
                completed.append(row['filename'])
        except Exception as exc:
            raise RuntimeError(f'Copy interrupted; preserve folder for review. Completed: {completed}') from exc
        return dict(status='copied', destination=str(target), count=len(completed), verification='sha256')
