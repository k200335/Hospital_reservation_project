import json
import time
import traceback
from django.http import JsonResponse
from django.http import HttpResponse
from django.utils import timezone  # 현재 시간 저장을 위해 추가
from django.shortcuts import render
from django.db import connection, connections
from django.views.decorators.csrf import csrf_exempt
from django.db import transaction  # 이 줄이 반드시 있어야 합니다!
from selenium.webdriver.common.action_chains import ActionChains
from django.views.decorators.csrf import csrf_exempt

# 셀레늄 및 크롤링 관련
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
# 모델 임포트 (클래스명을 CsiReceipt로 통일)
from .models import ProjectManagerMap
from .models import OuterreceiptNew, CsiReceipt
from datetime import datetime
import calendar  # 날짜 계산용
import traceback # 에러 상세 출력용 (이번 에러 해결 핵심)
from selenium.common.exceptions import UnexpectedAlertPresentException, NoAlertPresentException, TimeoutException

import xlwings as xw
from io import BytesIO
import os

import pythoncom
from django.conf import settings
import uuid # 고유 파일명을 위해 추가
from .models import ClientProject
from .models import ConsultMemo

from django.db.models import Sum
from .models import transactions
from django.shortcuts import render, get_object_or_404, redirect
from .models import TransactionCategory




def receipt_list(request):
    search_type = request.GET.get('search_type', 'rqcode')
    search_value = request.GET.get('search_value', '')
    date_type = request.GET.get('date_type', 'receiveday')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')

    receipts = OuterreceiptNew.objects.all().order_by('-idx')

    if search_value:
        filter_kwargs = {f"{search_type}__icontains": search_value}
        receipts = receipts.filter(**filter_kwargs)

    if start_date and end_date:
        date_filter = {f"{date_type}__range": [start_date, end_date]}
        receipts = receipts.filter(**date_filter)

    return render(request, 'board.html', {
        'receipts': receipts, 'search_type': search_type, 'search_value': search_value,
        'date_type': date_type, 'start_date': start_date, 'end_date': end_date,
    })

def save_csi_receipt(request):
    return render(request, 'save_csi_receipt.html')


# --- [2] CSI 사이트 데이터 크롤링 (Selenium) ---

@csrf_exempt
def fetch_csi_data(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '잘못된 접근입니다.'})

    driver = None
    try:
        data = json.loads(request.body)
        rq_numbers = data.get('rq_numbers', [])
        if not rq_numbers:
            return JsonResponse({'status': 'error', 'message': '선택된 RQ번호가 없습니다.'})

        # 브라우저 설정
        chrome_options = Options()
        # chrome_options.add_argument("--headless") # 필요시 주석 처리 (창 보기)
        chrome_options.add_argument("--no-sandbox")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        wait = WebDriverWait(driver, 10)

        # 로그인 로직
        driver.get("https://gcloud.csi.go.kr/cmq/main.do")
        wait.until(EC.element_to_be_clickable((By.ID, "userId"))).send_keys("youngjun")
        driver.find_element(By.ID, "pswd").send_keys("k*1800*92*")
        driver.find_element(By.CLASS_NAME, "login-btn").click()
        
        time.sleep(2)
        final_results = []

        for rq_no in rq_numbers:
            try:
                driver.get("https://gcloud.csi.go.kr/cmq/qtr/qltRqst/rqstRcvList.do")
                search_input = wait.until(EC.element_to_be_clickable((By.ID, "searchVal")))
                search_input.clear()
                search_input.send_keys(rq_no)
                driver.find_element(By.XPATH, "//button[contains(@onclick, 'go_search')]").click()
                
                time.sleep(1.5)
                wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "goSelectLink"))).click()
                wait.until(EC.presence_of_element_located((By.XPATH, "//th[contains(text(), '접수번호')]")))

                # 데이터 추출

                rcpt_no = driver.find_element(By.XPATH, "//th[contains(text(), '접수번호')]/following-sibling::td").text.strip()
                rcpt_date = driver.find_element(By.XPATH, "//th[contains(text(), '접수일시')]/following-sibling::td").text.strip()
                status = driver.find_element(By.XPATH, "//th[contains(text(), '최종진행상태')]/following-sibling::td").text.strip()
                biz_nm = driver.find_element(By.XPATH, "//th[text()='공사명']/following-sibling::td").text.strip()
                agency = driver.find_element(By.XPATH, "//th[contains(text(), '의뢰기관')]/following-sibling::td").text.strip()                

                    # 채취자 및 봉인명 추출 (에러 방지용 try-except)
                try:
                    pick_user = driver.find_element(By.XPATH, "//th[text()='채취자']/parent::tr/following-sibling::tr[1]/td[last()]").text
                    pick_user = pick_user.replace('성명', '').replace('(서명 완료)', '').strip()
                except: pick_user = ""
                
                try:
                    # [중요] 괄호 오타 수정됨
                    seal_name = driver.find_element(By.XPATH, "//th[contains(text(), '봉인명')]/following-sibling::td").text.strip()
                except: seal_name = ""

                # 3. [어제 성공한 코드] 특정처리자 추출 (BeautifulSoup 활용)
                html = driver.page_source
                soup = BeautifulSoup(html, 'html.parser')
                specific_user = "" # 특정처리자 초기값
                hist_section = soup.find(id="rqst_hist_div")

                if hist_section:
                    rows = hist_section.select("tbody tr")
                    for r in rows:
                        cols = r.find_all("td")
                        # 2번째 열에 기관명, 3번째 열에 이름이 있는 구조
                        if len(cols) >= 3 and "한국건설품질시험원" in cols[1].get_text():
                            specific_user = cols[2].get_text(strip=True)
                    # 4. 최종 리스트 구성 (순서가 매우 중요함!)
                    # 인덱스: 0:접수번호, 1:접수일시, 2:상태, 3:사업명, 4:의뢰기관, 5:채취자, 6:봉인명, 7:특정처리자

                    result_row = [rcpt_no, rcpt_date, status, biz_nm, agency, pick_user, seal_name, specific_user]
                    final_results.append(result_row)
            except Exception as e:
                print(f"항목 수집 실패 ({rq_no}): {e}")
                # 실패 시 목록으로 돌아가서 다음 번호 시도
            continue

        driver.quit()
        return JsonResponse({'status': 'success', 'results': final_results})

    except Exception as e:
        if driver: driver.quit()
        return JsonResponse({'status': 'error', 'message': str(e)})


# --- [3] MySQL 배정 현황 이력 조회 (핵심 로직) ---

@csrf_exempt
def fetch_assignment_history(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            items = data.get('items', [])
            results = []

            for item in items:
                proj = item.get('project', '').strip()
                clnt = item.get('client', '').strip()
                uid = item.get('u_id', '').strip()  # ⭐ 화면에서 보낸 의뢰번호 추출

                # 1. 과거 배정 이력 조회 (기존 로직 유지)
                history_qs = CsiReceipt.objects.filter(
                    project=proj, 
                    client=clnt
                ).exclude(manager__isnull=True).exclude(manager='').values_list('manager', flat=True).order_by('-id')

                unique_teams = []
                for team in history_qs:
                    if team not in unique_teams:
                        unique_teams.append(team)

                # 2. ⭐ 중복 확인: 현재 의뢰번호가 DB에 이미 존재하는지 체크
                # 존재하면 True, 없으면 False를 반환합니다.
                is_saved = CsiReceipt.objects.filter(u_id=uid).exists()

                results.append({
                    'history': ", ".join(unique_teams) if unique_teams else "이력 없음",
                    'is_saved': is_saved  # ⭐ 프론트엔드에 전달할 결과 추가
                })

            return JsonResponse({'status': 'success', 'results': results})
        except Exception as e:
            print(f"Error in fetch_assignment_history: {str(e)}")
            return JsonResponse({'status': 'error', 'message': str(e)})

    return JsonResponse({'status': 'error', 'message': '잘못된 요청 방식입니다.'})

# board/views.py

@csrf_exempt
def save_to_csi_receipts(request):
    if request.method == 'POST':
        try:
            # 1. 데이터 로드 및 검증
            raw_data = json.loads(request.body)
            data_list = raw_data.get('data', [])
            
            if not data_list:
                return JsonResponse({'status': 'error', 'message': '저장할 데이터가 선택되지 않았습니다.'})
            
            with connection.cursor() as cursor:
                # 2. UPSERT 쿼리 (의뢰번호가 UNIQUE 설정되어 있어야 작동함)
                sql = """
                    INSERT INTO csi_receipts (
                        의뢰번호, 접수번호, 접수일시, 진행상태, 사업명, 의뢰기관명, 
                        채취자, 봉인명, 처리자, 영업구분, 담당자, 확인, 
                        시료량, 구분, 현장담당자, 배정일자
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        접수번호 = VALUES(접수번호),
                        접수일시 = VALUES(접수일시),
                        진행상태 = VALUES(진행상태),
                        사업명 = VALUES(사업명),
                        의뢰기관명 = VALUES(의뢰기관명),
                        채취자 = VALUES(채취자),
                        봉인명 = VALUES(봉인명),
                        처리자 = VALUES(처리자),
                        영업구분 = VALUES(영업구분),
                        담당자 = VALUES(담당자),
                        확인 = VALUES(확인),
                        시료량 = VALUES(시료량),
                        구분 = VALUES(구분),
                        현장담당자 = VALUES(현장담당자),
                        배정일자 = VALUES(배정일자)                        
                """
                
                # 3. 데이터 매핑 (KeyError 방지를 위해 .get() 사용)
                params = [
                    (
                        d.get('u_id'), d.get('receipt_id'), d.get('receipt_date'), 
                        d.get('status'), d.get('project'), d.get('client'),
                        d.get('sampler'), d.get('seal'), d.get('processor'), 
                        d.get('sales_type'), d.get('manager'), d.get('check_col'),
                        d.get('amount'), d.get('type_col'), d.get('manager_name'), 
                        d.get('assign_date')
                    ) for d in data_list
                ]
                
                # 4. 일괄 실행
                cursor.executemany(sql, params)
                
            return JsonResponse({
                'status': 'success', 
                'message': f'{len(data_list)}건의 데이터가 DB에 반영(새로 저장 또는 기존 내용 갱신)되었습니다.'
            })
        except Exception as e:
            # 에러 발생 시 상세 내용 반환
            return JsonResponse({'status': 'error', 'message': f'DB 처리 중 오류: {str(e)}'})
            
    return JsonResponse({'status': 'error', 'message': '잘못된 요청 방식입니다.'})

# 데이터 가져와서 표에 뿌려주는 코드임
def search_by_assign_date(request):
    if request.method == 'POST':
        try:
            params = json.loads(request.body)
            manager = params.get('manager', '전체')
            filter_type = params.get('filter') # u_id, project, client 중 하나
            keyword = params.get('keyword', '').strip() # 검색어
            start_date = params.get('start_date')
            end_date = params.get('end_date')

            with connection.cursor() as cursor:
                # 1. 기본 SQL (날짜 조건은 필수)
                sql = """
                    SELECT 
                        의뢰번호, 접수번호, 접수일시, 진행상태, 사업명, 의뢰기관명, 
                        채취자, 봉인명, 처리자, 영업구분, 담당자, 확인, 
                        시료량, 구분, 현장담당자, 배정일자, 배정현황
                    FROM csi_receipts
                    WHERE 배정일자 BETWEEN %s AND %s
                """
                query_params = [start_date, end_date]

                # 2. 담당자 조건 추가
                if manager != "전체":
                    sql += " AND 담당자 = %s"
                    query_params.append(manager)
                
                # 3. 추가 검색 필터 (의뢰번호, 사업명, 의뢰기관명) ⭐추가된 부분
                if keyword:
                    if filter_type == "u_id":
                        sql += " AND 의뢰번호 LIKE %s"
                        query_params.append(f"%{keyword}%")
                    elif filter_type == "project":
                        sql += " AND 사업명 LIKE %s"
                        query_params.append(f"%{keyword}%")
                    elif filter_type == "client":
                        sql += " AND 의뢰기관명 LIKE %s"
                        query_params.append(f"%{keyword}%")

                # 4. 정렬 추가 (조건이 다 붙은 뒤에 정렬이 와야 합니다)
                sql += " ORDER BY 배정일자 DESC, 의뢰번호 DESC"

                cursor.execute(sql, query_params)
                
                # 결과 변환
                columns = [
                    'u_id', 'receipt_id', 'receipt_date', 'status', 'project', 'client',
                    'sampler', 'seal', 'processor', 'sales_type', 'manager', 'check_col',
                    'amount', 'type_col', 'manager_name', 'assign_date', 'assignment_history'
                ]
                results = [dict(zip(columns, row)) for row in cursor.fetchall()]

            return JsonResponse({'status': 'success', 'results': results})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
            
    return JsonResponse({'status': 'error', 'message': '잘못된 요청입니다.'})

    
# board/views.py

def csi_issue_view(request):
    """
    성적서 발급 관리 페이지(4분할 화면)를 열어주는 기본 뷰
    """
    # 오늘 날짜를 기본값으로 전달 (선택 사항)
    import datetime
    default_date = datetime.date.today().strftime('%Y-%m-%d')
    
    return render(request, 'csi_issue.html', {
        'default_date': default_date
    })
    
# --- [4] CSI 성적서 발급 정보 수집 (상세페이지 역추적 방식) --- 
@csrf_exempt
def fetch_csi_issue_data(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '잘못된 접근입니다.'})

    driver = None
    try:
        data = json.loads(request.body)
        # 1. 프론트엔드에서 보낸 시작일과 종료일 가져오기
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        if not start_date or not end_date:
            return JsonResponse({'status': 'error', 'message': '시작일과 종료일이 누락되었습니다.'})

        # 2. 하이픈(-) 제거하여 YYYYMMDD 형식으로 변환
        clean_start = start_date.replace("-", "")
        clean_end = end_date.replace("-", "")

        chrome_options = Options()
        chrome_options.add_argument("--window-size=1920,1080")
        # chrome_options.add_argument("--headless") # 필요시 주석 처리 (창 보기)
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        wait = WebDriverWait(driver, 15)

        # 1. 로그인
        driver.get("https://gcloud.csi.go.kr/cmq/main.do")
        wait.until(EC.element_to_be_clickable((By.ID, "userId"))).send_keys("youngjun")
        driver.find_element(By.ID, "pswd").send_keys("k*1800*92*")
        driver.find_element(By.CLASS_NAME, "login-btn").click()
        time.sleep(2)

        # 2. 메뉴 이동 및 검색 설정
        driver.get("https://gcloud.csi.go.kr/cmq/qti/qltAgntQltSttus/qltAgntQltSttusList.do")
        wait.until(EC.presence_of_element_located((By.NAME, "ymdKey")))
        
        # 발급일자 선택 로직
        driver.execute_script("""
            var select = document.querySelector('select[name="ymdKey"]');
            if (select) {
                for (var i = 0; i < select.options.length; i++) {
                    if (select.options[i].text.indexOf('발급일자') !== -1) {
                        select.selectedIndex = i;
                        select.dispatchEvent(new Event('change')); 
                        break;
                    }
                }
            }
        """)
        time.sleep(1.5)

        # 날짜 입력 및 검색
        # 1. 시작일 입력
        start_input = driver.find_element(By.ID, "startYmd")
        start_input.clear()
        start_input.send_keys(clean_start)  # clean_date 대신 clean_start 사용
        start_input.send_keys(Keys.ENTER)

        # 2. 종료일 입력
        end_input = driver.find_element(By.ID, "endYmd")
        end_input.clear()
        end_input.send_keys(clean_end)      # clean_date 대신 clean_end 사용
        end_input.send_keys(Keys.ENTER)
        
        driver.execute_script("go_search();")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "pagination")))
        time.sleep(2)

        # 3. 데이터 수집 루프
        final_results = []
        current_page_idx = 1 

        while True:
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "goSelectLink")))
            time.sleep(2) 
            
            first_cert_before = driver.find_elements(By.CLASS_NAME, "goSelectLink")[0].text.strip()
            rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")

            for i in range(len(rows)):
                current_rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")
                if i >= len(current_rows): break
                row = current_rows[i]
                
                # 목록 데이터 8개 추출
                try:
                    list_info = {
                        'cert_no': row.find_element(By.XPATH, "./td[2]").text.strip(),
                        'seal_name': row.find_element(By.XPATH, "./td[3]").text.strip(),
                        'project_name': row.find_element(By.XPATH, "./td[4]").text.strip(),
                        'agency': row.find_element(By.XPATH, "./td[5]").text.strip(),
                        'req_date': row.find_element(By.XPATH, "./td[6]").text.strip(),
                        'recv_date': row.find_element(By.XPATH, "./td[7]").text.strip(),
                        'wait_date': row.find_element(By.XPATH, "./td[8]").text.strip(),
                        'issue_date': row.find_element(By.XPATH, "./td[9]").text.strip()
                    }
                    target_link = row.find_element(By.XPATH, "./td[2]//a")
                except Exception:
                    continue

                # 상세페이지 진입하여 '의뢰번호' 수집
                try:
                    driver.execute_script("arguments[0].click();", target_link)
                    expand_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '품질검사 의뢰서 내역')]")))
                    driver.execute_script("arguments[0].click();", expand_btn)
                    time.sleep(1.2)
                    
                    rq_no = driver.find_element(By.XPATH, "//th[contains(text(), '의뢰번호')]/following-sibling::td").text.strip()
                except Exception:
                    rq_no = "추출 실패"

                # 최종 데이터 결합 (화면 표 순서에 최적화)
                final_results.append({
                    'u_id': rq_no,                   # 의뢰번호 (1순위)
                    'cert_no': list_info['cert_no'],   # 성적서번호
                    'seal_name': list_info['seal_name'], # 봉인명
                    'project_name': list_info['project_name'],
                    'agency': list_info['agency'],
                    'req_date': list_info['req_date'],
                    'recv_date': list_info['recv_date'],
                    'wait_date': list_info['wait_date'],
                    'issue_date': list_info['issue_date']                    
                })

                driver.execute_script("window.history.back();")
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "goSelectLink")))
                time.sleep(1.5)

            # 4. 페이징 처리
            try:
                next_page_num = current_page_idx + 1
                btn_xpath = f"//ul[contains(@class,'pagination')]//a[text()='{next_page_num}']"
                next_btns = driver.find_elements(By.XPATH, btn_xpath)
                
                if next_btns:
                    driver.execute_script("arguments[0].click();", next_btns[0])
                else:
                    driver.execute_script(f"goPage({next_page_num});")
                
                is_changed = False
                for _ in range(15):
                    time.sleep(1)
                    current_links = driver.find_elements(By.CLASS_NAME, "goSelectLink")
                    if current_links and current_links[0].text.strip() != first_cert_before:
                        is_changed = True
                        current_page_idx = next_page_num
                        break
                if not is_changed: break
            except: break

        driver.quit()
        return JsonResponse({'status': 'success', 'results': final_results})

    except Exception as e:
        if driver: driver.quit()
        return JsonResponse({'status': 'error', 'message': str(e)})


