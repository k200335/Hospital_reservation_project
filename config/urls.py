"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
"""from django.contrib import admin
from django.urls import path

urlpatterns = [
    path('admin/', admin.site.urls),
]"""

from django.contrib import admin
from django.urls import path
from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView
from board.views import receipt_list  # board 앱의 함수

# [중요] 여기서부터 에러 해결 부분입니다. 
# config 폴더에서 views를 import 하려던 줄을 삭제하세요.

# 1. 회원가입 로직 (이미 잘 작동하던 코드)
def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            messages.success(request, f'{username}님, 가입 완료되었습니다.')
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'signup.html', {'form': form})

# 🚀 추가: save_csi_receipt 함수도 urls.py에 직접 만듭니다.
def save_csi_receipt(request):
    return render(request, 'save_csi_receipt.html')

# 2. URL 경로 설정
urlpatterns = [
    path('', TemplateView.as_view(template_name='index.html'), name='home'),
    path('admin/', admin.site.urls),
    path('login/', auth_views.LoginView.as_view(template_name='index.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'), 
    path('signup/', signup, name='signup'),
    path('board/', receipt_list, name='board'),

    # 🚀 여기를 views. 없이 함수 이름만 적어주세요!
    path('save_csi_receipt/', save_csi_receipt, name='save_csi_receipt'),
]

