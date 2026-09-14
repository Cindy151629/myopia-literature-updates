"""Standalone public-metadata updater. Python standard library; no model calls.

PubMed: crdt == PubMedPubDate[PubStatus=entrez]; edat == pubmed;
lr == MedlineCitation/DateRevised. Publisher 'revised' is NOT database lr.
Raw abstracts/full texts are transient and never enter public state or snapshots.
"""
import argparse, calendar, copy, datetime as dt, hashlib, html, json, os, re, ssl, time, uuid
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UTC=dt.timezone.utc
ROOT=Path(__file__).resolve().parent
def now(): return dt.datetime.now(UTC).replace(microsecond=0).isoformat()
def digest(b): return hashlib.sha256(b).hexdigest()
def encoded(x): return (json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
def save(path,x):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp');temp.write_bytes(encoded(x));temp.replace(path)
def read(path,default=None): return json.loads(Path(path).read_text()) if Path(path).exists() else copy.deepcopy(default)
def text(n): return ' '.join(' '.join(n.itertext()).split()) if n is not None else ''
def doi(value):
    value=urllib.parse.unquote(str(value or '')).strip().lower()
    value=re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*','',value)
    return value if re.fullmatch(r'10\.\d{4,9}/[^\s<>"\x00-\x1f]+',value) else None
def safe_url(value):
    try:
        p=urllib.parse.urlsplit(str(value));return p.scheme=='https' and bool(p.hostname) and not p.username and not p.password and not re.search(r'[\x00-\x20\\<>"\x7f]',str(value))
    except ValueError:return False
def date_value(node):
    if node is None:return None
    parts=[node.findtext(k) for k in ('Year','Month','Day')]
    raw=text(node)
    if not parts[0] or not re.fullmatch(r'\d{4}',parts[0]):return {'value':None,'precision':'text','raw':raw}
    year=parts[0];month=parts[1]
    months={'Jan':'01','Feb':'02','Mar':'03','Apr':'04','May':'05','Jun':'06','Jul':'07','Aug':'08','Sep':'09','Oct':'10','Nov':'11','Dec':'12'}
    month=months.get(month,month)
    if not month or not month.isdigit() or not 1<=int(month)<=12:return {'value':year,'precision':'year','raw':raw}
    value=f'{year}-{int(month):02d}';day=parts[2]
    if day and day.isdigit():
        try:dt.date(int(year),int(month),int(day));return {'value':value+f'-{int(day):02d}','precision':'day','raw':raw}
        except ValueError:pass
    return {'value':value,'precision':'month','raw':raw}

def retention_cutoff(config,stamp,years=None):
    current=dt.date.fromisoformat(stamp[:10]);year=current.year-int(years if years is not None else config.get('retention',{}).get('years',1))
    return current.replace(year=year,day=min(current.day,calendar.monthrange(year,current.month)[1]))

def extended_journal(p,config):
    policy=config.get('retention',{})
    norm=lambda value:re.sub(r'[^a-z0-9]+',' ',str(value).lower().replace('&',' and ')).strip()
    name=norm(p.get('journal',''));aliases={norm(k):norm(v) for k,v in policy.get('journal_aliases',{}).items()}
    return aliases.get(name,name) in {norm(x) for x in policy.get('journal_whitelist',[])}

def selection_reason(p,config):
    """Metadata triage, not a claim of full-text quality assessment."""
    policy=config.get('retention',{}).get('selection',{})
    if not policy.get('enabled'):return None
    if policy.get('whitelist_only') and not extended_journal(p,config):return 'not_selected_journal'
    types=set(p.get('publication_types',[]));title=str(p.get('title','')).strip()
    if types & set(policy.get('ancillary_types',[])) or re.search(r'^(?:reply|comment|commentary|correction|corrigendum|erratum)\b|^re\s*:',title,re.I):return 'ancillary_publication'
    synthesis=bool(types & set(policy.get('synthesis_types',[])) or re.search(r'\bsystematic\s+review\b|\bmeta[\s\-–—]?analys(?:is|es)\b',title,re.I))
    verified=any(p.get('pmid')==r.get('pmid') and doi(p.get('doi'))==doi(r.get('doi')) for r in policy.get('verified_reports',[]))
    if policy.get('exclude_ordinary_reviews') and types & {'Review','Scoping Review'} and not synthesis and not verified:return 'ordinary_review'
    return None

_jcr_cache=None
def jcr_decision(p,config):
    """Only explicit source-reported JIF quartiles; unknown is a review queue."""
    global _jcr_cache
    policy=config.get('retention',{}).get('jcr',{})
    if not policy.get('enabled'):return 'disabled'
    if policy.get('metric')!='JIF Quartile':return 'jcr_unverified'
    norm=lambda s:re.sub(r'[^a-z0-9]+',' ',str(s).lower().replace('&',' and ')).strip()
    if _jcr_cache is None or _jcr_cache[0] is not policy:
        index={}
        for row in policy.get('journals',[]):
            for name in [row['journal']]+row.get('aliases',[]):
                key=norm(name)
                if key in index and index[key]!=row:raise ValueError('Ambiguous JCR journal identity')
                index[key]=row
        _jcr_cache=(policy,index)
    row=_jcr_cache[1].get(norm(p.get('journal','')))
    if not row or row.get('quartile') not in ('Q1','Q2','Q3','Q4'):return 'jcr_unverified'
    return 'q1' if row['quartile']=='Q1' else 'not_jcr_q1'

def publication_interval(p):
    """Earliest publication range, never database creation or revision dates."""
    ranges=[]
    for d in [p.get('dates',{}).get('journal')]+p.get('dates',{}).get('electronic',[]):
        value=(d or {}).get('value','') or '';precision=(d or {}).get('precision')
        try:
            if precision=='day' and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):lo=hi=dt.date.fromisoformat(value)
            elif precision=='month' and re.fullmatch(r'\d{4}-\d{2}',value):
                y,m=map(int,value.split('-'));lo=dt.date(y,m,1);hi=dt.date(y,m,calendar.monthrange(y,m)[1])
            elif precision=='year' and re.fullmatch(r'\d{4}',value):lo=dt.date(int(value),1,1);hi=dt.date(int(value),12,31)
            else:continue
            ranges.append((lo,hi))
        except (ValueError,TypeError):continue
    return (min(x[0] for x in ranges),min(x[1] for x in ranges)) if ranges else None

