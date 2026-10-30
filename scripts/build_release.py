"""Package only reviewed committed source; never copy a working directory."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

from check_publish import audit


def build(version, output, ref='HEAD'):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?',version):
        raise ValueError('Use a release version such as v0.1.0')
    findings,entries=audit(ref)
    if findings: raise ValueError('Publication guard failed: '+', '.join(findings))
    revision=subprocess.check_output(['git','rev-parse',ref+'^{commit}']).decode().strip()
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    path=output/f'home-monitoring-{version}.zip'
    prefix=f'home-monitoring-{version}/'
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,sha,mode in sorted(entries):
            entry=zipfile.ZipInfo(prefix+name,date_time=(2020,1,1,0,0,0))
            entry.external_attr=int(mode,8)<<16
            entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,subprocess.check_output(['git','cat-file','blob',sha]))
        info=zipfile.ZipInfo(prefix+'BUILD-INFO.json',date_time=(2020,1,1,0,0,0))
        info.external_attr=0o100644<<16
        archive.writestr(info,json.dumps({'version':version,'revision':revision,'source':'https://github.com/zandaulion/home-monitoring','contents':'Committed source, synthetic examples and documentation; private runtime data and model weights excluded'},indent=2)+'\n')
    checksum=hashlib.sha256(path.read_bytes()).hexdigest()
    (output/'SHA256SUMS').write_text(f'{checksum}  {path.name}\n')
    print(f'Built {path.name} from {revision[:12]} ({len(entries)} source files).')
    return path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',required=True)
    parser.add_argument('--output',type=Path,default=Path('dist'))
    parser.add_argument('--ref',default='HEAD')
    args=parser.parse_args()
    try: build(args.version,args.output,args.ref)
    except ValueError as error: parser.exit(1,str(error)+'\n')


if __name__=='__main__': main()
