"""Append a user-confirmed correction to an external JSONL log."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--log', type=Path, required=True)
    p.add_argument('--audio', type=Path, required=True)
    p.add_argument('--original', required=True)
    p.add_argument('--corrected', required=True)
    p.add_argument('--evidence', required=True, help='User confirmation, not model inference')
    p.add_argument('--scope', choices=['this-recording', 'store-term'], default='this-recording')
    p.add_argument('--start', type=float)
    p.add_argument('--end', type=float)
    args = p.parse_args()
    if not args.audio.is_file():
        p.error('Audio file does not exist')
    if not all(x.strip() for x in [args.original, args.corrected, args.evidence]):
        p.error('Correction and confirmation fields cannot be empty')
    if args.start is not None and args.start < 0:
        p.error('Start must be nonnegative')
    if args.end is not None and (args.start is None or args.end <= args.start):
        p.error('End requires start and must be greater than start')
    record = {'id': str(uuid.uuid4()), 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'audio': str(args.audio.resolve()), 'original': args.original,
              'corrected': args.corrected, 'evidence': args.evidence,
              'scope': args.scope, 'start': args.start, 'end': args.end,
              'kind': 'user_confirmed_correction'}
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    print(record['id'])


if __name__ == '__main__':
    main()
