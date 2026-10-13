from datetime import timedelta
import unittest
from itsdangerous import TimestampSigner, URLSafeTimedSerializer, SignatureExpired
class Clock(TimestampSigner):
    clock = 100
    def get_timestamp(self): return type(self).clock
class Acceptance(unittest.TestCase):
    def setUp(self):
        Clock.clock=100
        self.s=Clock('fixture-secret')
        self.token=self.s.sign(b'payload')
    def test_default_still_strict(self):
        Clock.clock=106
        with self.assertRaises(SignatureExpired): self.s.unsign(self.token,max_age=5)
    def test_expiry_boundary_is_inclusive(self):
        Clock.clock=107
        self.assertEqual(self.s.unsign(self.token,max_age=5,leeway=2),b'payload')
        Clock.clock=108
        with self.assertRaises(SignatureExpired): self.s.unsign(self.token,max_age=5,leeway=2)
    def test_future_boundary_is_inclusive(self):
        Clock.clock=98
        self.assertEqual(self.s.unsign(self.token,max_age=0,leeway=2),b'payload')
        Clock.clock=97
        with self.assertRaises(SignatureExpired): self.s.unsign(self.token,max_age=0,leeway=2)
    def test_types_and_negative(self):
        for value in (True,False,1.5,'2',timedelta(seconds=2)):
            with self.subTest(value=value), self.assertRaises(TypeError): self.s.unsign(self.token,max_age=5,leeway=value)
        with self.assertRaises(ValueError): self.s.unsign(self.token,max_age=5,leeway=-1)
    def test_no_expiry_is_only_allowed_with_zero(self):
        self.assertEqual(self.s.unsign(self.token),b'payload')
        with self.assertRaises(ValueError): self.s.unsign(self.token,leeway=1)
    def test_validate_preserves_argument_errors(self):
        with self.assertRaises(ValueError): self.s.validate(self.token,leeway=1)
        with self.assertRaises(TypeError): self.s.validate(self.token,max_age=5,leeway=True)
    def test_serializer_and_unsafe_forward_leeway(self):
        s=URLSafeTimedSerializer('fixture-secret',signer=Clock)
        token=s.dumps({'value':1});Clock.clock=107
        self.assertEqual(s.loads(token,max_age=5,leeway=2),{'value':1})
        self.assertEqual(s.loads_unsafe(token,max_age=5,leeway=2),(True,{'value':1}))
        with self.assertRaises(ValueError): s.loads_unsafe(token,leeway=1)
if __name__=='__main__': unittest.main(verbosity=2)
