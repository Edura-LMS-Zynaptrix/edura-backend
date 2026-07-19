import os
import sys

# edura-backend root → makes `shared` importable
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# course_service root → makes local modules importable
_SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC not in sys.path:
    sys.path.insert(0, _SVC)
