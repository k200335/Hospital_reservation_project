# board/models.py
from django.db import models

class OuterreceiptNew(models.Model):
    # 실제 DB에 존재하는 idx를 기본키로 설정
    idx = models.AutoField(primary_key=True) 
    
    # 요청하신 3개 컬럼 (DB 대소문자 명칭과 db_column을 일치시킴)
    receiptcode = models.CharField(db_column='receiptCode', max_length=10, blank=True, null=True)
    rqcode = models.CharField(db_column='rqCode', max_length=14, blank=True, null=True)
    accode = models.CharField(db_column='acCode', max_length=14, blank=True, null=True)

    class Meta:
        managed = False  # 기존 DB 보호
        db_table = 'OuterReceipt'  # dbo.OuterReceipt 테이블을 바라봄