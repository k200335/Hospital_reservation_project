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
from board.views import receipt_list  # board 앱의 함수를 불러옴

# 1. 회원가입 로직 (함수 뷰)
def signup(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            form.save()
            username = form.cleaned_data.get('username')
            messages.success(request, f'{username}님, 회원가입이 완료되었습니다! 로그인해 주세요.')
            return redirect('login')
    else:
        form = UserCreationForm()
    return render(request, 'signup.html', {'form': form})

# 2. URL 경로 설정
urlpatterns = [
    # 메인 페이지 (index.html)
    path('', TemplateView.as_view(template_name='index.html'), name='home'),
    
    # 관리자 페이지 (하나만 남김)
    path('admin/', admin.site.urls),
    
    # 로그인/로그아웃 페이지
    path('login/', auth_views.LoginView.as_view(template_name='index.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='home'), name='logout'), 
    
    # 회원가입 페이지
    path('signup/', signup, name='signup'),
    
    # 접수 목록 페이지 (성공하셨던 그 주소!)
    path('list/', receipt_list, name='receipt_list'),
]

