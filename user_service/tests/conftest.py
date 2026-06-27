import os
import sys

# Add edura-backend root to sys.path so `shared` package is importable
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Add service root so local modules (database, models, router) are importable
_SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC not in sys.path:
    sys.path.insert(0, _SVC)
