# Remote-Screen

輕量級 Windows 遠端桌面檢視與控制工具。在電腦上啟動單一 Python 服務或打包成獨立 `.exe` 執行檔，透過 **Cloudflare Quick Tunnel** 建立加密 HTTPS/WSS 連線，**手機不用安裝任何 App、不限同一個 Wi-Fi**，用瀏覽器打開即可查看螢幕並遠端操作。

## 主要特色

- **跨網路免設定**：啟動時自動透過 `cloudflared` 建立臨時 `https://*.trycloudflare.com` 加密隧道（首次執行自動下載固定版本 `2026.11.1` 並驗證 SHA-256，快取至 `%LOCALAPPDATA%\RemoteScreen\bin\cloudflared.exe`）。
- **無終端視窗系統匣運行**：打包後以 Windows GUI 子系統執行，常駐右下角系統匣，不會開啟終端機視窗，避免終端機背景造成額外 GPU 佔用。
- **低流量動態串流**：採用 `mss` 擷取螢幕與 `Pillow` JPEG 壓縮，畫面靜止時自動略過重複傳送，並支援多 monitor DPI 縮放座標修正。
- **完整鍵鼠與中英文輸入**：呼叫 Windows 原生 `SendInput` API，支援左鍵／雙擊／右鍵／拖拽／滾輪／常用組合鍵，以及 Unicode UTF-16LE 直接輸入中英文文字。
- **極簡手機觸控網頁**：支援單指輕點、雙擊、長按右鍵、雙指縮放與平移，工具列可切換點擊／拖拽／滾輪模式、呼叫輸入欄、快捷鍵列與畫質切換。
- **PIN 存取防護與跨站防禦**：採用加鹽 `PBKDF2-HMAC-SHA256` 雜湊驗證 PIN、隨機 256-bit `HttpOnly` Cookie Session，搭配單 IP 5 次／全域 20 次錯誤鎖定（僅採信來自本機回環隧道的 `CF-Connecting-IP` 標頭）、嚴格同源與 `Origin` / `Sec-Fetch-Site` 驗證（防 CSRF 與 CSWSH）、嚴格 `CSP`／安全標頭、請求大小上限，以及登出或逾期時即時中斷既有 WebSocket 連線。
- **選配 Telegram 通知**：啟動時可發送連線網址（固定自訂密碼不寫入明文 PIN），並在有新裝置登入（含來源 IP）、正常停止或異常結束時即時通知；本機記錄檔亦不寫入明文 PIN。

## 環境需求

- Windows 10 / Windows 11（使用 Win32 `SendInput`、`mss` 與系統匣整合）
- Python 3.11 以上（若從原始碼執行或自行打包）

## 快速開始

### 1. 從原始碼安裝與執行

```powershell
git clone https://github.com/Willy1012-Tu/Remote-Screen.git
cd Remote-Screen

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

python -m remote_screen
```

### 2. 打包為獨立 `.exe`（無終端視窗）

```powershell
pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
```

打包完成後會產生 `dist\RemoteScreen.exe`，直接雙擊即可執行。

## 操作說明

### 電腦端系統匣圖示

執行後會在 Windows 右下角系統匣顯示螢幕圖示（黃點代表建立隧道中，綠點代表已就緒），左鍵／右鍵選單提供：

- **顯示連線資訊 (網址 / PIN)**：以原生對話框顯示目前的公網網址、存取 PIN 與記錄檔路徑。
- **複製連線網址** / **複製 PIN**：直接複製到剪貼簿。
- **重發 Telegram 通知**：重新將目前網址與 PIN 發送到 Telegram。
- **開啟記錄檔**：開啟 `%LOCALAPPDATA%\RemoteScreen\remote_screen.log`。
- **停止並退出**：關閉本地伺服器與 `cloudflared` 隧道並結束程式（若圖示被隱藏，也可在工作管理員結束 `RemoteScreen.exe`）。

### 手機瀏覽器控制

- **單指輕點**：左鍵點擊（開啟工具列「右鍵」時為單次右鍵點擊）
- **單指雙擊**：左鍵雙擊
- **單指長按**：右鍵點擊
- **雙指捏合／拖曳**：任意模式下皆可放大、縮小與平移畫面（縮放後點擊仍依比例精準換算座標）
- **頂部工具列**：切換 `點擊` / `拖曳` / `滾輪` 模式、開啟 `鍵盤`（支援中英文輸入）、`快捷鍵`、`標準 / 清晰 / 省流` 畫質切換、`重新整理`、`復位`、`登出`（按下 `▴`/`▾` 可收合工具列）

## 設定與環境變數

