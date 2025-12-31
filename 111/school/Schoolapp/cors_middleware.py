"""
Custom CORS Middleware for Mobile App
Add this to your Django settings to allow cross-origin requests from the mobile app.
"""

class CORSMiddleware:
    """
    Simple CORS middleware to allow the mobile app to communicate with the API.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response
    
    def __call__(self, request):
        # Handle preflight OPTIONS requests
        if request.method == 'OPTIONS':
            response = self._create_preflight_response(request)
            return response
        
        # Process the request normally
        response = self.get_response(request)
        
        # Add CORS headers
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PATCH'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, Accept, Origin'
        
        return response
    
    def _create_preflight_response(self, request):
        """Create a response for preflight OPTIONS requests."""
        from django.http import HttpResponse
        
        response = HttpResponse()
        
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS, PATCH'
        response['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With, Accept, Origin'
        response['Access-Control-Max-Age'] = '86400'  # 24 hours
        
        response.status_code = 200
        return response
