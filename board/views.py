import json
import time
from django.http import JsonResponse
from django.shortcuts import render
from django.db import connection, connections
from django.views.decorators.csrf import csrf_exempt
from selenium.webdriver.common.action_chains import ActionChains
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
from .models import OuterreceiptNew, CsiReceipt
from datetime import datetime
import calendar  # 날짜 계산용
import traceback # 에러 상세 출력용 (이번 에러 해결 핵심)
from selenium.common.exceptions import UnexpectedAlertPresentException, NoAlertPresentException, TimeoutException


# --- [1] 기본 게시판 및 페이지 렌더링 ---

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
                        시료량, 구분, 현장담당자, 배정일자, 배정현황
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        배정일자 = VALUES(배정일자),
                        배정현황 = VALUES(배정현황)
                """
                
                # 3. 데이터 매핑 (KeyError 방지를 위해 .get() 사용)
                params = [
                    (
                        d.get('u_id'), d.get('receipt_id'), d.get('receipt_date'), 
                        d.get('status'), d.get('project'), d.get('client'),
                        d.get('sampler'), d.get('seal'), d.get('processor'), 
                        d.get('sales_type'), d.get('manager'), d.get('check_col'),
                        d.get('amount'), d.get('type_col'), d.get('manager_name'), 
                        d.get('assign_date'), d.get('assignment_history')
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
                    expand_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '품질시험 의뢰서 내역')]")))
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
            
            # 현재 페이지의 첫 번째 성적서 번호를 기억 (페이지 전환 확인용)
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
                    expand_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '품질시험 의뢰서 내역')]")))
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

# 여기서부터 테스트용 입니다(발급건수 카운터용)
# @csrf_exempt
# def fetch_combined_data(request):
#     try:
#         # 1. 파라미터 수집
#         if request.method == 'POST' and request.body:
#             import json
#             data = json.loads(request.body)
#             start_date = data.get('start', '').strip()
#             end_date = data.get('end', '').strip()
#             team_filter = data.get('team', '전체').strip()
#             search_query = data.get('text', '').strip()
#             raw_type = data.get('type', '').strip()
#         else:
#             start_date = request.GET.get('start', '').strip()
#             end_date = request.GET.get('end', '').strip()
#             team_filter = request.GET.get('team', '전체').strip()
#             search_query = request.GET.get('text', '').strip()
#             raw_type = request.GET.get('type', '').strip()

#         # 2. 타입 변환
#         search_type = '사업명'
#         if raw_type == 'client': search_type = '의뢰기관명'
#         elif raw_type == 'project': search_type = '사업명'
#         elif raw_type == 'req_code': search_type = '의뢰번호'
        
#         # 대소문자 구분 적용코드
#         where_clauses = []
#         params = []
#         if start_date and end_date:           
#             where_clauses.append("DATE(r.배정일자) BETWEEN %s AND %s")    
#             # 파라미터는 시간 없이 날짜만 전달합니다.
#             params.extend([start_date, end_date])
        
#         # 팀 필터도 대소문자 무시 적용
#         if team_filter and team_filter != '전체':
#             where_clauses.append("UPPER(r.담당자) LIKE %s")
#             params.append(f"%{team_filter.upper()}%")
            
#         if search_query:
#             q = f"%{search_query.upper()}%"
#             if search_type == '의뢰번호':
#                 where_clauses.append("UPPER(r.의뢰번호) LIKE %s")
#             elif search_type == '의뢰기관명':
#                 where_clauses.append("UPPER(r.의뢰기관명) LIKE %s")
#             else:
#                 where_clauses.append("UPPER(r.사업명) LIKE %s")
#             params.append(q)


#         where_sentence = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
#         mysql_query = f"""
#             SELECT r.*, i.성적서번호, i.발급일자, r.미인정 
#             FROM csi_receipts r 
#             LEFT JOIN csi_issue_results i ON r.의뢰번호 = i.의뢰번호 
#             {where_sentence}
#             ORDER BY r.담당자 ASC LIMIT 5000
#         """

