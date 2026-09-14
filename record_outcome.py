"""On failed deployment, keep the previous known-success release timestamp."""
import os,sys
from pathlib import Path
from updater import read,save,now
root=Path(sys.argv[1]);state=read(root/'state/state.json')
if state:
    result=dict(deployment=os.environ.get('DEPLOYMENT_OUTCOME','unknown'),verification=os.environ.get('VERIFICATION_OUTCOME','unknown'),recorded_at=now())
    state['last_deployment_attempt']=result;save(root/'state/state.json',state)
    if result['deployment']!='success' or result['verification']!='success':
        manifest=read(root/'public/manifest.json',{})
        save(root/'publication-status.json',dict(schema_version=1,domain_id=state['domain_id'],base_version=state['base_version'],data_version=manifest.get('data_version'),status='failed',checked_at=now(),error='Publication or external verification failed; previous verified release is retained',last_successful_publish=state.get('last_successful_publish')))