# 여기서부터 발급일 DB저장하는 코드임
@csrf_exempt
def save_csi_matching_data(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            selected_items = data.get('items', [])

            if not selected_items:
                return JsonResponse({'status': 'error', 'message': '저장할 항목이 없습니다.'})

            with connection.cursor() as cursor:
                # 🚀 INSERT + UPDATE (UPSERT) 쿼리
                # 성적서번호가 중복될 경우, 의뢰번호와 발급일자를 최신으로 갱신합니다.
                sql = """
                    INSERT INTO csi_issue_results (의뢰번호, 성적서번호, 발급일자)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        성적서번호 = VALUES(성적서번호),
                        발급일자 = VALUES(발급일자)
                """
                
                params = [
                    (item['u_id'], item['cert_no'], item['issue_date']) 
                    for item in selected_items
                ]
                
                cursor.executemany(sql, params)

            return JsonResponse({'status': 'success', 'message': f'{len(selected_items)}건 처리 완료 (저장/업데이트)'})

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        

# ------여기서부터 성적서 발급대기일 크롤링 페이지입니다--------
@csrf_exempt
def fetch_csi_wait_data(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '잘못된 접근입니다.'})

    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument("--window-size=1920,1080")
        # chrome_options.add_argument("--headless")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        wait = WebDriverWait(driver, 15)

        # 1. 로그인
        driver.get("https://gcloud.csi.go.kr/cmq/main.do")
        wait.until(EC.element_to_be_clickable((By.ID, "userId"))).send_keys("youngjun")
        driver.find_element(By.ID, "pswd").send_keys("k*1800*92*")
        driver.find_element(By.CLASS_NAME, "login-btn").click()
        time.sleep(2)

        # 2. 메뉴 이동 및 검색 (날짜 없이 바로 검색)
        driver.get("https://gcloud.csi.go.kr/cmq/qti/qltRptIssuWait/qltRptIssuWaitList.do")
        driver.execute_script("go_search();")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "pagination")))
        time.sleep(2)

        final_results = []
        current_page_idx = 1 

        # --- [3. 데이터 수집 및 페이징 루프 시작] ---
        while True:
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "goSelectLink")))
            time.sleep(2) 
            
            # 현재 페이지의 첫 번째 성적서 번호를 기억 (페이지 전환 확인용 접수번호 봉인명 시험검사종목 공사명 접수일자 확정일자 대기일자 진행상태)
            first_cert_before = driver.find_elements(By.CLASS_NAME, "goSelectLink")[0].text.strip()
            rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")

            for i in range(len(rows)):
                current_rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")
                if i >= len(current_rows): break
                row = current_rows[i]
                
                try:
                    # 목록 데이터 수집 (발급대기 페이지 td 순서)
                    list_info = {
                        'cert_no': row.find_element(By.XPATH, "./td[2]").text.strip(),
                        'seal_name': row.find_element(By.XPATH, "./td[3]").text.strip(),
                        'project_name': row.find_element(By.XPATH, "./td[4]").text.strip(),
                        'agency': row.find_element(By.XPATH, "./td[5]").text.strip(),
                        'req_date': row.find_element(By.XPATH, "./td[6]").text.strip(),
                        'recv_date': row.find_element(By.XPATH, "./td[7]").text.strip(),
                        'wait_date': row.find_element(By.XPATH, "./td[8]").text.strip()
                    }
                    target_link = row.find_element(By.XPATH, "./td[2]//a")

                    # 상세페이지 진입하여 의뢰/접수번호 수집
                    driver.execute_script("arguments[0].click();", target_link)
                    expand_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '품질검사 의뢰서 내역')]")))
                    driver.execute_script("arguments[0].click();", expand_btn)
                    time.sleep(1.2)
                    
                    try:
                        u_id = driver.find_element(By.XPATH, "//th[contains(text(), '의뢰번호')]/following-sibling::td").text.strip()
                    except: u_id = "미부여"
                    
                    try:
                        receipt_no = driver.find_element(By.XPATH, "//th[contains(text(), '접수번호')]/following-sibling::td").text.strip()
                    except: receipt_no = "-"

                    final_results.append({
                        'u_id': u_id,
                        'wait_date': list_info['wait_date'],
                        'receipt_no': receipt_no,
                        'cert_no': list_info['cert_no'],
                        'seal_name': list_info['seal_name'],
                        'project_name': list_info['project_name'],
                        'agency': list_info['agency'],
                        'req_date': list_info['req_date'],
                        'recv_date': list_info['recv_date']
                    })

                    driver.execute_script("window.history.back();")
                    wait.until(EC.presence_of_element_located((By.CLASS_NAME, "goSelectLink")))
                    time.sleep(1.5)

                except Exception:
                    continue

            # --- [4. 페이징 처리: 다음 페이지로 이동] ---
            try:
                next_page_num = current_page_idx + 1
                btn_xpath = f"//ul[contains(@class,'pagination')]//a[text()='{next_page_num}']"
                next_btns = driver.find_elements(By.XPATH, btn_xpath)
                
                if next_btns:
                    driver.execute_script("arguments[0].click();", next_btns[0])
                else:
                    # 텍스트로 못 찾을 경우 goPage 자바스크립트 함수 직접 호출
                    driver.execute_script(f"goPage({next_page_num});")
                
                # 페이지가 실제로 넘어갔는지 확인 (첫 번째 데이터가 바뀌었는지)
                is_changed = False
                for _ in range(15):
                    time.sleep(1)
                    current_links = driver.find_elements(By.CLASS_NAME, "goSelectLink")
                    if current_links and current_links[0].text.strip() != first_cert_before:
                        is_changed = True
                        current_page_idx = next_page_num
                        break
                
                if not is_changed: break # 다음 페이지로 안 넘어가면 종료
            except:
                break # 에러나거나 버튼 없으면 종료

        driver.quit()
        return JsonResponse({'status': 'success', 'results': final_results})

    except Exception as e:
        if driver: driver.quit()
        return JsonResponse({'status': 'error', 'message': str(e)})

# ---------------여기서 부터 발급대기일 입력하기-------------
@csrf_exempt
def save_csi_wait_data(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            selected_items = data.get('items', [])

            if not selected_items:
                return JsonResponse({'status': 'error', 'message': '저장할 항목이 없습니다.'})

            with connection.cursor() as cursor:
                # 🚀 사용자님 DB 컬럼명에 맞춘 UPSERT 쿼리
                # 1. 성적서번호 컬럼에는 "승인전" 고정값 입력
                # 2. 발급일자 컬럼에는 표의 '발급대기일자' 입력
                sql = """
                    INSERT IGNORE INTO csi_issue_results (의뢰번호, 성적서번호, 발급일자)
                    VALUES (%s, %s, %s)
                """
                
                # 파라미터 구성
                # item['u_id'] -> 의뢰번호
                # "승인전"      -> 성적서번호 컬럼에 들어갈 고정값
                # item['wait_date'] -> 발급일자 컬럼에 들어갈 데이터
                params = [
                    (item['u_id'], "승인전", item['wait_date']) 
                    for item in selected_items
                ]
                
                cursor.executemany(sql, params)

            return JsonResponse({
                'status': 'success', 
                'message': f'{len(selected_items)}건 처리 완료 (의뢰번호 기준 "승인전" 및 대기일자 업데이트)'
            })

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})