#         with connections['default'].cursor() as mysql_cursor:
#             mysql_cursor.execute(mysql_query, params)
#             columns = [col[0] for col in mysql_cursor.description]
#             mysql_rows = [dict(zip(columns, row)) for row in mysql_cursor.fetchall()]

#         # 4. MSSQL 데이터 매칭
#         req_codes = [str(row['의뢰번호']).strip() for row in mysql_rows if row.get('의뢰번호')]
#         mssql_dict = {}
#         if req_codes:
#             chunk_size = 1000
#             with connections['mssql'].cursor() as mssql_cursor:
#                 for i in range(0, len(req_codes), chunk_size):
#                     chunk = req_codes[i : i + chunk_size]
#                     placeholders = ', '.join(['%s'] * len(chunk))
#                     mssql_query = f"""
#                         SELECT a.sales, a.request_code, a.receipt_csi_code, a.receipt_code, b.completion_day, a.save_date, 
#                         b.builder, b.construction, c.specimen, d.supply_value, d.vat, d.rate,
#                         e.deposit_day, e.deposit, f.issue_date, f.company
#                         FROM dbo.Receipt a
#                         LEFT JOIN dbo.Customer b ON a.receipt_code = b.receipt_code
#                         LEFT JOIN dbo.Specimen_info c ON a.receipt_code = c.receipt_code
#                         LEFT JOIN dbo.Estimate d ON a.receipt_code = d.receipt_code
#                         LEFT JOIN dbo.Deposit e ON a.receipt_code = e.receipt_code
#                         LEFT JOIN dbo.Tax_Manager f ON a.receipt_code = f.receipt_code
#                         WHERE a.request_code IN ({placeholders})
#                     """
#                     mssql_cursor.execute(mssql_query, chunk)
#                     m_cols = [col[0] for col in mssql_cursor.description]
#                     for m_row in mssql_cursor.fetchall():
#                         m_item = dict(zip(m_cols, m_row))
#                         mssql_dict[str(m_item['request_code']).strip()] = m_item

#         # 5. 최종 데이터 합체 및 통계 집계
#         final_results = []
#         stats = {}  # stats로 변수명 통일
#         teams = ['1팀', '2팀', '3팀', '4팀', '5팀', '6팀']

#         for row in mysql_rows:
#             req_no = str(row.get('의뢰번호', '')).strip()
#             ms_info = mssql_dict.get(req_no, {})
            
#             # 발급일자 확인 (날짜 형식이 포함되어 있는지)
#             issue_date = str(row.get('발급일자', '')).strip()
#             is_issued = 1 if issue_date and issue_date not in ['None', '', '-', '0000-00-00'] else 0

#             # 합체 데이터 생성
#             res_item = {
#                 "담당자": row.get('담당자', ''),
#                 "영업구분": row.get('영업구분', ''),
#                 "의뢰번호": req_no,
#                 "접수일시": str(row.get('접수일시', '')),
#                 "접수번호": ms_info.get('receipt_csi_code', '-'),
#                 "QT번호": req_no if req_no.startswith('QT-') else ms_info.get('receipt_code', '-'),
#                 "성적서번호": row.get('성적서번호', '-'),
#                 # "발급일자": issue_date,
#                 "발급일자": str(row.get('발급일자')) if row.get('발급일자') else "",
#                 "의뢰기관명": row.get('의뢰기관명', ''),
#                 "사업명": ms_info.get('construction', row.get('사업명', '')),
#                 "공급가액": ms_info.get('supply_value', 0),
#                 "봉인명": ms_info.get('specimen', '-'),
#                 "준공예정일": str(ms_info.get('completion_day')) if ms_info.get('completion_day') else "",
#                 "실접수일": str(ms_info.get('save_date')) if ms_info.get('save_date') else "",
#                 "공급가액": ms_info.get('supply_value', 0),
#                 "부가세": ms_info.get('vat', 0),
#                 "할인율": ms_info.get('rate', 0),
#                 "입금일": ms_info.get('deposit_day', 0),
#                 "입금액": ms_info.get('deposit', 0),
#                 "계산서발행일": str(ms_info.get('issue_date')),
#                 "계산서발행회사명": ms_info.get('company', '-'),
#                 "미인정": row.get('미인정', '')   
#             }
#             final_results.append(res_item)