def retention_reason(p,config,stamp,base_ids=frozenset()):
    if not config.get('retention',{}).get('enabled'):return 'retained'
    if p['id'] in base_ids:return 'baseline_metadata'
    interval=publication_interval(p)
    if not interval:return 'publication_date_unconfirmed'
    extended=extended_journal(p,config)
    cutoff=retention_cutoff(config,stamp,config['retention'].get('extended_years',3) if extended else None);today=dt.date.fromisoformat(stamp[:10])
    if interval[1]<cutoff:return 'older_than_window'
    if interval[0]>today:return 'future_publication'
    if interval[0]<cutoff:return 'publication_date_unconfirmed'
    selected=selection_reason(p,config)
    if selected:return selected
    quartile=jcr_decision(p,config)
    if quartile not in ('q1','disabled'):return quartile
    return 'extended_journal' if extended and interval[0]<retention_cutoff(config,stamp) else 'retained'

class SourceError(RuntimeError):pass
class HTTP:
    def __init__(self,audit=None,max_requests=10000):
        self.audit=audit if audit is not None else [];self.max_requests=max_requests;self.count=0;self.last={}
        self.context=ssl.create_default_context(cafile='/etc/ssl/cert.pem' if Path('/etc/ssl/cert.pem').exists() else None)
    def get(self,url,params=None,max_bytes=25_000_000):
        if params:url+='?'+urllib.parse.urlencode(params)
        if not safe_url(url):raise SourceError('Invalid HTTPS source URL')
        host=urllib.parse.urlsplit(url).hostname
        if host not in {'eutils.ncbi.nlm.nih.gov','www.ebi.ac.uk','api.crossref.org'}:raise SourceError('Source host not allowed')
        for attempt in range(3):
            self.count+=1
            if self.count>self.max_requests:raise SourceError('Request budget exceeded; query not complete')
            time.sleep(max(0,self.last.get(host,0)+(.4 if host.startswith('eutils') else .12)-time.monotonic()))
            self.last[host]=time.monotonic();started=now()
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'MyopiaLiteratureUpdater/1.0 (public scholarly metadata; no AI)','Accept':'application/json, application/xml, text/xml'})
                with urllib.request.urlopen(req,timeout=35,context=self.context) as r:
                    payload=r.read(max_bytes+1);status=r.status;final=r.url
                if len(payload)>max_bytes:raise SourceError('Response exceeds byte limit')
                self.audit.append(dict(url=url,started_at=started,status=status,bytes=len(payload),sha256=digest(payload),final_url=final))
                return payload
            except (urllib.error.URLError,TimeoutError) as e:
                status=getattr(e,'code',None);self.audit.append(dict(url=url,started_at=started,status=status,error=type(e).__name__))
                if status in [401,403,404] or attempt==2:raise SourceError(f'{host}: HTTP {status or "network timeout"}') from e
                time.sleep(2**attempt)
        raise SourceError('Unreachable retry state')
    def json(self,url,params=None):
        try:return json.loads(self.get(url,params))
        except (ValueError,UnicodeDecodeError) as e:raise SourceError('Invalid source JSON') from e