# ----------------여기서부터 3번영역 QT번호 수정--------------------------STR
def get_panel3_data(request):
    """
    3번 영역: csi_receipts 테이블에서 검색 조건에 맞는 데이터를 불러옴
    """
    search_type = request.GET.get('search_type')
    search_text = request.GET.get('search_text', '').strip()

    try:
        with connections['default'].cursor() as cursor:
            # 1. 기본 쿼리 (ID 포함)
            sql = "SELECT ID, 의뢰번호, 접수번호, 사업명, 의뢰기관명, 영업구분, 담당자 FROM csi_receipts"
            params = []

            # 2. 드롭다운 검색 조건 처리
            if search_text:
                mapping = {
                    "request_code": "의뢰번호",
                    "agency": "의뢰기관명",
                    "project": "사업명"
                }
                column_name = mapping.get(search_type)
                if column_name:
                    sql += f" WHERE {column_name} LIKE %s"
                    params.append(f"%{search_text}%")

            # 3. 최신 데이터 순으로 정렬 (필요시)
            sql += " ORDER BY ID DESC LIMIT 1000"

            cursor.execute(sql, params)
            
            # 결과 가공
            columns = [col[0] for col in cursor.description]
            results = [dict(zip(columns, row)) for row in cursor.fetchall()]

            # 4. JSON 반환
            return JsonResponse(results, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def save_panel3_data(request):
    """
    3번 영역: ID를 기준으로 모든 컬럼 데이터를 일괄 업데이트
    """
    if request.method == 'POST':
        try:
            body = json.loads(request.body)
            items = body.get('items', [])

            if not items:
                return JsonResponse({"success": False, "error": "저장할 데이터가 없습니다."}, status=400)

            with connections['default'].cursor() as cursor:
                for item in items:
                    # 1. ID 값 추출 (대소문자 구분 없이 처리)
                    row_id = item.get('ID') or item.get('id')
                    
                    if row_id is not None:
                        # 2. ID를 기준으로 나머지 모든 필드 업데이트 SQL
                        sql = """
                            UPDATE csi_receipts 
                            SET 
                                의뢰번호 = %s, 
                                접수번호 = %s, 
                                사업명 = %s, 
                                의뢰기관명 = %s, 
                                영업구분 = %s, 
                                담당자 = %s
                            WHERE ID = %s
                        """
                        # 3. 데이터 매핑 (None일 경우 빈 문자열 처리)
                        params = [
                            item.get('의뢰번호', ''),
                            item.get('접수번호', ''),
                            item.get('사업명', ''),
                            item.get('의뢰기관명', ''),
                            item.get('영업구분', ''),
                            item.get('담당자', ''),
                            row_id
                        ]
                        
                        cursor.execute(sql, params)
            
            return JsonResponse({"success": True, "message": "성공적으로 업데이트되었습니다."})

        except Exception as e:
            print(f"Update Error: {e}")
            return JsonResponse({"success": False, "error": str(e)}, status=500)

    return JsonResponse({"success": False, "error": "잘못된 요청 방식입니다."}, status=400)    


# ----------------3번 영역 여기 까지---------------------------------------END



# ------------------------------------------여기부터 리퀘스트 수정 쿼리-------------STR
@csrf_exempt
def fetch_combined_data(request):
    try:
        # 1. 파라미터 수집
        if request.method == 'POST' and request.body:
            import json
            data = json.loads(request.body)
            start_date = data.get('start', '').strip()
            end_date = data.get('end', '').strip()
            team_filter = data.get('team', '전체').strip()
            search_query = data.get('text', '').strip()
            raw_type = data.get('type', '').strip()
        else:
            start_date = request.GET.get('start', '').strip()
            end_date = request.GET.get('end', '').strip()
            team_filter = request.GET.get('team', '전체').strip()
            search_query = request.GET.get('text', '').strip()
            raw_type = request.GET.get('type', '').strip()

        # 2. 타입 변환
        search_type = '사업명'
        if raw_type == 'client': search_type = '의뢰기관명'
        elif raw_type == 'project': search_type = '사업명'
        elif raw_type == 'req_code': search_type = '의뢰번호'
        
        # 대소문자 구분 적용코드
        where_clauses = []
        params = []
        if start_date and end_date:           
            where_clauses.append("DATE(r.배정일자) BETWEEN %s AND %s")    
            # 파라미터는 시간 없이 날짜만 전달합니다.
            params.extend([start_date, end_date])
        
        # 팀 필터도 대소문자 무시 적용
        if team_filter and team_filter != '전체':
            where_clauses.append("UPPER(r.담당자) LIKE %s")
            params.append(f"%{team_filter.upper()}%")
            
        if search_query:
            q = f"%{search_query.upper()}%"
            if search_type == '의뢰번호':
                where_clauses.append("UPPER(r.의뢰번호) LIKE %s")
            elif search_type == '의뢰기관명':
                where_clauses.append("UPPER(r.의뢰기관명) LIKE %s")
            else:
                where_clauses.append("UPPER(r.사업명) LIKE %s")
            params.append(q)


        where_sentence = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        mysql_query = f"""
            SELECT r.*, i.성적서번호, i.발급일자, r.미인정 
            FROM csi_receipts r 
            LEFT JOIN csi_issue_results i ON r.의뢰번호 = i.의뢰번호 
            {where_sentence}
            ORDER BY r.담당자 ASC LIMIT 5000
        """

        with connections['default'].cursor() as mysql_cursor:
            mysql_cursor.execute(mysql_query, params)
            columns = [col[0] for col in mysql_cursor.description]
            mysql_rows = [dict(zip(columns, row)) for row in mysql_cursor.fetchall()]

        # 4. MSSQL 데이터 매칭
        # 4. MSSQL 데이터 매칭 (RQ 여부에 따른 동적 컬럼 매핑)
        req_codes = [str(row['의뢰번호']).strip() for row in mysql_rows if row.get('의뢰번호')]
        mssql_dict = {}
        
        if req_codes:
            chunk_size = 500
            with connections['mssql'].cursor() as mssql_cursor:
                for i in range(0, len(req_codes), chunk_size):
                    chunk = req_codes[i : i + chunk_size]
                    
                    # RQ로 시작하는 그룹과 그 외(Q, E, T 등 전체) 그룹 분리
                    curr_rq = [c for c in chunk if c.upper().startswith('RQ')]
                    curr_etc = [c for c in chunk if not c.upper().startswith('RQ')]
                    
                    where_clauses = []
                    query_params = []
                    
                    # 1. RQ 번호 매칭 (request_code 컬럼)
                    if curr_rq:
                        placeholders = ', '.join(['%s'] * len(curr_rq))
                        where_clauses.append(f"a.request_code IN ({placeholders})")
                        query_params.extend(curr_rq)
                        
                    # 2. 그 외 모든 번호 매칭 (receipt_code 컬럼)
                    if curr_etc:
                        placeholders = ', '.join(['%s'] * len(curr_etc))
                        where_clauses.append(f"a.receipt_code IN ({placeholders})")
                        query_params.extend(curr_etc)
                    
                    if not where_clauses:
                        continue
                        
                    # OR로 연결하여 하나의 쿼리로 실행
                    where_sentence = " OR ".join(where_clauses)
                    
                    mssql_query = f"""
                        SELECT a.sales, a.request_code, a.receipt_csi_code, a.receipt_code, b.completion_day, a.save_date, 
                               b.builder, b.construction, c.specimen, d.supply_value, d.vat, d.rate,
                               e.deposit_day, e.deposit, f.issue_date, f.company
                        FROM dbo.Receipt a
                        LEFT JOIN dbo.Customer b ON a.receipt_code = b.receipt_code
                        LEFT JOIN dbo.Specimen_info c ON a.receipt_code = c.receipt_code
                        LEFT JOIN dbo.Estimate d ON a.receipt_code = d.receipt_code
                        LEFT JOIN dbo.Deposit e ON a.receipt_code = e.receipt_code
                        LEFT JOIN dbo.Tax_Manager f ON a.receipt_code = f.receipt_code
                        WHERE {where_sentence}
                    """
                    
                    mssql_cursor.execute(mssql_query, query_params)
                    m_cols = [col[0] for col in mssql_cursor.description]
                    
                    for m_row in mssql_cursor.fetchall():
                        m_item = dict(zip(m_cols, m_row))
                        # 매칭 딕셔너리에 request_code와 receipt_code를 모두 키로 저장하여 검색 효율 극대화
                        r_code = str(m_item.get('request_code', '')).strip()
                        qt_code = str(m_item.get('receipt_code', '')).strip()
                        
                        if r_code: mssql_dict[r_code] = m_item
                        if qt_code: mssql_dict[qt_code] = m_item

        # 5. 최종 데이터 합체 및 통계 집계
        final_results = []
        stats = {}  # stats로 변수명 통일
        teams = ['1팀', '2팀', '3팀', '4팀', '5팀', '6팀']

        for row in mysql_rows:
            req_no = str(row.get('의뢰번호', '')).strip()
            ms_info = mssql_dict.get(req_no, {})
            
            # 발급일자 확인 (날짜 형식이 포함되어 있는지)
            issue_date = str(row.get('발급일자', '')).strip()
            is_issued = 1 if issue_date and issue_date not in ['None', '', '-', '0000-00-00'] else 0

            # 합체 데이터 생성
            res_item = {
                "담당자": row.get('담당자', ''),
                "영업구분": row.get('영업구분', ''),
                "의뢰번호": req_no,
                "접수일시": str(row.get('접수일시', '')),
                "접수번호": ms_info.get('receipt_csi_code', '-'),
                "QT번호": req_no if req_no.startswith('QT-') else ms_info.get('receipt_code', '-'),
                "성적서번호": row.get('성적서번호', '-'),
                # "발급일자": issue_date,
                "발급일자": str(row.get('발급일자')) if row.get('발급일자') else "",
                "의뢰기관명": row.get('의뢰기관명', ''),
                "사업명": ms_info.get('construction', row.get('사업명', '')),
                "공급가액": ms_info.get('supply_value', 0),
                "봉인명": ms_info.get('specimen', '-'),
                "준공예정일": str(ms_info.get('completion_day')) if ms_info.get('completion_day') else "",
                "실접수일": str(ms_info.get('save_date')) if ms_info.get('save_date') else "",
                "공급가액": ms_info.get('supply_value', 0),
                "부가세": ms_info.get('vat', 0),
                "할인율": ms_info.get('rate', 0),
                "입금일": ms_info.get('deposit_day', 0),
                "입금액": ms_info.get('deposit', 0),
                "계산서발행일": str(ms_info.get('issue_date')),
                "계산서발행회사명": ms_info.get('company', '-'),
                "미인정": row.get('미인정', '')   
            }
            final_results.append(res_item)

            # [집계 로직]
            name = (res_item["영업구분"] or res_item["담당자"] or '').strip()
            if not name: continue

            # 팀 판별
            target_team = "미분류"
            for t in teams:
                if t in str(res_item["담당자"]):
                    target_team = t
                    break

            # 인정/미인정 판별
            type_key = "미인정건" if res_item["미인정"] else "인정건"

            # stats 구조 초기화
            if name not in stats:
                stats[name] = {t: {"인정건": {"금액": 0, "건수": 0, "발급": 0}, 
                                  "미인정건": {"금액": 0, "건수": 0, "발급": 0}} for t in teams}

            # 누적
            if target_team in teams:
                try:
                    price = int(float(str(res_item["공급가액"]).replace(',', '')))
                except:
                    price = 0
                stats[name][target_team][type_key]["금액"] += price
                stats[name][target_team][type_key]["건수"] += 1
                stats[name][target_team][type_key]["발급"] += is_issued

        return JsonResponse({'status': 'success', 'data': final_results, 'stats': stats})

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return JsonResponse({'status': 'error', 'message': str(e)})


# 5. 페이지 호출 함수 (AttributeError 해결)
def request_page(request):
    return render(request, 'request.html') 





# ----------------------------------------리퀘스트 수정쿼리-------------------------END


# 여기서부터 견적불러오기
def get_estimate_detail(request):
    qt_no = request.GET.get('qt_no', '').strip()
    
    print(f"\n[LOG] 상세 및 요약 데이터 요청 수신: {qt_no}")

    if not qt_no or qt_no in ['-', 'None', '']:
        return JsonResponse({'status': 'error', 'message': '유효하지 않은 QT번호입니다.'})

    try:
        with connections['mssql'].cursor() as cursor:
            # 1. 견적 상세 리스트 조회 (기존 유지)
            detail_query = """
                SELECT item_name as 시험항목, count as 수량, ei_cost as 단가, ei_price as 금액
                FROM dbo.Examination_Item
                WHERE receipt_code = %s
            """
            cursor.execute(detail_query, [qt_no])
            detail_columns = [col[0] for col in cursor.description]
            rows = [dict(zip(detail_columns, row)) for row in cursor.fetchall()]

            # 2. 금액 요약 데이터 조회 (새로 추가)
            # 요청하신 컬럼명 매칭: std_cost, basic_qty, basic 등
            summary_query = """
                SELECT 
                    std_cost as base_price,
                    basic_qty as base_cnt,
                    basic as base_fee,
                    process_qty as info_cnt,
                    process as info_fee,
                    commission as cond_fee,
                    sample as specimen_fee,
                    [tran_set] as travel_type,
                    [tran] as travel_fee,
                    impossible as no_discount_amt,
                    possible as yes_discount_amt,
                    rate as discount_rate,
                    discount as fixed_discount_amt,
                    supply_value as supply_value,
                    vat as vat
                FROM dbo.Estimate
                WHERE receipt_code = %s
            """
            cursor.execute(summary_query, [qt_no])
            summary_columns = [col[0] for col in cursor.description]
            summary_row = cursor.fetchone()
            
            # 데이터가 있으면 dict 변환, 없으면 빈 dict
            summary_data = dict(zip(summary_columns, summary_row)) if summary_row else {}

            print(f"[LOG] 상세: {len(rows)}건 / 요약 데이터 존재 여부: {'Yes' if summary_data else 'No'}")

        # 두 데이터를 합쳐서 전송
        return JsonResponse({
            'status': 'success', 
            'data': rows, 
            'summary': summary_data
        })
        
    except Exception as e:
        print(f"[LOG] 에러 발생: {str(e)}")
        return JsonResponse({'status': 'error', 'message': str(e)})
    
    
#----------1. 여기서부터 현장팀 정산 페이지 입니다.-------------
def field_payment_view(request):
    # 1. 허용된 아이디 리스트 설정
    allowed_ids = ["admin_work", "admin_home"]

    # 2. 권한 체크: 로그인 여부 및 아이디 확인
    if not request.user.is_authenticated or request.user.username not in allowed_ids:
        # 권한이 없을 경우 메시지와 함께 홈으로 리다이렉트
        messages.error(request, "해당 페이지에 접근할 권한이 없습니다.")
        return redirect('/')

    now = datetime.now()
    
    # 템플릿 에러를 방지하기 위해 생성한 월 리스트
    month_list = range(1, 13)
    
    context = {
        'current_year': now.year,
        'current_month': now.month,
        'month_list': month_list,
        'today_str': now.strftime('%Y-%m-%d'),
    }
    return render(request, 'field_payment.html', context)


# 다섯번째 수정

def bizmeka_sync(request):
    driver = None
    try:
        chrome_options = Options()
        user_data = r"C:\Users\김영준\AppData\Local\Google\Chrome\User Data_Selenium" # 복사한 경로 입력
        chrome_options.add_argument(f"user-data-dir={user_data}")
        chrome_options.add_argument("--start-maximized")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        wait = WebDriverWait(driver, 15)

        # 1. 로그인 시도
        driver.get("https://ezportal.bizmeka.com/")
        # driver.find_element(By.ID, "username").send_keys("k200335")
        driver.find_element(By.ID, "password").send_keys("k*1800*92*" + Keys.ENTER)
        
        # [수동 조작 1] 2차 인증 대기
        print(">>> [수동 조작 1] 2차 인증을 완료해 주세요.")
        start_time = time.time()
        auth_success = False
        while time.time() - start_time < 300:
            try: driver.switch_to.alert.accept()
            except: pass
            if "main" in driver.current_url:
                auth_success = True
                break
            time.sleep(1)

        if not auth_success:
            return JsonResponse({"status": "error", "message": "인증 시간 초과"})
        
        # 2. 일정 페이지 이동
        driver.get("https://ezgroupware.bizmeka.com/groupware/planner/calendar.do")
        time.sleep(3)

        # ------------------------------------------------------------------
        # [자동] 목록보기 버튼 클릭 (여러 방식 시도)
        # ------------------------------------------------------------------
        print(">>> [자동] 목록보기 버튼 클릭 시도...")
        try:
            # 1순위: 텍스트가 '목록'인 버튼 찾기
            list_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), '목록')]")))
            driver.execute_script("arguments[0].click();", list_btn)
        except:
            try:
                # 2순위: 타이틀 속성이 '목록보기'인 요소
                list_btn = driver.find_element(By.CSS_SELECTOR, "button[title='목록보기']")
                driver.execute_script("arguments[0].click();", list_btn)
            except:
                print(">>> 목록보기 자동 클릭 실패. 수동으로 '목록보기'를 눌러주세요.")

        # ------------------------------------------------------------------
        # [강화된 대기] 사용자가 날짜를 다 고를 때까지 대기
        # ------------------------------------------------------------------
        print("\n" + "="*60)
        print(">>> [수동 조작 2] '날짜 선택' -> '검색' 버튼을 클릭해 주세요.")
        print(">>> 검색 결과가 나오면 10초 뒤에 자동으로 수집이 시작됩니다.")
        print("="*60 + "\n")
        
        # 기존 데이터 잔상 때문에 넘어가는 것을 방지하기 위해 
        # 사용자가 '검색' 버튼을 눌러 결과가 나타날 때까지 넉넉하게 대기 (최대 10분)
        WebDriverWait(driver, 600).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.listview tbody tr"))
        )
        
        # 검색 버튼을 누른 직후에도 사용자가 더 수정할 수 있으므로 10초간 최종 대기
        time.sleep(20) 
        print(">>> 수집을 시작합니다. 브라우저를 만지지 마세요.")

        # 3. 데이터 수집 로직 (무한 루프 방지 및 페이징)
        # 3. 데이터 수집 로직
        # 3. 데이터 수집 로직 (image_4b4a2d 구조 반영)
        # 3. 데이터 수집 로직 (페이징 추가 버전)
        final_list = []
        last_page_data_sample = None  # 이전 페이지 데이터를 저장할 변수
        
        try:
            while True:
                # [대기] 현재 페이지의 테이블이 완전히 나타날 때까지 대기
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".content-list table.listview tbody tr")))
                
                # 1) 현재 페이지 데이터 수집
                current_rows = driver.find_elements(By.CSS_SELECTOR, ".content-list table.listview tbody tr")
                page_data_contents = [] # 중복 체크를 위한 현재 페이지 내용 요약
                
                print(f">>> 현재 페이지에서 {len(current_rows)}건을 수집합니다.")

                for i in range(len(current_rows)):
                    try:
                        rows_refresh = driver.find_elements(By.CSS_SELECTOR, ".content-list table.listview tbody tr")
                        row = rows_refresh[i]
                        tds = row.find_elements(By.TAG_NAME, "td")

                        if len(tds) >= 3:
                            time_text = tds[0].text.strip()
                            # [추가] 범주 데이터 추출 (두 번째 td)
                            category_val = tds[1].text.strip() 
                            
                            try:
                                title_el = tds[2].find_element(By.CSS_SELECTOR, "a.fc-title")
                                title_val = title_el.get_attribute("title") or title_el.text.strip()
                            except:
                                title_val = tds[2].text.strip()

                            item = {
                                "date": time_text[:10],    # 날짜
                                "category": category_val,  # 범주 [추가]
                                "title": title_val         # 제목
                            }
                            final_list.append(item)
                            # 비교용 샘플에 범주 추가하여 중복 체크 정확도 향상
                            page_data_contents.append(f"{item['date']}_{item['category']}_{item['title']}") 
                    except Exception:
                        continue

                # ---------------------------------------------------------
                # [핵심 추가] 이전 페이지와 데이터가 똑같으면 즉시 종료
                # ---------------------------------------------------------
                current_page_sample = "|".join(page_data_contents)
                if last_page_data_sample == current_page_sample:
                    print(">>> [확인] 이전 페이지와 데이터가 동일합니다. 루프를 종료합니다.")
                    # 마지막에 중복 추가된 데이터는 제거 (선택 사항)
                    for _ in range(len(current_rows)):
                        if final_list: final_list.pop()
                    break
                
                last_page_data_sample = current_page_sample # 현재 데이터를 이전 데이터로 저장
                # ---------------------------------------------------------

                # 2) 다음 페이지(>) 버튼 클릭 처리
                try:
                    next_btn = driver.find_element(By.CSS_SELECTOR, "ul.pagination li a i.fa-angle-right").find_element(By.XPATH, "..")
                    parent_li = next_btn.find_element(By.XPATH, "./..")
                    
                    if "disabled" in parent_li.get_attribute("class"):
                        print(">>> [확인] 버튼이 disabled 상태입니다. 종료합니다.")
                        break
                    
                    driver.execute_script("arguments[0].click();", next_btn)
                    time.sleep(5) # 페이지 전환 대기 시간 충분히 확보
                    
                except Exception as e:
                    # 백업 로직: 숫자 pagination 처리
                    try:
                        active_li = driver.find_element(By.CSS_SELECTOR, "ul.pagination li.active")
                        next_li = active_li.find_element(By.XPATH, "./following-sibling::li")
                        
                        if "disabled" in next_li.get_attribute("class"):
                            break
                            
                        next_link = next_li.find_element(By.TAG_NAME, "a")
                        driver.execute_script("arguments[0].click();", next_link)
                        time.sleep(5)
                    except:
                        break

            print(f">>> [최종 완료] 총 {len(final_list)}건 수집됨")
            return JsonResponse({"status": "success", "total_count": len(final_list), "data": final_list})

        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)})
    finally:
        if driver:
            driver.quit()

#여기서부터 비즈메카 QT데이터불러오기
def get_qt_db_data(request):
    # 1. 파라미터 수신
    builder = request.GET.get('builder', '').strip()
    start_date = request.GET.get('startDate', '')
    end_date = request.GET.get('endDate', '')

    print("\n" + "="*60)
    print(f"[검색요청] 시공사: {builder}, 기간: {start_date} ~ {end_date}")

    # 2. 쿼리 작성 (%s 기호 사용)
    # 주신 참고 코드의 JOIN 구조와 ISNULL 방식을 유지합니다.
    mssql_query = """
        SELECT 
            ISNULL(c.sales, '') as sales, 
            ISNULL(c.receipt_code, '') as receipt_code, 
            ISNULL(c.field_tester, '') as field_tester, 
            ISNULL(CONVERT(VARCHAR(10), b.getdate, 120), '') as getdate, 
            ISNULL(CONVERT(VARCHAR(10), c.request_day, 120), '') as request_day,
            ISNULL(a.builder, '') as builder, 
            ISNULL(a.construction, '') as construction, 
            ISNULL(b.specimen, '') as specimen, 
            ISNULL(b.specimen_qty, 0) as specimen_qty,
            ISNULL(d.supply_value, 0) as supply_value, 
            ISNULL(d.vat, 0) as vat, 
            ISNULL(a.cm_name, '') as cm_name, 
            ISNULL(a.qm_name, '') as qm_name
        FROM dbo.Receipt c
        LEFT JOIN dbo.Customer a      ON c.receipt_code = a.receipt_code
        LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
        LEFT JOIN dbo.Estimate d      ON c.receipt_code = d.receipt_code
        WHERE c.request_day BETWEEN %s AND %s
    """
    
    # 3. 파라미터 리스트 (주신 코드의 chunk 방식과 동일하게 리스트로 전달)
    query_params = [start_date, end_date]

    if builder:
        mssql_query += " AND a.builder LIKE %s"
        query_params.append(f"%{builder}%")

    mssql_query += " ORDER BY c.request_day DESC"

    try:
        with connections['mssql'].cursor() as mssql_cursor:
            mssql_cursor.execute(mssql_query, query_params)
            
            m_cols = [col[0] for col in mssql_cursor.description]
            rows = mssql_cursor.fetchall()
            
            results = [dict(zip(m_cols, m_row)) for m_row in rows]
            
            # 터미널 확인용 로그
            print(f"[조회결과] 총 {len(results)}건 데이터 추출 완료")
            if results:
                print(f"[필드 체크] 첫 번째 데이터: {results[0]}")

        return JsonResponse({'status': 'success', 'data': results})

    except Exception as e:
        print(f"[에러발생] {str(e)}")
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    

    # ---- 여기서 부터 현장팀 정산 관련 db 불러오기
