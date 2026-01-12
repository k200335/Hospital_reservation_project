from django.contrib import admin
from django.urls import path
from django.shortcuts import render, redirect
from django.contrib.auth.forms import UserCreationForm
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.views.generic import TemplateView

# board 앱의 views를 정확하게 가져옵니다.
from board import views 

# 1. 회원가입 로직 (별도 파일로 분리하지 않았다면 여기에 유지)
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

# 2. CSI 접수 팝업창 로직
def csi_receipt_view(request):
    return render(request, 'csi_receipt.html', {'is_popup': True})

# 3. URL 경로 설정
urlpatterns = [
    # 메인 페이지 및 관리자
    path('', TemplateView.as_view(template_name='index.html'), name='home'),
    path('admin/', admin.site.urls),

    # 인증 관련 (로그인/로그아웃/회원가입)
    path('login/', auth_views.LoginView.as_view(template_name='index.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(next_page='/'), name='logout'), 
    path('signup/', signup, name='signup'),

    # --- 게시판 기능 (board 앱의 views 연결) ---
    # 메인 게시판 리스트
    path('board/', views.receipt_list, name='receipt_list'),

    # CSI_접수 팝업 경로
    path('board/csi_receipt/', csi_receipt_view, name='csi_receipt'),

    # CSI 데이터 크롤링 (fetch_csi_data)
    path('fetch-csi/', views.fetch_csi_data, name='fetch_csi_data'),
    
    # 담당자 배정현황 불러오기 (MySQL 조회)
    path('fetch-assignment-history/', views.fetch_assignment_history, name='fetch_assignment_history'),
    path('save-to-csi/', views.save_to_csi_receipts, name='save_to_csi_receipts'),
    path('search-by-date/', views.search_by_assign_date, name='search_by_assign_date'),
    path('board/csi_issue/', views.csi_issue_view, name='csi_issue'),
    path('board/fetch-csi-issue/', views.fetch_csi_issue_data, name='fetch_csi_issue_data'),
    path('fetch-csi-data/', views.fetch_csi_issue_data, name='fetch_csi_issue_data'),
    path('save-csi-matching/', views.save_csi_matching_data, name='save_csi_matching_data'),
    # 1. 화면을 보여주는 URL (request.html 렌더링)
    path('request/', views.request_page, name='request_page'),
    
    # 2. 데이터를 가져오는 API URL (AG-Grid가 호출하는 경로)
    path('fetch_combined_data/', views.fetch_combined_data, name='fetch_combined_data'),
    path('get_estimate_detail/', views.get_estimate_detail, name='get_estimate_detail'),
    path('field_payment/', views.field_payment_view, name='field_payment'),
    path('bizmeka-sync/', views.bizmeka_sync, name='bizmeka_sync'),
    # 비즈메카 QT DB 불러오는코드
    path('get_qt_db_data/', views.get_qt_db_data, name='get_qt_db_data'),
    # 1. CSI 성적서 발급 정보 조회 (기존)
    path('fetch-csi-issue-data/', views.fetch_csi_issue_data, name='fetch_csi_issue_data'),

    # 2. CSI 성적서 발급대기 정보 조회 (신규 추가)
    path('fetch-csi-wait-data/', views.fetch_csi_wait_data, name='fetch_csi_wait_data'),
    # 2. CSI 성적서 발급대기 정보 DB저장하기
    path('save-csi-wait-data/', views.save_csi_wait_data, name='save_csi_wait_data'),
    # ---- 여기서 부터 현장팀 정산 관련 db 불러오기
    path('api/get_payment_detail', views.get_payment_detail, name='get_payment_detail'),
    # ----완료건 보기 중 데이터 불러오기
    path('api/get_finished_data', views.get_finished_data, name='get_finished_data'),
    # 드롭다운 DB 연동
    path('api/get-item-standards/', views.get_item_standards, name='get_item_standards'),
    # 정산 완료 저장
    path('api/save-settlement/', views.save_settlement_data, name='save_settlement'),
    
    path('receipt_settle_admin/', views.receipt_settle_admin, name='receipt_settle_admin'),
    
    # 3번 영역: 데이터 불러오기 (MySQL 조회)
    path('get_panel3_data/', views.get_panel3_data, name='get_panel3_data'),

    # 3번 영역: 데이터 저장하기 (수정된 내용 반영)
    path('save_panel3_data/', views.save_panel3_data, name='save_panel3_data'),
    
    # 4번 패널 데이터 저장 (신규 INSERT / 기존 UPDATE)
    path('api/save_panel4_data', views.save_panel4_data, name='save_panel4_data'),
    
    # 4번 패널 데이터 조회 (DB 불러오기)
    path('api/get_panel4_data', views.get_panel4_data, name='get_panel4_data'),
    
    # 엑셀 다운로드 API
    path('api/download_field_excel/', views.download_field_excel, name='download_field_excel'),
    # DB 수정 API
    path('api/update_finished_list/', views.update_finished_list, name='update_finished_list'),

    path('board/csi_pending/', views.csi_pending_view, name='csi_pending'),

    path('board/save_field_team_data/', views.save_field_team_data, name='save_field_team_data'),
    
]