class PubMed:
    url='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
    def __init__(self,http,page_size=500):self.http=http;self.page_size=page_size
    def search(self,query,field,start,end,log):
        term=f'({query}) AND ("{start:%Y/%m/%d}"[{field}] : "{end:%Y/%m/%d}"[{field}])'
        def page(offset,size):
            data=self.http.json(self.url+'esearch.fcgi',dict(db='pubmed',term=term,retmode='json',retmax=size,retstart=offset,sort='pub date'))
            if 'error' in data:raise SourceError(str(data['error']))
            r=data.get('esearchresult',{})
            if 'count' not in r or r.get('errorlist') or r.get('ERROR'):raise SourceError('Invalid or rejected PubMed query')
            return int(r['count']),r.get('idlist',[])
        count,_=page(0,0)
        if count>9999:
            if start>=end:raise SourceError('Single-day PubMed result exceeds 9999; needs narrower configured topics')
            mid=start+(end-start)//2
            left=self.search(query,field,start,mid,log);right=self.search(query,field,mid+dt.timedelta(days=1),end,log)
            return sorted(set(left+right))
        ids=[];pages=0
        for offset in range(0,count,self.page_size):
            current,part=page(offset,min(self.page_size,count-offset));pages+=1
            if current!=count or len(part)!=min(self.page_size,count-offset):raise SourceError('PubMed pagination changed or incomplete; retry next run')
            if any(not re.fullmatch(r'\d+',x) for x in part):raise SourceError('Invalid PMID')
            ids+=part
        if len(set(ids))!=count:raise SourceError('PubMed returned duplicate/incomplete IDs')
        log.append(dict(term=term,field=field,start=str(start),end=str(end),expected=count,retrieved=len(ids),pages=pages,complete=True,ids=ids))
        return ids
    def fetch(self,ids):
        if not ids:return []
        raw=self.http.get(self.url+'efetch.fcgi',dict(db='pubmed',id=','.join(ids),retmode='xml'))
        try:root=ET.fromstring(raw)
        except ET.ParseError as e:raise SourceError('Invalid PubMed XML') from e
        out=[parse_pubmed(n) for n in root.findall('PubmedArticle')]+[parse_book(n) for n in root.findall('PubmedBookArticle')]
        # Missing records must not silently advance the watermark.
        if {x['pmid'] for x in out}!=set(ids):raise SourceError('EFetch missing PMID: '+','.join(sorted(set(ids)-{x['pmid'] for x in out})))
        for p in out:p['provenance']=[dict(source='PubMed',url=f'https://pubmed.ncbi.nlm.nih.gov/{p["pmid"]}/',response_sha256=digest(raw))]
        return out

def parse_pubmed(n):
    c=n.find('MedlineCitation');a=c.find('Article');pmid=c.findtext('PMID')
    if not re.fullmatch(r'\d+',pmid or '') or a is None:raise SourceError('Unverified PubMed identity')
    ids={i.get('IdType'):text(i) for i in n.findall('./PubmedData/ArticleIdList/ArticleId')}
    authors=[]
    for author in a.findall('./AuthorList/Author'):
        name=text(author.find('CollectiveName')) or ' '.join(filter(None,[author.findtext('LastName'),author.findtext('ForeName') or author.findtext('Initials')]))
        if name:authors.append(name)
    history={d.get('PubStatus'):date_value(d) for d in n.findall('./PubmedData/History/PubMedPubDate')}
    dates={'journal':date_value(a.find('./Journal/JournalIssue/PubDate')),'electronic':[date_value(x) for x in a.findall('ArticleDate') if x.get('DateType')=='Electronic'],
           'created':history.get('entrez'),'entry':history.get('pubmed'),'modified':date_value(c.find('DateRevised'))}
    notices=[dict(type=x.get('RefType'),pmid=x.findtext('PMID'),citation=x.findtext('RefSource')) for x in c.findall('./CommentsCorrectionsList/CommentsCorrections')]
    types=[text(x) for x in a.findall('./PublicationTypeList/PublicationType')]
    return dict(id='PMID'+pmid,pmid=pmid,doi=doi(ids.get('doi')),pmcid=ids.get('pmc') if re.fullmatch(r'PMC\d+',ids.get('pmc','')) else None,
     title=text(a.find('ArticleTitle')),authors=authors,journal=text(a.find('./Journal/Title')),dates=dates,
     publication_types=types,publication_status='preprint' if 'Preprint' in types else 'published',
     notices=notices,aliases=[],version_relations=[],identity_verified=True,
     _abstract=' '.join(text(x) for x in a.findall('./Abstract/AbstractText')),
     _mesh=[text(x) for x in c.findall('./MeshHeadingList/MeshHeading/DescriptorName')])

def parse_book(n):
    c=n.find('BookDocument');pmid=c.findtext('PMID')
    if not re.fullmatch(r'\d+',pmid or ''):raise SourceError('Unverified book PMID')
    ids={i.get('IdType'):text(i) for i in c.findall('./ArticleIdList/ArticleId')}
    history={d.get('PubStatus'):date_value(d) for d in n.findall('./PubmedBookData/History/PubMedPubDate')}
    authors=[' '.join(filter(None,[a.findtext('LastName'),a.findtext('ForeName') or a.findtext('Initials')])) for a in c.findall('./AuthorList/Author')]
    return dict(id='PMID'+pmid,pmid=pmid,doi=doi(ids.get('doi')),pmcid=None,title=text(c.find('ArticleTitle')) or text(c.find('./Book/BookTitle')),authors=authors,journal=text(c.find('./Book/BookTitle')),
        dates=dict(journal=date_value(c.find('./Book/PubDate')),electronic=[],created=history.get('entrez'),entry=history.get('pubmed'),modified=date_value(c.find('DateRevised'))),
        publication_types=['Book Chapter']+[text(x) for x in c.findall('PublicationType')],publication_status='published',notices=[],aliases=[],version_relations=[],identity_verified=True,
        _abstract=' '.join(text(x) for x in c.findall('./Abstract/AbstractText')),_mesh=[],_book_accession=ids.get('bookaccession'))