def get_payment_detail(request):
    # 프론트엔드에서 넘어온 번호 (C251205029 등)
    # 변수명은 receipt_no지만 실제로는 receipt_code 값이 담겨 있습니다.
    receipt_no = request.GET.get('receipt_no', '').strip()
    
    print(f"\n[LOG] 결제 상세 요청 수신 (QT번호): {receipt_no}")

    if not receipt_no:
        return JsonResponse({'success': False, 'message': '번호가 누락되었습니다.'})

    try:
        with connections['mssql'].cursor() as cursor:
            # ⭐ 수정: 첫 번째 쿼리(find_qt_query)를 삭제하고 바로 사용합니다.
            qt_no = receipt_no 

            # 1. 견적 상세 리스트 조회
            detail_query = """
                SELECT item_name as 시험항목, count as 수량, ei_cost as 단가, ei_price as 금액
                FROM dbo.Examination_Item
                WHERE receipt_code = %s
            """
            cursor.execute(detail_query, [qt_no])
            detail_columns = [col[0] for col in cursor.description]
            estimate_items = [dict(zip(detail_columns, row)) for row in cursor.fetchall()]

            # 2. 금액 요약 데이터 조회
            summary_query = """
                SELECT 
                    std_cost as base_price, basic_qty as base_cnt, basic as base_fee,
                    process_qty as info_cnt, process as info_fee, commission as cond_fee,
                    sample as specimen_fee, [tran_set] as travel_type, [tran] as travel_fee,
                    impossible as no_discount_amt, possible as yes_discount_amt,
                    rate as discount_rate, discount as fixed_discount_amt,
                    supply_value as supply_value, vat as vat
                FROM dbo.Estimate
                WHERE receipt_code = %s
            """
            cursor.execute(summary_query, [qt_no])
            summary_columns = [col[0] for col in cursor.description]
            summary_row = cursor.fetchone()
            
            # 데이터가 없을 경우를 대비한 처리
            summary_data = dict(zip(summary_columns, summary_row)) if summary_row else {}

            # 로그 추가 (데이터 확인용)
            print(f"[LOG] 조회 결과 - 상세: {len(estimate_items)}건, 요약데이터: {'성공' if summary_data else '없음'}")

        # 결과 반환
        return JsonResponse({
            'success': True,
            'qt_no': qt_no,
            'estimate_items': estimate_items,
            'summary': summary_data
        })
        
    except Exception as e:
        print(f"[LOG] 서버 에러 발생: {str(e)}")
        return JsonResponse({'success': False, 'message': f"서버 내부 오류: {str(e)}"})
    
# ------------------------여기서부터 완료건 보기 관련----------------------------
def get_finished_data(request):
    year = request.GET.get('year')
    month = request.GET.get('month')
    manager = request.GET.get('manager')

    try:
        # MySQL (kcqt_qyalit) 연결 사용
        with connections['default'].cursor() as cursor:
            # 1. 기본 쿼리 작성 (요청하신 헤더 순서대로 SELECT)
            query = """
                SELECT 
                    ID, 시험수거일, 현장담당, 구분, 의뢰업체명, 시료명, 
                    공수, 출장비, 추가, 비고, 접수번호, 영업담당, 
                    시료채취자, 현장시험자, 지급여부, 순번
                FROM winapps_현장팀
                WHERE 시험수거일 LIKE %s
            """
            
            # 날짜 필터링 (YYYY-MM 형식)
            date_filter = f"{year}-"
            if month != 'all':
                date_filter += f"{int(month):02d}%"
            else:
                date_filter += "%"
            
            params = [date_filter]

            # 2. 담당자 필터링 추가
            if manager != '전체':
                query += " AND 현장담당 = %s"
                params.append(manager)

            cursor.execute(query, params)
            
            # 3. 결과 데이터를 딕셔너리 형태로 변환
            columns = [col[0] for col in cursor.description]
            data = [dict(zip(columns, row)) for row in cursor.fetchall()]

            return JsonResponse({'success': True, 'data': data})

    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})
    

# 드롭다운 DB연동
def get_item_standards(request):
    with connection.cursor() as cursor:
        # DB 테이블에서 기준 데이터 조회
        cursor.execute("""
            SELECT ID, 시험종목, 기본, 단가, 추가 
            FROM kcqt_qyalit.winapps_용역비기준
        """)
        rows = cursor.fetchall()
        
    # 자바스크립트에서 쓰기 편하게 리스트 형태로 변환
    data = []
    for row in rows:
        data.append({
            'id': row[0],
            'name': row[1],
            'base': row[2],   # 기본(공수)
            'price': row[3],  # 단가(출장비)
            'extra': row[4]   # 추가 금액
        })

    return JsonResponse(data, safe=False)

