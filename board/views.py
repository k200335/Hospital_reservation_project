import json
import time
from django.http import JsonResponse
from django.shortcuts import render
from django.db import connection, connections
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
from .models import OuterreceiptNew, CsiReceipt
from datetime import datetime
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
        chrome_options.add_argument("--headless") # 필요시 주석 처리 (창 보기)
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
                        의뢰번호 = VALUES(의뢰번호),
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
        
# 여기서 부터 QT 통합
# @csrf_exempt
# def fetch_combined_data(request):
#     try:
#         # 1. 파라미터 수집 (team 파라미터 추가)
#         if request.method == 'POST' and request.body:
#             try:
#                 import json
#                 data = json.loads(request.body)
#                 start_date = data.get('start', '').strip()
#                 end_date = data.get('end', '').strip()
#                 team_filter = data.get('team', '전체').strip() # [추가] 팀 정보
#                 search_query = data.get('text', '').strip()
#                 raw_type = data.get('type', '').strip()
#             except Exception:
#                 start_date = end_date = team_filter = search_query = raw_type = ""
#         else:
#             start_date = request.GET.get('start', '').strip()
#             end_date = request.GET.get('end', '').strip()
#             team_filter = request.GET.get('team', '전체').strip() # [추가] 팀 정보
#             search_query = request.GET.get('text', '').strip()
#             raw_type = request.GET.get('type', '').strip()

#         # 2. 타입 변환
#         search_type = '사업명'
#         if raw_type == 'client':
#             search_type = '의뢰기관명'
#         elif raw_type == 'project':
#             search_type = '사업명'
#         elif raw_type == 'req_code': 
#             search_type = '의뢰번호'

#         # 디버깅 출력 (팀 정보 포함)
#         print(f"DEBUG: 시작일={start_date}, 종료일={end_date}, 팀={team_filter}, 검색어={search_query}, 타입={search_type}")

#         # 3. MySQL: 조건부 쿼리 생성
#         where_clauses = []
#         params = []

#         # [날짜 조건]
#         if start_date and end_date:
#             where_clauses.append("r.배정일자 BETWEEN %s AND %s")
#             params.extend([f"{start_date} 00:00:00", f"{end_date} 23:59:59"])

#         # [팀별 필터 조건 추가] 
#         # team_filter가 '전체'가 아닐 경우에만 담당자 컬럼에서 해당 팀을 검색합니다.
#         if team_filter and team_filter != '전체':
#             where_clauses.append("r.담당자 LIKE %s")
#             params.append(f"%{team_filter}%")

#         # [검색어 조건]
#         if search_query:
#             if search_type == '의뢰번호':
#                 where_clauses.append("r.의뢰번호 LIKE %s")
#                 params.append(f"%{search_query}%")
#             elif search_type == '의뢰기관명':
#                 where_clauses.append("r.의뢰기관명 LIKE %s")
#                 params.append(f"%{search_query}%")
#             elif search_type == '사업명':
#                 where_clauses.append("r.사업명 LIKE %s")
#                 params.append(f"%{search_query}%")
#             else:
#                 where_clauses.append("(r.의뢰번호 LIKE %s OR r.의뢰기관명 LIKE %s OR r.사업명 LIKE %s)")
#                 params.extend([f"%{search_query}%"] * 3)

#         # 최종 WHERE 절 합성
#         where_sentence = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

#         mysql_query = f"""
#             SELECT r.*, i.성적서번호,i.발급일자,r.미인정 
#             FROM csi_receipts r 
#             LEFT JOIN csi_issue_results i ON r.의뢰번호 = i.의뢰번호 
#             {where_sentence}
#             ORDER BY r.담당자 ASC LIMIT 5000
#         """

#         with connections['default'].cursor() as mysql_cursor:
#             mysql_cursor.execute(mysql_query, params)
#             columns = [col[0] for col in mysql_cursor.description]
#             mysql_rows = [dict(zip(columns, row)) for row in mysql_cursor.fetchall()]

#         # 4. 의뢰번호 추출 및 MSSQL 데이터 매칭 (기존 로직 유지)
#         req_codes = [str(row['의뢰번호']).strip() for row in mysql_rows if row.get('의뢰번호')]
#         mssql_dict = {}

#         if req_codes:
#             chunk_size = 1000
#             with connections['mssql'].cursor() as mssql_cursor:
#                 for i in range(0, len(req_codes), chunk_size):
#                     chunk = req_codes[i : i + chunk_size]
#                     placeholders = ', '.join(['%s'] * len(chunk))
                    
#                     mssql_query = f"""
#                         SELECT 
#                             a.sales, a.request_code, a.receipt_csi_code, a.receipt_code, b.completion_day, a.save_date, 
#                             b.builder, b.construction, c.specimen, d.supply_value, d.vat,
#                             e.deposit_day, e.deposit, f.issue_date, f.company
#                         FROM dbo.Receipt a
#                         LEFT JOIN dbo.Customer b     ON a.receipt_code = b.receipt_code
#                         LEFT JOIN dbo.Specimen_info c ON a.receipt_code = c.receipt_code
#                         LEFT JOIN dbo.Estimate d      ON a.receipt_code = d.receipt_code
#                         LEFT JOIN dbo.Deposit e       ON a.receipt_code = e.receipt_code
#                         LEFT JOIN dbo.Tax_Manager f   ON a.receipt_code = f.receipt_code
#                         WHERE a.request_code IN ({placeholders})
#                     """
#                     mssql_cursor.execute(mssql_query, chunk)
#                     m_cols = [col[0] for col in mssql_cursor.description]
#                     for m_row in mssql_cursor.fetchall():
#                         m_item = dict(zip(m_cols, m_row))
#                         mssql_dict[str(m_item['request_code']).strip()] = m_item

#         # 5. 최종 데이터 합체 (기존 코드 유지)
#         final_results = []
#         for row in mysql_rows:
#             req_no = str(row.get('의뢰번호', '')).strip()
#             ms_info = mssql_dict.get(req_no, {})
            
#             # [핵심 변경] MySQL 의뢰번호가 QT-로 시작하면 이를 QT번호로 사용
#             if req_no.startswith('QT-'):
#                 display_qt_no = req_no
#             else:
#                 display_qt_no = ms_info.get('receipt_code', '-')         

#             final_results.append({
#                 "담당자": row.get('담당자', ''),
#                 "영업구분": row.get('영업구분', ''),
#                 "의뢰번호": req_no,
#                 "접수일시": str(row.get('접수일시', '')),
#                 "접수번호": ms_info.get('receipt_csi_code', '-'),
#                 # "QT번호": ms_info.get('receipt_code', '-'),
#                 "QT번호": display_qt_no, # 수정된 변수 적용
#                 "성적서번호": row.get('성적서번호', '-'),
#                 "발급일자": str(row.get('발급일자')) if row.get('발급일자') else "",
#                 "의뢰기관명": row.get('의뢰기관명', ''),
#                 "사업명": ms_info.get('construction', row.get('사업명', '')),
#                 "봉인명": ms_info.get('specimen', '-'),
#                 "준공예정일": str(ms_info.get('completion_day')) if ms_info.get('completion_day') else "",
#                 "실접수일": str(ms_info.get('save_date')) if ms_info.get('save_date') else "",
#                 "공급가액": ms_info.get('supply_value', 0),
#                 "부가세": ms_info.get('vat', 0),
#                 "입금일": ms_info.get('deposit_day', 0),
#                 "입금액": ms_info.get('deposit', 0),
#                 "계산서발행일": str(ms_info.get('issue_date')),
#                 "계산서발행회사명": ms_info.get('company', '-'),
#                 "미인정": row.get('미인정', '') if ms_info.get('issue_date') else ""
#             })

#         # [추가] 6. 통계 집계 로직
#         stats = {}
#         teams = ['1팀', '2팀', '3팀', '4팀', '5팀', '6팀']
        
#         print("\n" + ">>>" * 20)
#         print(" [실시간 집계 추적 시작]")
        
#         for idx, res in enumerate(final_results):
#             # 1. 원본 데이터 확인
#             raw_name = res.get('영업구분', '')
#             raw_manager = res.get('담당자', '')
#             raw_price = res.get('공급가액', 0)
#             req_no = res.get('의뢰번호', '번호없음')

#             # 2. 이름 결정 (영업구분이 우선, 없으면 담당자)
#             name = (raw_name or raw_manager or '').strip()
            
#             # 3. 금액 변환 과정 추적
#             try:
#                 # 숫자가 아닌 문자(콤마 등)가 섞였을 때를 대비
#                 clean_price = str(raw_price).replace(',', '')
#                 price = int(float(clean_price))
#             except:
#                 price = 0

#             # 4. 팀 판별 과정 추적
#             target_team = "미분류"
#             for t in teams:
#                 if t in str(raw_manager):
#                     target_team = t
#                     break

#             # 5. 인정/미인정 판별
#             is_unconfirmed = True if res.get('미인정') else False
#             type_key = "미인정건" if is_unconfirmed else "인정건"

#             # --- [터미널 실시간 출력] ---
#             # 모든 행을 출력하면 너무 많으니, 처음 20개 정도만 보거나 
#             # 금액이 있는 경우만 골라서 출력하여 흐름을 확인합니다.
#             if price > 0:
#                 print(f" -> [{req_no}] 이름:{name} | 팀:{target_team} | {type_key} | 금액:{price:,}원 >> [집계추가]")
#             else:
#                 # 금액이 0원인 것들은 왜 0원인지 확인
#                 print(f" -> [{req_no}] 집계제외(금액0): {name} | 원본금액데이터:{raw_price}")

#             # 6. 실제 데이터 누적
#             if not name: continue
            
#             if name not in stats:
#                 stats[name] = {t: {"인정건": {"금액": 0, "건수": 0}, "미인정건": {"금액": 0, "건수": 0}} for t in teams}

#             if target_team in teams:
#                 stats[name][target_team][type_key]["금액"] += price
#                 stats[name][target_team][type_key]["건수"] += 1

#         print(f" [최종 결과] 생성된 담당자 수: {len(stats)}명")
#         print("<<<" * 20 + "\n")
#         print(f"DEBUG: 최종 전달할 담당자 수: {len(stats)}명")
#         return JsonResponse({'status': 'success', 'data': final_results, 'stats': stats})

#     except Exception as e:
#         import traceback
#         print(traceback.format_exc())
#         return JsonResponse({'status': 'error', 'message': str(e)})




# 여기서부터 테스트용 입니다(발급건수 카운터용)
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

        # 3. MySQL 쿼리 실행
        where_clauses = []
        params = []
        if start_date and end_date:
            where_clauses.append("r.배정일자 BETWEEN %s AND %s")
            params.extend([f"{start_date} 00:00:00", f"{end_date} 23:59:59"])
        if team_filter and team_filter != '전체':
            where_clauses.append("r.담당자 LIKE %s")
            params.append(f"%{team_filter}%")
        if search_query:
            if search_type == '의뢰번호':
                where_clauses.append("r.의뢰번호 LIKE %s")
                params.append(f"%{search_query}%")
            elif search_type == '의뢰기관명':
                where_clauses.append("r.의뢰기관명 LIKE %s")
                params.append(f"%{search_query}%")
            else:
                where_clauses.append("r.사업명 LIKE %s")
                params.append(f"%{search_query}%")

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
        req_codes = [str(row['의뢰번호']).strip() for row in mysql_rows if row.get('의뢰번호')]
        mssql_dict = {}
        if req_codes:
            chunk_size = 1000
            with connections['mssql'].cursor() as mssql_cursor:
                for i in range(0, len(req_codes), chunk_size):
                    chunk = req_codes[i : i + chunk_size]
                    placeholders = ', '.join(['%s'] * len(chunk))
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
                        WHERE a.request_code IN ({placeholders})
                    """
                    mssql_cursor.execute(mssql_query, chunk)
                    m_cols = [col[0] for col in mssql_cursor.description]
                    for m_row in mssql_cursor.fetchall():
                        m_item = dict(zip(m_cols, m_row))
                        mssql_dict[str(m_item['request_code']).strip()] = m_item

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
    
#1. 여기서부터 현장팀 정산 페이지 입니다.
def field_payment_view(request):
    now = datetime.now()
    
    # 템플릿 에러(|split)를 방지하기 위해 월 리스트 생성
    month_list = range(1, 13)
    
    context = {
        'current_year': now.year,
        'current_month': now.month,
        'month_list': month_list,
        'today_str': now.strftime('%Y-%m-%d'),
    }
    return render(request, 'field_payment.html', context)

# 2. 두번째 작업

# def bizmeka_sync(request):
#     target_year = request.GET.get('year')
#     target_month = request.GET.get('month')
    
#     # [1] 드라이버 및 옵션 설정
#     chrome_options = Options()
#     chrome_options.add_argument("--start-maximized")
#     driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
#     try:
#         # 1. 로그인 및 알림창 처리
#         driver.get("https://ezportal.bizmeka.com/")
#         wait = WebDriverWait(driver, 15)
        
#         driver.find_element(By.ID, "username").send_keys("k200335")
#         driver.find_element(By.ID, "password").send_keys("k*1800*92*" + Keys.ENTER)
        
#         # 로그인 완료 대기
#         start_time = time.time()
#         while time.time() - start_time < 300:
#             try:
#                 driver.switch_to.alert.accept()
#             except: pass
#             if "main" in driver.current_url: break
#             time.sleep(1)

#         # 2. 일정 페이지 이동 및 월간 뷰 설정
#         driver.get("https://ezgroupware.bizmeka.com/groupware/planner/calendar.do")
#         time.sleep(3)
#         wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "fc-month-button"))).click()
#         time.sleep(1)

#         # 3. [핵심] 선택 버튼 없이 '이전' 버튼으로만 이동
#         # [3] 12월 이동 완료 후 (이전 버튼 로직은 그대로 유지)
#         while True:
#             center_title = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".fc-center h2"))).text.strip()
#             if target_year in center_title and f"{int(target_month)}월" in center_title:
#                 break
#             prev_btn = driver.find_element(By.CLASS_NAME, "fc-prev-button")
#             driver.execute_script("arguments[0].click();", prev_btn)
#             time.sleep(1.5)

#         # [4] 데이터 수집 (날짜 비교 없이 화면에 보이는 모든 일정을 긁음)
#         time.sleep(3) # 달력이 완전히 멈출 때까지 충분히 대기
        
#         # 1. 화면에 펼쳐져 있는 모든 일정 박스를 다 가져옵니다.
#         # span.fc-title 대신 div.fc-content를 사용하여 "양지훈/시료수거..." 전체 텍스트 확보
#         all_events = driver.find_elements(By.CSS_SELECTOR, ".fc-content")
        
#         final_list = []
#         for ev in all_events:
#             txt = ev.text.replace('\n', ' ').strip()
#             if txt:
#                 final_list.append({"content": txt})

#         # 2. '더보기(+N)' 버튼이 있는 날짜들만 골라내어 클릭 후 팝업 데이터 수집
#         more_links = driver.find_elements(By.CSS_SELECTOR, ".fc-more")
#         for link in more_links:
#             try:
#                 driver.execute_script("arguments[0].click();", link)
#                 time.sleep(0.8)
                
#                 # 팝업창 내의 일정들 추가 수집
#                 pop_events = driver.find_elements(By.CSS_SELECTOR, ".fc-more-popover .fc-content")
#                 for p_ev in pop_events:
#                     p_txt = p_ev.text.replace('\n', ' ').strip()
#                     if p_txt:
#                         final_list.append({"content": p_txt})
                
#                 # 팝업 닫기
#                 driver.find_element(By.CSS_SELECTOR, ".fc-more-popover .fc-close").click()
#                 time.sleep(0.3)
#             except: pass

#         # 최종 반환 (이제 0개가 나올 수 없습니다)
#         return JsonResponse({
#             "status": "success", 
#             "total_count": len(final_list), 
#             "data": final_list
#         })

#     except Exception as e:
#         return JsonResponse({"status": "error", "message": f"시스템 에러: {str(e)}"})
#     finally:
#         driver.quit() # 드라이버 종료를 finally에 두어 에러 시에도 창이 닫히도록 함



# 여기서 부터 테스트코드
def bizmeka_sync(request):
    target_year = request.GET.get('year')
    target_month = request.GET.get('month')
    
    # [1] 드라이버 및 옵션 설정
    chrome_options = Options()
    chrome_options.add_argument("--start-maximized")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    try:
        # 1. 로그인 처리
        driver.get("https://ezportal.bizmeka.com/")
        wait = WebDriverWait(driver, 15)
        
        driver.find_element(By.ID, "username").send_keys("k200335")
        driver.find_element(By.ID, "password").send_keys("k*1800*92*" + Keys.ENTER)
        
        # 알림창 처리 및 메인 진입 대기
        start_time = time.time()
        while time.time() - start_time < 300:
            try:
                driver.switch_to.alert.accept()
            except: pass
            if "main" in driver.current_url: break
            time.sleep(1)

        # 2. 일정 페이지 이동 및 월간 뷰 고정
        driver.get("https://ezgroupware.bizmeka.com/groupware/planner/calendar.do")
        time.sleep(3)
        wait.until(EC.element_to_be_clickable((By.CLASS_NAME, "fc-month-button"))).click()
        time.sleep(1)

        # 3. [이동] '이전' 버튼으로 목표 달 도달 (선택 버튼 무시)
        while True:
            center_title = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".fc-center h2"))).text.strip()
            if target_year in center_title and f"{int(target_month)}월" in center_title:
                break
            
            prev_btn = driver.find_element(By.CLASS_NAME, "fc-prev-button")
            driver.execute_script("arguments[0].click();", prev_btn)
            time.sleep(1.5)

        # 4. [수집] 이미 로딩된 데이터 싹쓸이 (textContent 활용)
        time.sleep(2) 
        final_list = []

        # 4-1. 화면에 보이는 기본 일정 수집
        events = driver.find_elements(By.CSS_SELECTOR, ".fc-content-skeleton .fc-content")
        for ev in events:
            try:
                # 텍스트를 강제로 긁어오는 textContent
                raw_text = ev.get_attribute("textContent").replace('\n', ' ').strip()
                parent_td = ev.find_element(By.XPATH, "./ancestor::td")
                target_date = parent_td.get_attribute("data-date")
                
                if raw_text:
                    # [터미널 확인용] 데이터가 긁히고 있는지 실시간으로 출력합니다.
                    print(f">>> [기본수집] 날짜: {target_date} | 내용: {raw_text[:30]}...")
                    
                    # 화면(image_020fa0.png)의 '날짜', '일정 상세내용' 필드에 정확히 매칭
                    final_list.append({
                        "date": target_date,   
                        "content": raw_text    
                    })
            except: pass

        # 4-2. '+N' 더보기 버튼 내 숨겨진 일정 수집
        more_links = driver.find_elements(By.CSS_SELECTOR, ".fc-more")
        for link in more_links:
            try:
                p_date = link.find_element(By.XPATH, "./ancestor::td").get_attribute("data-date")
                driver.execute_script("arguments[0].click();", link)
                time.sleep(0.5)
                
                pop_items = driver.find_elements(By.CSS_SELECTOR, ".fc-more-popover .fc-content")
                for p_item in pop_items:
                    p_txt = p_item.get_attribute("textContent").replace('\n', ' ').strip()
                    if p_txt:
                        # [터미널 확인용] 더보기 내부 데이터 수집 현황 출력
                        print(f"  └─ [더보기수집] 날짜: {p_date} | 내용: {p_txt[:30]}...")
                        
                        final_list.append({
                            "date": p_date,
                            "content": p_txt
                        })
                
                driver.find_element(By.CSS_SELECTOR, ".fc-more-popover .fc-close").click()
                time.sleep(0.2)
            except: pass

        # 최종 로그 출력
        print(f"=== 수집 완료! 총 {len(final_list)}개의 데이터를 찾았습니다. ===")

        # [핵심] JSON 반환 시 Key 이름을 화면 JS와 100% 일치시켜야 함
        return JsonResponse({
            "status": "success", 
            "total_count": len(final_list), 
            "data": final_list  # 여기서 보내는 'data'가 JS의 item.date, item.content로 연결됨
        })

    except Exception as e:
        print(f"!!! 에러 발생: {str(e)}") # 에러 내용을 터미널에 출력
        return JsonResponse({"status": "error", "message": f"시스템 에러: {str(e)}"})
    # finally:
    #     driver.quit()