def classify(p,routes):
    title=p['title'].lower();hay=title+' '+p.get('_abstract','').lower()+' '+' '.join(p.get('_mesh',[])).lower()
    family='ophthalmology';pending=False;reason=''
    if re.search(r'\b(myopia|myopic|ocular growth|axial elongation|emmetropi\w*)\b',title+' '+' '.join(p.get('_mesh',[])).lower()):
        reason='题名/受控词明确涉及近视、正视化或眼生长。'
    elif 'ocular' in routes and re.search(r'\b(retina\w*|sclera\w*|choroid\w*|ophthalm\w*|eye|ocular)\b',title) and re.search(r'crispr|editing|single.cell|spatial|gene therap|delivery|organoid',hay):
        reason='明确眼科组织及编辑、递送或组学方法，纳入眼科主线。'
    else:
        family='cross_domain'
        if re.search(r'perturb.seq|spatial perturbation|spatial transcriptom',hay) and re.search(r'fibroblast|extracellular matrix',hay):reason='可借鉴空间/扰动测量和成纤维细胞状态分析，用于巩膜ECM机制候选筛选；眼科适用性待验证。'
        elif re.search(r'editing',hay) and re.search(r'off.target|unintended|editing outcome|software|predict',hay):reason='可借鉴编辑结果/非预期产物测量或预测，用于眼科编辑模型的质量与安全评估；并非近视疗效证据。'
        elif re.search(r'target trial',hay) and re.search(r'discontinuation|cessation|time.varying|longitudinal',hay):reason='可借鉴目标试验和时间变化混杂的处理，用于近视停治/反弹纵向研究设计；迁移方案待精读验证。'
        else:pending=True;reason='命中检索式，但规则不能确认具体近视/眼科研究价值，进入待核对。'
    if family=='cross_domain':topic='跨领域方法学'
    elif re.search(r'atropine|orthokeratology|lens|trial|treatment|rebound',title):topic='临床防控与停治反弹'
    elif re.search(r'pathologic|maculopath|retinal detach|neovascular|staphyloma',title):topic='病理性近视与并发症'
    elif re.search(r'sclera|extracellular|collagen|biomechan',title):topic='巩膜、ECM 与生物力学'
    elif re.search(r'choroid|hypoxia|inflamm',title):topic='脉络膜、缺氧与炎症'
    elif re.search(r'crispr|editing|gene therap|delivery',title):topic='眼科基因编辑与递送'
    elif re.search(r'genetic|variant|genome.wide|mutation',title):topic='遗传易感与单基因'
    elif re.search(r'light|circadian|defocus|visual input',title):topic='视觉输入、光照与节律'
    elif re.search(r'single.cell|spatial|omics|transcriptom|atlas',title):topic='组学图谱与公共资源'
    elif re.search(r'retina|dopamine|rpe|neuron',title):topic='视网膜、RPE 与神经信号'
    else:topic='基础概念与流行病学'
    methods=[]
    for pattern,label in [(r'spatial','空间组学'),(r'single.cell|perturb.seq','单细胞组学'),(r'crispr|editing','基因编辑'),(r'target trial|causal','因果推断'),(r'imaging|foundation model','影像与生物测量'),(r'pooled|screen','基因与表达扰动')]:
        if re.search(pattern,hay):methods.append(label)
    tissue=[label for pat,label in [('sclera','巩膜'),('retina','视网膜'),('choroid','脉络膜'),(r'\brpe\b','RPE'),('organoid','类器官')] if re.search(pat,hay)]
    original_domain='肿瘤学' if re.search(r'tumou?r|cancer',title) else '基因编辑' if re.search(r'editing|crispr',title) else '通用研究方法' if family=='cross_domain' else '眼科'
    type_label='综述、共识与分级' if any('Review' in t for t in p['publication_types']) else '临床干预与人体实验' if any('Trial' in t for t in p['publication_types']) else '研究类型待核对'
    return dict(family=family,topic=topic,methods=methods,tissues=tissue,original_domain=original_domain,type=type_label,
     maturity='概念性借鉴' if family=='cross_domain' and not pending else '未能确认' if family=='cross_domain' else '眼科直接证据',
     pending=pending,reason=reason,code_data_availability='未能确认')

def official_fulltext_identity(raw,p):
    """A only for matching JATS article identity plus nontrivial actual body."""
    try:r=ET.fromstring(raw)
    except ET.ParseError:return False
    if r.tag!='article' or len(text(r.find('body')))<1000:return False
    ids={i.get('pub-id-type'):text(i) for i in r.findall('./front/article-meta/article-id')}
    identity=(p.get('pmid') and ids.get('pmid')==p['pmid']) or (p.get('doi') and doi(ids.get('doi'))==p['doi'])
    a=re.sub(r'\W','',p['title'].lower());b=re.sub(r'\W','',text(r.find('./front/article-meta/title-group/article-title')).lower())
    return bool(identity and a and b and (a==b or a.rstrip('.')==b.rstrip('.')))

