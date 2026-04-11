"""
Shared rate-limiter instance.

Defined here (not in main.py) so route modules can import it without
creating a circular import through main.py.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