@csrf_exempt
def save_settlement_data(request):
    if request.method == 'POST':
        try:
            data_list = json.loads(request.body)
            
            with connection.cursor() as cursor:
                for item in data_list:
                    # 1. 날짜 형식 변환: 마침표(.)가 들어오면 하이픈(-)으로 변경하여 저장
                    raw_date = item.get('시험수거일', '')
                    # 만약 2025.12.01 처럼 점이 찍혀 들어와도 2025-12-01로 바꿉니다.
                    clean_date = str(raw_date).replace('.', '-') if raw_date else ''
                    
                    # 2. 콤마(,) 제거 후 숫자로 변환
                    travel_fee = int(str(item.get('출장비', 0)).replace(',', ''))
                    extra_fee = int(str(item.get('추가', 0)).replace(',', ''))
                    
                    sql = """
                        INSERT INTO kcqt_qyalit.winapps_현장팀 (
                            시험수거일, 현장담당, 구분, 의뢰업체명, 시료명, 
                            공수, 출장비, 추가, 비고, 접수번호, 영업담당, 지급여부
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """
                    params = (
                        clean_date,           # 하이픈 형식 (예: 2025-12-01)
                        item.get('현장담당'),
                        item.get('구분'),
                        item.get('의뢰업체명'),
                        item.get('시료명'),
                        item.get('공수'),
                        travel_fee,
                        extra_fee,
                        item.get('비고'),
                        item.get('접수번호'),
                        item.get('영업담당'),
                        '미지급'               
                    )
                    cursor.execute(sql, params)
            
            return JsonResponse({'status': 'success', 'message': '기존 DB와 동일하게 하이픈(-) 형식으로 저장되었습니다.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    return JsonResponse({'status': 'error', 'message': 'Invalid Method'}, status=400)

# 완료건 DB수정
@csrf_exempt
def update_finished_list(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            items = data.get('items', [])
            with connection.cursor() as cursor:
                for item in items:
                    if not item.get('ID'): continue
                    sql = """
                        UPDATE winapps_현장팀 
                        SET 시험수거일=%s, 현장담당=%s, 구분=%s, 의뢰업체명=%s, 시료명=%s, 
                            공수=%s, 출장비=%s, 추가=%s, 비고=%s, 접수번호=%s, 
                            영업담당=%s, 순번=%s
                        WHERE ID = %s
                    """
                    cursor.execute(sql, [
                        item.get('시험수거일'), item.get('현장담당'), item.get('구분'),
                        item.get('의뢰업체명'), item.get('시료명'), item.get('공수'),
                        item.get('출장비'), item.get('추가'), item.get('비고'),
                        item.get('접수번호'), item.get('영업담당'), item.get('순번'),
                        item.get('ID')
                    ])
            return JsonResponse({"success": True, "message": f"{len(items)}건 수정 완료"})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})




def download_field_excel(request):
    if request.method == 'POST':
        start_time = datetime.now()  # 시작 시간 측정
        app = None
        temp_file_path = None
        
        try:
            pythoncom.CoInitialize() 
            
            data = json.loads(request.body)
            items = data.get('items', [])
            total_count = len(items)
            
            print(f"[{start_time}] 엑셀 생성 시작 - 총 {total_count}건")
            
            # 경로 설정
            template_path = os.path.join(settings.BASE_DIR, 'static', 'excel_templates', 'field_payment_template.xlsx')
            unique_filename = f"temp_field_{uuid.uuid4().hex}.xlsx"
            temp_file_path = os.path.join(settings.BASE_DIR, 'static', 'excel_templates', unique_filename)

            if not os.path.exists(template_path):
                return JsonResponse({"success": False, "message": "양식 파일을 찾을 수 없습니다."}, status=404)

            # 1. 엑셀 앱 실행
            print("Step 1: 엑셀 엔진(xlwings) 구동 중...")
            app = xw.App(visible=False, add_book=False)
            wb = app.books.open(template_path)
            ws = wb.sheets[0]

            # 2. 데이터 매핑 (메모리 작업)
            print("Step 2: 데이터 매핑 중...")
            rows_to_write = []
            for i, item in enumerate(items):
                rows_to_write.append([
                    i + 1,
                    str(item.get('시험수거일', '')),
                    str(item.get('현장담당', '')),
                    str(item.get('구분', '')),
                    str(item.get('의뢰업체명', '')),
                    str(item.get('시료명', '')),
                    item.get('공수', 0) or 0,
                    item.get('출장비', 0) or 0,
                    item.get('추가', 0) or 0,
                    str(item.get('비고', '')),
                    str(item.get('접수번호', '')),
                    str(item.get('영업담당', '')),
                    str(item.get('순번', ''))
                ])

            # 3. 데이터 쓰기
            if rows_to_write:
                print(f"Step 3: 엑셀 시트에 기록 중 ({total_count}건)...")
                ws.range('A5').value = rows_to_write

            # 4. 파일 저장 및 엑셀 종료
            print("Step 4: 임시 파일 생성 및 종료 중...")
            wb.save(temp_file_path)
            wb.close()
            app.quit()
            app = None # 중복 종료 방지

            # 5. 파일을 메모리로 읽기
            with open(temp_file_path, 'rb') as f:
                file_data = f.read()

            # 6. 임시 파일 즉시 삭제
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
                print(f"Step 5: 임시 파일 삭제 완료 ({unique_filename})")

            end_time = datetime.now()
            duration = end_time - start_time
            print(f"결과: 엑셀 생성 완료 (소요시간: {duration})")

            # 응답 생성
            current_date = end_time.strftime('%Y%m%d')
            response = HttpResponse(
                file_data, 
                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
            response['Content-Disposition'] = f'attachment; filename=field_payment_{current_date}.xlsx'
            
            # 커스텀 헤더에 소요 시간 정보 추가 (선택 사항)
            response['X-Generation-Duration'] = str(duration)
            
            return response

        except Exception as e:
            import traceback
            print("--- 엑셀 다운로드 오류 발생 ---")
            print(traceback.format_exc()) 
            return JsonResponse({"success": False, "message": str(e)}, status=500)
            
        finally:
            # 에러 발생 시에도 반드시 리소스 해제
            if app:
                try: 
                    app.quit()
                except: 
                    pass
            if temp_file_path and os.path.exists(temp_file_path):
                try: 
                    os.remove(temp_file_path)
                except: 
                    pass
            pythoncom.CoUninitialize()


# ------------------------여기까지가 완료건 보기 관련 끝----------------------------

# def receipt_settle_report(request):
#     """
#     영업팀 실적 정산 관리 전용 뷰
#     - 목적: 영업팀 실적 정산 (계산서 관련 정보 제외)
#     - 화면: receipt_settle_admin.html
#     """
#     # [A] 화면 초기 접속
#     start_date = request.GET.get('start_date')
#     if not start_date:
#         return render(request, 'receipt_settle_admin.html')

#     # [B] 조회 버튼 클릭 시 (JSON 데이터 처리)
#     try:
#         end_date = request.GET.get('end_date')
#         date_type = request.GET.get('date_type') 
#         search_type = request.GET.get('search_type')
#         search_text = request.GET.get('search_text', '').strip()

#         final_results = []
        
#         # 1. MSSQL 조회 (계산서 관련 테이블 Tax_Manager 조인 및 필드 제거)
#         with connections['mssql'].cursor() as mssql_cursor:
#             if date_type == "deposit":
#                 target_col = "e.deposit_day"
#                 q_start, q_end = start_date.replace('-', ''), end_date.replace('-', '')
#             else:
#                 target_col = "CONVERT(CHAR(10), c.save_date, 120)"
#                 q_start, q_end = start_date, end_date

#             mssql_where = f"WHERE {target_col} BETWEEN %s AND %s "
#             mssql_params = [q_start, q_end]

#             if search_text:
#                 if search_type == 'req_no': mssql_where += " AND c.request_code LIKE %s"
#                 elif search_type == 'receipt_no': mssql_where += " AND a.receipt_code LIKE %s"
#                 elif search_type == 'client': mssql_where += " AND a.builder LIKE %s"
#                 elif search_type == 'project': mssql_where += " AND a.construction LIKE %s"
#                 mssql_params.append(f"%{search_text}%")

#             mssql_query = f"""
#                 SELECT 
#                     RTRIM(c.request_code) as [의뢰번호], RTRIM(a.receipt_code) as [QT번호],
#                     c.sales as [영업담당], c.save_date as [실접수일],
#                     a.builder as [의뢰기관명], a.construction as [사업명],
#                     a.get_name as [시료채취자],
#                     b.specimen as [봉인명], b.specimen_qty as [시료량],
#                     d.supply_value as [공급가액], d.vat as [부가세],
#                     d.basic as [기본료], d.process as [정보처리비], d.sample as [시편제작비],
#                     d.tran_set as [출장비구분], d.[tran] as [출장비],
#                     e.deposit_day as [입금일], e.deposit as [입금액],
#                     g.price as [청구위탁시험비],
#                     ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
#                      WHERE receipt_code = a.receipt_code AND item_name LIKE '%%지게차%%'), 0) as [지게차운임],                    
#                     ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
#                     WHERE receipt_code = a.receipt_code AND item_name LIKE '%%시료수거비%%'), 0) as [시료수거비] 
#                 FROM dbo.Receipt c
#                 LEFT JOIN dbo.Customer a ON c.receipt_code = a.receipt_code
#                 LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
#                 LEFT JOIN dbo.Estimate d ON c.receipt_code = d.receipt_code
#                 LEFT JOIN dbo.Deposit e ON c.receipt_code = e.receipt_code
#                 LEFT JOIN dbo.Consignment g ON c.receipt_code = g.receipt_code
#                 {mssql_where}
#             """
#             mssql_cursor.execute(mssql_query, mssql_params)
#             columns = [col[0] for col in mssql_cursor.description]
#             mssql_rows = [dict(zip(columns, row)) for row in mssql_cursor.fetchall()]

#         # 2. MariaDB(MySQL) 데이터 매칭 및 중복 제거
#         if mssql_rows:
#             req_codes = [r['의뢰번호'] for r in mssql_rows if r['의뢰번호']]
#             qt_codes = [r['QT번호'] for r in mssql_rows if r['QT번호']]
#             csi_map, field_map, incentive_map = {}, {}, {}

#             with connections['default'].cursor() as my_cursor:
#                 # 2-1. CSI 영수증 정보 매칭
#                 if req_codes:
#                     placeholders = ', '.join(['%s'] * len(req_codes))
#                     my_cursor.execute(f"SELECT 의뢰번호, 담당자, 미인정 FROM csi_receipts WHERE 의뢰번호 IN ({placeholders})", req_codes)
#                     for row in my_cursor.fetchall():
#                         csi_map[str(row[0]).strip()] = {'담당자': row[1], '미인정': row[2]}

#                 # 2-2. 현장팀 정보 및 인센티브 정보 매칭
#                 if qt_codes:
#                     placeholders = ', '.join(['%s'] * len(qt_codes))
#                     # 현장 데이터 쿼리
#                     my_cursor.execute(f"SELECT 접수번호, 현장담당, 시료명, 공수, (출장비 + 추가) FROM winapps_현장팀 WHERE 접수번호 IN ({placeholders})", qt_codes)
#                     for row in my_cursor.fetchall():
#                         field_map[str(row[0]).strip()] = {
#                             '현장담당': row[1], '시료명': row[2], '공수': row[3], '지급액합계': row[4]
#                         }
                    
#                     # 인센티브(qt_issue) 쿼리
#                     my_cursor.execute(f"SELECT `QT번호`, `금액` FROM `qt_issue` WHERE `QT번호` IN ({placeholders})", qt_codes)
#                     for row in my_cursor.fetchall():
#                         incentive_map[str(row[0]).strip()] = row[1]

#             # 3. 데이터 결합 및 최종 결과 생성 (중복 제거 포함)
#             seen_qt = set() 
#             for row in mssql_rows:
#                 q_key = row['QT번호']
                
#                 # 중복된 QT번호는 건너뜀
#                 if q_key in seen_qt:
#                     continue
                
#                 r_key = row['의뢰번호']
                
#                 # 매칭 데이터가 없을 경우 기본값 설정
#                 c_info = csi_map.get(r_key, {'담당자': '-', '미인정': '0'})
#                 f_info = field_map.get(q_key, {'현장담당': '-', '시료명': '-', '공수': 0, '지급액합계': 0})
#                 incentive_val = incentive_map.get(q_key, 0)
                
#                 # 데이터 업데이트
#                 row.update({
#                     '담당자': c_info['담당자'], 
#                     '미인정': c_info['미인정'],
#                     '현장담당': f_info['현장담당'], 
#                     '시료명': f_info['시료명'], 
#                     '공수': f_info['공수'], 
#                     '지급액합계': f_info['지급액합계'],
#                     '인센티브': incentive_val
#                 })
                
#                 final_results.append(row)
#                 seen_qt.add(q_key)

#         return JsonResponse(final_results, safe=False)

#     except Exception as e:
#         return JsonResponse({"error": str(e)}, status=500)



def receipt_settle_report(request):
    """
    영업팀 실적 정산 관리 전용 뷰
    - 수정사항: 
      1. receipt_code(QT번호)가 'CX' 또는 'X'로 시작하는 데이터 제외
      2. MySQL(csi_receipts) 매칭 키를 '의뢰번호'에서 'QT번호(receipt_code)'로 변경
    """
    # [A] 화면 초기 접속
    start_date = request.GET.get('start_date')
    if not start_date:
        return render(request, 'receipt_settle_admin.html')

    # [B] 조회 버튼 클릭 시 (JSON 데이터 처리)
    try:
        end_date = request.GET.get('end_date')
        date_type = request.GET.get('date_type') 
        search_type = request.GET.get('search_type')
        search_text = request.GET.get('search_text', '').strip()

        final_results = []
        
        # 1. MSSQL 조회 (CX, X 시작 번호 제외)
        with connections['mssql'].cursor() as mssql_cursor:
            if date_type == "deposit":
                target_col = "e.deposit_day"
                q_start, q_end = start_date.replace('-', ''), end_date.replace('-', '')
            else:
                target_col = "CONVERT(CHAR(10), c.save_date, 120)"
                q_start, q_end = start_date, end_date

            mssql_where = f"""
                WHERE {target_col} BETWEEN %s AND %s 
                AND c.receipt_code NOT LIKE 'CX%%' 
                AND c.receipt_code NOT LIKE 'X%%'
            """
            mssql_params = [q_start, q_end]

            if search_text:
                if search_type == 'req_no': mssql_where += " AND c.request_code LIKE %s"
                elif search_type == 'receipt_no': mssql_where += " AND a.receipt_code LIKE %s"
                elif search_type == 'client': mssql_where += " AND a.builder LIKE %s"
                elif search_type == 'project': mssql_where += " AND a.construction LIKE %s"
                mssql_params.append(f"%{search_text}%")

            mssql_query = f"""
                SELECT 
                    RTRIM(c.request_code) as [의뢰번호], RTRIM(a.receipt_code) as [QT번호],
                    c.sales as [영업담당], c.save_date as [실접수일],
                    a.builder as [의뢰기관명], a.construction as [사업명],
                    a.get_name as [시료채취자],
                    b.specimen as [봉인명], b.specimen_qty as [시료량],
                    d.supply_value as [공급가액], d.vat as [부가세],
                    d.basic as [기본료], d.process as [정보처리비], d.sample as [시편제작비],
                    d.tran_set as [출장비구분], d.[tran] as [출장비],
                    e.deposit_day as [입금일], e.deposit as [입금액],
                    g.price as [청구위탁시험비],
                    ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
                     WHERE receipt_code = a.receipt_code AND item_name LIKE '%%지게차%%'), 0) as [지게차운임],                    
                    ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
                    WHERE receipt_code = a.receipt_code AND item_name LIKE '%%시료수거비%%'), 0) as [시료수거비] 
                FROM dbo.Receipt c
                LEFT JOIN dbo.Customer a ON c.receipt_code = a.receipt_code
                LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
                LEFT JOIN dbo.Estimate d ON c.receipt_code = d.receipt_code
                LEFT JOIN dbo.Deposit e ON c.receipt_code = e.receipt_code
                LEFT JOIN dbo.Consignment g ON c.receipt_code = g.receipt_code
                {mssql_where}
            """
            mssql_cursor.execute(mssql_query, mssql_params)
            columns = [col[0] for col in mssql_cursor.description]
            mssql_rows = [dict(zip(columns, row)) for row in mssql_cursor.fetchall()]

        # 2. MariaDB(MySQL) 데이터 매칭
        if mssql_rows:
            # 모든 매칭을 QT번호(receipt_code) 기준으로 수행하기 위해 qt_codes 추출
            qt_codes = [r['QT번호'] for r in mssql_rows if r['QT번호']]
            csi_map, field_map, incentive_map = {}, {}, {}

            with connections['default'].cursor() as my_cursor:
                if qt_codes:
                    placeholders = ', '.join(['%s'] * len(qt_codes))
                    
                    # 2-1. CSI 정보 매칭 (이제 의뢰번호 대신 QT번호로 매칭)
                    # csi_receipts 테이블의 'QT번호' 컬럼(또는 해당 역할의 컬럼)을 기준으로 조회
                    my_cursor.execute(f"SELECT QT번호, 담당자, 미인정 FROM csi_receipts WHERE QT번호 IN ({placeholders})", qt_codes)
                    for row in my_cursor.fetchall():
                        csi_map[str(row[0]).strip()] = {'담당자': row[1], '미인정': row[2]}

                    # 2-2. 현장팀 정보 매칭
                    my_cursor.execute(f"SELECT 접수번호, 현장담당, 시료명, 공수, (출장비 + 추가) FROM winapps_현장팀 WHERE 접수번호 IN ({placeholders})", qt_codes)
                    for row in my_cursor.fetchall():
                        field_map[str(row[0]).strip()] = {
                            '현장담당': row[1], '시료명': row[2], '공수': row[3], '지급액합계': row[4]
                        }
                    
                    # 2-3. 인센티브 정보 매칭
                    my_cursor.execute(f"SELECT `QT번호`, `금액` FROM `qt_issue` WHERE `QT번호` IN ({placeholders})", qt_codes)
                    for row in my_cursor.fetchall():
                        incentive_map[str(row[0]).strip()] = row[1]

            # 3. 데이터 결합 및 최종 결과 생성
            seen_qt = set() 
            for row in mssql_rows:
                q_key = row['QT번호']
                
                if q_key in seen_qt:
                    continue
                
                # 모든 매칭 정보를 q_key(QT번호)로 검색
                c_info = csi_map.get(q_key, {'담당자': '-', '미인정': '0'})
                f_info = field_map.get(q_key, {'현장담당': '-', '시료명': '-', '공수': 0, '지급액합계': 0})
                incentive_val = incentive_map.get(q_key, 0)
                
                row.update({
                    '담당자': c_info['담당자'], 
                    '미인정': c_info['미인정'],
                    '현장담당': f_info['현장담당'], 
                    '시료명': f_info['시료명'], 
                    '공수': f_info['공수'], 
                    '지급액합계': f_info['지급액합계'],
                    '인센티브': incentive_val
                })
                
                final_results.append(row)
                seen_qt.add(q_key)

        return JsonResponse(final_results, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def save_manager_mapping(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            projects = data.get('projects', [])
            manager = data.get('manager', '')

            # 사업별로 실장 이름을 저장하거나 업데이트
            for project_name in projects:
                # ProjectManagerMap은 프로젝트명과 실장명을 저장하는 테이블입니다.
                ProjectManagerMap.objects.update_or_create(
                    project_name=project_name,
                    defaults={'manager_name': manager}
                )
            
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
    return JsonResponse({'status': 'error', 'message': 'Invalid request'})


# views.py에 추가
def get_manager_mappings(request):
    # DB의 모든 데이터를 가져와서 { '사업명': '실장이름' } 형태의 딕셔너리로 만듭니다.
    mappings = ProjectManagerMap.objects.all()
    data = {m.project_name: m.manager_name for m in mappings}
    return JsonResponse(data)


# --------------------------------4번 분할화면 DB입력------STR
# [조회] QT번호 검색 기능을 포함한 데이터 불러오기
def get_panel4_data(request):
    # GET 파라미터에서 검색어 추출
    search_qt = request.GET.get('qt_no', '').strip()
    
    with connection.cursor() as cursor:
        # 1. 기본 쿼리 (필드명은 이미지와 동일하게 'QT번호', '금액' 사용)
        sql = "SELECT ID, QT번호, 금액 FROM settlement_amount"
        params = []
        
        # 2. 검색어가 있으면 WHERE 절 추가
        if search_qt:
            sql += " WHERE QT번호 LIKE %s"
            params.append(f"%{search_qt}%")
        
        sql += " ORDER BY ID DESC"
        cursor.execute(sql, params)
        
        # 3. 데이터 포맷팅 (JS에서 사용할 이름으로 변경)
        rows = cursor.fetchall()
        result_data = [
            {'id': r[0], 'receipt_code': r[1], 'applied_amount': r[2]} 
            for r in rows
        ]
        
    return JsonResponse({"success": True, "data": result_data})

# [저장] 신규(Insert)와 수정(Update)을 ID 유무로 판단하여 처리
# [views.py] save_panel4_data 함수 내부 수정
@csrf_exempt
def save_panel4_data(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            new_items = data.get('new_items', [])
            updated_items = data.get('updated_items', [])
            
            created_ids = [] 
            
            with connection.cursor() as cursor:
                # A. 신규 저장 (INSERT)
                for item in new_items:
                    cursor.execute(
                        # 1. 비고 컬럼과 %s 추가
                        "INSERT INTO qt_issue (QT번호, 금액, 비고) VALUES (%s, %s, %s)", 
                        # 2. item['memo'] 전달
                        [item['receipt_code'], item['applied_amount'], item.get('memo', '')]
                    )
                    # 방금 INSERT된 ID 가져오기
                    cursor.execute("SELECT LAST_INSERT_ID()")
                    new_id = cursor.fetchone()[0]
                    created_ids.append(new_id)
                
                # B. 기존 수정 (UPDATE)
                for item in updated_items:
                    cursor.execute(
                        # 3. SET 절에 비고 = %s 추가
                        "UPDATE qt_issue SET QT번호 = %s, 금액 = %s, 비고 = %s WHERE ID = %s",
                        # 4. 순서에 맞춰 [QT, 금액, 비고, ID] 전달
                        [item['receipt_code'], item['applied_amount'], item.get('memo', ''), item['id']]
                    )
            
            return JsonResponse({"success": True, "created_ids": created_ids})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})  

# [views.py]
# from django.http import JsonResponse
# from django.db import connection

def get_panel4_data(request):
    # URL 파라미터에서 qt_no 검색어 읽기
    search_qt = request.GET.get('qt_no', '').strip()
    
    with connection.cursor() as cursor:
        # 1. 기본 SQL 쿼리 (이미지 필드명 반영: QT번호, 금액)
        sql = "SELECT ID, QT번호, 금액, 비고 FROM qt_issue"
        params = []
        
        # 2. 검색어가 입력되었다면 WHERE 조건 추가
        if search_qt:
            sql += " WHERE QT번호 LIKE %s"
            params.append(f"%{search_qt}%")
        
        # 최신순 정렬
        sql += " ORDER BY ID DESC"
        
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        
        # 3. 프론트엔드 JS에서 인식할 수 있는 키(key) 이름으로 변환
        result_data = [
            {
                'id': r[0], 
                'receipt_code': r[1], 
                'applied_amount': r[2],
                'memo': r[3]
            } for r in rows
        ]
        
    return JsonResponse({"success": True, "data": result_data})


# ----------------------------------4번 분할화면 여기까지-----END


# --------------여기는 임시로 현장팀 자료 올리는 곳----작업중 페이지

def csi_pending_view(request):
    """
    작업중(csi_pending.html) 페이지를 보여주는 함수
    """
    return render(request, 'csi_pending.html')

@csrf_exempt
def save_field_team_data(request):
    if request.method == 'POST':
        try:
            # 1. 클라이언트로부터 JSON 데이터 수신
            data = json.loads(request.body)
            rows = data.get('rows', [])
            
            if not rows:
                return JsonResponse({"status": "error", "message": "저장할 데이터가 없습니다."}, status=400)

            # 2. DB 연결 및 인서트 실행
            with connections['default'].cursor() as cursor:
                # 제외 항목: 시료채취자, 현장시험자, 지급여부
                sql = """
                    INSERT INTO winapps_현장팀 (
                        시험수거일, 현장담당, 구분, 의뢰업체명, 시료명, 
                        공수, 출장비, 추가, 비고, 접수번호, 영업담당, 순번
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """
                
                # 여러 줄을 한 번에 입력하기 위한 데이터 가공
                params = [
                    (
                        row.get('시험수거일'), row.get('현장담당'), row.get('구분'),
                        row.get('의뢰업체명'), row.get('시료명'), row.get('공수'),
                        row.get('출장비'), row.get('추가'), row.get('비고'),
                        row.get('접수번호'), row.get('영업담당'), row.get('순번')
                    ) for row in rows
                ]
                
                cursor.executemany(sql, params)

            return JsonResponse({"status": "success", "message": f"{len(rows)}건이 성공적으로 저장되었습니다."})

        except Exception as e:
            return JsonResponse({"status": "error", "message": str(e)}, status=500)
    
    return JsonResponse({"status": "error", "message": "잘못된 접근입니다."}, status=405)

# 작업중 페이지 여기까지

# ---------------------------------여기서 부터 settlement_admin 시작입니다.---------------str

def settlement_report(request):
    # [A] 화면 초기 접속
    start_date = request.GET.get('start_date')
    if not start_date:
        return render(request, 'settlement_admin.html')

    # [B] 조회 버튼 클릭 시
    try:
        end_date = request.GET.get('end_date')
        date_type = request.GET.get('date_type') 
        search_type = request.GET.get('search_type')
        search_text = request.GET.get('search_text', '').strip()

        final_results = []
        
        # 1. MSSQL 조회
        with connections['mssql'].cursor() as mssql_cursor:
            if date_type == "deposit":
                target_col = "e.deposit_day"
                q_start, q_end = start_date.replace('-', ''), end_date.replace('-', '')
            else:
                target_col = "CONVERT(CHAR(10), c.save_date, 120)"
                q_start, q_end = start_date, end_date

            mssql_where = f"WHERE {target_col} BETWEEN %s AND %s "
            mssql_params = [q_start, q_end]

            if search_text:
                if search_type == 'req_no': mssql_where += " AND c.request_code LIKE %s"
                elif search_type == 'receipt_no': mssql_where += " AND a.receipt_code LIKE %s"
                elif search_type == 'client': mssql_where += " AND a.builder LIKE %s"
                elif search_type == 'project': mssql_where += " AND a.construction LIKE %s"
                mssql_params.append(f"%{search_text}%")

            mssql_query = f"""
                SELECT 
                    RTRIM(c.request_code) as [의뢰번호], RTRIM(a.receipt_code) as [QT번호],
                    c.sales as [영업담당], c.save_date as [실접수일],
                    a.builder as [의뢰기관명], a.construction as [사업명],
                    a.cm_name as [의뢰인성명], a.cm_tel as [현장전화], a.get_name as [시료채취자], a.qm_name as [품질담당자],
                    b.specimen as [봉인명], b.specimen_qty as [시료량],
                    d.supply_value as [공급가액], d.vat as [부가세],
                    d.basic as [기본료], d.process as [정보처리비], d.sample as [시편제작비],
                    d.tran_set as [출장비구분], d.[tran] as [출장비],
                    e.deposit_day as [입금일], e.deposit as [입금액],
                    f.company as [계산서발행회사명], f.issue_date as [계산서발행일], f.manager as [계산서담당자],
                    f.hp as [계산서hp], f.tel as [계산서tel], f.fax as [계산서fax], f.email as [계산서email], f.issue_employee as [계산서발행자],
                    g.price as [청구위탁시험비],
                    ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
                     WHERE receipt_code = a.receipt_code AND item_name LIKE '%%지게차%%'), 0) as [지게차운임],                    
                    ISNULL((SELECT SUM(ei_price) FROM dbo.Examination_Item 
                    WHERE receipt_code = a.receipt_code AND item_name LIKE '%%시료수거비%%'), 0) as [시료수거비] 
                FROM dbo.Receipt c
                LEFT JOIN dbo.Customer a ON c.receipt_code = a.receipt_code
                LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
                LEFT JOIN dbo.Estimate d ON c.receipt_code = d.receipt_code
                LEFT JOIN dbo.Deposit e ON c.receipt_code = e.receipt_code
                LEFT JOIN dbo.Tax_Manager f ON c.receipt_code = f.receipt_code
                LEFT JOIN dbo.Consignment g ON c.receipt_code = g.receipt_code
                {mssql_where}
            """
            mssql_cursor.execute(mssql_query, mssql_params)
            columns = [col[0] for col in mssql_cursor.description]
            mssql_rows = [dict(zip(columns, row)) for row in mssql_cursor.fetchall()]

        # 2. 데이터 매칭 및 중복 제거
        if mssql_rows:
            req_codes = [r['의뢰번호'] for r in mssql_rows if r['의뢰번호']]
            qt_codes = [r['QT번호'] for r in mssql_rows if r['QT번호']]
            csi_map, field_map, incentive_map = {}, {}, {} # incentive_map 추가

            with connections['default'].cursor() as my_cursor:
                if req_codes:
                    placeholders = ', '.join(['%s'] * len(req_codes))
                    my_cursor.execute(f"SELECT 의뢰번호, 담당자, 미인정 FROM csi_receipts WHERE 의뢰번호 IN ({placeholders})", req_codes)
                    for row in my_cursor.fetchall():
                        csi_map[str(row[0]).strip()] = {'담당자': row[1], '미인정': row[2]}

                if qt_codes:
                    placeholders = ', '.join(['%s'] * len(qt_codes))
                    # 1) 기존 field_map용 쿼리
                    my_cursor.execute(f"SELECT 접수번호, 현장담당, 시료명, 공수, (출장비 + 추가) FROM winapps_현장팀 WHERE 접수번호 IN ({placeholders})", qt_codes)
                    for row in my_cursor.fetchall():
                        field_map[str(row[0]).strip()] = {
                            '현장담당': row[1], '시료명': row[2], '공수': row[3], '지급액합계': row[4]
                        }
                    
                    # 2) 신규 인센티브 매칭용 쿼리 (qt_issue 테이블)
                    my_cursor.execute(f"SELECT `QT번호`, `금액` FROM `qt_issue` WHERE `QT번호` IN ({placeholders})", qt_codes)
                    for row in my_cursor.fetchall():
                        incentive_map[str(row[0]).strip()] = row[1] # 금액 저장

            # -------------------------------------------------------
            # [중복 제거 포인트] 중복 체크를 위한 Set 변수 생성
            # -------------------------------------------------------
            seen_qt = set() 
            
            for row in mssql_rows:
                q_key = row['QT번호']
                
                if q_key in seen_qt:
                    continue
                
                r_key = row['의뢰번호']
                c_info = csi_map.get(r_key, {'담당자': '-', '미인정': '0'})
                f_info = field_map.get(q_key, {'현장담당': '-', '시료명': '-', '공수': 0, '지급액합계': 0})
                incentive_val = incentive_map.get(q_key, 0) # 인센티브 금액 가져오기
                
                row.update({
                    '담당자': c_info['담당자'], 
                    '미인정': c_info['미인정'],
                    '현장담당': f_info['현장담당'], 
                    '시료명': f_info['시료명'], 
                    '공수': f_info['공수'], 
                    '지급액합계': f_info['지급액합계'],
                    '인센티브': incentive_val # 최종 데이터에 추가
                })
                
                final_results.append(row)
                seen_qt.add(q_key)
            # -------------------------------------------------------

        return JsonResponse(final_results, safe=False)

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ---------------------------------여기까지 settlement_admin 끝입니다.---------------end

# -----------------------여기부터 db연결 후 인센넣기----str

def get_qt_incentives(request):
    try:
        with connection.cursor() as cursor:
            # MySQL 문법에 맞춰 대괄호[]를 제거하거나 백틱(``)으로 수정합니다.
            # 한글 컬럼명이므로 안전하게 백틱을 사용하거나 그냥 입력합니다.
            sql = "SELECT `QT번호`, `금액` FROM `qt_issue`"
            cursor.execute(sql)
            
            columns = [col[0] for col in cursor.description]
            results = []
            
            for row in cursor.fetchall():
                row_dict = dict(zip(columns, row))
                
                # 금액이 VARCHAR이므로 숫자로 변환 처리 [이미지 데이터 구조 참고]
                val = row_dict.get('금액')
                if val:
                    try:
                        # 콤마나 공백 제거 후 float 변환
                        row_dict['금액'] = float(str(val).replace(',', '').strip())
                    except:
                        row_dict['금액'] = 0
                else:
                    row_dict['금액'] = 0
                    
                results.append(row_dict)
            
            return JsonResponse(results, safe=False)
            
    except Exception as e:
        # 에러 발생 시 터미널에 상세 내용을 찍습니다.
        print(f"!!! Django View Error: {str(e)}")
        return JsonResponse({"error": str(e)}, status=500)

# -----------------------여기까지 db연결 후 인센넣기-----end

# 1. [조회] 메인 페이지 로드
def notice(request):
    client_list = ClientProject.objects.all().order_by('-created_at')
    context = {
        'username': request.user.username if request.user.is_authenticated else "방문자",
        'client_list': client_list,
    }
    return render(request, 'notice.html', context)

# 2. [확인] 사업명 입력 시 DB에 있는지 미리 체크하는 기능
def get_project_detail(request):
    project_name = request.GET.get('project_name')
    if not project_name:
        return JsonResponse({'status': 'error', 'message': '사업명을 입력해주세요.'})

    try:
        with connections['mssql'].cursor() as cursor:
            # 사업명으로 시공사(builder)만 빠르게 조회
            query = "SELECT TOP 1 builder FROM dbo.Customer WHERE construction = %s"
            cursor.execute(query, [project_name])
            row = cursor.fetchone()

            if row:
                return JsonResponse({'status': 'success', 'builder': row[0]})
            else:
                return JsonResponse({'status': 'empty', 'message': '신규 현장입니다. 시공사를 직접 입력하세요.'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

# 3. [저장] 최종 등록 (DB 확인 로직 포함)


@csrf_exempt
def register_client(request):
    if request.method == 'POST':
        try:
            # 기존 코드처럼 POST 데이터를 수신
            name = request.POST.get('reg_name')
            phone = request.POST.get('reg_phone')
            email = request.POST.get('reg_email')
            project_name = request.POST.get('reg_project_name')
            company = request.POST.get('reg_company') 

            # 기존 코드처럼 직접 SQL 실행
            with connections['default'].cursor() as cursor:
                sql = """
                    INSERT INTO client_projects (
                        reg_name, reg_phone, reg_email, reg_company, 
                        reg_project_name, is_linked, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """
                cursor.execute(sql, [
                    name, phone, email, 
                    company if company else "미지정",
                    project_name,
                    1 if company else 0
                ])

            return JsonResponse({"status": "success", "message": "성공적으로 저장되었습니다."})

        except Exception as e:
            # 여기서 (1062, "Duplicate entry...") 에러가 난다면 
            # 100% DB의 reg_phone에 Unique 설정이 걸려있는 것입니다.
            return JsonResponse({"status": "error", "message": str(e)})
        
        

    
def search_clients(request):
    keyword = request.GET.get('keyword', '').strip()
    try:
        with connections['default'].cursor() as cursor:
            # reg_email 컬럼을 추가로 조회합니다.
            sql = """
                SELECT id, reg_name, reg_phone, reg_email, reg_company, reg_project_name 
                FROM client_projects 
                WHERE reg_name LIKE %s 
                   OR reg_company LIKE %s 
                   OR reg_project_name LIKE %s
                ORDER BY created_at DESC
            """
            search_param = f"%{keyword}%"
            cursor.execute(sql, [search_param, search_param, search_param])
            
            columns = [col[0] for col in cursor.description]
            data = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return JsonResponse({'status': 'success', 'data': data})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

    
def get_project_full_details(request):
    project_name = request.GET.get('project_name', '').strip()
    
    # %%를 사용하여 파이썬의 문자열 치환 에러를 방지하고
    # LIKE를 사용하여 사업명 매칭률을 높였습니다.
    mssql_query = """
        SELECT 
            a.request_code, a.receipt_code, 
            CONVERT(VARCHAR(10), a.save_date, 120) AS save_date,
            c.specimen,
            ISNULL(d.supply_value, 0) as supply_value,
            ISNULL(e.deposit, 0) as deposit,
            e.deposit_day,
            f.issue_date, f.company
        FROM dbo.Receipt a
        INNER JOIN dbo.Customer b      ON a.receipt_code = b.receipt_code
        LEFT JOIN dbo.Specimen_info c  ON a.receipt_code = c.receipt_code
        LEFT JOIN dbo.Estimate d       ON a.receipt_code = d.receipt_code
        LEFT JOIN dbo.Deposit e        ON a.receipt_code = e.receipt_code
        LEFT JOIN dbo.Tax_Manager f    ON a.receipt_code = f.receipt_code
        WHERE b.construction LIKE %s 
          AND a.receipt_code NOT LIKE 'X%%'
        ORDER BY a.save_date DESC
    """
    
    try:
        with connections['mssql'].cursor() as cursor:
            # 사업명 앞뒤에 %를 붙여서 부분이 일치하더라도 찾아오게 합니다.
            search_param = f"%{project_name}%"
            cursor.execute(mssql_query, [search_param])
            
            columns = [col[0] for col in cursor.description]
            data = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            return JsonResponse({'status': 'success', 'data': data})
            
    except Exception as e:
        import traceback
        print(f"MSSQL 에러 상세:\n{traceback.format_exc()}")
        return JsonResponse({'status': 'error', 'message': str(e)})
    
# ---------------------------memo 저장-----------------------str
@csrf_exempt # 실제 서비스에선 CSRF 토큰을 사용하는 것이 안전합니다
def save_consulting_memo(request):
    if request.method == 'POST':
        memo_id = request.POST.get('memo_id') # 수정 시 필요한 상담 PK
        client_id = request.POST.get('client_id')
        project_name = request.POST.get('project_name')
        category = request.POST.get('category')
        content = request.POST.get('content')
        
        try:
            with transaction.atomic(): # 상담과 업무예약 수정을 하나의 묶음으로 처리
                with connections['default'].cursor() as cursor:
                    if memo_id: # [수정 모드]
                        # 1. 상담 히스토리 업데이트
                        sql_update_memo = """
                            UPDATE consulting_memos 
                            SET category=%s, content=%s, project_name=%s
                            WHERE id=%s
                        """
                        cursor.execute(sql_update_memo, [category, content, project_name, memo_id])
                        
                        # 2. 연관된 업무 예약(Task)도 함께 수정
                        # (상담 내용이 바뀌면 캘린더/우측리스트에 나오는 Task 내용도 변경)
                        if '예약' in category:
                            sql_update_task = """
                                UPDATE task_management 
                                SET category=%s, content=%s, project_name=%s
                                WHERE client_id=%s AND created_at >= (SELECT created_at - INTERVAL 1 MINUTE FROM consulting_memos WHERE id=%s)
                                LIMIT 1
                            """
                            # 참고: 더 정확한 연동을 위해선 consulting_memos에 task_id 컬럼을 추가하는 것이 가장 좋습니다.
                            cursor.execute(sql_update_task, [category, content, project_name, client_id, memo_id])

                    else: # [신규 등록 모드]
                        # 기존 코드 유지
                        sql_memo = "INSERT INTO consulting_memos (client_id, project_name, category, content) VALUES (%s, %s, %s, %s)"
                        cursor.execute(sql_memo, [client_id, project_name, category, content])
                        
                        if '예약' in category:
                            sql_task = """
                                INSERT INTO task_management (client_id, project_name, category, content, start_date)
                                VALUES (%s, %s, %s, %s, CURDATE())
                            """
                            cursor.execute(sql_task, [client_id, project_name, category, content])
            
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})


# ------------------------------과거상담 기록 출력--------------str

# board/views.py 에 추가

def get_consulting_history(request):
    client_id = request.GET.get('client_id')
    
    try:
        with connections['default'].cursor() as cursor:
            # ★ 핵심: SELECT 뒤에 'id'를 반드시 추가하세요!
            sql = """
                SELECT id, category, content, 
                       DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i') as date
                FROM consulting_memos 
                WHERE client_id = %s 
                ORDER BY created_at DESC
            """
            cursor.execute(sql, [client_id])
            
            columns = [col[0] for col in cursor.description]
            data = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            return JsonResponse({'status': 'success', 'data': data})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})
    
    # --------------------------예약관리시스템----------------str

def save_consulting_memo(request):
    if request.method == 'POST':
        client_id = request.POST.get('client_id')
        project_name = request.POST.get('project_name')
        category = request.POST.get('category')
        content = request.POST.get('content')
        
        try:
            with connections['default'].cursor() as cursor:
                # 1. 히스토리 기록 (MySQL)
                sql_memo = "INSERT INTO consulting_memos (client_id, project_name, category, content) VALUES (%s, %s, %s, %s)"
                cursor.execute(sql_memo, [client_id, project_name, category, content])
                
                # 2. '예약' 버튼인 경우 업무 예약 테이블에도 저장
                if '예약' in category:
                    # 시작일은 오늘(CURDATE())로 자동 설정
                    sql_task = """
                        INSERT INTO task_management (client_id, project_name, category, content, start_date)
                        VALUES (%s, %s, %s, %s, CURDATE())
                    """
                    cursor.execute(sql_task, [client_id, project_name, category, content])
            
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        

# -----------------------------우츨리스트에 뿌려줄 데이터 전달함수-----------

# board/views.py
def get_active_tasks(request):
    try:
        with connections['default'].cursor() as cursor:
            # WHERE 절을 수정하여 완료된 건도 포함합니다.
            # 최근 등록 순으로 가져오되, 완료 여부(is_completed)를 함께 가져옵니다.
            sql = """
                SELECT id, category, project_name, content, 
                       DATE_FORMAT(start_date, '%Y-%m-%d') as start_date,
                       is_completed
                FROM task_management 
                ORDER BY is_completed ASC, created_at DESC 
                LIMIT 20
            """
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            tasks = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return JsonResponse({'status': 'success', 'data': tasks})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})