### 存取 PIN 優先順序

程式會依以下順序決定登入 PIN：

1. 命令列參數 `--pin`
2. 環境變數 `REMOTE_PIN`
3. 執行檔（或專案根目錄）旁的 `config.json`，格式：`{"pin": "你的密碼"}`
4. 以上皆未設定時，每次啟動隨機產生 6 位數 PIN

### Telegram 通知（選配）

未設定時會自動略過，完全不影響主程式運作。若需啟用，請於本機設定 Bot Token 與 Chat ID（`REMOTE_SCREEN_TG_*` 優先於通用變數）：

```powershell
setx REMOTE_SCREEN_TG_BOT_TOKEN "你的BotToken"
setx REMOTE_SCREEN_TG_CHAT_ID "你的ChatID"
```

（若未設定 `REMOTE_SCREEN_TG_BOT_TOKEN` / `REMOTE_SCREEN_TG_CHAT_ID`，程式會自動回退讀取 `TG_BOT_TOKEN` / `TG_CHAT_ID`。為降低憑證外流風險，若使用自訂固定密碼，通知訊息不會包含明文 PIN；有裝置登入成功時亦會推播來源 IP。）

### 常用命令列參數

| 參數 | 預設值 | 說明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 本機服務綁定主機位址 |
| `--port` | `0`（自動） | 本機服務埠號 |
| `--pin` | 隨機 6 位數 | 指定存取 PIN／密碼 |
| `--fps` | `4.0` | 最大畫面擷取幀率（`0.5`～`30.0`） |
| `--quality` | `60` | JPEG 壓縮品質（`20`～`95`） |
| `--max-width` | `1280` | 串流畫面最大寬度像素（`320`～`3840`） |
| `--monitor` | `1` | `mss` 螢幕編號 |
| `--local-only` | `False` | 僅啟動本機 HTTP/WS 服務，不開啟 Cloudflare 隧道 |
| `--no-telegram` | `False` | 停用 Telegram 通知 |
| `--no-tray` | `False` | 以無系統匣的命令列／背景模式執行 |
| `--log-file` | `%LOCALAPPDATA%\RemoteScreen\remote_screen.log` | 自訂記錄檔輸出路徑 |

## 安全與系統限制

- **Windows UAC 與鎖定畫面**：以一般使用者權限執行時，若電腦進入 Windows 鎖定畫面或跳出 UAC 系統管理員確認視窗，Windows 會基於安全隔離阻擋截圖與鍵鼠模擬；手機端會暫時顯示「Desktop temporarily unavailable」提示，待解鎖或關閉對話框後自動恢復。
- **僅供本人授權裝置使用**：本工具用於遠端檢視與操作自己的電腦，請妥善保管公網連結與 PIN。
- **未簽章 `.exe` 提示**：自行透過 PyInstaller 打包的單檔執行檔沒有商業數位簽章，部分防毒軟體或 Windows SmartScreen 可能會出現提示。

## 專案結構

```text
Remote-Screen/
├── build_exe.ps1                 # PyInstaller 單檔無終端執行檔打包腳本
├── launcher.py                   # 執行檔進入點包裝
├── pyproject.toml                # 套件與依賴設定
├── src/remote_screen/
│   ├── __main__.py               # 程式進入點、記錄檔與生命週期管理
│   ├── auth.py                   # PIN 驗證、Cookie Session 與 IP 鎖定
│   ├── config.py                 # 參數解析與設定載入
│   ├── notify.py                 # Telegram Bot API 通知
│   ├── screen.py                 # DPI 感知與 mss 截圖／差異編碼
│   ├── server.py                 # aiohttp HTTP 與 WebSocket 串流伺服器
│   ├── tray.py                   # Windows 系統匣圖示與剪貼簿工具
│   ├── tunnel.py                 # cloudflared 下載與隧道生命週期管理
│   ├── win_input.py              # Win32 SendInput 鍵鼠與 Unicode 輸入
│   └── static/
│       ├── index.html            # 手機端遠端桌面操作頁面
│       └── login.html            # PIN 登入頁面
└── tests/                        # pytest 單元與整合測試
```

## 執行測試

```powershell
pytest -v
```

## 授權與第三方元件

本專案採用 [MIT License](LICENSE) 授權。

打包或執行時使用的主要第三方元件各自遵循其原始授權條款，包括但不限於：`aiohttp`（Apache-2.0）、`Pillow`（HPND）、`mss`（MIT）、`qrcode`（BSD-3-Clause）、`certifi`（MPL-2.0）、`pystray`（LGPL-3.0）及執行期下載使用的 `cloudflared`（Apache-2.0）。
