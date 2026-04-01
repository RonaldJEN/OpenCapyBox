"""
測試報告生成驗證腳本
"""
import sys
from pathlib import Path

# 添加項目路徑
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def test_imports():
    """測試必要的模塊是否能導入"""
    try:
        import pytest
        print("[OK] pytest")
    except ImportError as e:
        print(f"[FAIL] pytest: {e}")
        return False
    
    try:
        import pytest_html
        print("[OK] pytest-html")
    except ImportError as e:
        print(f"[FAIL] pytest-html: {e}")
        return False
    
    try:
        import pytest_jsonreport
        print("[OK] pytest-json-report")
    except ImportError as e:
        print(f"[FAIL] pytest-json-report: {e}")
        return False
    
    try:
        import coverage_badge
        print("[OK] coverage-badge")
    except ImportError as e:
        print(f"[FAIL] coverage-badge: {e}")
        return False
    
    try:
        import coverage
        print("[OK] coverage")
    except ImportError as e:
        print(f"[FAIL] coverage: {e}")
        return False
    
    return True


def test_report_structure():
    """檢查報告目錄結構"""
    reports_dir = project_root / "reports"
    
    if not reports_dir.exists():
        print(f"[INFO] reports/ 目錄不存在，將在測試運行時創建")
        return True
    
    print(f"[OK] reports/ 目錄存在")
    
    # 列出現有報告文件
    if reports_dir.exists():
        print("\n現有報告文件:")
        for item in reports_dir.iterdir():
            if item.is_file():
                size = item.stat().st_size
                print(f"  - {item.name} ({size} bytes)")
            elif item.is_dir():
                print(f"  - {item.name}/ (目錄)")
    
    return True


def test_config():
    """檢查配置文件"""
    pyproject = project_root / "pyproject.toml"
    
    if not pyproject.exists():
        print("[WARN] pyproject.toml 不存在")
        return False
    
    with open(pyproject, 'r', encoding='utf-8') as f:
        content = f.read()
        
        if 'pytest-html' in content:
            print("[OK] pyproject.toml 包含 pytest-html")
        else:
            print("[WARN] pyproject.toml 未包含 pytest-html")
        
        if 'pytest-cov' in content:
            print("[OK] pyproject.toml 包含 pytest-cov")
        else:
            print("[WARN] pyproject.toml 未包含 pytest-cov")
    
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("測試報告配置驗證")
    print("=" * 60)
    
    print("\n1. 檢查依賴模塊...")
    if not test_imports():
        print("\n[ERROR] 缺少必要的依賴，請運行: pip install -e \".[dev]\"")
        sys.exit(1)
    
    print("\n2. 檢查報告目錄...")
    test_report_structure()
    
    print("\n3. 檢查配置文件...")
    test_config()
    
    print("\n" + "=" * 60)
    print("[SUCCESS] 配置驗證完成!")
    print("\n下一步:")
    print("  1. 運行測試: python run_tests.py")
    print("  2. 查看報告: reports/test_report.html")
    print("  3. 查看覆蓋率: reports/coverage_html/index.html")
    print("=" * 60)