PUBLIC_FIELDS={'id','pmid','doi','pmcid','title','authors','journal','dates','publication_types','publication_status','notices','aliases','version_relations','identity_verified','provenance','classification','links','fulltext','first_seen','last_verified','last_changed','matched_queries','crossref','retired','attention'}
def validate_record(p):
    if set(p)-PUBLIC_FIELDS:raise ValueError('Non-public field in record')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+',p['id']) or not p.get('title') or not p.get('identity_verified'):raise ValueError('Invalid record identity')
    if not p.get('pmid') and not p.get('doi'):raise ValueError('Missing identifiers')
    if p.get('doi') and doi(p['doi'])!=p['doi']:raise ValueError('DOI normalization mismatch')
    for link in p['links']+p['fulltext']['entries']:
        if not safe_url(link['url']):raise ValueError('Unsafe URL')
    if p['fulltext']['status'] not in ['A','B','C']:raise ValueError('Unknown full-text state')
    if p['fulltext']['status']=='A' and not p['fulltext'].get('identity_evidence'):raise ValueError('No full-text identity evidence')
    # Field allowlists also apply inside nested metadata; no hidden note payloads.
    def allow(value,fields):
        if not isinstance(value,dict) or set(value)-set(fields.split()):raise ValueError('Non-public nested field')
    allow(p['dates'],'journal electronic created entry modified')
    for value in [p['dates'].get(x) for x in ['journal','created','entry','modified']]+p['dates'].get('electronic',[]):
        if value is not None:allow(value,'value precision raw')
    allow(p['classification'],'family topic methods tissues original_domain type maturity pending reason code_data_availability')
    allow(p['fulltext'],'status checked_at entries reason identity_evidence previous_identity_evidence previous_A_checked_at body_attempt_at')
    for k in ['identity_evidence','previous_identity_evidence']:
        if p['fulltext'].get(k):allow(p['fulltext'][k],'url sha256 pmid doi checked_at')
    for item in p['links']+p['fulltext']['entries']:allow(item,'kind url status checked_at source')
    for item in p.get('provenance',[]):allow(item,'source url response_sha256')
    for item in p.get('notices',[]):allow(item,'type pmid citation doi source')
    for item in p.get('version_relations',[]):allow(item,'type doi source verified')
    allow(p['crossref'],'status checked_at url')
    return True

def keys(p):return {prefix+str(value) for prefix,value in [('pmid:',p.get('pmid')),('doi:',doi(p.get('doi')))] if value}
def metadata_content(p):
    """Exclude verification timestamps from substantive metadata change counts."""
    ignored={'last_verified','last_changed','provenance','matched_queries','checked_at','body_attempt_at','previous_A_checked_at'}
    def clean(value):
        if isinstance(value,dict):return {k:clean(v) for k,v in value.items() if k not in ignored}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    return clean(p)
def needs_attention(p):
    return any(re.search(r'retract|expression.?of.?concern|erratum|correct',str(n.get('type','')),re.I) for n in p['notices']) or any(re.search(r'retract|expression of concern',t,re.I) for t in p['publication_types'])
class IdentityIndex:
    def __init__(self,base,state):
        self.by_key={};self.by_id={}
        for p in base['records']+list(state['records'].values()):self.add(p)
    def add(self,p):
        self.by_id[p['id']]=p
        for key in keys(p):self.by_key.setdefault(key,set()).add(p['id'])
    def matches(self,p):
        ids=set().union(*(self.by_key.get(k,set()) for k in keys(p)))
        return {id:self.by_id[id] for id in ids}

def merge_record(state,base,p,stamp,index=None):
    matches=index.matches(p) if index else {x['id']:x for x in base['records']+list(state['records'].values()) if keys(x)&keys(p)}
    if len(matches)>1 or any(x.get('pmid') and p.get('pmid') and x['pmid']!=p['pmid'] for x in matches.values()):
        validate_record(p)
        conflict=dict(incoming_id=p['id'],identifiers=sorted(keys(p)),existing_ids=sorted(matches),reason='Identifier conflict; no existing record overwritten',last_seen=stamp,candidate=p)
        cid=digest(encoded(conflict['identifiers']))[:20];state['conflicts'][cid]=conflict;return 'conflict'
    if matches:
        stable=next(iter(matches));old=state['records'].get(stable,{})
        incoming_id=p['id'];p['id']=stable
        p['aliases']=sorted(set(p.get('aliases',[])+old.get('aliases',[])+([incoming_id] if incoming_id!=stable else [])))
    else:stable=p['id'];old={}
    p['first_seen']=old.get('first_seen',stamp);p['last_verified']=stamp
    # Verified version links stay valid unless explicitly superseded; never fuzzy-merge titles.
    p['version_relations']=list({x['doi']:x for x in old.get('version_relations',[])+p.get('version_relations',[])}.values())
    # A PubMed-only refresh must not erase a previously retrieved Crossref notice.
    carried=[x for x in old.get('notices',[]) if str(x.get('source','')).startswith('https://api.crossref.org/works/')]
    p['notices']=list({encoded(x):x for x in carried+p.get('notices',[])}.values())
    p['attention']=needs_attention(p)
    p['matched_queries']=sorted(set(old.get('matched_queries',[])+p.get('matched_queries',[])))
    if old.get('fulltext',{}).get('status')=='A' and p['fulltext']['status']!='A':
        p['fulltext']['previous_identity_evidence']=old['fulltext'].get('identity_evidence');p['fulltext']['previous_A_checked_at']=old['fulltext']['checked_at']
    changed=metadata_content(p)!=metadata_content(old)
    p['last_changed']=stamp if changed else old['last_changed']
    validate_record(p);state['records'][stable]=p
    if index:index.add(p)
    return 'added' if not old and not matches else 'updated' if changed else 'unchanged'

