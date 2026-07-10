import os
import sys

# Allow importing shared/ from edura-backend root
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Allow importing content_service modules
_SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SVC not in sys.path:
    sys.path.insert(0, _SVC)

os.environ.setdefault("SECRET_KEY", "test-secret-for-content-service")
os.environ.setdefault("CLOUDINARY_CLOUD_NAME", "test-cloud")
os.environ.setdefault("CLOUDINARY_API_KEY", "test-key")
os.environ.setdefault("CLOUDINARY_API_SECRET", "test-secret")
os.environ.setdefault("ENROLLMENT_SERVICE_URL", "http://localhost:8005")
