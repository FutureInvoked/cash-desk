"""Legal entity review remains placeable through plugin commands and Desktop API."""
from tests.plugin.test_api import client


def test_legal_conflict_is_visible_without_automatic_settlement(plugin,hermes_fakes,client):
 module,ctx=plugin;folder=hermes_fakes/'plugin-data/cash-desk';folder.mkdir(parents=True,exist_ok=True)
 data={'customers':'customer_id,name,email\nC1,Acme Ltd,ap@acme.example\n',
 'invoices':'number,customer_id,issue_date,due_date,amount,balance,currency\nINV-1001,C1,2026-10-01,2026-10-20,100,100,EUR\n',
 'bank':'id,date,amount,currency,counterparty,reference\nP1,2026-11-03,100,EUR,Acme GmbH,INV-1001\n'}
 for kind,text in data.items():
  path=folder/(kind+'.csv');path.write_text(text);assert ctx.call('cashdesk_import',kind=kind,path=str(path))['ok']
 result=ctx.call('cashdesk_match',as_of='2026-11-10');assert not result['applied'] and result['review'][0]['kind']=='PAYER'
 row=client[0].get('/review').json()['payments'][0];assert row['incoming'] and row['counterparty']=='Acme GmbH' and row['named']==['INV-1001']
 assert 'payer identity needs review' in ctx.command('review')
 with module._store() as s:assert not s.list_matches() and s.get_invoice('INV-1001').balance==100
 assert 'paid in full' in ctx.command('apply P1 INV-1001').lower()
 with module._store() as s:assert s.list_matches()[0].method=='person'
