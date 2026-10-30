"""Check exact Git objects for obvious private files and credentials before publishing.

A guardrail, not a substitute for reviewing the outgoing diff and commit metadata.
"""
import argparse
import json
from pathlib import PurePosixPath
import re
import subprocess

APP_ICONS = {'home_monitor/static/icons/'+name for name in ('icon-192.png','icon-512.png','icon-maskable.png','apple-touch-icon.png')}
PREVIEWS = {'docs/images/portfolio/'+name+'.png' for name in ('desktop','tablet','mobile','activity')}
CREDENTIAL = re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----)')


def audit(ref='HEAD', staged=False):
    if staged:
        raw = subprocess.check_output(['git','ls-files','-s','-z'])
        entries = [(entry.split(b'\t',1)[1].decode(), entry.split()[1].decode(), entry.split()[0].decode()) for entry in raw.split(b'\0') if entry]
    else:
        raw = subprocess.check_output(['git','ls-tree','-r','-z',ref])
        entries = [(entry.split(b'\t',1)[1].decode(),entry.split()[2].decode(),entry.split()[0].decode()) for entry in raw.split(b'\0') if entry]
    findings = []
    blobs = {}
    for name, sha, mode in entries:
        path=PurePosixPath(name)
        if mode not in ('100644','100755'):
            findings.append(name+': unexpected symlink or submodule'); continue
        if any(part in {'.runtime','.venv','output','qa','backups','deploy-backups','node_modules','.aws','.codex','.agents','dist','data','models'} for part in path.parts):
            findings.append(name+': runtime or private directory')
        if path.name in {'config.json','collector-config.json','.env','hosts.yml'} or path.name.startswith('.env.') or path.suffix.lower() in {'.pem','.key','.p12','.pfx','.db','.sqlite','.sqlite3','.onnx','.jpg','.jpeg','.webp','.heic','.mp4','.zip','.tar','.gz','.log'}:
            findings.append(name+': private configuration or data artifact')
        if path.suffix.lower()=='.png' and name not in APP_ICONS | PREVIEWS:
            findings.append(name+': unreviewed image path')
        blob=subprocess.check_output(['git','cat-file','blob',sha]); blobs[name]=blob
        if CREDENTIAL.search(blob): findings.append(name+': credential pattern')
    if PREVIEWS & set(blobs):
        import hashlib
        try:
            manifest=json.loads(blobs['docs/images/portfolio/manifest.json'])
            approved={entry['file']:entry['sha256'] for entry in manifest['screenshots']}
            for name in PREVIEWS:
                if hashlib.sha256(blobs[name]).hexdigest()!=approved[PurePosixPath(name).name]:
                    findings.append(name+': screenshot manifest mismatch')
        except (KeyError,ValueError,TypeError):
            findings.append('Synthetic screenshot manifest is incomplete')
    return findings, entries


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref',default='HEAD')
    parser.add_argument('--staged',action='store_true')
    args=parser.parse_args()
    findings,entries=audit(args.ref,args.staged)
    for finding in findings: print('REVIEW: '+finding)
    if findings: parser.exit(1,'Publication checks failed. Do not publish until reviewed.\n')
    print(f'Publication guard passed for {len(entries)} exact Git files. Manual privacy and metadata review is still required.')


if __name__=='__main__': main()