#             # [집계 로직]
#             name = (res_item["영업구분"] or res_item["담당자"] or '').strip()
#             if not name: continue

#             # 팀 판별
#             target_team = "미분류"
#             for t in teams:
#                 if t in str(res_item["담당자"]):
#                     target_team = t
#                     break

#             # 인정/미인정 판별
#             type_key = "미인정건" if res_item["미인정"] else "인정건"

#             # stats 구조 초기화
#             if name not in stats:
#                 stats[name] = {t: {"인정건": {"금액": 0, "건수": 0, "발급": 0}, 
#                                   "미인정건": {"금액": 0, "건수": 0, "발급": 0}} for t in teams}

#             # 누적
#             if target_team in teams:
#                 try:
#                     price = int(float(str(res_item["공급가액"]).replace(',', '')))
#                 except:
#                     price = 0
#                 stats[name][target_team][type_key]["금액"] += price
#                 stats[name][target_team][type_key]["건수"] += 1
#                 stats[name][target_team][type_key]["발급"] += is_issued

#         return JsonResponse({'status': 'success', 'data': final_results, 'stats': stats})

#     except Exception as e:
#         import traceback
#         print(traceback.format_exc())
#         return JsonResponse({'status': 'error', 'message': str(e)})


# # 5. 페이지 호출 함수 (AttributeError 해결)
# def request_page(request):
#     return render(request, 'request.html') 

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
# ------------------------여기까지가 완료건 보기 관련 끝----------------------------

# def receipt_settle_admin(request):
#     start_date = request.GET.get('start_date')
    
#     if start_date:
#         date_type = request.GET.get('date_type')
#         end_date = request.GET.get('end_date')
#         search_type = request.GET.get('search_type')
#         search_text = request.GET.get('search_text', '').strip()

#         # --- 1. MySQL 조회 (csi_receipts) ---
#         where_clauses = []
#         mysql_params = []
#         if search_type == 'sales_man' and search_text:
#             where_clauses.append("담당자 LIKE %s")
#             mysql_params.append(f"%{search_text}%")

#         where_sentence = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
#         mysql_query = f"SELECT 의뢰번호, 담당자 FROM csi_receipts {where_sentence} ORDER BY 의뢰번호 DESC LIMIT 5000"

#         # 🌟 담당자 매핑을 위한 딕셔너리 생성
#         mysql_manager_map = {}
#         with connections['default'].cursor() as mysql_cursor:
#             mysql_cursor.execute(mysql_query, mysql_params)
#             for row in mysql_cursor.fetchall():
#                 # { 'RQ-2026-0001': '홍길동' } 형태로 저장
#                 mysql_manager_map[str(row[0]).strip()] = row[1]

#         # --- 2. MSSQL 데이터 매칭 및 상세 조회 ---
#         final_results = []
#         if mysql_manager_map:
#             req_codes = list(mysql_manager_map.keys())
#             chunk_size = 500
            
#             with connections['mssql'].cursor() as mssql_cursor:
#                 for i in range(0, len(req_codes), chunk_size):
#                     chunk = req_codes[i : i + chunk_size]
#                     placeholders = ', '.join(['%s'] * len(chunk))
                    
