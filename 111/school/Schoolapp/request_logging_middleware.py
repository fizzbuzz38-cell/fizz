import logging
import time
import json
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

class RequestLoggingMiddleware(MiddlewareMixin):
    """
    Middleware to log every request and response, including duration.
    Helps in debugging timeouts and infinite loading issues.
    """

    def process_request(self, request):
        request.start_time = time.time()
        
        # Log request details
        try:
            body_preview = ''
            if request.content_type == 'application/json':
                try:
                    body_preview = f" Body: {request.body.decode('utf-8')[:100]}..." 
                except:
                    pass
            
            logger.info(f"==> [REQUEST START] {request.method} {request.get_full_path()} "
                        f"IP: {self.get_client_ip(request)}{body_preview}")
        except Exception:
            pass

    def process_response(self, request, response):
        duration = time.time() - getattr(request, 'start_time', time.time())
        status_code = getattr(response, 'status_code', 0)
        
        logger.info(f"<== [REQUEST END] {request.method} {request.get_full_path()} "
                    f"Status: {status_code} Duration: {duration:.3f}s")
        return response

    def process_exception(self, request, exception):
        duration = time.time() - getattr(request, 'start_time', time.time())
        logger.error(f"!!! [REQUEST EXCEPTION] {request.method} {request.get_full_path()} "
                     f"Duration: {duration:.3f}s Error: {str(exception)}", exc_info=True)
        return None

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip
