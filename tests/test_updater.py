import copy, datetime as dt, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import updater as u

STAMP='2026-09-14T01:17:00+00:00'
XML='''<PubmedArticle><MedlineCitation><PMID>1001</PMID><DateRevised><Year>2026</Year><Month>09</Month><Day>01</Day></DateRevised><Article><ArticleTitle>Myopia study fixture</ArticleTitle><Journal><Title>Fixture Journal</Title><JournalIssue><PubDate><Year>2025</Year><Month>Dec</Month></PubDate></JournalIssue></Journal><AuthorList><Author><LastName>Fixture</LastName><ForeName>Author</ForeName></Author></AuthorList><Abstract><AbstractText>Transient source abstract, not for public storage</AbstractText></Abstract><PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="doi">10.1234/FIXTURE</ArticleId></ArticleIdList><History><PubMedPubDate PubStatus="entrez"><Year>2026</Year><Month>08</Month><Day>30</Day></PubMedPubDate><PubMedPubDate PubStatus="pubmed"><Year>2025</Year><Month>12</Month><Day>03</Day></PubMedPubDate><PubMedPubDate PubStatus="revised"><Year>2025</Year><Month>11</Month><Day>02</Day></PubMedPubDate></History></PubmedData></PubmedArticle>'''
def raw():return u.parse_pubmed(u.ET.fromstring(XML))
def record():
    p=raw();p['classification']=u.classify(p,['mainline']);p['provenance']=[dict(source='PubMed',url='https://pubmed.ncbi.nlm.nih.gov/1001/')];return u.enrich(p,None,STAMP)
def config():
    c=u.read(u.ROOT/'domain.json');c['queries']=[dict(id='fixture',query='fixture[tiab]',route='mainline')];c['crossref_per_run']=0;c['fulltext_identity_checks_per_run']=0;return c
class NoHTTP:
    audit=[]
class FakePub:
    def __init__(self,*a):pass
    def search(self,q,f,s,e,log):return ['1001']
    def fetch(self,ids):return [raw()]
class FailedPub(FakePub):
    def search(self,q,f,s,e,log):raise u.SourceError('incomplete pagination')