#                     mssql_query = f"""
#                         SELECT 
#                             c.request_code as 의뢰번호, c.sales as 영업담당, c.save_date as 실접수일,
#                             a.receipt_code as QT번호, a.builder as 의뢰기관명, a.construction as 사업명,
#                             a.cm_name as 의뢰인성명, a.cm_tel as 현장전화, a.get_name as 시료채취자, a.qm_name as 품질담당자,
#                             b.specimen as 봉인명, b.specimen_qty as 시료량,
#                             d.supply_value as 공급가액, d.vat as 부가세, (d.supply_value + d.vat) as 합계,
#                             d.basic as 기본료, d.process as 정보처리비, d.sample as 시편제작비,
#                             d.tran_set as 출장비구분, d.[tran] as 출장비,
#                             e.deposit_day as 입금일, e.deposit as 입금액,
#                             f.company as 계산서발행회사명, f.issue_date as 계산서발행일, f.manager as 계산서담당자,
#                             f.hp as 계산서hp, f.tel as 계산서tel, f.fax as 계산서fax, f.email as 계산서email, f.issue_employee as 계산서발행자,
#                             g.price as 청구위탁시험비,
#                             (SELECT SUM(ei_price) FROM dbo.Examination_Item 
#                             WHERE receipt_code = a.receipt_code AND item_name LIKE '%%지게차%%') as 지게차운임
#                         FROM dbo.Receipt c
#                         LEFT JOIN dbo.Customer a ON c.receipt_code = a.receipt_code
#                         LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
#                         LEFT JOIN dbo.Estimate d ON c.receipt_code = d.receipt_code
#                         LEFT JOIN dbo.Deposit e ON c.receipt_code = e.receipt_code
#                         LEFT JOIN dbo.Tax_Manager f ON c.receipt_code = f.receipt_code
#                         LEFT JOIN dbo.Consignment g ON c.receipt_code = g.receipt_code
#                         WHERE (
#                             (c.request_code LIKE 'RQ-%%' AND c.request_code IN ({placeholders}))
#                             OR 
#                             (c.request_code NOT LIKE 'RQ-%%' AND c.receipt_code IN ({placeholders}))
#                         )
#                     """
                    
#                     dynamic_where = ""
#                     mssql_params = chunk + chunk
                    
#                     if start_date and end_date:
#                         col = "e.deposit_day" if date_type == "deposit" else "c.save_date"
#                         dynamic_where += f" AND {col} BETWEEN %s AND %s"
#                         mssql_params.extend([start_date, end_date])
                    
#                     if search_text:
#                         mapping = {"qt_no": "a.receipt_code", "agency": "a.builder", "project": "a.construction", "seal": "b.specimen"}
#                         if search_type in mapping:
#                             dynamic_where += f" AND {mapping[search_type]} LIKE %s"
#                             mssql_params.append(f"%{search_text}%")

#                     mssql_cursor.execute(mssql_query + dynamic_where, mssql_params)
#                     columns = [col[0] for col in mssql_cursor.description]
                    
#                     for m_row in mssql_cursor.fetchall():
#                         row_dict = dict(zip(columns, m_row))
                        
#                         # 🌟 [해결 포인트] MSSQL 데이터 한 줄 마다 MySQL에서 찾은 담당자를 넣어줌
#                         req_no = str(row_dict.get('의뢰번호', '')).strip()
#                         row_dict['담당자'] = mysql_manager_map.get(req_no, '-')
                        
#                         final_results.append(row_dict)

#         return JsonResponse(final_results, safe=False)

#     return render(request, 'receipt_settle_admin.html', {'final_results': json.dumps([], ensure_ascii=False)})

