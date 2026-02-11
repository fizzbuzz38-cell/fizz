"""
WSGI config for school project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""
import sys
from pathlib import Path
import os
import json
import logging

# Set up basic logging for startup diagnostics
logging.basicConfig(
    level=logging.DEBUG,
    format='[WSGI] [%(levelname)s] %(asctime)s - %(message)s'
)
wsgi_logger = logging.getLogger(__name__)

# Ensure the project parent directory is on sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# Add container path if it exists
container_path = '/app/111/school'
if container_path not in sys.path:
	try:
		if Path(container_path).exists():
			sys.path.insert(0, container_path)
	except Exception:
		pass

# Force correct settings module (override Railway's incorrect value if set)
os.environ['DJANGO_SETTINGS_MODULE'] = 'school.settings'

# Simple health check WSGI app
def health_app(environ, start_response):
	"""Minimal WSGI app that returns 200 OK."""
	status = '200 OK'
	response_headers = [('Content-Type', 'application/json')]
	start_response(status, response_headers)
	return [b'{"status": "ok"}']

# Try to import Django - if it fails, use health check only
_django_app = None
_error_message = None
_error_traceback = None

wsgi_logger.info("=" * 60)
wsgi_logger.info("Starting Django WSGI initialization...")
wsgi_logger.info("=" * 60)

try:
	from django.core.wsgi import get_wsgi_application
	_django_app = get_wsgi_application()
	wsgi_logger.info("[OK] Django WSGI application loaded successfully")
	print('[OK] Django WSGI application loaded successfully', flush=True)
	
	# Warm up database connection pool on first worker initialization
	try:
		from django.db import connections
		from django.db.utils import OperationalError
		db_conn = connections['default']
		wsgi_logger.info(f"Testing database connection to {db_conn.settings_dict['HOST']}...")
		print(f"[WSGI] Testing database connection to {db_conn.settings_dict['HOST']}...", flush=True)
		db_conn.cursor()
		wsgi_logger.info("[OK] Database connection successful!")
		print("[WSGI] [OK] Database connection successful!", flush=True)
	except OperationalError as e:
		wsgi_logger.error(f"[FAIL] Database connection failed: {e}")
		print(f"[WSGI] [FAIL] Database connection failed: {e}", flush=True)
	except Exception as e:
		wsgi_logger.error(f"[FAIL] Unexpected error connecting to database: {e}")
		print(f"[WSGI] [FAIL] Unexpected error connecting to database: {e}", flush=True)
except Exception as e:
	wsgi_logger.error(f"[FAIL] Failed to load Django: {e}", exc_info=True)
	print(f'[FAIL] Failed to load Django: {e}', flush=True)
	import traceback
	_error_traceback = traceback.format_exc()
	traceback.print_exc()
	_error_message = str(e)
	wsgi_logger.error(f"Traceback: {_error_traceback}")

	print(_error_traceback, flush=True)

# Main application wrapper
def application(environ, start_response):
	"""
	WSGI application that routes to Django if available,
	or returns a health check response.
	"""
	path_info = environ.get('PATH_INFO', '')
	request_method = environ.get('REQUEST_METHOD', 'GET')
	
	wsgi_logger.info(f"{'='*60}")
	wsgi_logger.info(f"[REQUEST] {request_method} {path_info}")
	wsgi_logger.info(f"{'='*60}")
	
	# Health check endpoint - always works
	if path_info in ('/health/', '/health'):
		wsgi_logger.info(f"Health check request: {path_info}")
		return health_app(environ, start_response)
	
	# If Django failed to load, return an error
	if _django_app is None:
		wsgi_logger.error(f"Django app not loaded. Returning 503 for {path_info}")
		status = '503 Service Unavailable'
		response_headers = [('Content-Type', 'application/json')]
		start_response(status, response_headers)
		error_dict = {
			'error': 'Django initialization failed',
			'message': str(_error_message),
			'traceback': str(_error_traceback) if _error_traceback else ''
		}
		error_json = json.dumps(error_dict).encode('utf-8')
		return [error_json]
	
	# Otherwise, use Django
	try:
		wsgi_logger.info(f"[DJANGO] Processing {request_method} {path_info}...")
		result = _django_app(environ, start_response)
		wsgi_logger.info(f"[DJANGO] Request completed successfully")
		return result
	except Exception as e:
		wsgi_logger.exception(f"[ERROR] Request failed for {path_info}: {e}")
		print(f'[ERROR] Request error: {e}', flush=True)
		import traceback
		error_trace = traceback.format_exc()
		print(error_trace, flush=True)
		wsgi_logger.error(f"[ERROR] Request traceback:\n{error_trace}")
		
		status = '500 Internal Server Error'
		response_headers = [('Content-Type', 'application/json')]
		try:
			start_response(status, response_headers)
		except Exception as start_resp_err:
			wsgi_logger.error(f"[ERROR] start_response failed: {start_resp_err}")
		
		# Use json.dumps to safely encode error details
		error_dict = {
			'error': 'Request failed',
			'message': str(e),
			'trace': error_trace[:1000]  # Limit trace length
		}
		error_json = json.dumps(error_dict).encode('utf-8')
		return [error_json]

