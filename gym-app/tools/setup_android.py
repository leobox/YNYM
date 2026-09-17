"""Fetch official Android SDK build artifacts and verify their published SHA-1."""
from pathlib import Path
import hashlib, urllib.request, zipfile, io, xml.etree.ElementTree as ET
root = Path(__file__).resolve().parent / 'android-sdk'
root.mkdir(parents=True, exist_ok=True)
repo = ET.fromstring(urllib.request.urlopen('https://dl.google.com/android/repository/repository2-1.xml').read())
for package, folder in [('build-tools;34.0.0', 'build-tools'), ('platforms;android-34', 'platform'), ('platform-tools', 'platform-tools')]:
    target = root / folder
    if target.exists():
        print(f'{package}: already present', flush=True)
        continue
    pkg = next(p for p in repo.findall('remotePackage') if p.get('path') == package)
    archive = next(a for a in pkg.findall('archives/archive') if a.findtext('host-os') in (None, 'windows'))
    url = 'https://dl.google.com/android/repository/' + archive.findtext('complete/url')
    print(f'Downloading {package}', flush=True)
    data = urllib.request.urlopen(url).read()
    assert hashlib.sha1(data).hexdigest() == archive.findtext('complete/checksum'), 'Checksum mismatch'
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for item in z.infolist():
            parts = Path(item.filename).parts[1:]
            if not parts: continue
            dest = target.joinpath(*parts)
            assert dest.resolve().is_relative_to(target.resolve())
            if item.is_dir(): dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(item))
    print(f'{package}: verified and extracted', flush=True)
