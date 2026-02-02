from django.db import models

# ==========================================================
# 1. MS SQL 전용 모델 (조회 전용 - OuterReceipt 테이블)
# ==========================================================
class OuterreceiptNew(models.Model):
    """
    MS SQL 서버의 데이터를 읽어오기 위한 모델입니다.
    기존 시스템의 데이터를 참조하는 용도로 사용됩니다.
    """
    idx           = models.AutoField(primary_key=True)
    receiptcode   = models.CharField(db_column='receiptCode', max_length=10, blank=True, null=True)
    rqcode        = models.CharField(db_column='rqCode', max_length=14, blank=True, null=True)
    accode        = models.CharField(db_column='acCode', max_length=14, blank=True, null=True)
    
    # CSI 데이터 수집용 필드
    rq_no         = models.CharField(max_length=50, blank=True, null=True)       # 의뢰번호
    receipt_no    = models.CharField(max_length=50, blank=True, null=True)       # 접수번호
    receipt_date  = models.CharField(max_length=50, blank=True, null=True)       # 접수일시
    current_status= models.CharField(max_length=50, blank=True, null=True)       # 최종진행상태
    project_name  = models.TextField(blank=True, null=True)                      # 공사명
    client_name   = models.CharField(max_length=100, blank=True, null=True)      # 의뢰기관
    picker_name   = models.CharField(max_length=50, blank=True, null=True)       # 채취자 성명
    seal_name     = models.TextField(blank=True, null=True)                      # 봉인명
    specific_user = models.CharField(max_length=50, blank=True, null=True)       # 처리자

    class Meta:
        managed = False          # Django가 테이블을 생성/수정하지 않음 (기존 DB 사용)
        db_table = 'OuterReceipt' # MS SQL 내 실제 테이블 이름

    def __str__(self):
        return f"[{self.rqcode}] {self.project_name or 'No Project'}"


# ==========================================================
# 2. MySQL 전용 모델 (조회 및 저장용 - csi_receipts 테이블)
# ==========================================================
class CsiReceipt(models.Model):
    """
    MySQL 서버에 데이터를 저장하고 배정 현황을 관리하는 모델입니다.
    """
    id           = models.AutoField(primary_key=True)
    u_id         = models.CharField(max_length=45, db_column='의뢰번호')
    receipt_id   = models.CharField(max_length=45, db_column='접수번호', null=True, blank=True)
    receipt_date = models.CharField(max_length=45, db_column='접수일시', null=True, blank=True)
    status       = models.CharField(max_length=45, db_column='진행상태')
    project      = models.CharField(max_length=100, db_column='사업명')
    client       = models.CharField(max_length=100, db_column='의뢰기관명')
    sampler      = models.CharField(max_length=45, db_column='채취자', null=True, blank=True)
    seal         = models.CharField(max_length=100, db_column='봉인명', null=True, blank=True)
    processor    = models.CharField(max_length=45, db_column='처리자', null=True, blank=True)
    sales_type   = models.CharField(max_length=45, db_column='영업구분')
    manager      = models.CharField(max_length=45, db_column='담당자', null=True, blank=True)
    confirm      = models.CharField(max_length=45, db_column='확인', null=True, blank=True)
    amount       = models.CharField(max_length=100, db_column='시료량', null=True, blank=True)
    category     = models.CharField(max_length=45, db_column='구분')
    site_manager = models.CharField(max_length=100, db_column='현장담당자', null=True, blank=True)
    assign_date  = models.CharField(max_length=45, db_column='배정일자', null=True, blank=True)
    assign_status= models.CharField(max_length=45, db_column='배정현황', null=True, blank=True)
    unapproved   = models.CharField(max_length=45, db_column='미인정', null=True, blank=True)

    class Meta:
        managed = False          # 이미 직접 테이블을 생성했으므로 False 유지
        db_table = 'csi_receipts' # MySQL 내 실제 테이블 이름

    def __str__(self):
        return self.project
    


    # -------------------------------여기서 부터 일정관리---------------------


# 1. 담당자 및 프로젝트 정보 (MySQL)
class ClientProject(models.Model):
    reg_name = models.CharField(max_length=50, verbose_name="담당자 성함")
    reg_phone = models.CharField(max_length=20, unique=True, verbose_name="연락처") # 중복 방지
    reg_email = models.EmailField(max_length=100, blank=True, null=True, verbose_name="이메일") # 추가됨
    reg_company = models.CharField(max_length=100, verbose_name="의뢰기관명")
    reg_project_name = models.CharField(max_length=200, verbose_name="MSSQL 사업명")
    is_linked = models.BooleanField(default=False, verbose_name="MSSQL 연결 여부")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.reg_name} ({self.reg_company})"

# 2. 상담 메모 (1:N 관계)
class ConsultMemo(models.Model):
    project = models.ForeignKey(ClientProject, on_delete=models.CASCADE, related_name='memos')
    content = models.TextField(verbose_name="상담내용")
    created_at = models.DateTimeField(auto_now_add=True)

# 3. 업무 예약 (1:N 관계)
class TaskReservation(models.Model):
    project = models.ForeignKey(ClientProject, on_delete=models.CASCADE, related_name='tasks')
    category = models.CharField(max_length=20) # EST(견적), TAX(세발), EXAM(시험)
    task_date = models.DateField()
    description = models.CharField(max_length=255)
    is_completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # -----------------------------여기서부터입출금관리페이지---------
class transactions(models.Model):
    id = models.AutoField(primary_key=True)
    date = models.DateField()
    category_id = models.IntegerField(null=True, blank=True) # 입금(1)/지출(2) 구분
    client_name = models.CharField(max_length=45, null=True, blank=True)
    
    # 수량 및 단가
    unit_price = models.DecimalField(max_digits=10, decimal_places=0, default=0)
    quantity = models.IntegerField(default=1)
    
    # 추가 추천 필드
    account_name = models.CharField(max_length=50, null=True, blank=True)
    category_main = models.CharField(max_length=50, null=True, blank=True)
    
    # 금액 정보
    supply_value = models.DecimalField(max_digits=10, decimal_places=0, default=0)
    vat = models.DecimalField(max_digits=10, decimal_places=0, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=0, default=0)
    
    # 메모 및 증빙
    description = models.CharField(max_length=255, null=True, blank=True) # 이제 '적요'만 저장
    note = models.TextField(null=True, blank=True) # '비고' 전용 컬럼 추가
    receipt_img = models.ImageField(upload_to='receipts/', null=True, blank=True)

    class Meta:
        db_table = 'transactions'
        managed = False
        
class TransactionCategory(models.Model):  # <-- 이 이름이 views.py의 이름과 토씨 하나 안 틀리고 같아야 함
    name = models.CharField(max_length=50, unique=True)
    icon = models.CharField(max_length=10, default="📂")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'transactioncategory'
        managed = False

    def __str__(self):
        return self.name