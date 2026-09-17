"""Build and sign a standalone APK using the verified SDK in ../tools/android-sdk."""
from pathlib import Path
import subprocess, shutil, zipfile, os, secrets, json, hashlib, xml.etree.ElementTree as ET

app=Path(__file__).resolve().parent
version=ET.parse(app/'AndroidManifest.xml').getroot().get('{http://schemas.android.com/apk/res/android}versionName')
apk_name='TimeFitness-'+version+'.apk'
sdk=app.parent/'tools'/'android-sdk'
bt=sdk/'build-tools'; jar=sdk/'platform'/'android.jar'; out=app/'build'
out.mkdir(exist_ok=True)
classes=out/('classes-'+version); classes.mkdir(exist_ok=True)
dex=out/('dex-'+version);dex.mkdir(exist_ok=True)
java=shutil.which('java'); javac=shutil.which('javac'); keytool=shutil.which('keytool')
def run(args):
    result=subprocess.run([str(a) for a in args],cwd=app,text=True,encoding='utf-8',errors='replace',capture_output=True)
    if result.returncode: raise RuntimeError(result.stdout+'\n'+result.stderr)
    if result.stdout.strip(): print(result.stdout.strip())
    if result.stderr.strip(): print(result.stderr.strip())
print('1/5 Compile Android shell',flush=True)
run([javac,'-source','8','-target','8','-encoding','UTF-8','-bootclasspath',jar,'-d',classes,*app.glob('java/**/*.java')])
print('2/5 Build DEX',flush=True)
run([java,'-cp',bt/'lib'/'d8.jar','com.android.tools.r8.D8','--min-api','23','--lib',jar,'--output',dex,*classes.glob('**/*.class')])
print('3/5 Package resources',flush=True)
unsigned=out/'unsigned.apk'
run([bt/'aapt.exe','package','-f','-M',app/'AndroidManifest.xml','-S',app/'res','-A',app/'assets','-I',jar,'-F',unsigned])
with zipfile.ZipFile(unsigned,'a',zipfile.ZIP_DEFLATED) as z:
    for f in dex.glob('*.dex'): z.write(f,f.name)
print('4/5 Align and sign',flush=True)
aligned=out/'aligned.apk'; apk=out/apk_name
run([bt/'zipalign.exe','-f','4',unsigned,aligned])
signing=app/'signing';signing.mkdir(exist_ok=True)
config=signing/'release.json';ks=signing/'time-fitness.jks'
if not config.exists():
    if ks.exists(): raise RuntimeError('Signing key exists without its configuration; refusing to replace it.')
    config.write_text(json.dumps({'alias':'timefitness','password':secrets.token_urlsafe(32)}),encoding='utf-8')
credentials=json.loads(config.read_text(encoding='utf-8'))
os.environ['TIME_FITNESS_SIGN_PASS']=credentials['password']
if not ks.exists():
    run([keytool,'-genkeypair','-keystore',ks,'-alias',credentials['alias'],'-keyalg','RSA','-keysize','2048','-validity','10950','-storepass:env','TIME_FITNESS_SIGN_PASS','-keypass:env','TIME_FITNESS_SIGN_PASS','-dname','CN=Time Fitness, OU=Personal, O=Time Fitness, C=KR'])
run([java,'-jar',bt/'lib'/'apksigner.jar','sign','--ks',ks,'--ks-key-alias',credentials['alias'],'--ks-pass','env:TIME_FITNESS_SIGN_PASS','--key-pass','env:TIME_FITNESS_SIGN_PASS','--v1-signing-enabled','true','--v2-signing-enabled','true','--v3-signing-enabled','true','--out',apk,aligned])
print('5/5 Verify signed APK',flush=True)
run([java,'-jar',bt/'lib'/'apksigner.jar','verify','--verbose','--print-certs',apk])
run([bt/'zipalign.exe','-c','4',apk])
release=app.parent/apk_name;shutil.copy2(apk,release)
release.with_suffix('.sha256').write_text(hashlib.sha256(release.read_bytes()).hexdigest()+'  '+apk_name+'\n',encoding='ascii')
print('APK: '+str(release),flush=True)