def new_state(config):
    return dict(schema_version=1,domain_id=config['domain_id'],base_version=config['base_version'],records={},watermarks={},conflicts={},runs=[],last_monthly=None,last_successful_search=None,last_successful_publish=None)

def window(config,state,q,field,stamp,monthly=False):
    key=q['id']+':'+field;signature=digest(encoded([q,config['rule_version']]))
    mark=state['watermarks'].get(key,{})
    end=dt.datetime.fromisoformat(stamp).date()
    if mark.get('signature')==signature and mark.get('successful_at'):
        start=dt.datetime.fromisoformat(mark['successful_at']).date()-dt.timedelta(days=config['overlap_days'])
    else:start=end-dt.timedelta(days=config['bootstrap_days'])
    if monthly:start=min(start,end-dt.timedelta(days=config['monthly_lookback_days']))
    return key,signature,start,end

def enrich(p,http,stamp,crossref=False,fulltext=False,prior=None):
    prior=prior or {}
    p['links']=[dict(kind='PubMed',url=f'https://pubmed.ncbi.nlm.nih.gov/{p["pmid"]}/',status='identity_verified_in_official_API',checked_at=stamp)] if p.get('pmid') else []
    if p.get('doi'):p['links'].append(dict(kind='DOI',url='https://doi.org/'+urllib.parse.quote(p['doi'],safe='/():;'),status='identifier_from_PubMed; landing_body_not_verified',checked_at=stamp))
    ft=dict(status='C',checked_at=stamp,entries=[],reason='未确认可访问全文；题录保留')
    if p.get('pmcid'):
        ft.update(status='B',reason='PubMed正式题录包含PMC索引；正文尚未核对',entries=[dict(kind='PMC',url=f'https://pmc.ncbi.nlm.nih.gov/articles/{p["pmcid"]}/',source='PubMed ArticleIdList')])
    if re.fullmatch(r'NBK\d+',p.get('_book_accession') or ''):
        ft.update(status='B',reason='PubMed包含Bookshelf正式索引；正文尚未核对',entries=[dict(kind='Bookshelf',url='https://www.ncbi.nlm.nih.gov/books/'+p['_book_accession']+'/',source='PubMed BookDocument ArticleIdList')])
    p['fulltext']=ft
    if crossref and p.get('doi'):
        try:
            url='https://api.crossref.org/works/'+urllib.parse.quote(p['doi'],safe='');d=http.json(url)['message']
            # Crossref often stores subtitles separately and uses inline JATS markup.
            # Compare complete normalized titles, never a prefix or fuzzy similarity.
            norm=lambda s:re.sub(r'\W','',html.unescape(re.sub(r'<[^>]*>','',str(s))).lower())
            titles=d.get('title',[]);subtitles=d.get('subtitle',[])
            candidates=titles+[t+': '+s for t in titles for s in subtitles if s]
            matched=doi(d.get('DOI'))==p['doi'] and any(norm(t)==norm(p['title']) for t in candidates)
            p['crossref']=dict(status='matched' if matched else 'needs_review',checked_at=stamp,url=url)
            if not matched:p['classification']['pending']=True;p['classification']['reason']+=' Crossref题名/DOI不一致，待核对。'
            for relation in ['has-preprint','is-preprint-of','is-version-of','has-version']:
                for v in d.get('relation',{}).get(relation,[]):
                    if v.get('id-type')=='doi' and doi(v.get('id')):p['version_relations'].append(dict(type=relation,doi=doi(v['id']),source=url,verified=True))
            for v in d.get('update-to',[]):p['notices'].append(dict(type=v.get('type','update'),doi=doi(v.get('DOI')),source=url))
        except (SourceError,KeyError,TypeError):p['crossref']=dict(status='unverified',checked_at=stamp)
    else:p['crossref']=copy.deepcopy(prior.get('crossref',dict(status='queued_or_not_applicable',checked_at=None)))
    if fulltext and p.get('pmcid'):
        ft['body_attempt_at']=stamp
        url=f'https://www.ebi.ac.uk/europepmc/webservices/rest/{p["pmcid"]}/fullTextXML'
        try:
            raw=http.get(url)
            if official_fulltext_identity(raw,p):
                ft.update(status='A',reason='已取得结构化主文并匹配文章身份；未精读，不代表质量认可',identity_evidence=dict(url=url,sha256=digest(raw),pmid=p.get('pmid'),doi=p.get('doi'),checked_at=stamp))
            else:ft['reason']='官方索引存在，但返回内容未通过正文身份核对'
        except SourceError as e:ft['reason']='正文未能核验/访问受限：'+str(e)
    elif prior.get('fulltext',{}).get('identity_evidence') and p.get('pmcid')==prior.get('pmcid') and p.get('title')==prior.get('title'):
        # Retain the date of the actual body check; this run did not recheck it.
        p['fulltext']=copy.deepcopy(prior['fulltext'])
    elif prior.get('fulltext',{}).get('body_attempt_at'):
        ft['body_attempt_at']=prior['fulltext']['body_attempt_at']
    # Abstracts are deliberately removed before persistence/publication.
    p.pop('_abstract',None);p.pop('_mesh',None);p.pop('_book_accession',None)
    p['attention']=needs_attention(p)
    p['retired']=False
    return p