class Tests(unittest.TestCase):
    def test_pubmed_date_semantics_and_precision(self):
        p=raw();self.assertEqual(p['dates']['created']['value'],'2026-08-30');self.assertEqual(p['dates']['entry']['value'],'2025-12-03');self.assertEqual(p['dates']['modified']['value'],'2026-09-01');self.assertEqual(p['dates']['journal']['value'],'2025-12');self.assertEqual(p['dates']['journal']['precision'],'month')
    def test_pubmed_book_chapter(self):
        xml='<PubmedBookArticle><BookDocument><PMID>36943999</PMID><ArticleIdList><ArticleId IdType="bookaccession">NBK589695</ArticleId></ArticleIdList><Book><BookTitle>Fixture handbook</BookTitle><PubDate><Year>2026</Year></PubDate></Book><ArticleTitle>Fixture chapter</ArticleTitle><AuthorList><Author><LastName>Author</LastName></Author></AuthorList></BookDocument></PubmedBookArticle>'
        p=u.parse_book(u.ET.fromstring(xml));self.assertEqual(p['pmid'],'36943999');self.assertIn('Book Chapter',p['publication_types']);p['classification']=u.classify(p,[]);p=u.enrich(p,None,STAMP);self.assertEqual(p['fulltext']['status'],'B');self.assertTrue(p['fulltext']['entries'][0]['url'].endswith('/NBK589695/'));self.assertIsNone(p['dates']['created'])
    def test_http_429_503_timeout_retry_and_invalid_json(self):
        from urllib.error import HTTPError, URLError
        class Response:
            status=200;url='https://eutils.ncbi.nlm.nih.gov/test'
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return b'{"ok":true}'
        for error in [HTTPError(Response.url,429,'rate limit',{},None),HTTPError(Response.url,503,'unavailable',{},None),URLError('timeout')]:
            with self.subTest(error=str(error)),patch.object(u.urllib.request,'urlopen',side_effect=[error,Response()]) as call,patch.object(u.time,'sleep'):
                http=u.HTTP();self.assertEqual(http.json(Response.url),{'ok':True});self.assertEqual(call.call_count,2);self.assertEqual(len(http.audit),2)
        with patch.object(u.HTTP,'get',return_value=b'not json'):
            with self.assertRaises(u.SourceError):u.HTTP().json(Response.url)
    def test_identity_index_equivalent_to_reference_merge(self):
        base={'records':[{'id':'DOI_fixture','doi':'10.1234/fixture','pmid':None}]};a=u.new_state(config());b=u.new_state(config());index=u.IdentityIndex(base,b)
        items=[record(),record()];conflict=record();conflict.update(id='PMID1002',pmid='1002');items.append(conflict)
        for p in items:self.assertEqual(u.merge_record(a,base,copy.deepcopy(p),STAMP),u.merge_record(b,base,copy.deepcopy(p),STAMP,index))
        self.assertEqual(a,b)
    def test_reverification_time_is_not_content_update(self):
        p=record();s=u.new_state(config());base={'records':[]};u.merge_record(s,base,p,STAMP);p=record();later='2026-09-21T01:17:00+00:00'
        for link in p['links']:link['checked_at']=later
        p['fulltext']['checked_at']=later
        self.assertEqual(u.merge_record(s,base,p,later),'unchanged');self.assertEqual(s['records']['PMID1001']['last_changed'],STAMP)
        p=copy.deepcopy(s['records']['PMID1001']);p['title']='Corrected title';self.assertEqual(u.merge_record(s,base,p,later),'updated')
    def test_crossref_notice_survives_pubmed_only_refresh(self):
        s=u.new_state(config());p=record();p['notices']=[dict(type='retraction',doi='10.1234/correction',source='https://api.crossref.org/works/10.1234/fixture')];u.merge_record(s,{'records':[]},p,STAMP)
        u.merge_record(s,{'records':[]},record(),STAMP);self.assertTrue(s['records']['PMID1001']['attention']);self.assertEqual(len(s['records']['PMID1001']['notices']),1)
    def test_partial_dates(self):
        self.assertEqual(u.date_value(u.ET.fromstring('<D><Year>2025</Year></D>'))['value'],'2025');self.assertEqual(u.date_value(u.ET.fromstring('<D><MedlineDate>2025 Winter</MedlineDate></D>'))['precision'],'text')
    def test_normalized_doi_and_safe_protocols(self):
        self.assertEqual(u.doi('https://doi.org/10.1234/ABC'),'10.1234/abc')
        for value in ['javascript:alert(1)','http://example.com','https://user:pass@example.com','https://example.com/\nx']:self.assertFalse(u.safe_url(value))
    def test_abstract_never_persisted(self):
        p=record();u.validate_record(p);self.assertNotIn('_abstract',p);self.assertNotIn('Transient source abstract',json.dumps(p));p['private_notes']='x'
        with self.assertRaises(ValueError):u.validate_record(p)
    def test_stable_alias_and_no_duplicates(self):
        c=config();s=u.new_state(c);base={'records':[{'id':'DOI_fixture','doi':'10.1234/fixture','pmid':None}]}
        self.assertEqual(u.merge_record(s,base,record(),STAMP),'updated');self.assertEqual(list(s['records']),['DOI_fixture']);self.assertIn('PMID1001',s['records']['DOI_fixture']['aliases'])
        self.assertEqual(u.merge_record(s,base,record(),STAMP),'unchanged');self.assertEqual(len(s['records']),1)
    def test_identifier_conflict_is_retained(self):
        s=u.new_state(config());base={'records':[{'id':'PMID9999','pmid':'9999','doi':'10.1234/fixture'}]};self.assertEqual(u.merge_record(s,base,record(),STAMP),'conflict');self.assertFalse(s['records']);self.assertEqual(len(s['conflicts']),1);self.assertIn('candidate',next(iter(s['conflicts'].values())))
    def test_missing_doi_is_valid_missing_all_ids_is_not(self):
        p=record();p['doi']=None;u.validate_record(p);p['pmid']=None
        with self.assertRaises(ValueError):u.validate_record(p)
    def test_version_relations_do_not_fuzzy_merge(self):
        s=u.new_state(config());p=record();p['version_relations']=[dict(doi='10.1234/preprint',type='has-preprint',verified=True)];u.merge_record(s,{'records':[]},p,STAMP);p=record();p.update(id='PMID1002',pmid='1002',doi='10.1234/preprint');u.merge_record(s,{'records':[]},p,STAMP);self.assertEqual(len(s['records']),2)
    def test_date_overlap_and_changed_rules(self):
        c=config();s=u.new_state(c);q=c['queries'][0];key,sig,start,end=u.window(c,s,q,'crdt',STAMP);self.assertEqual((end-start).days,90)
        s['watermarks'][key]={'signature':sig,'successful_at':'2026-08-17T00:00:00+00:00'};self.assertEqual(str(u.window(c,s,q,'crdt',STAMP)[2]),'2026-07-18')
        q['query']='changed';self.assertEqual((u.window(c,s,q,'crdt',STAMP)[3]-u.window(c,s,q,'crdt',STAMP)[2]).days,90)
    def test_failed_query_does_not_advance(self):
        c=config();s=u.new_state(c)
        with patch.object(u,'PubMed',FakePub):s,log=u.run(c,{'records':[]},s,NoHTTP(),STAMP)
        old=copy.deepcopy(s['watermarks'])
        with patch.object(u,'PubMed',FailedPub):result,log=u.run(c,{'records':[]},s,NoHTTP(),'2026-09-21T01:17:00+00:00')
        self.assertEqual(result['watermarks'],old);self.assertEqual(log['status'],'degraded');self.assertEqual(result['last_successful_search'],STAMP);self.assertEqual(len(result['records']),1)
    def test_fresh_state_restore_idempotent_and_monthly_due(self):
        c=config()
        with patch.object(u,'PubMed',FakePub):
            s,l=u.run(c,{'records':[]},u.new_state(c),NoHTTP(),STAMP);s2,l2=u.run(c,{'records':[]},json.loads(json.dumps(s)),NoHTTP(),STAMP)
            self.assertEqual(s['records'],s2['records']);self.assertFalse(l2['monthly'])
            s3,l3=u.run(c,{'records':[]},s2,NoHTTP(),'2026-10-19T01:17:00+00:00');self.assertTrue(l3['monthly']);self.assertTrue(any(x['id']=='monthly-known-ids' for x in l3['queries']))
    def test_full_pagination_and_truncation(self):
        class H:
            def json(self,url,p):
                ids=[str(x) for x in range(7)];return {'esearchresult':{'count':'7','idlist':ids[p['retstart']:p['retstart']+p['retmax']]}}
        pub=u.PubMed(H(),3);log=[];ids=pub.search('fixture','crdt',dt.date(2026,1,1),dt.date(2026,1,2),log);self.assertEqual(len(ids),7);self.assertEqual(log[0]['pages'],3)
        class Broken(H):
            def json(self,url,p):
                r=super().json(url,p)
                if p['retstart']:r['esearchresult']['idlist']=[]
                return r
        with self.assertRaises(u.SourceError):u.PubMed(Broken(),3).search('x','lr',dt.date(2026,1,1),dt.date(2026,1,2),[])
    def test_search_over_10000_is_date_split(self):
        class H:
            def json(self,url,p):
                full='2026/01/01"[crdt] : "2026/01/02' in p['term'];ids=['1'] if '2026/01/01"[crdt] :' in p['term'] else ['2'];return {'esearchresult':{'count':'10001' if full else '1','idlist':ids if p['retmax'] else []}}
        log=[];ids=u.PubMed(H(),500).search('x','crdt',dt.date(2026,1,1),dt.date(2026,1,2),log);self.assertEqual(ids,['1','2']);self.assertEqual(len(log),2)
    def test_body_identity_not_login_or_other_paper(self):
        p=record();body='<article><front><article-meta><article-id pub-id-type="pmid">1001</article-id><title-group><article-title>Myopia study fixture</article-title></title-group></article-meta></front><body><p>'+('Actual fixture body. '*100)+'</p></body></article>'
        self.assertTrue(u.official_fulltext_identity(body.encode(),p));self.assertFalse(u.official_fulltext_identity(body.replace('1001','1002').encode(),p));self.assertFalse(u.official_fulltext_identity(b'<html>Login verification CAPTCHA</html>',p))
    def test_403_not_marked_missing_index(self):
        class H:
            def get(self,*a):raise u.SourceError('HTTP 403')
        p=raw();p['pmcid']='PMC1001';p['classification']=u.classify(p,[]);result=u.enrich(p,H(),STAMP,fulltext=True);self.assertEqual(result['fulltext']['status'],'B');self.assertIn('403',result['fulltext']['reason'])
    def test_ai_alone_pending_concrete_transfer_concept_only(self):
        p=raw();p['title']='AI cancer model';p['_mesh']=[];self.assertTrue(u.classify(p,['transfer'])['pending']);p['title']='Spatial transcriptomics of cancer fibroblasts';c=u.classify(p,['transfer']);self.assertFalse(c['pending']);self.assertEqual(c['family'],'cross_domain');self.assertEqual(c['maturity'],'概念性借鉴')
    def test_validated_snapshot_atomic_hash_and_privacy(self):
        c=config();s=u.new_state(c);u.merge_record(s,{'records':[]},record(),STAMP)
        with tempfile.TemporaryDirectory() as tmp:
            m=u.publish_snapshot(s,c,tmp);rawdata=(Path(tmp)/m['snapshot']).read_bytes();self.assertEqual(u.digest(rawdata),m['sha256']);before=(Path(tmp)/'manifest.json').read_bytes();s['records']['PMID1001']['private_notes']='secret'
            with self.assertRaises(ValueError):u.publish_snapshot(s,c,tmp)
            self.assertEqual((Path(tmp)/'manifest.json').read_bytes(),before)
    def test_retraction_is_attention(self):
        p=raw();p['publication_types'].append('Retracted Publication');p['classification']=u.classify(p,[]);self.assertTrue(u.enrich(p,None,STAMP)['attention'])
if __name__=='__main__':unittest.main()