@csrf_exempt # 테스트를 위해 잠시 추가 (성공하면 나중에 빼셔도 됩니다)
def complete_task(request):
    if request.method == 'POST':
        task_id = request.POST.get('task_id')
        try:
            with connections['default'].cursor() as cursor:
                # 해당 ID의 is_completed를 1(완료)로 업데이트
                sql = "UPDATE task_management SET is_completed = 1 WHERE id = %s"
                cursor.execute(sql, [task_id])
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        


# ---------------캘린더 작업용-----------------

# views.py
def get_calendar_events(request):
    try:
        with connections['default'].cursor() as cursor:
            # 완료되지 않은 업무와 완료된 업무 모두 캘린더에 표시
            sql = """
                SELECT 
                    project_name as title, 
                    start_date as start,
                    category,
                    is_completed
                FROM task_management
            """
            cursor.execute(sql)
            columns = [col[0] for col in cursor.description]
            events = [dict(zip(columns, row)) for row in cursor.fetchall()]
            
            # 완료된 업무는 제목 앞에 [완료]를 붙이거나 색상을 다르게 설정
            for event in events:
                if event['is_completed'] == 1:
                    event['title'] = "[완료] " + event['title']
                    event['color'] = '#adb5bd' # 회색
                else:
                    event['color'] = '#28a745' if event['category'] == '시험예약' else '#007bff'
                    
        return JsonResponse(events, safe=False)
    except Exception as e:
        return JsonResponse([], safe=False)
    
    # ------------------------폴더생성관리------------str
    
            


def manage_folder(request):
    # 1. 데이터 가져오기 (POST 우선, 없으면 GET)
    if request.method == 'POST':
        data = request.POST
    else:
        data = request.GET

    action = data.get('action')
    name = data.get('name', '이름없음').strip()
    # 전화번호에서 하이픈 제거 및 공백 제거
    phone = data.get('phone', '0000').replace('-', '').strip()
    project_name = data.get('project_name', '사업명미정').strip()

    # 2. 기본 경로 설정
    base_root = r"F:\20160116_내자료\007_업무_영업팀\010_일반상담 견적요청 자료보관"
    
    # 3. [중요] 폴더명 규칙 변경 (동일인 통합)
    # 이제 ID(505, 506) 대신 이름과 전화번호를 1차 폴더명으로 사용합니다.
    # 이렇게 하면 DB ID가 달라도 이름과 번호가 같으면 같은 폴더로 들어갑니다.
    client_folder_name = f"{name}_{phone}"
    
    # 4. 전체 경로 생성 (기본경로 \ 이름_번호 \ 사업명)
    # 예: F:\...\심종열_01089968759\대전사옥 신축공사
    target_path = os.path.join(base_root, client_folder_name, project_name)

    # --- 생성 로직 ---
    if action == 'create':
        try:
            # exist_ok=True: 상위 폴더(이름_번호)가 이미 있어도 에러 없이 하위(사업명)만 생성함
            os.makedirs(target_path, exist_ok=True)
            return JsonResponse({
                'status': 'success', 
                'message': f'[{name}]님의 [{project_name}] 폴더가 준비되었습니다.'
            })
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f'생성 실패: {str(e)}'})

    # --- 열기 로직 ---
    elif action == 'open':
        try:
            if os.path.exists(target_path):
                os.startfile(target_path)
                return JsonResponse({'status': 'success'})
            else:
                # 만약 사업 폴더가 없으면 상위 폴더(고객 폴더)라도 있는지 확인
                parent_path = os.path.dirname(target_path)
                if os.path.exists(parent_path):
                    os.startfile(parent_path)
                    return JsonResponse({'status': 'success', 'message': '사업 폴더가 없어 고객 폴더를 엽니다.'})
                
                return JsonResponse({'status': 'error', 'message': '폴더를 찾을 수 없습니다. 생성을 먼저 눌러주세요.'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': f'열기 실패: {str(e)}'})

    return JsonResponse({'status': 'error', 'message': '잘못된 요청입니다.'})

# ----------------------------담담자 및 현장 수정------------------------------str
def save_client_project(request):
    data = json.loads(request.body)
    client_id = data.get('client_id')
    is_new_project = data.get('is_new_project') # True면 추가, False면 수정
    
    new_name = data.get('name')
    new_phone = data.get('phone', '').replace('-', '') # 하이픈 제거
    new_project_name = data.get('project_name')
    new_company = data.get('company')

    if client_id:
        try:
            client = Client.objects.get(id=client_id)
            
            # [폴더 변경을 위한 준비]
            # 기존 정보로 폴더 경로 생성
            old_name = client.reg_name
            old_phone = client.reg_phone.replace('-', '')
            old_project = client.reg_project_name
            
            base_dir = "F:\20160116_내자료\007_업무_영업팀\010_일반상담 견적요청 자료보관"  # 실제 사용하는 상위 경로로 수정하세요
            old_folder_path = os.path.join(base_dir, f"{old_name}_{old_phone}", old_project)
            new_folder_path = os.path.join(base_dir, f"{new_name}_{new_phone}", new_project_name)

            if is_new_project:
                # 1. 신규 현장 추가 (새 레코드 생성)
                Client.objects.create(
                    reg_name=new_name,
                    reg_phone=data.get('phone'),
                    reg_company=new_company,
                    reg_project_name=new_project_name
                )
                # 추가일 때는 기존 폴더를 건드릴 필요가 없음 (나중에 폴더생성 버튼 누를 때 만들어짐)
            
            else:
                # 2. 기존 현장 수정 (Update)
                # 만약 현장명이 바뀌었다면 실제 폴더 이름도 변경 시도
                if old_project != new_project_name and os.path.exists(old_folder_path):
                    try:
                        # 상위 폴더(이름_번호)가 바뀌었을 수도 있으므로 체크 후 변경
                        parent_path = os.path.join(base_dir, f"{new_name}_{new_phone}")
                        if not os.path.exists(parent_path):
                            os.makedirs(parent_path)
                        
                        os.rename(old_folder_path, new_folder_path)
                    except Exception as e:
                        print(f"폴더명 변경 실패: {e}")

                # DB 정보 업데이트
                client.reg_name = new_name
                client.reg_phone = data.get('phone')
                client.reg_project_name = new_project_name
                client.reg_company = new_company
                client.save()

            return JsonResponse({'status': 'success', 'message': '저장되었습니다.'})
            
        except Client.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '대상자를 찾을 수 없습니다.'})
    
    return JsonResponse({'status': 'error', 'message': '잘못된 요청입니다.'})