def run(config,base,state,http,stamp=None):
    stamp=stamp or now();state=copy.deepcopy(state)
    before=copy.deepcopy(state['records'])
    if (state['domain_id'],state['base_version'])!=(config['domain_id'],config['base_version']):raise ValueError('State belongs to different library')
    monthly_anchor=state.get('last_monthly') or state.get('monthly_anchor')
    monthly=bool(monthly_anchor) and (dt.datetime.fromisoformat(stamp)-dt.datetime.fromisoformat(monthly_anchor)).days>=config['monthly_interval_days']
    runlog=dict(id=stamp+'-'+uuid.uuid4().hex[:8],started_at=stamp,finished_at=None,event=os.environ.get('GITHUB_EVENT_NAME','local'),queries=[],errors=[],counts={k:0 for k in ['candidates','added','updated','unchanged','conflict']},monthly=monthly,status='running',code_sha256=digest(Path(__file__).read_bytes()),config_sha256=digest(encoded(config)))
    pub=PubMed(http,config['page_size']);seen={};crleft=config['crossref_per_run'];ftleft=config['fulltext_identity_checks_per_run']
    base_ids={p['id'] for p in base['records']}
    for q in config['queries']:
        for field in ['crdt','lr']:
            print('Retrieving '+q['id']+':'+field,flush=True)
            key,signature,start,end=window(config,state,q,field,stamp,monthly)
            qlog=dict(id=key,signature=signature,start=str(start),end=str(end),pages=[],complete=False)
            try:
                query=q['query']
                if config.get('retention',{}).get('enabled'):
                    cutoff=retention_cutoff(config,stamp,config['retention'].get('extended_years',3))
                    query=f'({query}) AND ("{cutoff:%Y/%m/%d}"[dp] : "{end:%Y/%m/%d}"[dp])'
                ids=pub.search(query,field,start,end,qlog['pages'])
                if len(ids)>config['max_records_per_query']:raise SourceError('Configured record budget exceeded')
                missing=[id for id in ids if id not in seen];fetched=[]
                for i in range(0,len(missing),config['fetch_batch_size']):fetched+=pub.fetch(missing[i:i+config['fetch_batch_size']])
                seen.update({p['pmid']:p for p in fetched})
                # Full query transaction: no watermark or partial rows committed on failure.
                transaction=copy.deepcopy(state);identity_index=IdentityIndex(base,transaction);counts={k:0 for k in runlog['counts']}
                for id in ids:
                    p=copy.deepcopy(seen[id]);p['matched_queries']=[q['id']]
                    exempt=bool(base_ids & identity_index.matches(p).keys())
                    reason=retention_reason(p,config,stamp,{p['id']} if exempt else base_ids)
                    if reason not in ('retained','extended_journal','baseline_metadata','jcr_unverified'):
                        excluded=qlog.setdefault('publication_window_excluded',{})
                        excluded[reason]=excluded.get(reason,0)+1
                        continue
                    prior=next((x for x in identity_index.matches(p).values() if x['id'] in transaction['records']),{})
                    routes=[x['route'] for x in config['queries'] if x['id'] in prior.get('matched_queries',[])+[q['id']]]
                    p['classification']=classify(p,routes)
                    def due(value,days):return not value or (dt.datetime.fromisoformat(stamp)-dt.datetime.fromisoformat(value)).days>=days
                    old_cr=prior.get('crossref',{})
                    matcher_recheck=old_cr.get('status')=='needs_review' and (old_cr.get('checked_at') or '')<config.get('crossref_identity_revision','')
                    do_cr=bool(crleft and p.get('doi') and (matcher_recheck or due(old_cr.get('checked_at'),7 if old_cr.get('status')=='unverified' else 28)))
                    do_ft=bool(ftleft and p.get('pmcid') and due(prior.get('fulltext',{}).get('body_attempt_at'),28))
                    p=enrich(p,http,stamp,do_cr,do_ft,prior);crleft-=int(do_cr);ftleft-=int(do_ft)
                    if prior.get('crossref',{}).get('checked_at') and not do_cr:p['crossref']=prior['crossref']
                    if p['crossref']['status']=='needs_review':p['classification']['pending']=True
                    action=merge_record(transaction,base,p,stamp,identity_index);counts[action]+=1;counts['candidates']+=1
                transaction['watermarks'][key]=dict(signature=signature,successful_at=stamp,start=str(start),end=str(end),retrieved=len(ids))
                state=transaction
                for k,v in counts.items():runlog['counts'][k]+=v
                qlog.update(complete=True,retrieved=len(ids))
            except (SourceError,ValueError,KeyError,TypeError) as e:
                qlog['error']=str(e);runlog['errors'].append(dict(query=key,error=str(e)))
            runlog['queries'].append(qlog)
    # Monthly authoritative recheck of all known PMIDs, including historical base IDs.
    if monthly:
        ids=sorted({x['pmid'] for x in base['records']+list(state['records'].values()) if x.get('pmid')})
        qlog=dict(id='monthly-known-ids',complete=False,retrieved=0)
        try:
            transaction=copy.deepcopy(state);identity_index=IdentityIndex(base,transaction)
            for i in range(0,len(ids),config['fetch_batch_size']):
                for p in pub.fetch(ids[i:i+config['fetch_batch_size']]):
                    old=next((x for x in identity_index.matches(p).values() if x['id'] in transaction['records']),{})
                    p['classification']=old.get('classification') or classify(p,['mainline']);p['matched_queries']=['monthly-known-ids']
                    p=enrich(p,http,stamp,prior=old);merge_record(transaction,base,p,stamp,identity_index);qlog['retrieved']+=1
            state=transaction;qlog['complete']=True
        except (SourceError,ValueError) as e:qlog['error']=str(e);runlog['errors'].append(dict(query=qlog['id'],error=str(e)))
        runlog['queries'].append(qlog)
    if not runlog['errors']:
        state['last_successful_search']=stamp
        state.setdefault('monthly_anchor',stamp)
        if monthly:state['last_monthly']=stamp
    added=set(state['records'])-set(before)-{x['id'] for x in base['records']}
    updated={id for id,p in state['records'].items() if id not in added and metadata_content(p)!=metadata_content(before.get(id,{}))}
    runlog['processing_counts']=runlog['counts']
    runlog['counts']=dict(candidates=len(seen),added=len(added),updated=len(updated),conflicts=len(state['conflicts']),cumulative=len(state['records']))
    runlog.update(finished_at=now(),status='success' if not runlog['errors'] else 'degraded',unique_candidates=len(seen),requests=http.audit,
        pending_review=sum(p['classification']['pending'] for p in state['records'].values()))
    state['runs'].append(runlog)
    return state,runlog

