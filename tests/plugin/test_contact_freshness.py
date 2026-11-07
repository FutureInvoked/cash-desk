"""Authoritative contact updates revoke tool/Desktop approval and redraft safely."""
from tests.plugin.test_api import client


def test_imported_contact_update_revokes_desktop_authority_and_redrafts(plugin,hermes_fakes,client):
 module,ctx=plugin;folder=hermes_fakes/'plugin-data/cash-desk';folder.mkdir(parents=True,exist_ok=True)
 texts={'customers':'customer_id,name,email,contact_name\nC1,Acme Ltd,old@acme.example,Ann\n',
 'invoices':'number,customer_id,issue_date,due_date,amount,balance,currency\nINV-1001,C1,2026-10-01,2026-10-20,100,100,EUR\n'}
 for kind,text in texts.items():
  p=folder/(kind+'.csv');p.write_text(text);assert ctx.call('cashdesk_import',kind=kind,path=str(p))['ok']
 old=ctx.call('cashdesk_draft',as_of='2026-11-10')['drafts'][0];assert client[0].post(f"/drafts/{old['id']}/approve",json={'hash':old['hash']}).status_code==200
 p=folder/'contacts-current.csv';p.write_text('customer_id,name,email,contact_name\nC1,Acme Ltd,new@acme.example,Ben\n');assert ctx.call('cashdesk_import',kind='customers',path=str(p))['ok']
 assert client[0].post(f"/drafts/{old['id']}/approve",json={'hash':old['hash']}).status_code==409
 assert 'customer contact changed' in ctx.command('approve '+old['id']+' '+old['hash'])
 with module._store() as s:assert s.approval(old['id']) is None and s.get_draft(old['id']).recipients==('old@acme.example',)
 fresh=ctx.call('cashdesk_draft',as_of='2026-11-10')['drafts'][0]
 with module._store() as s:
  d=s.get_draft(fresh['id']);assert d.recipients==('new@acme.example',) and 'Ben' in d.body and s.approval(d.id) is None
 assert client[0].post(f"/drafts/{fresh['id']}/approve",json={'hash':fresh['hash']}).status_code==200
