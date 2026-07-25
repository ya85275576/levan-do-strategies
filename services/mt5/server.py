"""
MT5 IPC Server — Node.js <-> Python 通信橋接

透過 stdin/stdout JSON 行協議與 Node.js 父行程通信。

協議格式：
  - Node.js → Python: 每行一個 JSON 物件
    {"id": 1, "method": "get_account_info", "params": {}}
  - Python → Node.js: 每行一個 JSON 物件
    {"id": 1, "result": {"success": true, "data": ...}}
    或 {"id": 1, "error": "錯誤訊息"}

支援方法：
  - initialize            初始化 MT5 連線
  - shutdown              關閉 MT5 連線
  - ping                  連線狀態檢查
  - get_account_info      帳戶資訊查詢
  - get_positions         持倉查詢
  - place_order           下單
  - close_position        平倉
  - get_orders            訂單查詢
  - get_simulated_orders  取得模擬訂單記錄
  - get_simulated_positions 取得模擬持倉快照
  - reset_simulation      重置模擬狀態

使用方式（Node.js 端）：
  1. spawn('python3', ['services/mt5/server.py'])
  2. 寫入 stdin: json_line + '\\n'
  3. 從 stdout 讀取回應

注意：
  此腳本設計為長時間運行的子行程，不應直接由終端使用者呼叫。
"""

import sys
import json
import traceback

# 匯入橋接模組
sys.path.insert(0, '.')
from services.mt5.bridge import (
    initialize,
    shutdown,
    ping as bridge_ping,
    get_account_info,
    get_positions,
    place_order,
    close_position,
    get_orders,
    get_simulated_orders,
    get_simulated_positions,
    reset_simulation,
)

# 方法分發表
METHODS = {
    "initialize": initialize,
    "shutdown": shutdown,
    "ping": bridge_ping,
    "get_account_info": get_account_info,
    "get_positions": get_positions,
    "place_order": place_order,
    "close_position": close_position,
    "get_orders": get_orders,
    "get_simulated_orders": get_simulated_orders,
    "get_simulated_positions": get_simulated_positions,
    "reset_simulation": reset_simulation,
}


def handle_request(request: dict) -> dict:
    """
    處理單一 IPC 請求。
    請求格式：{"id": ..., "method": "...", "params": {...}}
    回覆格式：{"id": ..., "result": {...}} 或 {"id": ..., "error": "..."}
    """
    req_id = request.get("id", 0)
    method = request.get("method", "")
    params = request.get("params", {})

    if not method:
        return {"id": req_id, "error": "缺少 method 欄位"}

    func = METHODS.get(method)
    if not func:
        return {"id": req_id, "error": f"不支援的方法: {method}"}

    try:
        # 根據方法簽章決定參數傳遞方式
        if params and isinstance(params, dict):
            result = func(**params)
        else:
            result = func()
        return {"id": req_id, "result": result}
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return {"id": req_id, "error": str(e)}


def main():
    """
    主迴圈：從 stdin 讀取 JSON 行請求，回寫 JSON 行回應到 stdout。
    """
    # 初始化時自動執行 ping 確認
    stderr_log("MT5 IPC Server 啟動中...")
    stderr_log(f"Python 版本: {sys.version}")
    stderr_log("等待 stdin JSON 命令...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError as e:
            stderr_log(f"JSON 解析錯誤: {e}")
            response = {"id": 0, "error": f"JSON 解析錯誤: {e}"}
        else:
            response = handle_request(request)

        # 寫入 stdout（Node.js 父行程讀取）
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    stderr_log("MT5 IPC Server 結束")


def stderr_log(msg: str):
    """輸出日誌到 stderr（不干擾 stdout IPC 協議）"""
    print(f"[MT5 IPC] {msg}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
