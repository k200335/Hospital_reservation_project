from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = 'django-insecure-(t_$e)djiv%k4(e8mc&^$!dlerw4$-f58@$qsaju2_i9rd@j4p'
DEBUG = True
ALLOWED_HOSTS = ['*']

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'board', # 우리 앱
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ==========================================================
# Database 설정 (MySQL default / MS SQL mssql_db)
# ==========================================================
DATABASES = {
    'default': {  # MySQL (저장 및 배정현황 관리용)
        'ENGINE': 'django.db.backends.mysql',
        'NAME': 'kcqt_qyalit',
        'USER': 'kcqt_kyj',
        'PASSWORD': '1977519',
        'HOST': '221.155.228.179',
        'PORT': '3306',
    },
    'mssql': {  # Cafe24 MS SQL (데이터 조회용)
        'ENGINE': 'mssql',
        'NAME': 'kcqt77',
        'USER': 'kcqt77',
        'PASSWORD': 'a9465518*',
        'HOST': 'sql19-103.cafe24.com',
        'PORT': '',
        'OPTIONS': {
            'driver': 'ODBC Driver 17 for SQL Server',
        },
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# Internationalization (한국 설정)
LANGUAGE_CODE = 'ko-kr'
TIME_ZONE = 'Asia/Seoul'
USE_I18N = True
USE_TZ = False # DB 저장 시 한국 시간 그대로 저장

# Static files
STATIC_URL = '/static/'
STATICFILES_DIRS = [ BASE_DIR / 'static' ]

# Login/Logout Redirect
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# settings.py 파일 하단 확인
STATIC_URL = '/static/'

# 아래 설정이 들어있어야 폴더를 인식합니다.
STATICFILES_DIRS = [
    BASE_DIR / "static",
]
