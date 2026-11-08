# -*- coding: utf-8 -*-
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitServices')

from Autodesk.Revit.DB import (
    Transaction,
    Level,
    Grid,
    Line,
    XYZ,
    FilteredElementCollector
)

# 단위 변환 함수 (mm -> feet)
def mm_to_ft(mm_val):
    return float(mm_val) / 304.8

# 현재 문서 가져오기 (pyRevit / RevitPythonShell 환경 호환)
try:
    doc = __revit__.ActiveUIDocument.Document
except NameError:
    pass

# 기존 레벨 및 그리드 이름 수집 (중복 이름 방지용)
existing_levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
existing_level_dict = {lvl.Name: lvl for lvl in existing_levels}

existing_grids = FilteredElementCollector(doc).OfClass(Grid).ToElements()
existing_grid_dict = {grd.Name: grd for grd in existing_grids}

# 세팅 데이터 정의
# 1F를 기준(0mm)으로 설정, 지하 층고 4,500mm, 지상 기준층 층고 4,800mm 적용
levels_data = [
    ('B2F', -9000.0),
    ('B1F', -4500.0),
    ('1F', 0.0),
    ('2F', 4800.0),
    ('3F', 9600.0),
    ('4F', 14400.0),
    ('5F', 19200.0)
]

# 그리드 생성 범위 계산 (여유 마진 포함)
x_coords = [i * 8000.0 for i in range(5)]  # X1 ~ X5 (0, 8000, 16000, 24000, 32000)
y_coords = [j * 8000.0 for j in range(4)]  # Y1 ~ Y4 (0, 8000, 16000, 24000)

margin = 5000.0
x_min = min(x_coords) - margin
x_max = max(x_coords) + margin
y_min = min(y_coords) - margin
y_max = max(y_coords) + margin

created_levels_count = 0
updated_levels_count = 0
created_grids_count = 0
skipped_grids_count = 0

# 트랜잭션 실행
t = Transaction(doc, 'AI Auto Setup')
t.Start()

try:
    # 1. 레벨 생성 및 갱신
    for name, elev_mm in levels_data:
        elev_ft = mm_to_ft(elev_mm)
        if name in existing_level_dict:
            # 이미 존재하는 레벨은 높이 갱신
            target_level = existing_level_dict[name]
            target_level.Elevation = elev_ft
            updated_levels_count += 1
        else:
            # 신규 레벨 생성
            new_level = Level.Create(doc, elev_ft)
            try:
                new_level.Name = name
            except Exception:
                pass
            created_levels_count += 1

    # 2. X축 그리드 생성 (X1 ~ X5, Y방향 수직선)
    for idx, x_mm in enumerate(x_coords, start=1):
        g_name = 'X{0}'.format(idx)
        if g_name in existing_grid_dict:
            skipped_grids_count += 1
            continue

        p1 = XYZ(mm_to_ft(x_mm), mm_to_ft(y_min), 0.0)
        p2 = XYZ(mm_to_ft(x_mm), mm_to_ft(y_max), 0.0)
        line = Line.CreateBound(p1, p2)
        grid = Grid.Create(doc, line)
        try:
            grid.Name = g_name
        except Exception:
            pass
        created_grids_count += 1

    # 3. Y축 그리드 생성 (Y1 ~ Y4, X방향 수평선)
    for idx, y_mm in enumerate(y_coords, start=1):
        g_name = 'Y{0}'.format(idx)
        if g_name in existing_grid_dict:
            skipped_grids_count += 1
            continue

        p1 = XYZ(mm_to_ft(x_min), mm_to_ft(y_mm), 0.0)
        p2 = XYZ(mm_to_ft(x_max), mm_to_ft(y_mm), 0.0)
        line = Line.CreateBound(p1, p2)
        grid = Grid.Create(doc, line)
        try:
            grid.Name = g_name
        except Exception:
            pass
        created_grids_count += 1

    t.Commit()
    status_str = '성공'
except Exception as ex:
    t.RollBack()
    status_str = '실패 ({0})'.format(str(ex))

RESULT_MSG = '[세팅 완료] 트랜잭션: {0} | 레벨 생성: {1}개, 갱신: {2}개 | 그리드 생성: {3}개, 건너뜀: {4}개'.format(
    status_str,
    created_levels_count,
    updated_levels_count,
    created_grids_count,
    skipped_grids_count
)