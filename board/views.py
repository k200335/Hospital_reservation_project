from django.shortcuts import render
from .models import OuterreceiptNew
from django.db.models import Q

def receipt_list(request):
    # 1. 검색 데이터 가져오기
    search_type = request.GET.get('search_type', 'rqcode')  # 의뢰번호, 봉인명 등
    search_value = request.GET.get('search_value', '')     # 검색어
    date_type = request.GET.get('date_type', 'receiveday') # 등록일자, 접수일자 등
    start_date = request.GET.get('start_date', '')         # 시작일
    end_date = request.GET.get('end_date', '')             # 종료일

    # 2. 기본 쿼리셋
    receipts = OuterreceiptNew.objects.all().order_by('-idx')

    # 3. 텍스트 필터링
    if search_value:
        filter_kwargs = {f"{search_type}__icontains": search_value}
        receipts = receipts.filter(**filter_kwargs)

    # 4. 날짜 필터링 (선택한 날짜 종류에 따라 필터링)
    if start_date and end_date:
        date_filter = {f"{date_type}__range": [start_date, end_date]}
        receipts = receipts.filter(**date_filter)

    context = {
        'receipts': receipts,
        'search_type': search_type,
        'search_value': search_value,
        'date_type': date_type,
        'start_date': start_date,
        'end_date': end_date,
    }
    return render(request, 'receipt_list.html', context)