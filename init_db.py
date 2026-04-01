"""初始化數據庫腳本"""
import sys
sys.path.insert(0, '.')

try:
    from src.api.models import init_db
    init_db()
    print("✅ Database initialized successfully!")
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
