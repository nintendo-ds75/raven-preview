import inspect
from unittest.mock import patch
import pytest
from itsdangerous import TimedSerializer,URLSafeTimedSerializer,TimestampSigner,SignatureExpired

@pytest.mark.parametrize('cls',[TimedSerializer,URLSafeTimedSerializer])
def test_signed_default_contract(cls):
 with patch('itsdangerous.timed.time.time',return_value=1000):
  old=cls('secret');limited=cls('secret',default_max_age=10);token=limited.dumps({'ok':True})
 with patch('itsdangerous.timed.time.time',return_value=1010):assert limited.loads(token)=={'ok':True}
 with patch('itsdangerous.timed.time.time',return_value=1011):
  with pytest.raises(SignatureExpired):limited.loads(token)
  with pytest.raises(SignatureExpired):limited.loads(token,max_age=None)
  assert limited.loads(token,max_age=20)=={'ok':True}
  assert limited.loads_unsafe(token)==(False,{'ok':True})
  assert limited.loads_unsafe(token,max_age=20)==(True,{'ok':True})
  assert old.loads(token)=={'ok':True}
 with patch('itsdangerous.timed.time.time',return_value=999):
  with pytest.raises(SignatureExpired):limited.loads(token)

@pytest.mark.parametrize('cls',[TimedSerializer,URLSafeTimedSerializer])
def test_zero_and_negative(cls):
 with pytest.raises(ValueError):cls('secret',default_max_age=-1)
 with patch('itsdangerous.timed.time.time',return_value=1000):
  limited=cls('secret',default_max_age=0);token=limited.dumps('a');assert limited.loads(token)=='a'
 with patch('itsdangerous.timed.time.time',return_value=1001):
  with pytest.raises(SignatureExpired):limited.loads(token)

def test_signer_unchanged():
 assert 'default_max_age' not in inspect.signature(TimestampSigner).parameters
 assert not hasattr(TimestampSigner('secret'),'default_max_age')

@pytest.mark.parametrize('cls',[TimedSerializer,URLSafeTimedSerializer])
def test_fallback_cannot_bypass_age(cls):
 import hashlib
 with patch('itsdangerous.timed.time.time',return_value=1000):
  token=cls('secret',signer_kwargs={'digest_method':hashlib.sha1}).dumps('legacy')
  current=cls('secret',default_max_age=10,signer_kwargs={'digest_method':hashlib.sha512},fallback_signers=[{'digest_method':hashlib.sha1}])
 with patch('itsdangerous.timed.time.time',return_value=1005):assert current.loads(token)=='legacy'
 with patch('itsdangerous.timed.time.time',return_value=1011):
  with pytest.raises(SignatureExpired):current.loads(token)
  assert current.loads_unsafe(token)==(False,'legacy')
  assert current.loads(token,max_age=20)=='legacy'