# ----------------------------메모 수정 삭제 -------------------str

# 메모 수정 뷰
@csrf_exempt
def update_memo(request):
    if request.method == 'POST':
        memo_id = request.POST.get('memo_id')
        new_content = request.POST.get('content')
        
        try:
            with transaction.atomic():
                with connections['default'].cursor() as cursor:
                    # 1. 수정 전의 원본 데이터 정보 가져오기
                    cursor.execute("""
                        SELECT client_id, category, content 
                        FROM consulting_memos WHERE id = %s
                    """, [memo_id])
                    row = cursor.fetchone()
                    if not row:
                        return JsonResponse({'status': 'error', 'message': '메모 없음'})
                    
                    client_id, category, old_content = row

                    # 2. 상담 메모 본체 수정
                    cursor.execute("UPDATE consulting_memos SET content = %s WHERE id = %s", [new_content, memo_id])

                    # 3. 업무 예약(task_management) 동기화
                    if '예약' in category:
                        # [보정된 로직] 같은 고객이고, 수정 전의 내용(old_content)을 가진 가장 최근 Task를 찾아 수정
                        sql_update_task = """
                            UPDATE task_management 
                            SET content = %s 
                            WHERE client_id = %s 
                            AND content = %s
                            ORDER BY created_at DESC 
                            LIMIT 1
                        """
                        cursor.execute(sql_update_task, [new_content, client_id, old_content])

            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        


@csrf_exempt
def delete_memo(request):
    if request.method == 'POST':
        memo_id = request.POST.get('memo_id')
        
        try:
            with transaction.atomic():
                with connections['default'].cursor() as cursor:
                    # 1. 삭제 전, 해당 메모의 내용(content)과 고객ID를 미리 가져옵니다.
                    cursor.execute("SELECT client_id, content, category FROM consulting_memos WHERE id = %s", [memo_id])
                    row = cursor.fetchone()
                    
                    if row:
                        client_id, content, category = row
                        
                        # 2. 상담 메모 삭제
                        cursor.execute("DELETE FROM consulting_memos WHERE id = %s", [memo_id])

                        # 3. 예약 관련 카테고리라면 업무 예약 테이블에서도 삭제
                        if '예약' in category:
                            # 동일 고객, 동일 내용인 가장 최근 업무를 삭제
                            sql_delete_task = """
                                DELETE FROM task_management 
                                WHERE client_id = %s 
                                AND TRIM(content) = TRIM(%s)
                            """
                            cursor.execute(sql_delete_task, [client_id, content])

            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        
        # ---------------------챠트시작-------------------str


def get_stats(request):
    try:
        # 1. 파라미터 수집
        year_str = request.GET.get('year')
        month_str = request.GET.get('month', '1').zfill(2)
        year = int(year_str)
        month = int(month_str)
        mode = request.GET.get('mode', 'daily')

        # 2. 필터 조건 설정
        mysql_filter = f"{year}%" if mode == 'yearly' else f"{year}-{month_str}%"
        mssql_filter_no_hyphen = f"{year}" if mode == 'yearly' else f"{year}{month_str}"
        
        last_index = 12 if mode == 'yearly' else calendar.monthrange(year, month)[1]
        date_func = "MONTH" if mode == 'yearly' else "DAY"

        teams = ['1팀', '2팀', '3팀', '4팀', '5팀', '6팀']
        result_data = {
            team: { 
                'receipt': [0] * last_index, 
                'issue': [0] * last_index,           # 파란색 선
                'matched_issue': [0] * last_index,   # 녹색 선
                'sales': [0] * last_index, 
                'deposit': [0] * last_index 
            } for team in teams
        }

        # 3. [MySQL] 접수/발급 데이터 (그래프 선 데이터 포함)
        mapping_dict = {}
        with connection.cursor() as cursor:
            cursor.execute("SELECT TRIM(의뢰번호), TRIM(담당자) FROM csi_receipts WHERE 담당자 IS NOT NULL")
            for req_no, owner in cursor.fetchall():
                mapping_dict[req_no] = owner

            # (1) 접수 건수
            cursor.execute(f"SELECT {date_func}(STR_TO_DATE(배정일자, '%Y-%m-%d')) as idx, TRIM(담당자), COUNT(*) FROM csi_receipts WHERE 배정일자 LIKE '{mysql_filter}' GROUP BY idx, 담당자")
            for idx, team, cnt in cursor.fetchall():
                if team in result_data and idx:
                    result_data[team]['receipt'][int(idx)-1] = cnt

            # (2) 발급 총건수 (파란색 선)
            cursor.execute(f"SELECT {date_func}(STR_TO_DATE(I.발급일자, '%Y-%m-%d')) as idx, TRIM(R.담당자), COUNT(*) FROM csi_issue_results I INNER JOIN csi_receipts R ON I.의뢰번호 = R.의뢰번호 WHERE I.발급일자 LIKE '{mysql_filter}' GROUP BY idx, R.담당자")
            for idx, team, cnt in cursor.fetchall():
                if team in result_data and idx:
                    result_data[team]['issue'][int(idx)-1] = cnt

            # (3) 매칭 발급건수 (녹색 선)
            cursor.execute(f"SELECT {date_func}(STR_TO_DATE(R.배정일자, '%Y-%m-%d')) as idx, TRIM(R.담당자), COUNT(*) FROM csi_receipts R INNER JOIN csi_issue_results I ON R.의뢰번호 = I.의뢰번호 WHERE R.배정일자 LIKE '{mysql_filter}' GROUP BY idx, R.담당자")
            for idx, team, cnt in cursor.fetchall():
                if team in result_data and idx:
                    result_data[team]['matched_issue'][int(idx)-1] = cnt

        # 4. [MSSQL] 매출(공급가만)/입금 처리
        with connections['mssql'].cursor() as mssql_cursor:
            
            # --- [매출액: 실접수일 기준 + 부가세 제외] ---
            s_idx = "MONTH(R.save_date)" if mode == 'yearly' else "DAY(R.save_date)"
            sales_where = f"YEAR(R.save_date) = {year}"
            if mode != 'yearly':
                sales_where += f" AND MONTH(R.save_date) = {month}"

            # 🎯 수정 포인트: E.vat를 더하지 않고 ISNULL(E.supply_value, 0)만 합산
            sales_sql = f"""
                SELECT {s_idx}, LTRIM(RTRIM(R.request_code)), SUM(ISNULL(E.supply_value, 0)) 
                FROM dbo.Receipt R 
                LEFT JOIN dbo.Estimate E ON R.receipt_code = E.receipt_code 
                WHERE {sales_where}
                GROUP BY {s_idx}, R.request_code
            """
            mssql_cursor.execute(sales_sql)
            for idx, req_code, val in mssql_cursor.fetchall():
                team = mapping_dict.get(req_code)
                if team in result_data and idx:
                    idx_val = int(idx) - 1
                    if 0 <= idx_val < last_index:
                        result_data[team]['sales'][idx_val] += int(val or 0)

            # --- [입금액: 기존 유지] ---
            d_idx = "CAST(SUBSTRING(D.deposit_day, 5, 2) AS INT)" if mode == 'yearly' else "CAST(SUBSTRING(D.deposit_day, 7, 2) AS INT)"
            deposit_sql = f"""
                SELECT {d_idx}, LTRIM(RTRIM(R.request_code)), SUM(ISNULL(D.deposit, 0)) 
                FROM dbo.Deposit D 
                INNER JOIN dbo.Receipt R ON D.receipt_code = R.receipt_code 
                WHERE D.deposit_day LIKE '{mssql_filter_no_hyphen}%' 
                GROUP BY {d_idx}, R.request_code
            """
            mssql_cursor.execute(deposit_sql)
            for idx, req_code, val in mssql_cursor.fetchall():
                team = mapping_dict.get(req_code)
                if team in result_data and idx:
                    idx_val = int(idx) - 1
                    if 0 <= idx_val < last_index:
                        result_data[team]['deposit'][idx_val] += int(val or 0)

        return JsonResponse(result_data)

    except Exception as e:
        print(traceback.format_exc())
        return JsonResponse({'error': str(e)}, status=500)
    
    # ----------------여기서부터 입출금 관리 페이지 시작입니다.----------

def transaction_list(request):
    db_data = transactions.objects.all().order_by('-date')
    
    # 합계 계산 로직 (category_id 1: 입금, 2: 지출)
    income_total = transactions.objects.filter(category_id=1).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    expense_total = transactions.objects.filter(category_id=2).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
    balance = income_total - expense_total

    context = {
        'transactions_data': db_data,
        'income_total': f"{income_total:,}", # 콤마 표시
        'expense_total': f"{expense_total:,}",
        'balance': f"{balance:,}",
    }
    return render(request, 'transactions.html', context)


def transactions_save(request):
    if request.method == 'POST':
        try:
            # 1. 수집 및 저장
            transactions.objects.create(
                date=request.POST.get('date'),
                category_id=request.POST.get('type'),  # 입금(1)/지출(2) 구분
                client_name=request.POST.get('client'),
                
                # 수량과 단가
                unit_price=request.POST.get('unit_price', 0),
                quantity=request.POST.get('qty', 1),
                
                # 결제 수단 및 분류
                account_name=request.POST.get('account_name'),
                category_main=request.POST.get('category_main'),
                
                # 금액 정보
                supply_value=request.POST.get('supply', 0),
                vat=request.POST.get('vat', 0),
                total_amount=request.POST.get('total', 0),
                
                # [수정된 부분] 적요와 비고를 각각의 컬럼에 따로 저장
                description=request.POST.get('summary'), # 적요만 저장
                note=request.POST.get('note'),           # 비고(메모) 따로 저장
                
                # 영수증 파일
                receipt_img=request.FILES.get('file_path') 
            )
            return JsonResponse({'status': 'success'})
            
        except Exception as e:
            print(f"Error saving transaction: {e}")
            return JsonResponse({'status': 'error', 'message': str(e)})
        
        #---------------수정 및 삭제 기능----------------STR
        # 1. 내역 삭제
# 삭제 처리 함수
def transaction_delete(request, pk):
    if request.method == 'POST':
        try:
            # pk(아이디)값으로 데이터를 찾아서 삭제합니다.
            item = get_object_or_404(transactions, pk=pk)
            item.delete()
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
            
    return JsonResponse({'status': 'error', 'message': '잘못된 접근입니다.'})

# 모달에 채울 데이터를 서버에서 보내주는 부분
def get_transaction_detail(request, pk):
    try:
        # 1. 데이터 가져오기 (없으면 404 에러)
        item = get_object_or_404(transactions, pk=pk)
        
        # 2. 이미지 URL 안전하게 추출
        receipt_url = None
        try:
            if item.receipt_img and hasattr(item.receipt_img, 'url'):
                receipt_url = item.receipt_img.url
        except ValueError:
            # 파일 필드에 이름은 있는데 실제 파일이 서버에 없을 경우 에러 방지
            receipt_url = None

        # 3. 데이터 조립 (데이터가 하나라도 None이면 에러날 수 있으므로 getattr 사용)
        data = {
            'date': item.date.strftime('%Y-%m-%d') if item.date else '',
            'type': item.category_id,
            'category_main': getattr(item, 'category_main', ''),
            'account_name': getattr(item, 'account_name', ''),
            'client': getattr(item, 'client_name', ''),
            'summary': getattr(item, 'description', ''),
            'note': getattr(item, 'note', ''),
            'unit_price': item.unit_price or 0,
            'qty': item.quantity or 0,
            'supply': item.supply_value or 0,
            'vat': item.vat or 0,
            'total': item.total_amount or 0,
            'receipt_url': receipt_url, # 안전하게 추출된 URL
        }
        print(f"ID {pk}의 이미지 경로: {receipt_url}")
        return JsonResponse(data)

    except Exception as e:
        # 에러 발생 시 터미널(CMD) 창에 에러 내용을 상세히 출력합니다.
        print("--- 서버 에러 상세 발생 ---")
        print(traceback.format_exc()) 
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)
    
    # [수정 저장] 실제 DB 데이터를 업데이트하는 함수
def transaction_update(request, pk):
    if request.method == 'POST':
        try:
            item = get_object_or_404(transactions, pk=pk)
            
            # 일반 텍스트 데이터 업데이트
            item.date = request.POST.get('date')
            item.category_id = request.POST.get('type')
            item.category_main = request.POST.get('category_main')
            item.account_name = request.POST.get('account_name')
            item.client_name = request.POST.get('client')
            item.description = request.POST.get('summary')
            item.note = request.POST.get('note')
            item.unit_price = request.POST.get('unit_price', 0)
            item.quantity = request.POST.get('qty', 1)
            item.supply_value = request.POST.get('supply', 0)
            item.vat = request.POST.get('vat', 0)
            item.total_amount = request.POST.get('total', 0)
            
            # [이미지 처리 핵심] 새로운 파일이 넘어왔을 때만 업데이트
            if request.FILES.get('file_path'):
                item.receipt_img = request.FILES.get('file_path')
            
            item.save()
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})
        
        
def get_categories(request):
    # MySQL의 transactioncategory 테이블에서 모든 데이터를 가져옵니다.
    categories = TransactionCategory.objects.all().order_by('name')
    data = [{'name': cat.name, 'icon': cat.icon} for cat in categories]
    return JsonResponse(data, safe=False)
        
        # 1. 분류 추가 (Create)
def add_category(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        icon = request.POST.get('icon', '📂') # 아이콘 없으면 기본값
        
        if name:
            # get_or_create는 이미 있으면 가져오고, 없으면 만듭니다 (중복 방지)
            category, created = TransactionCategory.objects.get_or_create(
                name=name, 
                defaults={'icon': icon}
            )
            if created:
                return JsonResponse({'status': 'success', 'message': '새 분류가 등록되었습니다.'})
            else:
                return JsonResponse({'status': 'error', 'message': '이미 존재하는 분류입니다.'})
    return JsonResponse({'status': 'error', 'message': '잘못된 요청입니다.'})

# 1. 분류 수정
def update_category(request):
    if request.method == 'POST':
        old_name = request.POST.get('old_name')
        new_name = request.POST.get('new_name')
        try:
            category = TransactionCategory.objects.get(name=old_name)
            category.name = new_name
            category.save()
            return JsonResponse({'status': 'success'})
        except TransactionCategory.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '존재하지 않는 분류입니다.'})

# 2. 분류 삭제
def delete_category(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        try:
            category = TransactionCategory.objects.get(name=name)
            category.delete()
            return JsonResponse({'status': 'success'})
        except TransactionCategory.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '이미 삭제된 분류입니다.'})
        
        # ----------------------의뢰번호 매칭하기------------STR
