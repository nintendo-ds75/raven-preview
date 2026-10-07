"""Conservative as-of filtering for external context, with explicit rejection receipts."""
import datetime,hashlib,json

def epoch(value):
 if not isinstance(value,str) or not value:return None
 try:
  parsed=datetime.datetime.fromisoformat(value.replace('Z','+00:00'))
  if parsed.tzinfo is None:return None
  return parsed.timestamp()
 except ValueError:return None

def admit(record,cutoff,allowed_commits,trusted_receipts=frozenset()):
 """No temporal field is silently replaced by ingestion time."""
 ceiling=epoch(cutoff)
 if ceiling is None:raise ValueError('Timezone-qualified cutoff required')
 identity={k:record.get(k) for k in ('provider','namespace','kind','external_id')}
 reason=''
 mode=record.get('version_basis')
 if mode=='git_immutable':
  if record.get('commit_id') not in allowed_commits:reason='commit_not_in_cutoff_ancestry'
 elif mode=='immutable_event':
  if record.get('immutable_snapshot_sha256') not in trusted_receipts:reason='immutable_receipt_not_independently_verified'
  elif epoch(record.get('occurred_at')) is None:reason='event_time_unknown'
  elif epoch(record['occurred_at'])>=ceiling:reason='event_after_cutoff'
 else:
  created=epoch(record.get('created_at'));updated=epoch(record.get('updated_at'))
  if created is None or updated is None:reason='mutable_body_revision_time_unknown'
  elif created>=ceiling or updated>=ceiling:reason='mutable_record_created_or_updated_after_cutoff'
 if record.get('review_commit_id') and record['review_commit_id'] not in allowed_commits:reason=reason or 'reviewed_commit_not_in_cutoff_ancestry'
 if record.get('provider_access') is None:reason=reason or 'provider_access_unknown'
 elif not isinstance(record['provider_access'],dict) or record['provider_access'].get('authorized') is not True:reason=reason or 'provider_access_not_authorized'
 return {'accepted':not reason,'reason':reason or 'within_declared_cutoff_and_access_scope','identity':identity,'record_sha256':hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
