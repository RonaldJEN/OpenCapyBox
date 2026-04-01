#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
測試運行腳本 - 運行所有測試並生成報告

腳本位置: deploy/test_scripts/run_tests.py
報告輸出: tests/reports/

用法:
    python deploy/test_scripts/run_tests.py              # 運行所有測試並生成報告
    python deploy/test_scripts/run_tests.py --quick      # 快速運行，不生成覆蓋率
    python deploy/test_scripts/run_tests.py --html       # 運行後自動打開 HTML 報告
    python deploy/test_scripts/run_tests.py -k "security"  # 只運行包含 security 的測試
"""
import subprocess
import sys
import os
import webbrowser
import json
from pathlib import Path
from datetime import datetime

# 設置 Windows 控制台編碼為 UTF-8
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
    except:
        pass
    # 重新配置 stdout 和 stderr
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')


def get_project_root() -> Path:
    """獲取項目根目錄（包含 pyproject.toml 的目錄）"""
    # 從當前腳本位置向上查找
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "pyproject.toml").exists():
            return current
        current = current.parent
    # 如果找不到，使用腳本的父目錄的父目錄的父目錄
    # deploy/test_scripts/run_tests.py -> 項目根目錄
    return Path(__file__).resolve().parent.parent.parent


def main():
    """運行測試並生成報告"""
    project_root = get_project_root()
    
    # 腳本所在目錄
    script_dir = Path(__file__).resolve().parent
    
    # 報告輸出到 deploy/test_scripts/reports/ 目錄
    reports_dir = script_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    
    # 解析命令行參數
    args = sys.argv[1:]
    quick_mode = "--quick" in args
    open_html = "--html" in args
    
    # 過濾掉我們的自定義參數
    pytest_args = [a for a in args if a not in ("--quick", "--html")]
    
    # 構建 pytest 命令
    cmd = [sys.executable, "-m", "pytest"]
    
    if quick_mode:
        # 快速模式：不生成覆蓋率
        cmd.extend([
            "-v",
            "--tb=short",
        ])
    else:
        # 完整模式：包含覆蓋率
        cmd.extend([
            "-v",
            "--tb=short",
            "--cov=src",
            "--cov-report=term-missing",
            f"--cov-report=html:{reports_dir / 'coverage_html'}",
            f"--cov-report=xml:{reports_dir / 'coverage.xml'}",
            f"--junitxml={reports_dir / 'junit.xml'}",
            f"--html={reports_dir / 'test_report.html'}",
            "--self-contained-html",
            f"--json-report",
            f"--json-report-file={reports_dir / 'test_report.json'}",
        ])
    
    # 添加用戶指定的額外參數
    cmd.extend(pytest_args)
    
    print("=" * 60)
    print(f"[TEST] 運行測試 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"命令: {' '.join(str(c) for c in cmd)}")
    print("-" * 60)
    sys.stdout.flush()
    
    # 運行測試，直接輸出到控制台
    result = subprocess.run(cmd, cwd=project_root)
    
    print("\n" + "=" * 60)
    
    if result.returncode == 0:
        print("[PASS] 所有測試通過!")
    else:
        print(f"[FAIL] 測試失敗 (退出碼: {result.returncode})")
    
    # 顯示報告位置
    if not quick_mode:
        print("\n[REPORTS] 測試報告位置:")
        html_path = reports_dir / 'coverage_html' / 'index.html'
        test_report_path = reports_dir / 'test_report.html'
        xml_path = reports_dir / 'coverage.xml'
        junit_path = reports_dir / 'junit.xml'
        json_path = reports_dir / 'test_report.json'
        
        print(f"   • HTML 測試報告:    {test_report_path}")
        print(f"   • HTML 覆蓋率報告: {html_path}")
        print(f"   • XML 覆蓋率報告:  {xml_path}")
        print(f"   • JUnit 報告:      {junit_path}")
        print(f"   • JSON 報告:       {json_path}")
        
        # 生成覆蓋率徽章
        try:
            badge_result = subprocess.run(
                [sys.executable, "-m", "coverage_badge", "-o", 
                 str(reports_dir / "coverage.svg"), "-f"],
                cwd=project_root,
                capture_output=True,
                text=True
            )
            if badge_result.returncode == 0:
                print(f"   • 覆蓋率徽章:      {reports_dir / 'coverage.svg'}")
        except Exception as e:
            print(f"   [WARN] 無法生成覆蓋率徽章: {e}")
        
        # 生成測試報告總結
        try:
            generate_summary_report(reports_dir)
            print(f"   • 測試總結報告:    {reports_dir / 'summary.md'}")
        except Exception as e:
            print(f"   [WARN] 無法生成總結報告: {e}")
        
        # 自動打開 HTML 報告
        if open_html:
            if test_report_path.exists():
                print(f"\n[BROWSER] 正在打開測試報告...")
                webbrowser.open(str(test_report_path.absolute().as_uri()))
            if html_path.exists():
                print(f"[BROWSER] 正在打開覆蓋率報告...")
                webbrowser.open(str(html_path.absolute().as_uri()))
    
    print("=" * 60)
    
    return result.returncode


def generate_summary_report(reports_dir: Path):
    """生成測試總結報告"""
    summary_path = reports_dir / "summary.md"
    
    # 讀取 JSON 測試報告
    json_report_path = reports_dir / "test_report.json"
    if not json_report_path.exists():
        return
    
    with open(json_report_path, 'r', encoding='utf-8') as f:
        test_data = json.load(f)
    
    # 讀取覆蓋率數據（從 coverage.xml 或者直接運行 coverage report）
    coverage_info = get_coverage_info()
    
    # 生成 Markdown 報告
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("# 測試報告總結\n\n")
        f.write(f"**生成時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        # 測試結果概覽
        f.write("## 測試結果概覽\n\n")
        summary = test_data.get('summary', {})
        total = summary.get('total', 0)
        passed = summary.get('passed', 0)
        failed = summary.get('failed', 0)
        skipped = summary.get('skipped', 0)
        errors = summary.get('error', 0)
        duration = test_data.get('duration', 0)
        
        f.write(f"- **總測試數**: {total}\n")
        f.write(f"- **通過**: :white_check_mark: {passed}\n")
        if failed > 0:
            f.write(f"- **失敗**: :x: {failed}\n")
        if skipped > 0:
            f.write(f"- **跳過**: :fast_forward: {skipped}\n")
        if errors > 0:
            f.write(f"- **錯誤**: :boom: {errors}\n")
        f.write(f"- **執行時間**: {duration:.2f}秒\n")
        
        # 成功率
        success_rate = (passed / total * 100) if total > 0 else 0
        f.write(f"- **成功率**: {success_rate:.1f}%\n\n")
        
        # 覆蓋率信息
        if coverage_info:
            f.write("## 代碼覆蓋率\n\n")
            f.write(f"- **整體覆蓋率**: {coverage_info.get('total', 'N/A')}\n")
            f.write(f"- **語句覆蓋**: {coverage_info.get('statements', 'N/A')}\n")
            f.write(f"- **分支覆蓋**: {coverage_info.get('branches', 'N/A')}\n\n")
            
            # 覆蓋率徽章
            badge_path = reports_dir / "coverage.svg"
            if badge_path.exists():
                f.write("![Coverage](./coverage.svg)\n\n")
        
        # 測試詳情
        f.write("## 測試詳情\n\n")
        tests = test_data.get('tests', [])
        
        # 按文件分組
        test_by_file = {}
        for test in tests:
            nodeid = test.get('nodeid', '')
            file_path = nodeid.split('::')[0] if '::' in nodeid else 'unknown'
            if file_path not in test_by_file:
                test_by_file[file_path] = []
            test_by_file[file_path].append(test)
        
        for file_path, file_tests in sorted(test_by_file.items()):
            f.write(f"### {file_path}\n\n")
            for test in file_tests:
                name = test.get('nodeid', '').split('::')[-1]
                outcome = test.get('outcome', 'unknown')
                duration = test.get('duration', 0)
                
                outcome_emoji = {
                    'passed': ':white_check_mark:',
                    'failed': ':x:',
                    'skipped': ':fast_forward:',
                    'error': ':boom:'
                }.get(outcome, ':question:')
                
                f.write(f"- {outcome_emoji} **{name}** ({duration:.3f}s)\n")
                
                # 如果測試失敗，顯示錯誤信息
                if outcome in ('failed', 'error'):
                    call_info = test.get('call', {})
                    longrepr = call_info.get('longrepr', '')
                    if longrepr:
                        f.write(f"  ```\n  {longrepr[:200]}...\n  ```\n")
            f.write("\n")
        
        # 報告文件鏈接
        f.write("## 詳細報告\n\n")
        f.write("- [HTML 測試報告](./test_report.html)\n")
        f.write("- [HTML 覆蓋率報告](./coverage_html/index.html)\n")
        f.write("- [XML 覆蓋率報告](./coverage.xml)\n")
        f.write("- [JUnit XML 報告](./junit.xml)\n")
        f.write("- [JSON 測試報告](./test_report.json)\n")


def get_coverage_info():
    """獲取覆蓋率信息"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "coverage", "report"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            # 解析覆蓋率輸出
            lines = result.stdout.strip().split('\n')
            for line in lines:
                if line.startswith('TOTAL'):
                    parts = line.split()
                    if len(parts) >= 4:
                        return {
                            'total': parts[-1],
                            'statements': parts[1] if len(parts) > 1 else 'N/A',
                            'branches': parts[3] if len(parts) > 3 else 'N/A'
                        }
    except Exception:
        pass
    return None


if __name__ == "__main__":
    sys.exit(main())
