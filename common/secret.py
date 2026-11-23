# the notion token travels inside queue messages, so keep it encrypted there
import base64
import hashlib

from cryptography.fernet import Fernet

from . import config

_f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.TOKEN_SECRET.encode()).digest()))


def seal(s):
    return _f.encrypt(s.encode()).decode()


def unseal(s):
    return _f.decrypt(s.encode()).decode()
