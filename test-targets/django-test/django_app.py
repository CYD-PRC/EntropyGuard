"""
Django 2.0.7 vulnerable test application
Vulnerabilities:
- DEBUG=True in production (information disclosure)
- CSRF exempt endpoints (CVE-2019-3498 adjacent)
- XSS in api/echo
- Exposed admin endpoint
"""
import os
import sys

import django
from django.conf import settings
from django.urls import path
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib import admin
from django.core.management import execute_from_command_line

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY='vulnerable-secret-key-12345678',
        ALLOWED_HOSTS=['*'],
        ROOT_URLCONF=__name__,
        MIDDLEWARE=[
            'django.middleware.security.SecurityMiddleware',
            'django.contrib.sessions.middleware.SessionMiddleware',
            'django.middleware.common.CommonMiddleware',
            'django.middleware.csrf.CsrfViewMiddleware',
            'django.contrib.auth.middleware.AuthenticationMiddleware',
            'django.contrib.messages.middleware.MessageMiddleware',
        ],
        INSTALLED_APPS=[
            'django.contrib.contenttypes',
            'django.contrib.auth',
            'django.contrib.sessions',
            'django.contrib.admin',
            'django.contrib.messages',
            'django.contrib.staticfiles',
        ],
        DATABASES={
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
            }
        },
        TEMPLATES=[{
            'BACKEND': 'django.template.backends.django.DjangoTemplates',
            'DIRS': [],
            'APP_DIRS': True,
            'OPTIONS': {
                'context_processors': [
                    'django.template.context_processors.debug',
                    'django.template.context_processors.request',
                    'django.contrib.auth.context_processors.auth',
                    'django.contrib.messages.context_processors.messages',
                ],
            },
        }],
        STATIC_URL='/static/',
        CSRF_COOKIE_HTTPONLY=False,
        CSRF_COOKIE_SECURE=False,
    )

django.setup()

# Vulnerable: CSRF exempt + command injection surface
@csrf_exempt
def api_exec(request):
    cmd = request.GET.get('cmd', '') or request.POST.get('cmd', '')
    return JsonResponse({
        'status': 'ok',
        'cmd': cmd,
        'result': '[sandbox] received: ' + cmd,
        'debug_enabled': settings.DEBUG,
    })

# Vulnerable: XSS via reflected input
def api_echo(request):
    msg = request.GET.get('msg', '')
    return HttpResponse('<html><body>You said: ' + msg + '</body></html>')

# Exposed config endpoint
def api_config(request):
    return JsonResponse({
        'secret_key': settings.SECRET_KEY,
        'debug': settings.DEBUG,
        'allowed_hosts': settings.ALLOWED_HOSTS,
        'csrf_cookie_httponly': settings.CSRF_COOKIE_HTTPONLY,
    })

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/exec', api_exec),
    path('api/echo', api_echo),
    path('api/config', api_config),
    path('', lambda r: JsonResponse({
        'app': 'Django',
        'version': django.get_version(),
        'debug': settings.DEBUG,
        'vulnerabilities': ['DEBUG mode', 'CSRF bypass', 'XSS', 'Secret key disclosure'],
    })),
]

if __name__ == '__main__':
    execute_from_command_line([sys.argv[0], 'migrate', '--run-syncdb'])
    print("[!] Django 2.0.7 starting with DEBUG=True (vulnerable)")
    print("[!] CVE-2019-3498: CSRF bypass risk")
    print("[!] Endpoints: /admin/, /api/exec, /api/echo, /api/config")
    execute_from_command_line([sys.argv[0], 'runserver', '0.0.0.0:8080'])