def receipt_settle_admin(request):
    start_date = request.GET.get('start_date')
    
    if start_date:
        try:
            date_type = request.GET.get('date_type')
            end_date = request.GET.get('end_date')
            search_type = request.GET.get('search_type')
            search_text = request.GET.get('search_text', '').strip()

            # --- 1. MySQL 담당자 정보 조회 ---
            mysql_manager_map = {}
            with connections['default'].cursor() as mysql_cursor:
                # search_type이 'sales_man'일 경우 필터링 적용
                where_sql = "WHERE 담당자 LIKE %s" if search_type == 'sales_man' and search_text else ""
                param = [f"%{search_text}%"] if where_sql else []
                
                mysql_query = f"SELECT 의뢰번호, 담당자, 미인정 FROM csi_receipts {where_sql} ORDER BY 의뢰번호 DESC LIMIT 5000"
                mysql_cursor.execute(mysql_query, param)
                for row in mysql_cursor.fetchall():
                    mysql_manager_map[str(row[0]).strip()] = {
                        '담당자': row[1],
                        '미인정': row[2]  # 보통 0/1 혹은 Y/N으로 저장되어 있을 것입니다.
                    }

            # --- 2. MSSQL 데이터 조회 ---
            final_results = []
            if mysql_manager_map:
                req_codes = list(mysql_manager_map.keys())
                chunk_size = 500
                
                with connections['mssql'].cursor() as mssql_cursor:
                    for i in range(0, len(req_codes), chunk_size):
                        chunk = req_codes[i : i + chunk_size]
                        placeholders = ', '.join(['%s'] * len(chunk))
                        
                        # MSSQL 메인 쿼리
                        mssql_query = f"""
                        SELECT 
                            c.request_code as 의뢰번호, c.sales as 영업담당, c.save_date as 실접수일,
                            a.receipt_code as QT번호, a.builder as 의뢰기관명, a.construction as 사업명,
                            a.cm_name as 의뢰인성명, a.cm_tel as 현장전화, a.get_name as 시료채취자, a.qm_name as 품질담당자,
                            b.specimen as 봉인명, b.specimen_qty as 시료량,
                            d.supply_value as 공급가액, d.vat as 부가세, (d.supply_value + d.vat) as 합계,
                            d.basic as 기본료, d.process as 정보처리비, d.sample as 시편제작비,
                            d.tran_set as 출장비구분, d.[tran] as 출장비,
                            e.deposit_day as 입금일, e.deposit as 입금액,
                            f.company as 계산서발행회사명, f.issue_date as 계산서발행일, f.manager as 계산서담당자,
                            f.hp as 계산서hp, f.tel as 계산서tel, f.fax as 계산서fax, f.email as 계산서email, f.issue_employee as 계산서발행자,
                            g.price as 청구위탁시험비,
                            (SELECT SUM(ei_price) FROM dbo.Examination_Item 
                            WHERE receipt_code = a.receipt_code AND item_name LIKE '%%지게차%%') as 지게차운임
                        FROM dbo.Receipt c
                        LEFT JOIN dbo.Customer a ON c.receipt_code = a.receipt_code
                        LEFT JOIN dbo.Specimen_info b ON c.receipt_code = b.receipt_code
                        LEFT JOIN dbo.Estimate d ON c.receipt_code = d.receipt_code
                        LEFT JOIN dbo.Deposit e ON c.receipt_code = e.receipt_code
                        LEFT JOIN dbo.Tax_Manager f ON c.receipt_code = f.receipt_code
                        LEFT JOIN dbo.Consignment g ON c.receipt_code = g.receipt_code
                        WHERE (
                            (c.request_code LIKE 'RQ-%%' AND c.request_code IN ({placeholders}))
                            OR 
                            (c.request_code NOT LIKE 'RQ-%%' AND c.receipt_code IN ({placeholders}))
                        )
                    """
                        
                        # 동적 조건 추가
                        dynamic_where = ""
                        mssql_params = chunk + chunk # placeholders가 2개이므로 2번 넣음
                        
                        # if start_date and end_date:
                        #     col = "c.save_date" if date_type == "receipt" else "c.save_date" # 필요시 d.deposit_day 등으로 수정
                        #     dynamic_where += f" AND {col} BETWEEN %s AND %s"
                        #     mssql_params.extend([start_date, end_date])

                        if start_date and end_date:
                            if date_type == "receipt":
                                col = "c.save_date"
                                # 실접수일은 하이픈 형식이므로 그대로 사용
                                query_start = start_date 
                                query_end = end_date
                            elif date_type == "deposit":
                                col = "e.deposit_day"
                                # 🌟 입금일은 하이픈을 제거하여 '20261216' 형태로 변환
                                query_start = start_date.replace('-', '')
                                query_end = end_date.replace('-', '')
                            else:
                                col = "c.save_date"
                                query_start = start_date
                                query_end = end_date
                                
                            dynamic_where += f" AND {col} BETWEEN %s AND %s"
                            mssql_params.extend([query_start, query_end]) # 변환된 날짜 파라미터 삽입
                        
                        mssql_cursor.execute(mssql_query + dynamic_where, mssql_params)
                        columns = [col[0] for col in mssql_cursor.description]
                        current_chunk_rows = [dict(zip(columns, row)) for row in mssql_cursor.fetchall()]

                        # 🌟 3. MSSQL 결과의 QT번호로 MySQL 'winapps_현장팀' 조회 (지게차 방식)
                        qt_list = [str(r.get('QT번호')).strip() for r in current_chunk_rows if r.get('QT번호')]
                        field_team_map = {}

                        if qt_list:
                            with connections['default'].cursor() as my_cursor:
                                f_placeholders = ', '.join(['%s'] * len(qt_list))
                                f_sql = f"""
                                    SELECT 접수번호, 현장담당, 시료명, 공수, (출장비 + 추가) as 지급액합계 
                                    FROM winapps_현장팀 
                                    WHERE 접수번호 IN ({f_placeholders})
                                """
                                my_cursor.execute(f_sql, qt_list)
                                f_cols = [c[0] for c in my_cursor.description]
                                for f_row in my_cursor.fetchall():
                                    f_dict = dict(zip(f_cols, f_row))
                                    field_team_map[str(f_dict['접수번호']).strip()] = f_dict

                        # 🌟 4. 최종 데이터 병합
                        for row_dict in current_chunk_rows:
                            req_no = str(row_dict.get('의뢰번호', '')).strip()
                            qt_no = str(row_dict.get('QT번호', '')).strip()

                            # 🌟 수정된 맵 구조에서 담당자 정보를 가져옴
                            manager_info = mysql_manager_map.get(req_no, {'담당자': '-', '미인정': '-'})
                            row_dict['담당자'] = manager_info.get('담당자', '-')
                            row_dict['미인정'] = manager_info.get('미인정', '-') # 🌟 미인정 데이터 추가
                            
                            f_info = field_team_map.get(qt_no, {})
                            row_dict['현장담당'] = f_info.get('현장담당', '-')
                            row_dict['시료명'] = f_info.get('시료명', '-')
                            row_dict['공수'] = f_info.get('공수', 0)
                            row_dict['지급액합계'] = f_info.get('지급액합계', 0)

                            final_results.append(row_dict)

            return JsonResponse(final_results, safe=False)
        
        except Exception as e:
            # 에러 발생 시 HTML이 아닌 JSON으로 에러 메시지 반환 (중요!)
            return JsonResponse({"error": str(e)}, status=500)

    return render(request, 'receipt_settle_admin.html', {'final_results': json.dumps([], ensure_ascii=False)})

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
@csrf_exempt
def save_panel4_data(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            new_items = data.get('new_items', [])
            updated_items = data.get('updated_items', [])
            
            with connection.cursor() as cursor:
                # A. 신규 데이터: QT번호 중복 체크 후 INSERT
                for item in new_items:
                    cursor.execute("SELECT 1 FROM settlement_amount WHERE QT번호 = %s", [item['receipt_code']])
                    if cursor.fetchone(): 
                        continue # 이미 있으면 통과
                    
                    cursor.execute(
                        "INSERT INTO settlement_amount (QT번호, 금액) VALUES (%s, %s)", 
                        [item['receipt_code'], item['applied_amount']]
                    )
                
                # B. 기존 데이터: ID 기준으로 UPDATE
                for item in updated_items:
                    cursor.execute(
                        "UPDATE settlement_amount SET QT번호 = %s, 금액 = %s WHERE ID = %s",
                        [item['receipt_code'], item['applied_amount'], item['id']]
                    )
            
            return JsonResponse({"success": True, "message": "저장 완료"})
        except Exception as e:
            return JsonResponse({"success": False, "message": str(e)})     


# ----------------------------------4번 분할화면 여기까지-----END