def publish_snapshot(state,config,output,base=None):
    """A cumulative snapshot; manifest switched last. Old immutable versions retained."""
    output=Path(output);all_records=sorted(state['records'].values(),key=lambda p:p['id']);stamp=now()
    base=base if base is not None else read(ROOT/'base_index.json',{'records':[]})
    base_ids={p['id'] for p in base['records']};selection={}
    records=[]
    for p in all_records:
        reason=retention_reason(p,config,stamp,base_ids);selection[reason]=selection.get(reason,0)+1
        if reason in ('retained','extended_journal','baseline_metadata','jcr_unverified'):records.append(p)
    for p in records:validate_record(p)
    snapshot=dict(schema_version=1,domain_id=config['domain_id'],base_version=config['base_version'],records=records,conflicts=list(state['conflicts'].values()))
    raw=encoded(snapshot);version=digest(raw);name='snapshots/'+version+'.json'
    target=output/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    latest=state['runs'][-1] if state['runs'] else {}
    manifest=dict(schema_version=1,domain_id=config['domain_id'],base_version=config['base_version'],data_version=version,generated_at=now(),record_count=len(records),snapshot=name,sha256=version,
      last_attempt=latest.get('started_at'),last_successful_search=state['last_successful_search'],last_successful_publish=state['last_successful_publish'],
      run_status=latest.get('status','not_run'),queries=[{k:v for k,v in q.items() if k!='pages'} for q in latest.get('queries',[])],
      source_watermarks=state['watermarks'],counts=latest.get('counts',{}),pending_review=sum(p['classification']['pending'] for p in records),
      latest_event=latest.get('event'),schedule=dict(cron='17 1 * * 1',timezone='UTC',local_time='每周一北京时间09:17'),
      schedule_observed=any(r.get('event')=='schedule' for r in state['runs']),limitations=config['limitations'])
    if config.get('retention',{}).get('enabled'):
        manifest['retention']=dict(years=config['retention']['years'],cutoff=retention_cutoff(config,stamp).isoformat(),extended_years=config['retention'].get('extended_years',3),extended_cutoff=retention_cutoff(config,stamp,config['retention'].get('extended_years',3)).isoformat(),journal_whitelist_count=len(config['retention'].get('journal_whitelist',[])),as_of=stamp[:10],date_basis='earliest_electronic_or_journal_publication',counts=selection,stored_history_count=len(all_records))
        manifest['retention']['jcr']={k:v for k,v in config['retention'].get('jcr',{}).items() if k!='journals'}
        manifest['retention']['selection']=config['retention'].get('selection',{})
        manifest['retention']['quartile_pending']=selection.get('jcr_unverified',0)
    save(output/'manifest.json',manifest);save(output/'run-status.json',{k:v for k,v in latest.items() if k not in ['requests','queries']})
    (output/'.nojekyll').write_text('');return manifest

def main():
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['run','republish']);ap.add_argument('--state-dir',default='state');ap.add_argument('--output',default='public');ap.add_argument('--config',default=str(ROOT/'domain.json'));ap.add_argument('--base',default=str(ROOT/'base_index.json'));args=ap.parse_args()
    config=read(args.config);base=read(args.base);statepath=Path(args.state_dir)/'state.json'
    state=read(statepath,new_state(config))
    if args.command=='run':
        http=HTTP(max_requests=config['max_requests']);state,log=run(config,base,state,http)
        save(statepath,state);save(Path(args.state_dir)/'runs'/(log['id'].replace(':','-')+'.json'),log)
    manifest=publish_snapshot(state,config,args.output,base)
    print(json.dumps({k:manifest[k] for k in ['data_version','record_count','run_status','last_successful_search']},ensure_ascii=False))
    # Failure is explicit, but valid partial query results and status remain publishable.
    if state['runs'] and state['runs'][-1]['status']!='success':return 2
    return 0
if __name__=='__main__':raise SystemExit(main())