@csrf_exempt
def fetch_panel5_data(request):
    try:
        # 1. 파라미터 수집 (GET 방식)
        search_type = request.GET.get('type', 'all').strip()
        search_text = request.GET.get('text', '').strip()

        # 2. MySQL 조회: QT번호가 비어있는(NULL, 빈 문자열, 혹은 '-') 데이터만 전체 추출
        # r.QT번호가 비어있다는 조건을 최우선으로 적용합니다.
        where_clauses = ["(r.QT번호 IS NULL OR r.QT번호 = '' OR r.QT번호 = '-')"]
        params = []

        # 검색어가 있을 경우 추가 필터링
        if search_text:
            if search_type == 'request_code':
                where_clauses.append("UPPER(r.의뢰번호) LIKE %s")
            elif search_type == 'qt_no': # QT번호 검색은 사실상 결과가 없겠지만 구조상 유지
                where_clauses.append("UPPER(r.QT번호) LIKE %s")
            elif search_type == 'project':
                where_clauses.append("UPPER(r.사업명) LIKE %s")
            elif search_type == 'agency':
                where_clauses.append("UPPER(r.의뢰기관명) LIKE %s")
            params.append(f"%{search_text.upper()}%")

        where_sentence = "WHERE " + " AND ".join(where_clauses)
        
        # LIMIT을 제거하여 조건에 맞는 모든 데이터(2,000건 이상)를 가져옵니다.
        mysql_query = f"""
            SELECT r.의뢰번호, r.사업명, r.의뢰기관명
            FROM csi_receipts r
            {where_sentence}
            ORDER BY r.의뢰번호 DESC
        """

        with connections['default'].cursor() as cursor:
            cursor.execute(mysql_query, params)
            columns = [col[0] for col in cursor.description]
            mysql_rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        # 3. MSSQL 데이터 매칭 (chunk_size를 이용해 모든 의뢰번호 처리)
        req_codes = [str(row['의뢰번호']).strip() for row in mysql_rows if row.get('의뢰번호')]
        mssql_dict = {}
        
        if req_codes:
            chunk_size = 500
            with connections['mssql'].cursor() as mssql_cursor:
                for i in range(0, len(req_codes), chunk_size):
                    chunk = req_codes[i : i + chunk_size]
                    
                    # RQ로 시작하는 그룹과 그 외 그룹 분리 로직
                    curr_rq = [c for c in chunk if c.upper().startswith('RQ')]
                    curr_etc = [c for c in chunk if not c.upper().startswith('RQ')]
                    
                    ms_where = []
                    ms_params = []
                    
                    if curr_rq:
                        placeholders = ', '.join(['%s'] * len(curr_rq))
                        ms_where.append(f"a.request_code IN ({placeholders})")
                        ms_params.extend(curr_rq)
                    if curr_etc:
                        placeholders = ', '.join(['%s'] * len(curr_etc))
                        ms_where.append(f"a.receipt_code IN ({placeholders})")
                        ms_params.extend(curr_etc)
                    
                    if not ms_where:
                        continue
                        
                    where_sentence_ms = " OR ".join(ms_where)
                    mssql_query = f"""
                        SELECT a.request_code, a.receipt_code
                        FROM dbo.Receipt a
                        WHERE {where_sentence_ms}
                    """
                    
                    mssql_cursor.execute(mssql_query, ms_params)
                    m_cols = [col[0] for col in mssql_cursor.description]
                    
                    for m_row in mssql_cursor.fetchall():
                        m_item = dict(zip(m_cols, m_row))
                        r_code = str(m_item.get('request_code', '')).strip()
                        qt_code = str(m_item.get('receipt_code', '')).strip()
                        
                        # 매칭 딕셔너리에 저장
                        if r_code: mssql_dict[r_code] = qt_code
                        if qt_code: mssql_dict[qt_code] = qt_code

        # 4. 최종 데이터 조립
        final_results = []
        for row in mysql_rows:
            req_no = str(row.get('의뢰번호', '')).strip()
            # MSSQL 매칭 값이 있으면 가져오고 없으면 '-'
            qt_val = mssql_dict.get(req_no, "-")

            final_results.append({
                "request_code": req_no,
                "qt_no": qt_val,
                "project_name": row.get('사업명', ''),
                "agency_name": row.get('의뢰기관명', '')
            })

        return JsonResponse({'success': True, 'data': final_results})

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return JsonResponse({'success': False, 'message': str(e)})
    
    
    # ---------------------저장하기--------
    
@csrf_exempt
def save_panel5_data(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': '잘못된 요청 방식입니다.'})

    try:
        import json
        data = json.loads(request.body)
        items = data.get('items', [])

        if not items:
            return JsonResponse({'success': False, 'message': '저장할 데이터가 없습니다.'})

        success_count = 0
        with connections['default'].cursor() as cursor:
            for item in items:
                req_code = item.get('request_code')
                qt_no = str(item.get('qt_no', '')).strip()

                # 📌 [검증 1] QT번호가 '-', 'None', 혹은 빈값이면 저장하지 않고 건너뜀
                if qt_no in ['-', '', 'None', 'NULL']:
                    continue

                # 📌 [검증 2] 정상적인 데이터만 업데이트
                sql = "UPDATE csi_receipts SET QT번호 = %s WHERE 의뢰번호 = %s"
                cursor.execute(sql, [qt_no, req_code])
                success_count += 1

        return JsonResponse({
            'success': True, 
            'message': f'총 {success_count}건의 유효한 QT번호가 저장되었습니다.'
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return JsonResponse({'success': False, 'message': str(e)})
        
        # ----------------------의뢰번호 매칭하기------------END

        # ----------------------여기서부터 적정성 평가------------str
# board/views.py 맨 아래에 추가
def csi_evaluation_view(request):
    return render(request, 'csi_evaluation.html')

# ----------------------위 코드 지우면 안됨------------

def get_table_data_with_retry(driver, retries=3):
    for i in range(retries):
        try:
            # 아코디언이 이미 열려 있다고 가정하고 데이터 탐색
            script = """
            var parent = document.querySelector("div[id^='collapse_3_']");
            var parent_ajax = document.querySelector("#rqstViewAjax");
            if (!parent) return ["추출 실패"] * 5; // 영역이 없으면 실패 반환

            var leader = parent.querySelector("div.table-scrollable table > tbody > tr:nth-child(1) > td:nth-child(4)");
            var tester = parent.querySelector("div.table-scrollable table > tbody > tr:nth-child(1) > td:nth-child(5)");
            var item = parent.querySelector("div.table-scrollable table > tbody > tr:nth-child(1) > td.t-left.font-bold");
            var method = parent.querySelector("div.table-scrollable table > tbody > tr:nth-child(2) > td");
            var date = parent_ajax ? parent_ajax.querySelector("table > tbody > tr:nth-child(6) > td:nth-child(2)") : null;
            
            return [
                leader ? leader.innerText : "추출 실패",
                tester ? tester.innerText : "추출 실패",
                item ? item.innerText : "추출 실패",
                method ? method.innerText : "추출 실패",
                date ? date.innerText : "추출 실패"
            ];
            """
            results = driver.execute_script(script)
            # 결과가 모두 "추출 실패"라면 다시 시도
            if all(r == "추출 실패" for r in results): raise Exception("데이터 없음")
            return results
        except:
            if i == retries - 1: return ["추출 실패"] * 5
            time.sleep(2) # 재시도 시 대기 시간 증가


@csrf_exempt
def fetch_csi_released_ledger_data(request):
    """
    [CSI 발급대장 수집 - 최종 마스터 완결판]
    colspan 및 rowspan 요소를 완벽하게 정제하여 매핑 오류를 100% 원천 차단합니다.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': '잘못된 접근입니다.'})

    driver = None
    try:
        data = json.loads(request.body)
        start_date = data.get('start_date')
        end_date = data.get('end_date')

        if not start_date or not end_date:
            return JsonResponse({'status': 'error', 'message': '시작일과 종료일이 누락되었습니다.'})

        clean_start = start_date.replace("-", "")
        clean_end = end_date.replace("-", "")

        chrome_options = Options()
        chrome_options.add_argument("--window-size=1920,1080")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
        wait = WebDriverWait(driver, 15)

        # 1. 로그인
        driver.get("https://gcloud.csi.go.kr/cmq/main.do")
        wait.until(EC.element_to_be_clickable((By.ID, "userId"))).send_keys("youngjun")
        driver.find_element(By.ID, "pswd").send_keys("k*1800*92*")
        driver.find_element(By.CLASS_NAME, "login-btn").click()
        time.sleep(2)

        # 2. 메뉴 이동 및 검색 설정
        driver.get("https://gcloud.csi.go.kr/cmq/qti/qltAgntQltSttus/qltAgntQltSttusList.do")
        wait.until(EC.presence_of_element_located((By.NAME, "ymdKey")))
        
        driver.execute_script("""
            var select = document.querySelector('select[name="ymdKey"]');
            if (select) {
                for (var i = 0; i < select.options.length; i++) {
                    if (select.options[i].text.indexOf('발급일자') !== -1) {
                        select.selectedIndex = i;
                        select.dispatchEvent(new Event('change')); 
                        break;
                    }
                }
            }
        """)
        time.sleep(1.5)

        start_input = driver.find_element(By.ID, "startYmd")
        start_input.clear()
        start_input.send_keys(clean_start)
        start_input.send_keys(Keys.ENTER)

        end_input = driver.find_element(By.ID, "endYmd")
        end_input.clear()
        end_input.send_keys(clean_end)
        end_input.send_keys(Keys.ENTER)
        
        driver.execute_script("go_search();")
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "pagination")))
        time.sleep(2)

        # 3. 데이터 수집 루프
        final_results = []
        current_page_idx = 1 

        while True:
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "goSelectLink")))
            time.sleep(2) 
            
            first_cert_before = driver.find_elements(By.CLASS_NAME, "goSelectLink")[0].text.strip()
            rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")

            for i in range(len(rows)):
                current_rows = driver.find_elements(By.CSS_SELECTOR, "table.table-striped tbody tr")
                if i >= len(current_rows): break
                row = current_rows[i]
                
                list_info = {
                    'cert_no': '추출 실패', 'seal_name': '추출 실패', 'project_name': '추출 실패',
                    'agency': '추출 실패', 'req_date': '추출 실패', 'recv_date': '추출 실패',
                    'wait_date': '추출 실패', 'issue_date': '추출 실패'
                }
                
                try:
                    list_info['cert_no'] = row.find_element(By.XPATH, "./td[2]").text.strip()
                    list_info['seal_name'] = row.find_element(By.XPATH, "./td[3]").text.strip()
                    list_info['project_name'] = row.find_element(By.XPATH, "./td[4]").text.strip()
                    list_info['agency'] = row.find_element(By.XPATH, "./td[5]").text.strip()
                    list_info['req_date'] = row.find_element(By.XPATH, "./td[6]").text.strip()
                    list_info['recv_date'] = row.find_element(By.XPATH, "./td[7]").text.strip()
                    list_info['wait_date'] = row.find_element(By.XPATH, "./td[8]").text.strip()
                    list_info['issue_date'] = row.find_element(By.XPATH, "./td[9]").text.strip()
                    
                    target_link = row.find_element(By.XPATH, "./td[2]//a")
                except Exception:
                    continue 

                rq_no = "추출 실패"
                receipt_no = "추출 실패"
                technical_leader = "추출 실패"
                tester = "추출 실패"
                
                try:
                    # 1. 상세 페이지 이동 후 주소가 바뀌었는지 확인 (안전장치)
                    driver.execute_script("arguments[0].click();", target_link)
                    
                    # 상세 페이지 진입 대기 (id=rqstViewAjax가 나타날 때까지)
                    wait.until(EC.presence_of_element_located((By.ID, "rqstViewAjax")))
                    time.sleep(1.5) # 페이지 렌더링을 위해 충분히 대기

                    # 1) 의뢰번호/접수번호 수집 (알려주신 CSS Selector 사용)
                    script_ids = """
                    var container = document.querySelector("#rqstViewAjax");
                    var req_td = container.querySelector("div.table-scrollable table > tbody > tr:nth-child(1) > td:nth-child(2)");
                    var rec_td = container.querySelector("div.table-scrollable table > tbody > tr:nth-child(1) > td:nth-child(4)");
                    return [
                        req_td ? req_td.innerText : "추출 실패",
                        rec_td ? rec_td.innerText : "추출 실패"
                    ];
                    """
                    ids = driver.execute_script(script_ids)
                    rq_no = ids[0].strip()
                    receipt_no = ids[1].strip()

                    # 2) 성적서 내역 데이터 수집 (기존 재시도 함수 사용)
                    expand_btn_2 = WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '품질검사 성적서 내역')]")))
                    driver.execute_script("arguments[0].click();", expand_btn_2)
                    time.sleep(1.5)

                    results = get_table_data_with_retry(driver)
                    
                    technical_leader = results[0].split("\n")[0].strip()
                    tester = results[1].split("\n")[0].replace(" ", "").strip()
                    test_item = results[2].strip()
                    test_standard = results[3].strip()
                    receive_date = results[4].strip()

                except Exception as e:
                    print(f"상세 페이지 파싱 오류: {e}")
                    # 실패 시 초기값 유지
                    technical_leader, tester, test_item, test_standard, receive_date = ["추출 실패"] * 5

                # 3) 결과 저장
                final_results.append({
                    'req_no': rq_no,
                    'cert_no': list_info['cert_no'],
                    'seal_name': list_info['seal_name'],
                    'project_name': list_info['project_name'],
                    'agency': list_info['agency'],
                    'req_date': list_info['req_date'],
                    'recv_date': list_info['recv_date'],
                    'wait_date': list_info['wait_date'],
                    'issue_date': list_info['issue_date'],
                    'receipt_no': receipt_no,
                    'tester': tester,
                    'technical_leader': technical_leader,
                    'test_item': test_item,
                    'test_standard': test_standard,
                    'receive_date': receive_date,
                    'remark_management_no': ''
                })

                driver.execute_script("window.history.back();")
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "goSelectLink")))
                time.sleep(1.5)

            # 페이징 처리
            try:
                next_page_num = current_page_idx + 1
                btn_xpath = f"//ul[contains(@class,'pagination')]//a[text()='{next_page_num}']"
                next_btns = driver.find_elements(By.XPATH, btn_xpath)
                
                if next_btns:
                    driver.execute_script("arguments[0].click();", next_btns[0])
                else:
                    driver.execute_script(f"goPage({next_page_num});")
                
                is_changed = False
                for _ in range(15):
                    time.sleep(1)
                    current_links = driver.find_elements(By.CLASS_NAME, "goSelectLink")
                    if current_links and current_links[0].text.strip() != first_cert_before:
                        is_changed = True
                        current_page_idx = next_page_num
                        break
                if not is_changed: break
            except: break

        driver.quit()
        return JsonResponse({'status': 'success', 'results': final_results})

    except Exception as e:
        if driver: driver.quit()
        return JsonResponse({'status': 'error', 'message': str(e)})
    

 # ----------------------QT번호매칭-----------
@csrf_exempt
def match_management_no(request):
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid method'})

    try:
        data = json.loads(request.body)
        items = data.get('items', [])
        
        # 1. 그리드에서 의뢰번호 리스트 추출
        req_codes = [str(item.get('req_no', '')).strip() for item in items if item.get('req_no')]
        req_codes = list(set(req_codes)) # 중복 제거
        
        if not req_codes:
            return JsonResponse({'success': True, 'updated_rows': []})

        # 2. MSSQL에서 매칭 데이터 조회 (CHUNK 처리)
        mssql_dict = {}
        chunk_size = 500
        with connections['mssql'].cursor() as cursor:
            for i in range(0, len(req_codes), chunk_size):
                chunk = req_codes[i : i + chunk_size]
                placeholders = ', '.join(['%s'] * len(chunk))
                
                # RQ로 시작하면 request_code, 아니면 receipt_code로 매칭
                ms_where = f"(request_code IN ({placeholders}) OR receipt_code IN ({placeholders}))"
                cursor.execute(f"SELECT request_code, receipt_code FROM dbo.Receipt WHERE {ms_where}", chunk + chunk)
                
                for row in cursor.fetchall():
                    m_item = dict(zip(['r_code', 'qt_code'], row))
                    # 매칭된 값을 저장
                    if m_item['r_code']: mssql_dict[m_item['r_code']] = m_item['qt_code']
                    if m_item['qt_code']: mssql_dict[m_item['qt_code']] = m_item['qt_code']

        # 3. 결과 조립 (프론트엔드 그리드 행 업데이트용)
        updated_rows = []
        for item in items:
            req_no = str(item.get('req_no', '')).strip()
            if req_no in mssql_dict:
                item['remark_management_no'] = mssql_dict[req_no] # 비고(관리번호) 필드 업데이트
                updated_rows.append(item)

        return JsonResponse({'success': True, 'updated_rows': updated_rows})

    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)})


# --------발급대장 MYSQL에 저장하기-------------------------

@csrf_exempt
def save_all_to_mysql(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            all_rows = data.get('items', [])

            if not all_rows:
                return JsonResponse({'status': 'error', 'message': '저장할 데이터가 없습니다.'})

            with connections['default'].cursor() as cursor:
                # 13개 컬럼을 모두 포함하는 쿼리
                sql = """
                    INSERT INTO csi_receipts_new (
                        receipt_no, cert_no, issue_date, seal_name, agency, 
                        project_name, tester, technical_leader, remark_management_no, 
                        req_no, test_item, test_standard, receive_date
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        cert_no = VALUES(cert_no),
                        issue_date = VALUES(issue_date),
                        seal_name = VALUES(seal_name),
                        agency = VALUES(agency),
                        project_name = VALUES(project_name),
                        tester = VALUES(tester),
                        technical_leader = VALUES(technical_leader),
                        remark_management_no = VALUES(remark_management_no),
                        test_item = VALUES(test_item),
                        test_standard = VALUES(test_standard),
                        receive_date = VALUES(receive_date)
                """
                
                # 프론트엔드에서 넘어오는 필드명을 그대로 get()으로 매핑
                params = [
                    (
                        row.get('receipt_no'), row.get('cert_no'), row.get('issue_date'), 
                        row.get('seal_name'), row.get('agency'), row.get('project_name'), 
                        row.get('tester'), row.get('technical_leader'), row.get('remark_management_no'), 
                        row.get('req_no'), row.get('test_item'), row.get('test_standard'), row.get('receive_date')
                    )
                    for row in all_rows
                ]
                
                cursor.executemany(sql, params)

            return JsonResponse({'status': 'success', 'message': f'{len(all_rows)}건 저장/갱신 완료'})

        except Exception as e:
            return JsonResponse({'status': 'error', 'message': str(e)})

        # ----------------------여기까지 적정성 평가------------end
