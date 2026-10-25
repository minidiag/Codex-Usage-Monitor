"""Read-only privacy inventory. Never writes matched values or decrypts credentials."""
import io,json,re,sqlite3,tarfile,zipfile,struct
from collections import Counter
from pathlib import Path
from client.paths import user_root

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.local/privacy-audit-results.json'
PATTERNS={
 'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----'),
 'telemetry_token':re.compile(r'\bcu[rw]_[A-Za-z0-9_-]{30,}\b'),
 'openai_key':re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
 'github_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
 'jwt':re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b'),
 'email':re.compile(r'(?<![\w.+-])[A-Za-z0-9_.+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b'),
 'windows_profile_path':re.compile(r'(?i)\bC:(?:\\+|/)Users(?:\\+|/)[^\\/\s"<>\x00]+'),
 'public_ipv4':re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
 'embedded_url_password':re.compile(r'\b(?:postgres(?:ql)?|https?|mysql)://[^\s:@/]+:([^\s@/]{3,})@'),
 'literal_secret_assignment':re.compile(r'''(?ix)["']?(?:password|passwd|secret|read_token|write_token|identity_key|access_token|refresh_token)["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']'''),
}
findings=[];archive_counts={};errors=[];pngs=[];dbs=[]
def scan(name,data,kind):
 texts=[data.decode('utf-8',errors='replace')]
 if data[:2] in (b'\xff\xfe',b'\xfe\xff') or name.endswith(('.exe','.dll','.pyc')):
  texts.append(data.decode('utf-16-le',errors='replace'))
 for text in texts:
  for category,regex in PATTERNS.items():
   for m in regex.finditer(text):
    if category=='public_ipv4':
     a=list(map(int,m.group().split('.')))
     if any(x>255 for x in a) or a[0] in (0,10,127) or a[:2]==[192,168] or (a[0]==172 and 16<=a[1]<=31):continue
    findings.append(dict(path=name,category=category,line=text.count('\n',0,m.start())+1,kind=kind))
def archive(name,data,depth=0):
 if depth>2:return
 try:
  if name.endswith(('.tar.gz','.tgz')):
   with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as z:
    members=[m for m in z if m.isfile()];archive_counts[name]=len(members)
    for m in members:
     raw=z.extractfile(m).read();n=name+'!/'+m.name;scan(n,raw,'archive')
     if m.name.endswith(('.whl','.zip','.tar.gz')):archive(n,raw,depth+1)
  elif name.endswith(('.zip','.whl','.docx','.xlsx')):
   with zipfile.ZipFile(io.BytesIO(data)) as z:
    members=[m for m in z.infolist() if not m.is_dir()];archive_counts[name]=len(members)
    for m in members:
     raw=z.read(m);n=name+'!/'+m.filename;scan(n,raw,'archive')
     if m.filename.endswith(('.zip','.whl')):archive(n,raw,depth+1)
 except Exception as e:errors.append(dict(path=name,error=type(e).__name__))
def png(name,data):
 offset=8;chunks=[]
 while offset+12<=len(data):
  size=struct.unpack('>I',data[offset:offset+4])[0];typ=data[offset+4:offset+8].decode('ascii',errors='replace')
  if typ in ('tEXt','iTXt','zTXt','eXIf'):chunks.append(typ)
  offset+=12+size
 pngs.append(dict(path=name,metadata_chunks=chunks))
def db(name,p):
 try:
  with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as con:
   tables=[]
   for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
    safe='"'+t.replace('"','""')+'"'
    columns=[row[1] for row in con.execute('PRAGMA table_info('+safe+')')]
    tables.append(dict(table=t,columns=columns,rows=con.execute('SELECT count(*) FROM '+safe).fetchone()[0]))
   dbs.append(dict(path=name,tables=tables))
 except Exception as e:errors.append(dict(path=name,error=type(e).__name__))
files=[];total=0
for p in sorted(ROOT.rglob('*')):
 if not p.is_file() or p==OUT:continue
 n=p.relative_to(ROOT).as_posix()
 try:
  data=p.read_bytes();files.append(n);total+=len(data);scan(n,data,'project')
  if n.endswith(('.zip','.whl','.tar.gz','.tgz','.docx','.xlsx')):archive(n,data)
  if data.startswith(b'\x89PNG\r\n\x1a\n'):png(n,data)
  if data.startswith(b'SQLite format 3\x00'):db(n,p)
 except Exception as e:errors.append(dict(path=n,error=type(e).__name__))
external=[]
print(json.dumps(dict(project_scan_done=True,files=len(files))),flush=True)
base=user_root()
if base.exists():
 for p in sorted(base.rglob('*')):
  if not p.is_file():continue
  # Do not expose account-derived avatar filenames in the report.
  n='[current-user-appdata]/'+('state/avatars/[image]' if 'avatars' in p.parts else p.relative_to(base).as_posix())
  try:
   data=p.read_bytes();external.append(dict(path=n,bytes=len(data)));scan(n,data,'appdata')
   if data.startswith(b'SQLite format 3\x00'):db(n,p)
   if data.startswith(b'\x89PNG\r\n\x1a\n'):png(n,data)
  except Exception as e:errors.append(dict(path=n,error=type(e).__name__))
unique={json.dumps(v,sort_keys=True):v for v in findings}
result=dict(project_files=len(files),project_bytes=total,findings=list(unique.values()),archives=archive_counts,databases=dbs,pngs=pngs,external_files=external,errors=errors)
OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(project_files=len(files),project_bytes=total,categories=Counter(f['category'] for f in unique.values()),archive_count=len(archive_counts),database_count=len(dbs),appdata_files=len(external),errors=errors),ensure_ascii=False))
